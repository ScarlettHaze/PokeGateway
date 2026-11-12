"""DNS server.

Answers A queries for Nintendo WFC / GameSpy / Pokemon hosts with this
server's IP and forwards everything else to an upstream resolver, so the DS
can keep using it for normal lookups.
"""

import asyncio
import logging
import socket
import struct

log = logging.getLogger("dns")

QTYPE_A = 1
QTYPE_AAAA = 28
QCLASS_IN = 1


def parse_question(packet: bytes):
    """Return (txid, flags, qname, qtype, qclass, end_offset) for the first
    question of a query. Raises ValueError for malformed packets."""
    if len(packet) < 12:
        raise ValueError("short packet")
    txid, flags, qdcount = struct.unpack(">HHH", packet[:6])
    if qdcount < 1:
        raise ValueError("no question")
    labels = []
    pos = 12
    while True:
        if pos >= len(packet):
            raise ValueError("truncated name")
        length = packet[pos]
        pos += 1
        if length == 0:
            break
        if length & 0xC0:
            raise ValueError("compression in question")
        labels.append(packet[pos:pos + length].decode("ascii", "replace"))
        pos += length
    if pos + 4 > len(packet):
        raise ValueError("truncated question")
    qtype, qclass = struct.unpack(">HH", packet[pos:pos + 4])
    return txid, flags, ".".join(labels).lower(), qtype, qclass, pos + 4


def build_response(query: bytes, q_end: int, answers_ip=None, ttl=300,
                   rcode=0) -> bytes:
    txid, qflags = struct.unpack(">HH", query[:4])
    rd = qflags & 0x0100
    flags = 0x8000 | 0x0400 | rd | 0x0080 | rcode  # QR, AA, RD copy, RA
    ancount = 1 if answers_ip else 0
    header = struct.pack(">HHHHHH", txid, flags, 1, ancount, 0, 0)
    question = query[12:q_end]
    out = header + question
    if answers_ip:
        out += struct.pack(">HHHIH", 0xC00C, QTYPE_A, QCLASS_IN, ttl, 4)
        out += socket.inet_aton(answers_ip)
    return out


class Resolver:
    def __init__(self, config, public_ip):
        self.cfg = config
        self.ip = public_ip
        self.domains = [d.lower().strip(".") for d in config.redirect_domains]

    def is_ours(self, name: str) -> bool:
        name = name.strip(".")
        return any(name == d or name.endswith("." + d) for d in self.domains)

    async def resolve(self, packet: bytes) -> bytes:
        try:
            _, _, qname, qtype, qclass, q_end = parse_question(packet)
        except ValueError as exc:
            log.debug("Bad query: %s", exc)
            return b""
        catch_all = getattr(self.cfg, "dns_redirect_all", False)
        if self.is_ours(qname) or catch_all:
            if qtype == QTYPE_A and qclass == QCLASS_IN:
                tag = "" if self.is_ours(qname) else " [catch-all]"
                log.info("%s -> %s%s", qname, self.ip, tag)
                return build_response(packet, q_end, self.ip, self.cfg.dns_ttl)
            # AAAA / other types for our hosts: empty NOERROR so clients
            # fall back to IPv4.
            return build_response(packet, q_end, None)
        # Not one of ours and not catching all: log what we're forwarding so
        # we can see every host the console asks for.
        log.info("%s -> (forwarded to %s)", qname, self.cfg.dns_upstream)
        return await self.forward(packet, q_end)

    async def forward(self, packet: bytes, q_end: int) -> bytes:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        class _Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if not fut.done():
                    fut.set_result(data)

            def error_received(self, exc):
                if not fut.done():
                    fut.set_exception(exc)

        transport = None
        try:
            transport, _ = await loop.create_datagram_endpoint(
                _Proto, remote_addr=(self.cfg.dns_upstream, self.cfg.dns_upstream_port))
            transport.sendto(packet)
            return await asyncio.wait_for(fut, 3.0)
        except (asyncio.TimeoutError, OSError) as exc:
            log.debug("Upstream DNS failed: %r", exc)
            return build_response(packet, q_end, None, rcode=2)  # SERVFAIL
        finally:
            if transport:
                transport.close()


class DNSUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, resolver):
        self.resolver = resolver
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        asyncio.ensure_future(self._handle(data, addr))

    async def _handle(self, data, addr):
        resp = await self.resolver.resolve(data)
        if resp:
            self.transport.sendto(resp, addr)


async def _handle_tcp(resolver, reader, writer):
    try:
        while True:
            hdr = await reader.readexactly(2)
            (length,) = struct.unpack(">H", hdr)
            data = await reader.readexactly(length)
            resp = await resolver.resolve(data)
            if not resp:
                break
            writer.write(struct.pack(">H", len(resp)) + resp)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


async def start(config, public_ip):
    resolver = Resolver(config, public_ip)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        lambda: DNSUDPProtocol(resolver), local_addr=(config.bind, config.dns_port))
    tcp = await asyncio.start_server(
        lambda r, w: _handle_tcp(resolver, r, w), config.bind, config.dns_port)
    log.info("DNS listening on %s:%d (answering with %s)", config.bind,
             config.dns_port, public_ip)
    return tcp
