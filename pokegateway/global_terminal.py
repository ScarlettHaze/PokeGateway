"""Global Terminal server (pkgdsprod.nintendo.co.jp).

Gen 4 (Platinum/HGSS, TCP 12400, plain): Battle Videos, Dress-up photos,
Box uploads, Trainer Rankings. Messages are XOR-obfuscated with a fixed pad.

Gen 5 (BW/B2W2, TCP 12401 with SSLv3): Battle Videos and Musical photos.
Python's ssl module cannot speak SSLv3/RC4, so this server listens on a
plain port and a legacy TLS terminator forwards 12401 to it (see deploy/).

Message: u32 total length (LE), u8 request type, u8 ?, ..., 0x140 byte
header (pid at 0x08), request specific body at 0x140.
Response: u32 length, u8 type, u8 copy, u16 result (0 = OK), body.
"""

import asyncio
import logging
import struct

log = logging.getLogger("gt")

PAD4 = bytes.fromhex(
    "1f98a546765c3d0e931833280b07038202438a86db383419d6f959b2ad6a7dbc"
    "eee03a3fca4c2568f4a95bf722605a6ffa1b79e917b1009caa5e9dffeaa00d4b"
    "75f661855dbbdcfb642e7aabf1e8440cb88fa80a8ebde13bfc3c9f1a56c5e2f5"
    "47d9d78ccd97f07b8bc34f450490811e6bc9d373c6e724ba32f3c0ec57ccc4b6"
    "c1aeaf88f284ce4a0f9441b4742ad1701cd4b0c20908169bb58d2bd289b799a1"
    "306554409671febf3106e514e6da4826ac879ad8a6eb92cffd771d219e363553"
    "3ed0d562585f637c7e5229122c78059155e3a2b9f8509513807f1127cb374e51"
    "15efa7724d8349a469de20a367df1042396c2dc723e4ddedbe66b32f016e6dc8")

MIN_LEN, MAX_LEN = 0x140, 7820

OK = b"\x00\x00"
FAIL = b"\x02\x00"


def crypt4(msg: bytearray):
    """Gen 4 obfuscation (symmetric) of everything after byte 4."""
    if len(msg) < 5:
        return
    offset = (msg[0] + msg[4]) & 0xFF
    for i in range(5, len(msg)):
        msg[i] ^= PAD4[(i + offset) & 0xFF]


class GlobalTerminal:
    def __init__(self, db, gen):
        self.db = db
        self.gen = gen

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername") or ("?", 0)
        try:
            head = await asyncio.wait_for(reader.readexactly(4), 10)
            length = struct.unpack("<I", head)[0]
            if not MIN_LEN <= length <= MAX_LEN:
                log.warning("GT%d: bad length %d from %s", self.gen, length, peer[0])
                return
            body = await asyncio.wait_for(reader.readexactly(length - 4), 30)
            msg = bytearray(head + body)
            if self.gen == 4:
                crypt4(msg)
            resp = self.process(bytes(msg), peer[0])
            writer.write(resp)
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            log.exception("GT%d error", self.gen)
        finally:
            writer.close()

    def process(self, data: bytes, ip: str) -> bytes:
        rtype = data[4]
        pid = struct.unpack_from("<i", data, 8)[0]
        handler = (HANDLERS4 if self.gen == 4 else HANDLERS5).get(rtype)
        try:
            body = handler(self, data, pid) if handler else FAIL
        except Exception:
            log.exception("GT%d request %02x failed", self.gen, rtype)
            body = FAIL
        log.info("GT%d: type %02x pid %d from %s -> %s", self.gen, rtype, pid, ip,
                 "ok" if body[:2] == OK else "fail")
        out = bytearray(struct.pack("<I", 6 + len(body)) + bytes([rtype, data[5]]) + body)
        if self.gen == 4:
            crypt4(out)
        return bytes(out)

    # ------------------------------------------------------------ Gen 4
    def box_upload(self, d, pid):
        if len(d) != 0x360:
            return FAIL
        label = struct.unpack_from("<i", d, 0x140)[0]
        serial = self.db.upload_add("box4", pid, d[0x144:0x144 + 0x21C], search1=label)
        return OK + struct.pack("<Q", serial) if serial else FAIL

    def box_search(self, d, pid):
        if len(d) != 0x14C:
            return FAIL
        label = struct.unpack_from("<i", d, 0x144)[0]
        rows = self.db.upload_search("box4", search1=label, limit=20)
        out = OK + struct.pack("<i", len(rows))
        for r in rows:
            out += struct.pack("<iiQ", r["pid"], r["search1"], r["serial"]) + r["data"]
        return out

    def dressup_upload(self, d, pid):
        if len(d) != 0x220:
            return FAIL
        data = d[0x140:0x140 + 0xE0]
        species = struct.unpack_from("<H", data, 0x8C)[0]
        serial = self.db.upload_add("dressup4", pid, data, search1=species)
        return OK + struct.pack("<Q", serial) if serial else FAIL

    def dressup_search(self, d, pid):
        if len(d) != 0x14C:
            return FAIL
        species = struct.unpack_from("<H", d, 0x144)[0]
        rows = self.db.upload_search("dressup4", search1=species, limit=10)
        out = OK + struct.pack("<i", len(rows))
        for r in rows:
            out += struct.pack("<iQ", r["pid"], r["serial"]) + r["data"]
        return out

    # ------------------------------------------------------------ battle videos
    def _bv_sizes(self):
        # (upload request length, record length, header length)
        return (0x1E8C, 0x1D4C, 0xE4) if self.gen == 4 else (0x1AE8, 0x18A4, 0xC4)

    def bv_upload(self, d, pid):
        req_len, rec_len, hdr_len = self._bv_sizes()
        if len(d) != req_len:
            return FAIL
        record = d[0x140:0x140 + rec_len]
        header = record[:hdr_len]
        serial = self.db.upload_add(
            "bv%d" % self.gen, pid, record, header=header,
            country=header[0x17], region=header[0x18], meta=header[0xA6])
        if serial:
            log.info("GT%d: battle video %s uploaded by %d", self.gen, format_serial(serial), pid)
        return OK + struct.pack("<Q", serial) if serial else FAIL

    def bv_search(self, d, pid):
        if len(d) != 0x15C:
            return FAIL
        _, _, hdr_len = self._bv_sizes()
        ranking = struct.unpack_from("<I", d, 0x140)[0]
        species = struct.unpack_from("<H", d, 0x144)[0]
        meta = d[0x146]
        if self.gen == 4:
            country, region = d[0x147], d[0x148]
            meta_on = meta != 0xFF
        else:
            country, region = d[0x14A], d[0x14B]
            meta_on = d[0x148] != 0
        kind = "bv%d" % self.gen
        if ranking not in (0, 0xFFFFFFFF):
            # Rankings: 1 = latest 30, 2 = link battles, 3 = Battle Frontier /
            # Subway. We approximate all of them with "most saved / newest".
            rows = self.db.upload_search(kind, limit=30,
                                         order="saved DESC, created DESC" if ranking != 1
                                         else "created DESC")
        else:
            if species != 0xFFFF:
                rows = self.db.upload_search_species(kind, species, limit=200)
            else:
                rows = self.db.upload_search(kind, limit=200)
            rows = [r for r in rows
                    if (not meta_on or r["meta"] == meta)
                    and (country == 0xFF or r["country"] == country)
                    and (region == 0xFF or r["region"] == region)][:30]
        out = OK + struct.pack("<i", len(rows))
        for r in rows:
            out += struct.pack("<iQ", r["pid"], r["serial"]) + r["header"][:hdr_len]
        return out

    def bv_watch(self, d, pid):
        if len(d) != 0x14C:
            return FAIL
        serial = struct.unpack_from("<Q", d, 0x140)[0]
        r = self.db.upload_get("bv%d" % self.gen, serial)
        if r is None:
            return FAIL
        return OK + struct.pack("<iQ", r["pid"], r["serial"]) + r["data"]

    def bv_saved(self, d, pid):
        if len(d) != 0x148:
            return FAIL
        serial = struct.unpack_from("<Q", d, 0x140)[0]
        return OK if self.db.upload_flag_saved("bv%d" % self.gen, serial) else FAIL

    # ------------------------------------------------------------ trainer rankings (Gen 4)
    def rankings_head(self, d, pid):
        # Trainer Rankings are not implemented yet (the leaderboard report
        # format is only partially known). Failing here makes the game show
        # a connection error for Rankings only.
        return FAIL

    def rankings_search(self, d, pid):
        return FAIL

    # ------------------------------------------------------------ Musicals (Gen 5)
    def musical_upload(self, d, pid):
        if len(d) != 0x370:
            return FAIL
        data = d[0x140:0x140 + 0x230]
        species = struct.unpack_from("<H", data, 0)[0]
        serial = self.db.upload_add("musical5", pid, data, search1=species)
        return OK + struct.pack("<Q", serial) if serial else FAIL

    def musical_search(self, d, pid):
        if len(d) != 0x14C:
            return FAIL
        species = struct.unpack_from("<H", d, 0x144)[0]
        rows = self.db.upload_search("musical5", search1=species, limit=10)
        out = OK + struct.pack("<i", len(rows))
        for r in rows:
            out += struct.pack("<iQ", r["pid"], r["serial"]) + r["data"]
        return out


HANDLERS4 = {
    0x08: GlobalTerminal.box_upload, 0x09: GlobalTerminal.box_search,
    0x20: GlobalTerminal.dressup_upload, 0x21: GlobalTerminal.dressup_search,
    0xD8: GlobalTerminal.bv_upload, 0xD9: GlobalTerminal.bv_search,
    0xDA: GlobalTerminal.bv_watch, 0xDB: GlobalTerminal.bv_saved,
    0xF0: GlobalTerminal.rankings_head, 0xF1: GlobalTerminal.rankings_search,
}
HANDLERS5 = {
    0x08: GlobalTerminal.musical_upload, 0x09: GlobalTerminal.musical_search,
    0xF0: GlobalTerminal.bv_upload, 0xF1: GlobalTerminal.bv_search,
    0xF2: GlobalTerminal.bv_watch, 0xF3: GlobalTerminal.bv_saved,
}


def format_serial(serial: int) -> str:
    s = "%012d" % serial
    return "%s-%s-%s" % (s[:2], s[2:7], s[7:])


async def start(db, bind, port, gen):
    gt = GlobalTerminal(db, gen)
    srv = await asyncio.start_server(gt.handle, bind, port)
    log.info("Global Terminal (Gen %d) listening on %s:%d", gen, bind, port)
    return srv
