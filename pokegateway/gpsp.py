"""GameSpy Presence Search Player (gpsp.gs.nintendowifi.net:29901)."""

import asyncio
import logging

from .util import gs_build, gs_parse

log = logging.getLogger("gpsp")


class GPSPServer:
    def __init__(self, db):
        self.db = db

    async def handle(self, reader, writer):
        buffer = ""
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), 60)
                if not chunk:
                    break
                buffer += chunk.decode("latin-1")
                messages, buffer = gs_parse(buffer)
                for msg in messages:
                    writer.write(self.respond(msg))
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    def respond(self, msg) -> bytes:
        cmd = msg["__cmd__"]
        if cmd == "otherslist":
            out = "\\otherslist\\"
            for pid in msg.get("opids", "").split("|"):
                if not pid.isdigit():
                    continue
                p = self.db.get_profile(int(pid))
                if p:
                    out += "\\o\\%s\\uniquenick\\%s" % (p["profileid"], p["uniquenick"])
            out += "\\oldone\\\\final\\"
            return out.encode("latin-1")
        if cmd == "valid":
            return gs_build("vr", "0")
        if cmd == "nicks":
            return gs_build("nr", "0", ("ndone", ""))
        # search / others / anything else: empty result
        return gs_build("bsrdone", "")


async def start(db, bind, port):
    server = GPSPServer(db)
    srv = await asyncio.start_server(server.handle, bind, port)
    log.info("GPSP listening on %s:%d", bind, port)
    return srv
