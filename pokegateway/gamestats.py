"""gamestats2.gs.nintendowifi.net - GTS, Battle Tower/Subway, trainer
profiles and Wi-Fi Plaza lobby helpers for Gen 4 (/pokemondpds/...) and
Gen 5 (/syachi2ds/web/...), plus the Gen 5 Pokemon validation server
(pkvldtprod.nintendo.co.jp/pokemon/validate).

Request flow ("gamestats" protocol):
  1. GET ?pid=N                  -> 32 char random token
  2. GET ?pid=N&hash=H&data=D    -> H = sha1(salt + token), D = url-safe
     base64 of [checksum ^ mask (BE)] + payload (optionally XOR-encrypted).
"""

import base64
import hashlib
import logging
import secrets
import struct
import time

from .http_server import Response
from .pokemon import (GTS_RECORD_SIZE_4, GTS_RECORD_SIZE_5, GtsRecord,
                      fake_tower_leader, fake_tower_record)

log = logging.getLogger("gamestats")


class GamestatsError(Exception):
    def __init__(self, status=400):
        super().__init__(status)
        self.status = status


class GameProfile:
    """Per-game constants of the gamestats protocol."""

    def __init__(self, salt, rng_mul, rng_add, rng_mod, hash_mask, version,
                 hashed_response, encrypted):
        self.salt = salt
        self.rng_mul = rng_mul
        self.rng_add = rng_add
        self.rng_mod = rng_mod
        self.hash_mask = hash_mask
        self.version = version            # 2: pid + payload, 3: pid + len + payload
        self.hashed_response = hashed_response
        self.encrypted = encrypted

    def decode(self, data: str) -> bytes:
        raw = base64.b64decode(data.replace("-", "+").replace("_", "/") + "==="[: (4 - len(data) % 4) % 4])
        if len(raw) < 4:
            raise GamestatsError()
        checksum = struct.unpack(">i", raw[:4])[0] ^ _s32(self.hash_mask)
        body = raw[4:]
        if self.encrypted:
            rand = _s32((checksum & 0xFFFF) | (checksum << 16))
            out = bytearray(len(body))
            for i, b in enumerate(body):
                rand = (((rand & 0xFFFFFFFF) * self.rng_mul + self.rng_add) & 0xFFFFFFFF) % self.rng_mod
                out[i] = b ^ ((rand >> 16) & 0xFF)
            body = bytes(out)
        if _s32(sum(body)) != checksum:
            raise GamestatsError()
        return body

    def encode(self, payload: bytes) -> str:
        """Inverse of decode() - used by tests / client simulators."""
        checksum = _s32(sum(payload))
        body = payload
        if self.encrypted:
            rand = _s32((checksum & 0xFFFF) | (checksum << 16))
            out = bytearray(len(body))
            for i, b in enumerate(body):
                rand = (((rand & 0xFFFFFFFF) * self.rng_mul + self.rng_add) & 0xFFFFFFFF) % self.rng_mod
                out[i] = b ^ ((rand >> 16) & 0xFF)
            body = bytes(out)
        head = struct.pack(">i", checksum ^ _s32(self.hash_mask))
        return base64.b64encode(head + body).decode().replace("+", "-").replace("/", "_")

    def token_hash(self, token: str) -> str:
        return hashlib.sha1((self.salt + token).encode("ascii")).hexdigest()

    def finish(self, payload: bytes) -> bytes:
        if not self.hashed_response:
            return payload
        b64 = base64.b64encode(payload).decode().replace("+", "-").replace("/", "_")
        return payload + hashlib.sha1((self.salt + b64 + self.salt).encode()).hexdigest().encode()


def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


GEN4 = GameProfile("sAdeqWo3voLeC5r16DYv", 0x45, 0x1111, 0x80000000, 0x4A3B2C1D,
                   version=2, hashed_response=False, encrypted=True)
GEN4_WEB = GameProfile("uLMOGEiiJogofchScpXb", 0x000244FD, 0x00006015, 0x10000000,
                       0x5B440E7E, version=3, hashed_response=True, encrypted=True)
GEN5 = GameProfile("HZEdGCzcGGLvguqUEKQN", 0x0001D935, 0x00002DD5, 0x00000008,
                   0x2DB842B2, version=3, hashed_response=True, encrypted=False)


class Session:
    def __init__(self, pid, path, token, hash_):
        self.pid = pid
        self.path = path
        self.token = token
        self.hash = hash_
        self.expires = time.time() + 600
        self.tag = None


class Sessions:
    def __init__(self):
        self.by_hash = {}

    def create(self, profile, pid, path):
        self.prune()
        token = "".join(secrets.choice(
            "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz") for _ in range(32))
        sess = Session(pid, path, token, profile.token_hash(token))
        self.by_hash[sess.hash] = sess
        return sess

    def get(self, hash_):
        sess = self.by_hash.get(hash_)
        if sess and sess.expires < time.time():
            self.remove(sess)
            return None
        return sess

    def find(self, pid, path):
        for sess in list(self.by_hash.values()):
            if sess.pid == pid and sess.path == path and sess.tag is not None \
                    and sess.expires >= time.time():
                return sess
        return None

    def remove(self, sess):
        if sess is not None:
            self.by_hash.pop(sess.hash, None)

    def prune(self):
        now = time.time()
        for h, s in list(self.by_hash.items()):
            if s.expires < now:
                del self.by_hash[h]


def code(n):
    return struct.pack("<H", n)


class Gamestats:
    def __init__(self, db):
        self.db = db
        self.sessions = Sessions()

    # ------------------------------------------------------------ dispatch
    def handle(self, req):
        path = req.path
        if path.startswith("/pokemondpds/web/"):
            profile, gen = GEN4_WEB, 4
        elif path.startswith("/pokemondpds/"):
            profile, gen = GEN4, 4
        elif path.startswith("/syachi2ds/web/"):
            profile, gen = GEN5, 5
        else:
            return Response(404, b"Not found")

        form = req.form()
        try:
            pid = int(form.get("pid", ""))
        except ValueError:
            return Response(400, b"Bad request")

        if "data" not in form and "hash" not in form:
            sess = self.sessions.create(profile, pid, path)
            return Response(200, sess.token.encode())

        sess = self.sessions.get(form.get("hash", ""))
        if sess is None:
            return Response(400, b"Bad request")
        try:
            data = profile.decode(form.get("data", ""))
            if len(data) < 4 or struct.unpack("<i", data[:4])[0] != pid:
                raise GamestatsError()
            if profile.version == 3:
                if len(data) < 8 or struct.unpack("<i", data[4:8])[0] + 8 != len(data):
                    raise GamestatsError()
                payload = data[8:]
            else:
                payload = data[4:]
            sess.path = path
            handler = ROUTES.get(path)
            if handler is None:
                self.sessions.remove(sess)
                log.warning("Unhandled gamestats path %s", path)
                raise GamestatsError(404)
            body = handler(self, gen, pid, payload, sess, req)
        except GamestatsError as exc:
            self.sessions.remove(sess)
            return Response(exc.status, b"Bad request" if exc.status == 400 else b"Error")
        return Response(200, profile.finish(body))

    # ------------------------------------------------------------ common
    def set_profile(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        if len(data) != 100:
            raise GamestatsError()
        self.db.set_gamestats_profile(gen, pid, data, req.client_ip)
        # Two 32-bit result codes; zeros = continue normally. (Gen 4 also uses
        # this for Wii message registration; we just accept.)
        return b"\x00" * 8

    # ------------------------------------------------------------ GTS
    def gts_info(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        return code(1)

    def gts_result(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        rec = self.db.gts_get(gen, pid)
        if rec is None:
            return code(5)          # nothing deposited
        if rec["is_exchanged"]:
            return rec["data"]      # a traded Pokemon is waiting
        return code(4)              # own Pokemon still listed

    def gts_get(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        rec = self.db.gts_get(gen, pid)
        return code(5) if rec is None else rec["data"]

    def gts_delete(self, gen, pid, data, sess, req):
        """Remove a received (traded) Pokemon after the game stored it."""
        self.sessions.remove(sess)
        rec = self.db.gts_get(gen, pid)
        fail = code(3) if gen == 4 else code(5)
        if rec is None or not rec["is_exchanged"]:
            return fail
        return code(1) if self.db.gts_delete(gen, pid) else code(5)

    def gts_return(self, gen, pid, data, sess, req):
        """Take your own deposited Pokemon back."""
        self.sessions.remove(sess)
        rec = self.db.gts_get(gen, pid)
        if rec is None or rec["is_exchanged"] or not self.db.gts_lock_ok(gen, pid, pid):
            return code(2)
        return code(1) if self.db.gts_delete(gen, pid) else code(2)

    def _record_size(self, gen):
        return GTS_RECORD_SIZE_4 if gen == 4 else GTS_RECORD_SIZE_5

    def gts_post(self, gen, pid, data, sess, req):
        size = self._record_size(gen)
        expected = size if gen == 4 else 432   # gen 5 adds a validation signature
        if len(data) != expected:
            self.sessions.remove(sess)
            raise GamestatsError()
        if self.db.gts_get(gen, pid) is not None:
            self.sessions.remove(sess)
            return code(0x0E)       # already something there, kick back out
        rec = GtsRecord(gen, data[:size])
        if not rec.valid():
            self.sessions.remove(sess)
            return code(0x0C)       # "That Pokemon may not be offered for trade!"
        rec.is_exchanged = 0
        rec.pid = pid
        rec.time_deposited = time.gmtime()
        rec.time_exchanged = None
        sess.tag = rec
        return code(1)

    def gts_post_finish(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        prev = self.sessions.find(pid, req.path.replace("post_finish.asp", "post.asp"))
        if prev is None:
            return code(2)
        self.sessions.remove(prev)
        rec = prev.tag
        if self.db.gts_get(gen, pid) is not None:
            return code(2)
        self.db.gts_put(gen, pid, rec)
        log.info("GTS%d: %d deposited species %d lv %d asking for %d", gen, pid,
                 rec.species, rec.level, rec.req_species)
        return code(1)

    def gts_search(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        if len(data) < 7 or len(data) > 8:
            raise GamestatsError()
        species, gender, min_lv, max_lv, _unk, count = struct.unpack("<HBBBBB", data[:7])
        country = data[7] if len(data) > 7 else 0
        if species < 1:
            raise GamestatsError()
        out = b"" if gen == 4 else code(1)
        count = min(count, 7)
        if count < 1:
            return out
        rows = self.db.gts_search(gen, pid, species, gender, min_lv, max_lv, country, count)
        for row in rows:
            out += row["data"]
        self.db.gts_set_last_search(gen, pid)
        return out

    def gts_exchange(self, gen, pid, data, sess, req):
        size = self._record_size(gen)
        expected = size + 4 if gen == 4 else 432
        if len(data) != expected:
            self.sessions.remove(sess)
            raise GamestatsError()
        upload = GtsRecord(gen, data[:size])
        upload.is_exchanged = 0
        target_pid = struct.unpack("<i", data[size:size + 4])[0]
        target = self.db.gts_get(gen, target_pid)
        last_search = self.db.gts_get_last_search(gen, pid)
        if (target is None or last_search is None or target["is_exchanged"]
                or target["deposited"] > last_search):
            self.sessions.remove(sess)
            return code(2)          # "it was traded to another Trainer"
        result = GtsRecord(gen, target["data"])
        if not upload.valid() or not upload.can_trade(result):
            self.sessions.remove(sess)
            return code(0x0C)
        if not self.db.gts_lock(gen, target_pid, pid):
            self.sessions.remove(sess)
            return code(2)
        sess.tag = (upload, result, target_pid)
        return target["data"]

    def gts_exchange_finish(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        prev = self.sessions.find(pid, req.path.replace("exchange_finish.asp", "exchange.asp"))
        if prev is None:
            return code(2)
        self.sessions.remove(prev)
        upload, result, target_pid = prev.tag
        current = self.db.gts_get(gen, target_pid)
        if (current is None or current["data"] != result.raw or current["is_exchanged"]
                or not self.db.gts_lock_ok(gen, target_pid, pid)):
            return code(2)
        # The depositor gets the uploader's Pokemon, flagged as traded, under
        # their own pid; it is delivered by their next result.asp call.
        traded = GtsRecord(gen, upload.raw)
        traded.flag_traded(result)
        self.db.gts_delete(gen, target_pid)
        self.db.gts_put(gen, target_pid, traded)
        self.db.gts_log_trade(gen, target_pid, pid, result.species)
        log.info("GTS%d: trade %d <-> %d (species %d for %d)", gen, pid, target_pid,
                 upload.species, result.species)
        return code(1)

    # ------------------------------------------------------------ Battle Tower / Subway
    def tower_info(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        return code(1)

    def tower_roomnum(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        return code(50)

    def tower_download(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        if len(data) != 2 or data[0] > 9 or data[1] > 49:
            raise GamestatsError()
        rank, room = data[0], data[1]
        rec_size = 228 if gen == 4 else 240
        opponents = [r["data"][:rec_size] for r in
                     self.db.tower_opponents(gen, pid, rank, room, 7)]
        while len(opponents) < 7:
            opponents.insert(0, fake_tower_record(gen, rank, len(opponents)))
        leaders = [r["data"][rec_size - 60: rec_size - 26] for r in
                   self.db.tower_leaders(gen, rank, room)]
        while len(leaders) < 30:
            leaders.append(fake_tower_leader(gen))
        return b"".join(opponents) + b"".join(leaders)

    def tower_upload(self, gen, pid, data, sess, req):
        self.sessions.remove(sess)
        if gen == 4:
            if len(data) != 239:
                raise GamestatsError()
            rank, room, won = data[0xE4], data[0xE5], data[0xE6]
            record = data[:228]
        else:
            if len(data) != 388:
                raise GamestatsError()
            rank, room, won = data[0xF0], data[0xF1], data[0xF2]
            record = data[:240]
        if won > 0 and rank <= 9 and room <= 49:
            self.db.tower_update(gen, pid, rank, room, won, record, won == 7)
        return code(1)

    # ------------------------------------------------------------ Wi-Fi Plaza (Pt/HGSS)
    def lobby_check_profile(self, gen, pid, data, sess, req):
        if len(data) != 168:
            raise GamestatsError()
        # Echo the profile back (minus the 12 byte prefix the game skips).
        return b"\x00" * 4 + data[4 + 12:4 + 164]

    def lobby_schedule(self, gen, pid, data, sess, req):
        # 4 byte status + schedule: u32 duration (s), i32 unknown,
        # i32 footprint option, u8 room type, u8 season, u16 entry count,
        # then (i32 time, i32 event) pairs.
        events = [(0, 1), (0, 7), (0, 11), (780, 8), (840, 2), (840, 9),
                  (900, 3), (900, 10), (900, 12), (960, 4), (960, 9), (960, 13),
                  (960, 15), (1020, 5), (1020, 14), (1020, 16), (1075, 18),
                  (1080, 6), (1080, 13), (1080, 17), (1140, 11), (1200, 19)]
        choice = secrets.randbelow(13)
        if choice == 0:
            room, footprint = 4, 1          # rare Mew room, Arceus footprints
        else:
            room, footprint = (choice - 1) // 3, 0
        body = struct.pack("<IiiBBH", 1200, 0, footprint, room, 0, len(events))
        for t, ev in events:
            body += struct.pack("<ii", t, ev)
        return b"\x00" * 4 + body

    def lobby_vip(self, gen, pid, data, sess, req):
        return b"\x00" * 4

    def lobby_questionnaire(self, gen, pid, data, sess, req):
        # Status + current question + last week's question (352 bytes each:
        # id, public id, text/answers (blank -> built-in text for ids < 1000),
        # 12 flag bytes, is_special), last week's three answer counts and
        # 12 trailing bytes. Total 732 bytes.
        def question(qid, public_id):
            return (struct.pack("<ii", qid, public_id) + b"\x00" * 328 +
                    bytes([0, 1, 1, 1, 1, 1, 0, 1, 1, 0, 0, 0]) + struct.pack("<i", 0))
        body = (question(298, 45) + question(297, 44) +
                struct.pack("<iii", 126, 70, 51) + struct.pack("<iii", 356, 273, 131))
        return b"\x00" * 4 + body

    def lobby_submit_questionnaire(self, gen, pid, data, sess, req):
        return b"\x00" * 4 + b"thx\x00"


ROUTES = {}


def _route(paths, fn):
    for p in paths:
        ROUTES[p] = fn


for _gen_prefix in ("/pokemondpds", "/syachi2ds/web"):
    _route([_gen_prefix + "/common/setProfile.asp"], Gamestats.set_profile)
    _route([_gen_prefix + "/worldexchange/info.asp"], Gamestats.gts_info)
    _route([_gen_prefix + "/worldexchange/result.asp"], Gamestats.gts_result)
    _route([_gen_prefix + "/worldexchange/get.asp"], Gamestats.gts_get)
    _route([_gen_prefix + "/worldexchange/delete.asp"], Gamestats.gts_delete)
    _route([_gen_prefix + "/worldexchange/return.asp"], Gamestats.gts_return)
    _route([_gen_prefix + "/worldexchange/post.asp"], Gamestats.gts_post)
    _route([_gen_prefix + "/worldexchange/post_finish.asp"], Gamestats.gts_post_finish)
    _route([_gen_prefix + "/worldexchange/search.asp"], Gamestats.gts_search)
    _route([_gen_prefix + "/worldexchange/exchange.asp"], Gamestats.gts_exchange)
    _route([_gen_prefix + "/worldexchange/exchange_finish.asp"], Gamestats.gts_exchange_finish)
    _route([_gen_prefix + "/battletower/info.asp"], Gamestats.tower_info)
    _route([_gen_prefix + "/battletower/roomnum.asp"], Gamestats.tower_roomnum)
    _route([_gen_prefix + "/battletower/download.asp"], Gamestats.tower_download)
    _route([_gen_prefix + "/battletower/upload.asp"], Gamestats.tower_upload)

_route(["/pokemondpds/web/enc/lobby/checkProfile.asp"], Gamestats.lobby_check_profile)
_route(["/pokemondpds/web/enc/lobby/getSchedule.asp"], Gamestats.lobby_schedule)
_route(["/pokemondpds/web/enc/lobby/getVIP.asp"], Gamestats.lobby_vip)
_route(["/pokemondpds/web/enc/lobby/getQuestionnaire.asp"], Gamestats.lobby_questionnaire)
_route(["/pokemondpds/web/enc/lobby/submitQuestionnaire.asp"], Gamestats.lobby_submit_questionnaire)


# ---------------------------------------------------------------------------
# pkvldtprod.nintendo.co.jp/pokemon/validate (Gen 5)
# ---------------------------------------------------------------------------

def pkvldt(req):
    """Gen 5 asks this server to validate Pokemon before GTS deposits and
    Random Matchups. The real server returned a signature the game does not
    verify, so we accept everything."""
    data = req.body
    nul = data.find(b"\x00")
    if nul < 0:
        return Response(400, b"Bad request")
    offset = nul + 1
    req_type = struct.unpack("<h", data[offset:offset + 2])[0] if len(data) >= offset + 2 else -1
    offset += 2
    if req_type in (0x0000, 0x0100):           # random matchup / GTS
        count = max(0, (len(data) - offset) // 220)
    else:
        count = 6
    body = b"\x00" + b"\x00\x00\x00\x00" * count + b"\x00" * 128
    return Response(200, body)
