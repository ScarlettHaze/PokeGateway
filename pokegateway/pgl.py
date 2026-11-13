"""Pokemon Global Link / Game Sync (en-ds.pokemon-gl.com, host "external").

Gen 5 (BW/B2W2) talks to the Global Link at /dsio/gw for Game Sync: register
a Game Sync ID, upload the save ("tuck in" Pokemon), and download Dream World
results. Ported from the Entralinked project.

Request: /dsio/gw?p=<action>&tok=<servicetoken>&gsid=<id>&rom=<n>&langcode=<n>
with HTTP Basic auth (user "pokemon"). Response: a 128-byte status block
(int32 status + 124 pad) optionally followed by action-specific data.
"""

import base64
import logging
import struct

from .http_server import Response
from .util import gsid_stringify, gsid_valid

log = logging.getLogger("pgl")

# PGL status codes.
OK = 0
NO_DATA = 1
ALREADY_EXISTS = 2
ERROR = 3
NO_ACCOUNT = 8

BASIC_USER = "pokemon"
BASIC_PASS = "2Phfv9MY"


# Join Avenue / Random Matchup "world battle" (competition) progress states,
# from DREAM_WORLD_MATCHUP_TYPE in the game source (dreamworld_netdata.h).
MATCHUP_NONE = 0
MATCHUP_SIGNUP = 1
MATCHUP_ENTRY = 2
MATCHUP_END = 3
MATCHUP_RETIRE = 4
ENTRYFLAG_DS_WRITE = 0
ENTRYFLAG_GPF_WRITE = 1


def _status_block(code, extra=b""):
    """The 128-byte `gs_response` header every /dsio/gw body starts with,
    followed by the response data.

      long  ret_cd      (0 = OK)
      ulong body_size   (length of the data after the header)
      ulong start_sec   (server time; 0 is accepted)
      char  yobi1[8]
      ushort desc_cd
      char  desc_msg[80]
      char  yobi2[25]
      char  ext_info[1] == 'Z'   (position-check marker the game verifies)

    4+4+4+8+2+80+25+1 = 128 bytes, then `extra`. The game reads the payload at
    &response[sizeof(gs_response)], i.e. offset 128.
    """
    header = struct.pack("<iII", code, len(extra), 0)   # ret_cd, body_size, start_sec
    header += b"\x00" * 8                                 # yobi1
    header += struct.pack("<H", 0)                        # desc_cd
    header += b"\x00" * 80                                # desc_msg
    header += b"\x00" * 25                                # yobi2
    header += b"Z"                                        # ext_info (must be 'Z')
    assert len(header) == 128
    return header + extra


class PGL:
    def __init__(self, db):
        self.db = db

    def _authorized(self, req):
        auth = req.headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return True          # be lenient: accept if no/odd auth
        try:
            user, _, pw = base64.b64decode(auth.split(" ", 1)[1]).decode("latin-1").partition(":")
        except Exception:
            return True
        return user == BASIC_USER and pw == BASIC_PASS

    def handle(self, req):
        p = req.query.get("p", "")
        gsid = req.query.get("gsid")
        rom = _int(req.query.get("rom"))
        lang = _int(req.query.get("langcode"))
        log.info("PGL %s p=%s gsid=%s rom=%s lang=%s auth=%s body=%d", req.method, p,
                 gsid, rom, lang, "yes" if req.headers.get("authorization") else "no",
                 len(req.body))
        if not self._authorized(req):
            return Response(401, b"", {"WWW-Authenticate": 'Basic realm="pgl"'})

        handler = ROUTES.get(p)
        if handler is None:
            log.warning("PGL: unhandled action p=%r", p)
            return Response(200, _status_block(OK), _octet())
        try:
            body = handler(self, req, gsid, rom, lang)
        except Exception:
            log.exception("PGL action %s failed", p)
            body = _status_block(ERROR)
        return Response(200, body, _octet())

    # ------------------------------------------------------------ account
    def account_createdata(self, req, gsid, rom, lang):
        # Body is the raw Game Sync ID as a decimal integer.
        raw = req.body.decode("latin-1").replace("\x00", "").strip()
        try:
            gsid_str = gsid_stringify(int(raw))
        except ValueError:
            return _status_block(NO_ACCOUNT)
        if not gsid_valid(gsid_str):
            return _status_block(NO_ACCOUNT)
        if self.db.pgl_exists(gsid_str):
            return _status_block(ALREADY_EXISTS)
        self.db.pgl_create(gsid_str, rom, lang)
        log.info("PGL: registered Game Sync ID %s", gsid_str)
        return _status_block(OK)

    def account_create_upload(self, req, gsid, rom, lang):
        if gsid and not self.db.pgl_exists(gsid):
            self.db.pgl_create(gsid, rom, lang)
        return _status_block(OK)

    def account_playstatus(self, req, gsid, rom, lang):
        player = self.db.pgl_get(gsid) if gsid else None
        if player is None:
            return _status_block(NO_ACCOUNT)
        # status block + a 2-byte play status (0 = awake / ready).
        return _status_block(OK, struct.pack("<H", player["status"]))

    def sleepily_bitlist(self, req, gsid, rom, lang):
        if gsid and not self.db.pgl_exists(gsid):
            return _status_block(NO_DATA)
        # 128-byte bitmap: every species (1..649) available to tuck in.
        bits = bytearray(128)
        for species in range(1, 650):
            bits[species // 8] |= 1 << (species % 8)
        return _status_block(OK, bytes(bits))

    # ------------------------------------------------------------ save data
    def savedata_upload(self, req, gsid, rom, lang):
        if not gsid or not self.db.pgl_exists(gsid):
            return _status_block(NO_DATA)
        # Store the raw save for now; mark the player "sleeping" (tucked in).
        self.db.pgl_update(gsid, status=1, rom=rom, lang=lang)
        log.info("PGL: %s uploaded save (%d bytes), now sleeping", gsid, len(req.body))
        return _status_block(OK)

    def savedata_download(self, req, gsid, rom, lang):
        player = self.db.pgl_get(gsid) if gsid else None
        if player is None:
            return _status_block(NO_DATA)
        # No Dream World content yet: a valid "ok, nothing to deliver" payload.
        # dream counter(4) + 10 encounters(80) + levels(2) + pad(1) +
        # 3 dlc indices(3) + decor flags(2) + items(60) + decor(130) + short(2)
        extra = (struct.pack("<i", 0) + b"\x00" * 80 + struct.pack("<h", 0) + b"\x00"
                 + b"\x00\x00\x00" + b"\x00\x00" + b"\x00" * 60)
        extra += b"".join(struct.pack("<H", 0x7E) + b"\x00" * 24 for _ in range(5))
        extra += struct.pack("<H", 0)
        return _status_block(OK, extra)

    def savedata_download_finish(self, req, gsid, rom, lang):
        if gsid and self.db.pgl_exists(gsid):
            self.db.pgl_update(gsid, status=0)      # awake again
        return _status_block(OK)

    def savedata_getbw(self, req, gsid, rom, lang):
        # Memory Link (BW -> B2W2). No linked save: report "none".
        return _status_block(NO_ACCOUNT)

    def worldbattle_download(self, req, gsid, rom, lang):
        # Random Matchup (rating battles) GETs worldbattle.download before it
        # will let you battle. The game reads a WORLDBATTLE_STATE_DATA:
        #   u32 WifiMatchUpID; u8 GPFEntryFlg; u8 WifiMatchUpState; u8 signin; u8 dummy
        # and in SEQ_CHECK_GPF the ONLY field it checks is `signin`: non-zero ->
        # proceed to the battle, zero -> "no server" and bail. Report the player
        # as signed up and entered so rating matchmaking is allowed.
        state = struct.pack("<IBBBB",
                            1,                   # WifiMatchUpID (open competition)
                            ENTRYFLAG_GPF_WRITE, # GPFEntryFlg: written by the server
                            MATCHUP_ENTRY,       # WifiMatchUpState: entered
                            1,                   # signin: registered  <-- the gate
                            0)                   # dummy
        return _status_block(OK, state)

    def worldbattle_upload(self, req, gsid, rom, lang):
        # The game POSTs a WORLDBATTLE_SET_DATA to record its entry state; just
        # acknowledge it.
        return _status_block(OK)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _octet():
    return {"Content-Type": "application/octet-stream"}


ROUTES = {
    "account.createdata": PGL.account_createdata,
    "account.create.upload": PGL.account_create_upload,
    "account.playstatus": PGL.account_playstatus,
    "sleepily.bitlist": PGL.sleepily_bitlist,
    "savedata.upload": PGL.savedata_upload,
    "savedata.download": PGL.savedata_download,
    "savedata.download.finish": PGL.savedata_download_finish,
    "savedata.getbw": PGL.savedata_getbw,
    "worldbattle.upload": PGL.worldbattle_upload,
    "worldbattle.download": PGL.worldbattle_download,
}


# Backwards-compatible module-level entry point used by app.py.
_singleton = {}


def handle(req, db=None):
    if db is not None:
        _singleton["pgl"] = PGL(db)
    pgl = _singleton.get("pgl")
    if pgl is None:
        return Response(200, _status_block(OK), _octet())
    return pgl.handle(req)
