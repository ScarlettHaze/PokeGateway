"""Pokemon Global Link / Game Sync (en-ds.pokemon-gl.com, host "external").

Gen 5 (BW/B2W2) talks to the Global Link at /dsio/gw for Game Sync: Dream
World save upload/download, C-Gear skins, Memory Link, etc. This is a large
service (see the Entralinked project); for now this handler logs the exact
requests the game makes and returns a minimal "nothing here" response, so we
can see the flow and build out real responses from there.

The game authenticates with HTTP Basic auth (user "pokemon").
"""

import logging
import struct

from .http_server import Response

log = logging.getLogger("pgl")

# PGL status codes (from Entralinked): 0 = OK, 1 = no data, 8 = no account.
STATUS_NO_DATA = 1


def handle(req):
    p = req.query.get("p", "")
    log.info("PGL %s %s  p=%s gsid=%s rom=%s lang=%s auth=%s body=%d bytes",
             req.method, req.path, p, req.query.get("gsid"), req.query.get("rom"),
             req.query.get("langcode"), req.headers.get("authorization", "-"),
             len(req.body))
    if req.body:
        log.debug("PGL body: %s", req.body[:128].hex())

    # Minimal response: a little-endian status word saying "no data". This is
    # a placeholder until the Game Sync service is implemented; it at least
    # lets us observe which PGL calls the game makes.
    body = struct.pack("<i", STATUS_NO_DATA)
    return Response(200, body, {"Content-Type": "application/octet-stream"})
