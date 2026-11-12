"""HTTP request routing for all web-based services."""

import logging

from . import pgl
from .dls1 import DLS1
from .gamestats import Gamestats, pkvldt
from .http_server import Response
from .nas import NAS, conntest

log = logging.getLogger("app")


class WebApp:
    def __init__(self, config, db, state):
        self.db = db
        self.state = state          # dict with "gpcm", "registry" for the status page
        self.nas = NAS(db)
        self.gamestats = Gamestats(db)
        self.dls1 = DLS1(config.dls1_dir, db)

    async def __call__(self, req):
        host, path = req.host, req.path
        if host.startswith("conntest.") or path == "/conntest.html":
            return conntest(req)
        if path in ("/ac", "/pr") or host.startswith("nas"):
            return self.nas.handle(req)
        if path == "/download" or host.startswith("dls1."):
            return self.dls1.handle(req)
        if path.startswith("/pokemondpds/") or path.startswith("/syachi2ds/"):
            return self.gamestats.handle(req)
        if path == "/pokemon/validate" or host.startswith("pkvldtprod."):
            return pkvldt(req)
        if path.startswith("/dsio/gw") or host.endswith("pokemon-gl.com") or host == "external":
            return pgl.handle(req)
        if path == "/":
            return self.status_page()
        return Response(404, b"Not found")

    def status_page(self):
        c = self.db.counts()
        gpcm = self.state.get("gpcm")
        registry = self.state.get("registry")
        online = len(gpcm.sessions) if gpcm else 0
        hosting = len([s for s in registry.sessions.values() if s.registered]) if registry else 0
        rows = "".join(
            "<tr><td>Gen %d</td><td>#%d</td><td>Lv %d</td><td>#%d (Lv %d-%s)</td><td>%s</td></tr>"
            % (g["gen"], g["species"], g["level"], g["req_species"], g["req_min"],
               g["req_max"] or "100", "traded" if g["is_exchanged"] else "listed")
            for g in self.db.gts_list()[:50])
        body = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PokeGateway</title>
<style>body{{font-family:system-ui,sans-serif;max-width:760px;margin:2em auto;padding:0 16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ccc;padding:4px;text-align:left}}</style>
</head><body><h1>PokeGateway</h1>
<p>Nintendo Wi-Fi Connection replacement for Pokemon Gen 4 &amp; 5.</p>
<ul>
<li>Registered profiles: {c['users']}</li>
<li>Online now: {online} (matchmaking sessions: {hosting})</li>
<li>GTS listings: Gen 4 {c['gts4']}, Gen 5 {c['gts5']} &middot; trades completed: {c['trades']}</li>
<li>Battle videos: {c['battle_videos']} &middot; dress-ups: {c['dressups']} &middot;
boxes: {c['boxes']} &middot; musicals: {c['musicals']}</li>
</ul>
<h2>GTS</h2><table><tr><th>Gen</th><th>Offered</th><th>Level</th><th>Wants</th><th>State</th></tr>
{rows or '<tr><td colspan="5">Nothing listed yet.</td></tr>'}</table>
</body></html>"""
        return Response(200, body.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8"})

