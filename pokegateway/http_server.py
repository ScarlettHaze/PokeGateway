"""Minimal asyncio HTTP server.

The DS speaks very simple HTTP, so this deliberately avoids third-party
dependencies. Responses are always HTTP/1.0 and close the connection: the
DS's download (DLS1) client fails with "A communication error has
occurred" when it receives an HTTP/1.1 response.
"""

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass, field

log = logging.getLogger("http")

MAX_BODY = 1024 * 1024

REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 500: "Internal Server Error"}


@dataclass
class Request:
    method: str
    path: str
    query: dict
    headers: dict
    body: bytes
    client_ip: str

    @property
    def host(self) -> str:
        return self.headers.get("host", "").split(":")[0].lower()

    def form(self) -> dict:
        """GET query or urlencoded POST body, single values."""
        if self.method == "POST" and self.body:
            parsed = urllib.parse.parse_qs(self.body.decode("latin-1"),
                                           keep_blank_values=True)
            return {k: v[0] for k, v in parsed.items()}
        return dict(self.query)


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: dict = field(default_factory=dict)

    def encode(self) -> bytes:
        headers = {"Content-Type": "text/plain", **self.headers}
        headers["Content-Length"] = str(len(self.body))
        headers["Connection"] = "close"
        lines = ["HTTP/1.0 %d %s" % (self.status, REASONS.get(self.status, "Unknown"))]
        lines += ["%s: %s" % (k, v) for k, v in headers.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + self.body


async def read_request(reader, peer_ip):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = (lines[0].split(" ", 2) + ["", ""])[:3]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    body = b""
    length = int(headers.get("content-length", "0") or 0)
    if length > MAX_BODY:
        raise ValueError("body too large")
    if length:
        body = await reader.readexactly(length)
    parsed = urllib.parse.urlsplit(target)
    query = {k: v[0] for k, v in
             urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
    # A TLS-terminating proxy in front of us passes the real client address.
    client_ip = headers.get("x-forwarded-for", peer_ip).split(",")[0].strip()
    return Request(method.upper(), parsed.path or "/", query, headers, body, client_ip)


class HTTPServer:
    def __init__(self, app):
        self.app = app  # async callable(Request) -> Response

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        try:
            req = await asyncio.wait_for(read_request(reader, peer[0]), 30)
        except Exception as exc:  # malformed / timeout / disconnect
            log.debug("Bad request from %s: %r", peer[0], exc)
            writer.close()
            return
        try:
            resp = await self.app(req)
        except Exception:
            log.exception("Error handling %s %s", req.method, req.path)
            resp = Response(500, b"Server error")
        log.info("%s %s%s -> %d (%d bytes) [%s]", req.method, req.host, req.path,
                 resp.status, len(resp.body), req.client_ip)
        try:
            writer.write(resp.encode())
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()


async def start(app, bind, port):
    server = HTTPServer(app)
    srv = await asyncio.start_server(server.handle, bind, port)
    log.info("HTTP listening on %s:%d", bind, port)
    return srv
