"""GameSpy matchmaking: Query & Reporting (QR2, UDP 27900) and the server
browser / master server (TCP 28910).

The DS "hosts" a matchmaking session by heartbeating to the QR server; other
DSes then query the server browser with a filter (e.g. "friends with these
profile ids" or "random opponents with these settings"), and send connection
requests that the server forwards over the host's QR socket.
"""

import asyncio
import logging
import socket
import struct
import time

from . import enctypex
from .sqlfilter import FilterError, matches
from .util import ip_to_int_le, random_str

log = logging.getLogger("matchmaking")

SERVER_TIMEOUT = 61

F_PRIVATE_IP = 0x02
F_CONNECT_NEGOTIATE = 0x04
F_ICMP_IP = 0x08
F_NONSTANDARD_PORT = 0x10
F_NONSTANDARD_PRIVATE_PORT = 0x20
F_HAS_KEYS = 0x40


class Registry:
    """Live matchmaking sessions reported through QR."""

    def __init__(self):
        self.sessions = {}      # (ip, port, session_id) -> Session

    def prune(self):
        now = time.time()
        for key, s in list(self.sessions.items()):
            if now - s.last_seen > SERVER_TIMEOUT:
                del self.sessions[key]

    def listed(self, gamename):
        self.prune()
        return [s for s in self.sessions.values()
                if s.registered and s.data.get("gamename") == gamename]

    def find_by_addr(self, ip, port):
        self.prune()
        for s in self.sessions.values():
            if not s.registered:
                continue
            if s.addr[0] == ip and port in (s.addr[1], int(s.data.get("localport", 0) or 0)):
                return s
        return None


class Session:
    def __init__(self, addr, sid_raw):
        self.addr = addr
        self.sid_raw = sid_raw
        self.data = {}
        self.challenge = None
        self.registered = False
        self.last_seen = time.time()


class QRProtocol(asyncio.DatagramProtocol):
    def __init__(self, registry, game_keys):
        self.registry = registry
        self.game_keys = game_keys
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def send(self, data, addr):
        # The DS SDK is sensitive to replies arriving too quickly.
        asyncio.get_running_loop().call_later(0.05, self.transport.sendto, data, addr)

    def datagram_received(self, data, addr):
        try:
            self.handle(data, addr)
        except Exception:
            log.exception("QR packet error from %s", addr)

    def handle(self, data, addr):
        if not data:
            return
        cmd = data[0]
        if cmd == 0x09:                                   # availability check
            self.send(b"\xfe\xfd\x09\x00\x00\x00\x00", addr)
            return
        if len(data) < 5:
            return
        sid_raw = data[1:5]
        key = (addr[0], addr[1], sid_raw)
        sess = self.registry.sessions.get(key)
        if sess is None:
            sess = self.registry.sessions[key] = Session(addr, sid_raw)
        sess.last_seen = time.time()

        if cmd == 0x03:                                   # heartbeat
            self.heartbeat(sess, data[5:], addr)
        elif cmd == 0x01:                                 # challenge response
            # The response is an RC4/base64 digest of our challenge keyed with
            # the game's secret key. We accept it without verifying so that
            # unknown game keys don't break hosting.
            sess.registered = True
            self.send(b"\xfe\xfd\x0a" + sid_raw, addr)
            log.info("QR: %s:%d registered %s (pid %s)", addr[0], addr[1],
                     sess.data.get("gamename"), sess.data.get("dwc_pid"))
        elif cmd in (0x08, 0x07, 0x02, 0x05, 0x0A):      # keepalive / acks / echo
            pass
        else:
            log.debug("QR: unknown command %02x", cmd)

    def heartbeat(self, sess, payload, addr):
        # Key/value section ends with a double NUL; player/team data follows.
        kv_part = payload.split(b"\x00\x00", 1)[0]
        parts = kv_part.split(b"\x00")
        kv = {}
        for i in range(0, len(parts) - 1, 2):
            kv[parts[i].decode("latin-1")] = parts[i + 1].decode("latin-1")
        if kv.get("statechanged") == "2":
            self.registry.sessions.pop((addr[0], addr[1], sess.sid_raw), None)
            return
        if kv.get("gamename") and kv["gamename"] not in self.game_keys:
            log.warning("QR: no secret key known for game %r (add it to [gamekeys])",
                        kv["gamename"])
        kv["publicip"] = str(ip_to_int_le(addr[0]))
        kv["publicport"] = str(addr[1])
        sess.data.update(kv)
        if sess.challenge is None:
            addr_hex = "".join("%02X" % int(x) for x in addr[0].split("."))
            sess.challenge = random_str(6) + "00" + addr_hex + "%04X" % addr[1]
            self.send(b"\xfe\xfd\x01" + sess.sid_raw + sess.challenge.encode() + b"\x00", addr)

    def forward(self, target: Session, payload: bytes):
        cookie = struct.pack("<I", int.from_bytes(random_str(4).encode(), "little"))
        self.transport.sendto(b"\xfe\xfd\x06" + target.sid_raw + cookie + payload, target.addr)


# ---------------------------------------------------------------------------
# Server browser
# ---------------------------------------------------------------------------

def _cstr(buf, pos):
    end = buf.index(b"\x00", pos)
    return buf[pos:end].decode("latin-1"), end + 1


class ServerBrowser:
    def __init__(self, registry, qr, game_keys):
        self.registry = registry
        self.qr = qr
        self.game_keys = game_keys

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        buf = b""
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), 120)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 3:
                    length = struct.unpack(">H", buf[:2])[0]
                    if length < 3:
                        return
                    if len(buf) < length:
                        break
                    packet, buf = buf[:length], buf[length:]
                    out = self.packet(packet, peer)
                    if out:
                        writer.write(out)
                        await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, ValueError):
            pass
        except Exception:
            log.exception("Server browser error")
        finally:
            writer.close()

    def packet(self, packet, peer):
        cmd = packet[2]
        if cmd == 0x00:
            return self.list_request(packet, peer)
        if cmd == 0x02:
            dest_ip = socket.inet_ntoa(packet[3:7])
            dest_port = struct.unpack(">H", packet[7:9])[0]
            target = self.registry.find_by_addr(dest_ip, dest_port)
            if target is not None and self.qr is not None:
                self.qr.forward(target, packet[9:])
                log.debug("SB: forwarded %d bytes to %s:%d", len(packet) - 9, dest_ip, dest_port)
            else:
                log.debug("SB: no session at %s:%d", dest_ip, dest_port)
        return b""

    def list_request(self, p, peer):
        pos = 3
        pos += 2 + 4            # list version, encoding version, game version
        query_game, pos = _cstr(p, pos)
        game_name, pos = _cstr(p, pos)
        challenge = p[pos:pos + 8]
        pos += 8
        filt, pos = _cstr(p, pos)
        fields_raw, pos = _cstr(p, pos)
        options = struct.unpack(">I", p[pos:pos + 4])[0] if len(p) >= pos + 4 else 0
        pos += 4
        max_servers = 0
        if options & 0x80 and len(p) >= pos + 4:
            max_servers = struct.unpack(">I", p[pos:pos + 4])[0]
        fields = [f for f in fields_raw.split("\\") if f.strip()]

        key = self.game_keys.get(game_name)
        if key is None:
            log.warning("SB: no secret key for %r - list request ignored", game_name)
            return b""

        if (not filt and not fields) or options & 0x02:
            out = socket.inet_aton(peer[0]) + struct.pack(">H", 6500)
            return enctypex.encrypt(key, challenge, out)

        found = []
        for s in self.registry.listed(query_game):
            try:
                ok = matches(filt, s.data) if filt else True
            except FilterError as exc:
                log.warning("SB: bad filter %r: %s", filt, exc)
                ok = False
            if ok:
                found.append(s)
                if max_servers and len(found) >= max_servers:
                    break
        log.info("SB: %s list for %s filter %r -> %d", peer[0], query_game, filt, len(found))

        out = socket.inet_aton(peer[0]) + struct.pack(">H", peer[1])
        out += bytes([len(fields)])
        for f in fields:
            out += b"\x00" + f.encode("latin-1") + b"\x00"
        out += b"\x00"                                     # no popular values
        for s in found:
            out += self.server_entry(s, fields)
        out += b"\x00" + b"\xff\xff\xff\xff"
        return enctypex.encrypt(key, challenge, out)

    @staticmethod
    def server_entry(s, fields):
        d = s.data
        flags = F_HAS_KEYS | F_NONSTANDARD_PORT | F_ICMP_IP
        body = socket.inet_aton(s.addr[0])
        body += struct.pack(">H", s.addr[1])
        if "natneg" in d:
            flags |= F_CONNECT_NEGOTIATE
        if "localip0" in d:
            flags |= F_PRIVATE_IP
            try:
                body += socket.inet_aton(d["localip0"])
            except OSError:
                body += b"\x00" * 4
        if "localport" in d:
            flags |= F_NONSTANDARD_PRIVATE_PORT
            body += struct.pack(">H", int(d["localport"] or 0) & 0xFFFF)
        body += b"\x00\x00\x00\x00"                        # ICMP ip
        out = bytes([flags]) + body
        for f in fields:
            out += b"\xff" + str(d.get(f, "")).encode("latin-1") + b"\x00"
        return out


async def start(config):
    registry = Registry()
    loop = asyncio.get_running_loop()
    _, qr = await loop.create_datagram_endpoint(
        lambda: QRProtocol(registry, config.game_keys),
        local_addr=(config.bind, config.qr_port))
    sb = ServerBrowser(registry, qr, config.game_keys)
    srv = await asyncio.start_server(sb.handle, config.bind, config.sb_port)
    log.info("QR listening on udp %s:%d, server browser on tcp %s:%d",
             config.bind, config.qr_port, config.bind, config.sb_port)
    return registry, srv
