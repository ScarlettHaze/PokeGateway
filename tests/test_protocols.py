"""End-to-end protocol tests that play the part of a DS client."""

import asyncio
import os
import socket
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pokegateway import enctypex, global_terminal  # noqa: E402
from pokegateway.app import WebApp  # noqa: E402
from pokegateway.config import Config  # noqa: E402
from pokegateway.db import Database  # noqa: E402
from pokegateway.dns_server import Resolver, build_response, parse_question  # noqa: E402
from pokegateway.gamestats import GEN4, GEN5  # noqa: E402
from pokegateway.gpcm import GPCMServer, login_proof, login_response  # noqa: E402
from pokegateway.http_server import Request  # noqa: E402
from pokegateway.matchmaking import QRProtocol, Registry, ServerBrowser  # noqa: E402
from pokegateway.natneg import MAGIC, NatNegProtocol  # noqa: E402
from pokegateway.pokemon import GTS_RECORD_SIZE_4, GTS_RECORD_SIZE_5, GtsRecord, \
    fake_tower_leader, fake_tower_record  # noqa: E402
from pokegateway.sqlfilter import matches  # noqa: E402
from pokegateway.util import gs_parse, nas_decode_form, nas_encode_form  # noqa: E402


def dns_query(name, qtype=1):
    q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    for label in name.split("."):
        q += bytes([len(label)]) + label.encode()
    return q + b"\x00" + struct.pack(">HH", qtype, 1)


def run(coro):
    return asyncio.run(coro)


def make_request(path, query=None, body=b"", method="GET", host="example"):
    return Request(method, path, query or {}, {"host": host}, body, "10.0.0.5")


class DNSTest(unittest.TestCase):
    def setUp(self):
        self.resolver = Resolver(Config(), "192.168.1.50")

    def test_redirects_wfc_hosts(self):
        for name in ("nas.nintendowifi.net", "gpcm.gs.nintendowifi.net",
                     "pokemondpds.master.gs.nintendowifi.net", "pkgdsprod.nintendo.co.jp",
                     "GAMESTATS2.GS.NINTENDOWIFI.NET"):
            resp = run(self.resolver.resolve(dns_query(name)))
            self.assertEqual(struct.unpack(">H", resp[6:8])[0], 1, name)
            self.assertEqual(socket.inet_ntoa(resp[-4:]), "192.168.1.50")

    def test_not_ours(self):
        self.assertFalse(self.resolver.is_ours("nintendo.co.jp"))
        self.assertFalse(self.resolver.is_ours("evilnintendowifi.net"))
        self.assertTrue(self.resolver.is_ours("conntest.nintendowifi.net"))

    def test_aaaa_is_empty(self):
        resp = run(self.resolver.resolve(dns_query("nas.nintendowifi.net", 28)))
        self.assertEqual(struct.unpack(">H", resp[6:8])[0], 0)
        self.assertEqual(struct.unpack(">H", resp[2:4])[0] & 0xF, 0)

    def test_parse(self):
        q = dns_query("a.b.c")
        _, _, name, qtype, _, end = parse_question(q)
        self.assertEqual((name, qtype, end), ("a.b.c", 1, len(q)))
        self.assertTrue(build_response(q, end, "1.2.3.4").endswith(socket.inet_aton("1.2.3.4")))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = Config()
        cfg.dls1_dir = os.path.join(self.tmp, "dlc")
        self.db = Database(":memory:")
        self.app = WebApp(cfg, self.db, {})

    def http(self, *a, **kw):
        return run(self.app(make_request(*a, **kw)))

    def nas(self, fields):
        body = nas_encode_form(fields).strip()
        # The DS uses '*' for '=' padding just like the server.
        resp = self.http("/ac", body=body, method="POST", host="nas.nintendowifi.net")
        self.assertEqual(resp.status, 200)
        return nas_decode_form(resp.body)


class NASTest(Base):
    def test_conntest(self):
        r = self.http("/", host="conntest.nintendowifi.net")
        self.assertIn(b"This is test.html page", r.body)
        self.assertEqual(r.headers["X-Organization"], "Nintendo")

    def test_login_flow(self):
        created = self.nas({"action": "acctcreate", "gsbrcd": "IPKEjv6", "macadr": "0009bf112233"})
        self.assertEqual(created["returncd"], "002")
        login = self.nas({"action": "login", "userid": created["userid"], "gsbrcd": "IPKEjv6",
                          "macadr": "0009bf112233", "ingamesn": "LYRA"})
        self.assertEqual(login["returncd"], "001")
        self.assertTrue(login["token"].startswith("NDS"))
        self.assertEqual(len(login["challenge"]), 8)
        stored = self.db.get_authtoken(login["token"])
        self.assertEqual(stored["userid"], created["userid"])

    def test_profanity(self):
        body = nas_encode_form({"words": "a\tb\tc"}).strip()
        r = nas_decode_form(self.http("/pr", body=body, method="POST").body)
        self.assertEqual(r["prwords"], "000")

    def test_banned(self):
        login = self.nas({"action": "login", "userid": "0000000000009", "gsbrcd": "ADAEabc"})
        token = self.db.get_authtoken(login["token"])
        p = self.db.get_or_create_profile(token["userid"], "ADAEabc")
        self.db.set_banned(p["profileid"])
        again = self.nas({"action": "login", "userid": "0000000000009", "gsbrcd": "ADAEabc"})
        self.assertEqual(again["returncd"], "3914")


def gts_record(gen, species, level, wants, gender=1, pid=0):
    size = GTS_RECORD_SIZE_4 if gen == 4 else GTS_RECORD_SIZE_5
    buf = bytearray(os.urandom(0xEC)) + bytearray(size - 0xEC)
    struct.pack_into("<HBBHBBB", buf, 0xEC, species, gender, level, wants, 3, 0, 0)
    struct.pack_into("<i", buf, 0x108, pid)
    return bytes(buf)


class GamestatsClient:
    def __init__(self, test, profile, prefix, pid):
        self.t, self.profile, self.prefix, self.pid = test, profile, prefix, pid

    def call(self, page, payload):
        url = self.prefix + page
        token = self.t.http(url, {"pid": str(self.pid)}).body.decode()
        self.t.assertEqual(len(token), 32)
        data = struct.pack("<i", self.pid)
        if self.profile.version == 3:
            data += struct.pack("<i", len(payload))
        data += payload
        resp = self.t.http(url, {"pid": str(self.pid), "hash": self.profile.token_hash(token),
                                 "data": self.profile.encode(data)})
        self.t.assertEqual(resp.status, 200, resp.body)
        body = resp.body
        if self.profile.hashed_response:
            body, digest = body[:-40], body[-40:]
            self.t.assertEqual(self.profile.finish(body)[-40:], digest)
        return body


class GTSTest(Base):
    def run_trade(self, gen, profile, prefix, extra):
        alice = GamestatsClient(self, profile, prefix, 1001)
        bob = GamestatsClient(self, profile, prefix, 2002)
        self.assertEqual(alice.call("/common/setProfile.asp", b"\x00" * 100), b"\x00" * 8)
        self.assertEqual(alice.call("/worldexchange/info.asp", b""), b"\x01\x00")
        self.assertEqual(alice.call("/worldexchange/result.asp", b""), b"\x05\x00")

        # Alice lists a level 20 Pikachu (25) asking for an Eevee (133).
        rec_a = gts_record(gen, 25, 20, 133)
        self.assertEqual(alice.call("/worldexchange/post.asp", rec_a + extra), b"\x01\x00")
        self.assertEqual(alice.call("/worldexchange/post_finish.asp", b"\x00" * 8), b"\x01\x00")
        self.assertEqual(alice.call("/worldexchange/result.asp", b""), b"\x04\x00")

        # Bob searches for Pikachu and trades his Eevee.
        found = bob.call("/worldexchange/search.asp", struct.pack("<HBBBBB", 25, 3, 0, 0, 0, 7))
        if gen == 5:
            self.assertEqual(found[:2], b"\x01\x00")
            found = found[2:]
        size = len(rec_a)
        self.assertEqual(len(found), size)
        listed = GtsRecord(gen, found)
        self.assertEqual(listed.species, 25)
        self.assertEqual(listed.pid, 1001)

        rec_b = gts_record(gen, 133, 15, 25)
        tail = struct.pack("<i", 1001) if gen == 4 else struct.pack("<i", 1001) + extra[4:]
        got = bob.call("/worldexchange/exchange.asp", rec_b + tail)
        self.assertEqual(got, found)
        self.assertEqual(bob.call("/worldexchange/exchange_finish.asp", b"\x00" * 8), b"\x01\x00")

        # Alice receives Bob's Eevee.
        incoming = alice.call("/worldexchange/result.asp", b"")
        self.assertEqual(len(incoming), size)
        traded = GtsRecord(gen, incoming)
        self.assertEqual(traded.is_exchanged, 1)
        self.assertEqual(incoming[:0xEC], rec_b[:0xEC])
        self.assertEqual(alice.call("/worldexchange/delete.asp", b""), b"\x01\x00")
        self.assertEqual(alice.call("/worldexchange/result.asp", b""), b"\x05\x00")
        self.assertEqual(self.db.counts()["trades"], 1)

    def test_gen4_trade(self):
        self.run_trade(4, GEN4, "/pokemondpds", b"")

    def test_gen5_trade(self):
        self.run_trade(5, GEN5, "/syachi2ds/web", b"\x00" * (432 - GTS_RECORD_SIZE_5))

    def test_return_own_pokemon(self):
        c = GamestatsClient(self, GEN4, "/pokemondpds", 77)
        c.call("/worldexchange/post.asp", gts_record(4, 1, 5, 4))
        c.call("/worldexchange/post_finish.asp", b"\x00" * 8)
        self.assertEqual(c.call("/worldexchange/return.asp", b""), b"\x01\x00")
        self.assertIsNone(self.db.gts_get(4, 77))

    def test_bad_hash_rejected(self):
        r = self.http("/pokemondpds/worldexchange/info.asp",
                      {"pid": "5", "hash": "0" * 40, "data": GEN4.encode(struct.pack("<i", 5))})
        self.assertEqual(r.status, 400)

    def test_invalid_record_refused(self):
        c = GamestatsClient(self, GEN4, "/pokemondpds", 88)
        self.assertEqual(c.call("/worldexchange/post.asp", gts_record(4, 999, 5, 4)), b"\x0c\x00")

    def test_tower(self):
        for gen, prefix, profile, size in ((4, "/pokemondpds", GEN4, 228),
                                           (5, "/syachi2ds/web", GEN5, 240)):
            c = GamestatsClient(self, profile, prefix, 42)
            out = c.call("/battletower/download.asp", b"\x00\x00")
            self.assertEqual(len(out), 7 * size + 30 * 34)
            self.assertEqual(len(fake_tower_record(gen, 0, 0)), size)
            self.assertEqual(len(fake_tower_leader(gen)), 34)

    def test_plaza(self):
        c = GamestatsClient(self, GEN4.__class__("uLMOGEiiJogofchScpXb", 0x000244FD, 0x00006015,
                                                 0x10000000, 0x5B440E7E, 3, True, True),
                            "/pokemondpds/web/enc/lobby", 9)
        self.assertEqual(len(c.call("/getQuestionnaire.asp", b"")), 732)
        sched = c.call("/getSchedule.asp", b"")
        self.assertEqual(struct.unpack_from("<H", sched, 4 + 14)[0], (len(sched) - 20) // 8)

    def test_pkvldt(self):
        body = b"NDStoken\x00" + struct.pack("<h", 0x100) + b"\x00" * 220
        r = self.http("/pokemon/validate", body=body, method="POST")
        self.assertEqual(r.body, b"\x00" + b"\x00" * 4 + b"\x00" * 128)

    def test_status_page(self):
        r = self.http("/", host="192.168.1.50")
        self.assertIn(b"PokeGateway", r.body)


class DLS1Test(Base):
    def post(self, **kw):
        body = nas_encode_form(kw).strip()
        return self.http("/download", body=body, method="POST", host="dls1.nintendowifi.net")

    def write(self, gamecd, files, list_txt=None):
        d = os.path.join(self.tmp, "dlc", gamecd)
        os.makedirs(d, exist_ok=True)
        for name, data in files.items():
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(data)
        if list_txt is not None:
            with open(os.path.join(d, "_list.txt"), "w") as fh:
                fh.write(list_txt)

    def login_token(self, devtime):
        return self.nas({"action": "login", "userid": "0000000000002", "gsbrcd": "IPGEabc",
                         "devtime": devtime})["token"]

    def test_gen4_one_gift_picked_by_ds_date(self):
        # HeartGold (IPKE) falls back to SoulSilver's folder, as in DLS1-Files.
        self.write("IPGE", {"a.myg": b"A" * 936, "b.myg": b"B" * 936, "c.myg": b"C" * 936},
                   "a.myg\t\t\t\t\t936\r\nb.myg\t\t\t\t\t936\r\nc.myg\t\t\t\t\t936\r\n")
        self.assertEqual(self.post(action="count", gamecd="IPKE").body, b"1")
        token = self.login_token("260102120000")      # 2 January -> day 2 -> index 1
        listing = self.post(action="list", gamecd="IPKE", token=token).body
        self.assertEqual(listing, b"b.myg\t\t\t\t\t936\r\n")
        self.assertEqual(self.post(action="contents", gamecd="IPKE", contents="b.myg").body,
                         b"B" * 936)
        self.assertEqual(self.post(action="contents", gamecd="../etc", contents="x").status, 403)

    def test_gen5_mystery_gift_filtered_by_game(self):
        lines = ("bw_only.bin\t\tMYSTERY_E\t300000\t\t720\r\n"
                 "b2w2_only.bin\t\tMYSTERY_E\tC00000\t\t720\r\n"
                 "french.bin\t\tMYSTERY_F\tF00000\t\t720\r\n"
                 "PWT_custom_E.bin\tTITLE\tWBT_E\t\t\t55540\r\n")
        self.write("IRAO", {"bw_only.bin": b"1" * 720, "b2w2_only.bin": b"2" * 720,
                            "french.bin": b"3" * 720, "PWT_custom_E.bin": b"P"}, lines)
        token = self.login_token("260101000000")
        # Black 2 (rhgamecd IREO) sees only the B2W2 English gift.
        args = dict(gamecd="IRAO", rhgamecd="IREO", attr1="MYSTERY_E", token=token)
        self.assertEqual(self.post(action="count", **args).body, b"1")
        self.assertTrue(self.post(action="list", **args).body.startswith(b"b2w2_only.bin\t"))
        # White (IRBO) sees only the BW English gift.
        args["rhgamecd"] = "IRBO"
        self.assertTrue(self.post(action="list", **args).body.startswith(b"bw_only.bin\t"))
        # PWT tournaments use the normal attribute listing.
        pwt = self.post(action="list", gamecd="IRAO", rhgamecd="IREO", attr1="WBT_E").body
        self.assertTrue(pwt.startswith(b"PWT_custom_E.bin\tTITLE\tWBT_E"))


class FakeWriter:
    def __init__(self, ip="10.0.0.9"):
        self.buf = b""
        self.ip = ip
        self.closed = False

    def write(self, data):
        self.buf += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def get_extra_info(self, name):
        return (self.ip, 5000) if name == "peername" else None

    def messages(self):
        msgs, _ = gs_parse(self.buf.decode("latin-1"))
        self.buf = b""
        return msgs


class GPCMTest(Base):
    def login(self, server, userid, gsbrcd):
        from pokegateway.gpcm import GPCMClient
        nas = self.nas({"action": "login", "userid": userid, "gsbrcd": gsbrcd})
        w = FakeWriter()
        c = GPCMClient(server, None, w)
        c.send(b"")
        cchal = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
        resp = login_response(c.challenge, nas["challenge"], cchal, nas["token"])
        c.cmd_login({"__cmd__": "login", "__cmd_val__": "", "challenge": cchal,
                     "authtoken": nas["token"], "response": resp, "id": "1"})
        msgs = w.messages()
        lc = [m for m in msgs if m["__cmd__"] == "lc"][0]
        self.assertEqual(lc["__cmd_val__"], "2")
        self.assertEqual(lc["proof"], login_proof(c.challenge, nas["challenge"], cchal, nas["token"]))
        return c, w, int(lc["profileid"])

    def test_login_and_friends(self):
        server = GPCMServer(self.db)
        a, wa, pa = self.login(server, "0000000000002", "IPGEaaa")
        b, wb, pb = self.login(server, "0000000000003", "IPGEbbb")
        self.assertNotEqual(pa, pb)
        a.cmd_addbuddy({"__cmd__": "addbuddy", "newprofileid": str(pb)})
        b.cmd_addbuddy({"__cmd__": "addbuddy", "newprofileid": str(pa)})
        self.assertTrue(any(m["__cmd__"] == "bm" and m["__cmd_val__"] == "4" for m in wb.messages()))
        # Status changes reach the mutual friend.
        wa.messages()
        b.cmd_status({"__cmd__": "status", "__cmd_val__": "1", "statstring": "Wi-Fi Club",
                      "locstring": ""})
        bms = [m for m in wa.messages() if m["__cmd__"] == "bm" and m["__cmd_val__"] == "100"]
        self.assertTrue(bms and "Wi-Fi Club" in bms[-1]["msg"])

    def test_bad_response(self):
        from pokegateway.gpcm import GPCMClient
        nas = self.nas({"action": "login", "userid": "0000000000005", "gsbrcd": "IRBOxyz"})
        w = FakeWriter()
        c = GPCMClient(GPCMServer(self.db), None, w)
        c.cmd_login({"__cmd__": "login", "challenge": "x" * 32, "authtoken": nas["token"],
                     "response": "0" * 32, "id": "1"})
        self.assertEqual(w.messages()[0]["__cmd__"], "error")


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((bytes(data), addr))


class MatchmakingTest(unittest.TestCase):
    def test_qr_and_server_browser(self):
        async def scenario():
            registry = Registry()
            qr = QRProtocol(registry, {"syachi2ds": "tXH2sN"})
            qr.transport = FakeTransport()
            host = ("203.0.113.7", 50000)
            sid = b"\x11\x22\x33\x44"
            kv = {"localip0": "192.168.0.20", "localport": "50000", "natneg": "1",
                  "gamename": "syachi2ds", "publicip": "0", "publicport": "0",
                  "dwc_pid": "7", "dwc_mtype": "0", "dwc_mver": "90", "numplayers": "1",
                  "maxplayers": "2"}
            payload = b"".join(k.encode() + b"\x00" + v.encode() + b"\x00" for k, v in kv.items())
            qr.datagram_received(b"\x03" + sid + payload + b"\x00\x00", host)
            await asyncio.sleep(0.1)
            self.assertEqual(qr.transport.sent[-1][0][:7], b"\xfe\xfd\x01" + sid)
            qr.datagram_received(b"\x01" + sid + b"whatever\x00", host)
            await asyncio.sleep(0.1)
            self.assertEqual(qr.transport.sent[-1][0], b"\xfe\xfd\x0a" + sid)

            sb = ServerBrowser(registry, qr, {"syachi2ds": "tXH2sN"})
            challenge = b"abcdefgh"
            req = (b"\x00\x01\x03\x00\x00\x00\x00" + b"syachi2ds\x00syachi2ds\x00" + challenge +
                   b"dwc_mver = 90 and dwc_pid != 8 and numplayers < maxplayers\x00" +
                   b"\\dwc_pid\\numplayers\x00" + b"\x00\x00\x00\x00")
            req = struct.pack(">H", len(req) + 2) + req
            out = sb.packet(req, ("198.51.100.1", 40000))
            plain = enctypex.decrypt("tXH2sN", challenge, out)
            self.assertEqual(plain[:4], socket.inet_aton("198.51.100.1"))
            self.assertIn(socket.inet_aton("203.0.113.7") + struct.pack(">H", 50000), plain)
            self.assertIn(b"\xff7\x00\xff1\x00", plain)
            self.assertTrue(plain.endswith(b"\x00\xff\xff\xff\xff"))

            # Connection request forwarded through the QR socket.
            msg = b"\x02" + socket.inet_aton("203.0.113.7") + struct.pack(">H", 50000) + b"HELLO"
            sb.packet(struct.pack(">H", len(msg) + 2) + msg, ("198.51.100.1", 40000))
            fwd, addr = qr.transport.sent[-1]
            self.assertEqual(addr, host)
            self.assertTrue(fwd.startswith(b"\xfe\xfd\x06" + sid) and fwd.endswith(b"HELLO"))

            # Filter that excludes the host.
            req2 = req.replace(b"dwc_pid != 8", b"dwc_pid != 7")
            plain2 = enctypex.decrypt("tXH2sN", challenge, sb.packet(req2, ("198.51.100.1", 1)))
            self.assertNotIn(socket.inet_aton("203.0.113.7"), plain2)
        run(scenario())

    def test_filter(self):
        s = {"dwc_pid": "5", "dwc_mresv": "5", "numplayers": "1", "maxplayers": "2", "name": "Ab"}
        self.assertTrue(matches("dwc_pid = 5 and numplayers < maxplayers", s))
        self.assertFalse(matches("dwc_mresv != dwc_pid", s))
        self.assertTrue(matches("(dwc_pid = 1 or dwc_pid = 5) and not dwc_pid = 3", s))
        self.assertTrue(matches("name like 'A%'", s))
        self.assertTrue(matches("dwc_pid & 1 = 1", s))
        with self.assertRaises(ValueError):
            matches("__import__('os')", s)

    def test_available(self):
        qr = QRProtocol(Registry(), {})
        qr.transport = FakeTransport()

        async def go():
            qr.datagram_received(b"\x09\x00\x00\x00\x00\x00pokemondpds\x00", ("1.2.3.4", 5))
            await asyncio.sleep(0.1)
        run(go())
        self.assertEqual(qr.transport.sent[0][0], b"\xfe\xfd\x09\x00\x00\x00\x00")


class NatNegTest(unittest.TestCase):
    def test_pairing(self):
        nn = NatNegProtocol()
        nn.transport = FakeTransport()
        cookie = b"\x00\x00\x30\x39"

        def init(index, port_type, addr):
            pkt = MAGIC + b"\x03\x00" + cookie + bytes([port_type, index, 0]) + \
                socket.inet_aton("192.168.0.2") + struct.pack(">H", 1234) + b"pokemondpds\x00"
            nn.datagram_received(pkt, addr)

        init(0, 1, ("203.0.113.1", 1111))
        self.assertEqual(nn.transport.sent[-1][0][7], 0x01)       # init ack
        init(1, 1, ("203.0.113.2", 2222))
        connects = [(d, a) for d, a in nn.transport.sent if d[7] == 0x05]
        self.assertEqual(len(connects), 2)
        targets = {a: (socket.inet_ntoa(d[12:16]), struct.unpack(">H", d[16:18])[0])
                   for d, a in connects}
        self.assertEqual(targets[("203.0.113.1", 1111)], ("203.0.113.2", 2222))
        self.assertEqual(targets[("203.0.113.2", 2222)], ("203.0.113.1", 1111))


class GlobalTerminalTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def request(self, gen, rtype, body, total):
        msg = bytearray(total)
        struct.pack_into("<I", msg, 0, total)
        msg[4] = rtype
        struct.pack_into("<i", msg, 8, 1234)
        msg[0x140:0x140 + len(body)] = body
        if gen == 4:
            global_terminal.crypt4(msg)
        gt = global_terminal.GlobalTerminal(self.db, gen)
        if gen == 4:
            global_terminal.crypt4(msg)       # server decrypts on receive
        out = bytearray(gt.process(bytes(msg), "1.1.1.1"))
        if gen == 4:
            global_terminal.crypt4(out)
        self.assertEqual(struct.unpack_from("<I", out)[0], len(out))
        return bytes(out[6:])

    def test_gen4_battle_video(self):
        record = bytearray(os.urandom(0x1D4C))
        struct.pack_into("<H", record, 0x80, 25)    # Pikachu in the party
        r = self.request(4, 0xD8, bytes(record), 0x1E8C)
        self.assertEqual(r[:2], b"\x00\x00")
        serial = struct.unpack_from("<Q", r, 2)[0]
        self.assertGreaterEqual(serial, 100000000000)

        search = struct.pack("<IHBBB", 0, 25, 0xFF, 0xFF, 0xFF)
        r = self.request(4, 0xD9, search, 0x15C)
        self.assertEqual(struct.unpack_from("<i", r, 2)[0], 1)
        r = self.request(4, 0xDA, struct.pack("<Q", serial), 0x14C)
        self.assertEqual(r[2 + 12:], bytes(record))
        self.assertEqual(self.request(4, 0xDB, struct.pack("<Q", serial), 0x148), b"\x00\x00")

    def test_gen4_crypt_roundtrip(self):
        m = bytearray(os.urandom(400))
        orig = bytes(m)
        global_terminal.crypt4(m)
        global_terminal.crypt4(m)
        self.assertEqual(bytes(m), orig)

    def test_gen5_musical_and_video(self):
        data = os.urandom(0x230)
        r = self.request(5, 0x08, data, 0x370)
        self.assertEqual(r[:2], b"\x00\x00")
        species = struct.unpack_from("<H", data, 0)[0]
        r = self.request(5, 0x09, b"\x00" * 4 + struct.pack("<H", species), 0x14C)
        self.assertEqual(struct.unpack_from("<i", r, 2)[0], 1)
        r = self.request(5, 0xF0, os.urandom(0x18A4), 0x1AE8)
        self.assertEqual(r[:2], b"\x00\x00")

    def test_dressup_and_box(self):
        self.assertEqual(self.request(4, 0x20, os.urandom(0xE0), 0x220)[:2], b"\x00\x00")
        self.assertEqual(self.request(4, 0x08, struct.pack("<i", 2) + os.urandom(0x21C), 0x360)[:2],
                         b"\x00\x00")
        r = self.request(4, 0x09, b"\x00" * 4 + struct.pack("<i", 2), 0x14C)
        self.assertEqual(struct.unpack_from("<i", r, 2)[0], 1)


class EncTypeXTest(unittest.TestCase):
    def test_roundtrip(self):
        for n in (1, 10, 500):
            data = os.urandom(n)
            enc = enctypex.encrypt("1vTlwb", b"12345678", data)
            self.assertEqual(enctypex.decrypt("1vTlwb", b"12345678", enc), data)


class ServersStartTest(unittest.TestCase):
    """Boot every listener on ephemeral ports and talk to a few over sockets."""

    def test_boot(self):
        from pokegateway import dns_server, gpcm, http_server, matchmaking, natneg

        async def go():
            cfg = Config()
            cfg.bind = "127.0.0.1"
            for attr in ("dns_port", "http_port", "gpcm_port", "gpsp_port", "qr_port",
                         "natneg_port", "sb_port", "gt4_port", "gt5_port"):
                setattr(cfg, attr, 0)
            db = Database(":memory:")
            web = await http_server.start(WebApp(cfg, db, {}), "127.0.0.1", 0)
            port = web.sockets[0].getsockname()[1]
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"GET / HTTP/1.1\r\nHost: conntest.nintendowifi.net\r\n\r\n")
            data = await r.read()
            self.assertIn(b"200 OK", data)
            self.assertIn(b"This is test.html page", data)

            _, gp = await gpcm.start(db, "127.0.0.1", 0)
            r, w = await asyncio.open_connection("127.0.0.1", gp.sockets[0].getsockname()[1])
            hello = await asyncio.wait_for(r.read(200), 2)
            self.assertTrue(hello.startswith(b"\\lc\\1\\challenge\\"))
            w.close()

            dns = await dns_server.start(cfg, "10.1.2.3")
            dport = dns.sockets[0].getsockname()[1]
            r, w = await asyncio.open_connection("127.0.0.1", dport)
            q = dns_query("nas.nintendowifi.net")
            w.write(struct.pack(">H", len(q)) + q)
            ln = struct.unpack(">H", await r.readexactly(2))[0]
            resp = await r.readexactly(ln)
            self.assertEqual(socket.inet_ntoa(resp[-4:]), "10.1.2.3")
            w.close()
            await matchmaking.start(cfg)
            await natneg.start("127.0.0.1", 0)
            for s in (web, gp, dns):
                s.close()
        run(go())


if __name__ == "__main__":
    unittest.main()
