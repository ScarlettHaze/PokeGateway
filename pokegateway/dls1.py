"""dls1.nintendowifi.net - Nintendo download server (Wi-Fi Mystery Gift).

Files are served from <dls1_dir>/<gamecd>/ (e.g. dlc/IPKE/). Optionally a
_list.txt in that directory controls the listing; each line is
    filename<TAB><TAB>attr1<TAB>attr2<TAB>attr3<TAB>size
Without _list.txt every file in the directory is offered.
"""

import logging
import os
import re

from .http_server import Response
from .util import nas_decode_form

log = logging.getLogger("dls1")

SAFE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class DLS1:
    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _dir(self, gamecd):
        if not SAFE.match(gamecd or ""):
            return None
        return os.path.join(self.root, gamecd)

    def _entries(self, path):
        list_file = os.path.join(path, "_list.txt")
        if os.path.isfile(list_file):
            with open(list_file, "r", encoding="latin-1") as fh:
                return [l.rstrip("\r\n").split("\t") for l in fh if l.strip()]
        if not os.path.isdir(path):
            return []
        out = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if name.startswith("_") or not os.path.isfile(full):
                continue
            out.append([name, "", "", "", "", str(os.path.getsize(full))])
        return out

    def handle(self, req):
        if req.method != "POST":
            return Response(404, b"")
        post = nas_decode_form(req.body)
        path = self._dir(post.get("gamecd", ""))
        if path is None:
            return Response(403, b"")
        action = post.get("action", "").lower()
        headers = {"X-DLS-Host": "http://127.0.0.1/"}
        entries = self._filter(self._entries(path), post)
        if action == "count":
            return Response(200, str(len(entries)).encode(), headers)
        if action == "list":
            offset = int(post.get("offset", "0") or 0)
            num = int(post.get("num", "0") or 0) or len(entries)
            chosen = entries[offset:offset + num]
            body = "".join("\t".join(e) + "\r\n" for e in chosen) or "\r\n"
            return Response(200, body.encode("latin-1"), headers)
        if action == "contents":
            name = post.get("contents", "")
            if not SAFE.match(name) or name.startswith("_"):
                return Response(403, b"")
            full = os.path.join(path, name)
            if not os.path.isfile(full):
                return Response(404, b"")
            with open(full, "rb") as fh:
                data = fh.read()
            log.info("Serving %s/%s to %s", post.get("gamecd"), name, req.client_ip)
            headers.update({"Content-Type": "application/x-dsdl",
                            "Content-Disposition": 'attachment; filename="%s"' % name})
            return Response(200, data, headers)
        return Response(200, b"", headers)

    @staticmethod
    def _filter(entries, post):
        out = []
        for e in entries:
            e = (e + [""] * 6)[:6]
            ok = True
            for i, key in enumerate(("attr1", "attr2", "attr3")):
                want = post.get(key)
                if want and e[2 + i] != want:
                    ok = False
            if ok:
                out.append(e)
        return out
