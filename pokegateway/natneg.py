"""GameSpy NAT negotiation (natneg1/2/3.gs.nintendowifi.net, UDP 27901).

Two peers that want to connect both send INIT packets carrying the same
cookie (session id). Once both sides (client index 0 and 1) are known, each
is told the other's public address with a CONNECT packet.

Packet layout: magic(6) version(1) type(1) cookie(4) ...
"""

import asyncio
import logging
import socket
import struct
import time

log = logging.getLogger("natneg")

MAGIC = b"\xfd\xfc\x1e\x66\x6a\xb2"

NN_INIT, NN_INITACK, NN_ERTTEST, NN_ERTACK = 0x00, 0x01, 0x02, 0x03
NN_CONNECT, NN_CONNECT_ACK, NN_CONNECT_PING = 0x05, 0x06, 0x07
NN_BACKUP_TEST, NN_BACKUP_ACK = 0x08, 0x09
NN_ADDRESS_CHECK, NN_ADDRESS_REPLY = 0x0A, 0x0B
NN_NATIFY_REQUEST, NN_REPORT, NN_REPORT_ACK = 0x0C, 0x0D, 0x0E


class Peer:
    def __init__(self):
        self.addrs = {}         # port type -> (ip, port)
        self.local = None       # (ip, port) from the packet
        self.connected = False


class NatNegProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.sessions = {}      # cookie -> {"peers": {index: Peer}, "time": t, "sent": bool}

    def connection_made(self, transport):
        self.transport = transport

    def send(self, data, addr):
        self.transport.sendto(bytes(data), addr)

    def datagram_received(self, data, addr):
        if len(data) < 12 or not data.startswith(MAGIC):
            return
        try:
            self.handle(data, addr)
        except Exception:
            log.exception("natneg packet error")

    def prune(self):
        now = time.time()
        for cookie, s in list(self.sessions.items()):
            if now - s["time"] > 60:
                del self.sessions[cookie]

    def handle(self, data, addr):
        rtype = data[7]
        cookie = data[8:12]
        if rtype == NN_INIT and len(data) >= 21:
            self.init(data, cookie, addr)
        elif rtype == NN_CONNECT_ACK and len(data) >= 14:
            s = self.sessions.get(cookie)
            if s and data[13] in s["peers"]:
                s["peers"][data[13]].connected = True
        elif rtype == NN_ADDRESS_CHECK and len(data) >= 15:
            out = bytearray(data[:15]) + socket.inet_aton(addr[0]) + struct.pack(">H", addr[1])
            out[7] = NN_ADDRESS_REPLY
            self.send(out, addr)
        elif rtype == NN_NATIFY_REQUEST:
            out = bytearray(data)
            out[7] = NN_ERTTEST
            self.send(out, addr)
        elif rtype == NN_BACKUP_TEST:
            out = bytearray(data)
            out[7] = NN_BACKUP_ACK
            self.send(out, addr)
        elif rtype == NN_REPORT and len(data) >= 21:
            out = bytearray(data[:21])
            out[7] = NN_REPORT_ACK
            out[14] = 0
            self.send(out, addr)
        elif rtype in (NN_ERTACK, NN_CONNECT_PING):
            pass
        else:
            log.debug("natneg: unhandled type %02x", rtype)

    def init(self, data, cookie, addr):
        port_type, index = data[12], data[13]
        local_ip = socket.inet_ntoa(data[15:19])
        local_port = struct.unpack(">H", data[19:21])[0]

        ack = bytearray(data[:14]) + b"\xff\xff\x6d\x16\xb5\x7d\xea"
        ack[7] = NN_INITACK
        self.send(ack, addr)

        self.prune()
        s = self.sessions.setdefault(cookie, {"peers": {}, "time": time.time()})
        peer = s["peers"].setdefault(index, Peer())
        peer.addrs[port_type] = addr
        peer.local = (local_ip, local_port)

        if len(s["peers"]) < 2:
            return
        # (Re)send CONNECT to every peer that hasn't acknowledged yet; later
        # INITs may carry a better (game socket) address.
        for idx, me in s["peers"].items():
            if me.connected:
                continue
            for oidx, other in s["peers"].items():
                if oidx == idx:
                    continue
                target = self._game_addr(other)
                dest = me.addrs.get(1) or me.addrs.get(0) or next(iter(me.addrs.values()))
                pkt = bytearray(data[:12]) + socket.inet_aton(target[0]) + \
                    struct.pack(">H", target[1]) + b"\x42\x00"
                pkt[7] = NN_CONNECT
                self.send(pkt, dest)
                log.info("natneg: cookie %s peer %d -> %s:%d", cookie.hex(), idx, *target)

    @staticmethod
    def _game_addr(peer):
        """Best guess of the address the other side should punch to: the
        public IP seen on the game socket (port types 1-3), falling back to
        the reported local port."""
        for pt in (1, 2, 3):
            if pt in peer.addrs:
                return peer.addrs[pt]
        ip, port = next(iter(peer.addrs.values()))
        if peer.local and peer.local[1]:
            port = peer.local[1]
        return ip, port


async def start(bind, port):
    loop = asyncio.get_running_loop()
    _, proto = await loop.create_datagram_endpoint(NatNegProtocol, local_addr=(bind, port))
    log.info("NATNEG listening on udp %s:%d", bind, port)
    return proto
