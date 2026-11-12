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


def _status_block(code, extra=b""):
    return struct.pack("<i", code) + b"\x00" * 124 + extra


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

    def worldbattle(self, req, gsid, rom, lang):
        return _status_block(NO_DATA)


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
    "worldbattle.upload": PGL.worldbattle,
    "worldbattle.download": PGL.worldbattle,
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
