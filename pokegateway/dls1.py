"""dls1.nintendowifi.net - Nintendo download server (Wi-Fi Mystery Gift,
Gen 5 PWT tournaments, ...).

Files live in <dls1_dir>/<gamecd>/, laid out like the DLS1-Files archive
(https://github.com/InternalLoss/DLS1-Files), e.g. dlc/IPGE/, dlc/IRAO/.
An optional _list.txt controls the listing; each line is tab separated:

    filename  description  attr1  attr2  attr3  size

Without _list.txt every file in the directory is offered with blank
attributes (fine for Gen 4, but Gen 5 gifts need the attributes).

Pokemon specifics (same behaviour as AltWFC/Wiimmfi):
  * Gen 4 (DP/Pt/HGSS) expect exactly one gift per connection. One file is
    picked by the DS's clock: index = (day of year - 1) % number of gifts,
    so players choose a gift by changing the DS date.
  * Gen 5 (BW/B2W2) always send gamecd IRAJ/IRAK/IRAO; the real game is in
    rhgamecd. Mystery Gift entries (attr1 MYSTERY_<lang>) carry a hex bit
    mask in attr2 saying which games may receive them:
        0x100000 Black, 0x200000 White, 0x400000 White 2, 0x800000 Black 2
    Non-gift downloads (e.g. B2W2 PWT tournaments, attr1 WBT_<lang>) use
    the normal attribute/offset listing.
"""

import logging
import os
import random
import re
import time

from .http_server import Response
from .util import nas_decode_form

log = logging.getLogger("dls1")

SAFE = re.compile(r"^[A-Za-z0-9_.\-]+$")

GEN4_PREFIXES = ("ADA", "APA", "CPU", "IPG", "IPK")
# Folder fallbacks: Pearl and HeartGold share Diamond's / SoulSilver's gifts.
FOLDER_FALLBACK = {"APA": "ADA", "IPK": "IPG"}
GEN5_GAME_BITS = {"A": 0x100000, "B": 0x200000, "D": 0x400000, "E": 0x800000}


class DLS1:
    def __init__(self, root, db=None):
        self.root = os.path.abspath(root)
        self.db = db

    def _dir(self, gamecd):
        if not SAFE.match(gamecd or ""):
            return None
        path = os.path.join(self.root, gamecd)
        if not os.path.isdir(path) and gamecd[:3] in FOLDER_FALLBACK:
            alt = os.path.join(self.root, FOLDER_FALLBACK[gamecd[:3]] + gamecd[3:])
            if os.path.isdir(alt):
                return alt
        return path

    @staticmethod
    def _entries(path):
        list_file = os.path.join(path, "_list.txt")
        out = []
        if os.path.isfile(list_file):
            with open(list_file, "r", encoding="latin-1") as fh:
                for line in fh:
                    line = line.rstrip("\r\n")
                    if line.strip():
                        out.append((line.split("\t") + [""] * 6)[:6])
            return out
        if not os.path.isdir(path):
            return []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if name.startswith("_") or name.startswith(".") or not os.path.isfile(full):
                continue
            out.append([name, "", "", "", "", str(os.path.getsize(full))])
        return out

    @staticmethod
    def _filter_attrs(entries, post):
        out = []
        for e in entries:
            if all(not post.get(key) or e[2 + i] == post[key]
                   for i, key in enumerate(("attr1", "attr2", "attr3"))):
                out.append(e)
        return out

    @staticmethod
    def _filter_gen5_game(entries, rhgamecd):
        bit = GEN5_GAME_BITS.get(rhgamecd[2:3]) if rhgamecd else None
        if bit is None:
            return entries
        out = []
        for e in entries:
            try:
                if int(e[3] or "0", 16) & bit:
                    out.append(e)
            except ValueError:
                continue
        return out

    def _pick_by_date(self, entries, post):
        if not entries:
            return []
        index = None
        nas = self.db.get_authtoken(post.get("token", "")) if self.db else None
        devtime = (nas or {}).get("devtime", "")
        try:
            yday = time.strptime(devtime, "%y%m%d%H%M%S").tm_yday
            index = (yday - 1) % len(entries)
        except ValueError:
            index = random.randrange(len(entries))
        return [entries[index]]

    def _select(self, entries, post):
        """Return (entries to list, count to report)."""
        gamecd = post.get("gamecd", "")
        attr1 = post.get("attr1", "")
        filtered = self._filter_attrs(entries, post)
        if gamecd.startswith("IRA") and attr1.startswith("MYSTERY"):
            filtered = self._filter_gen5_game(filtered, post.get("rhgamecd", ""))
            return self._pick_by_date(filtered, post), len(filtered)
        if gamecd[:3] in GEN4_PREFIXES:
            picked = self._pick_by_date(filtered, post)
            return picked, len(picked)
        offset = int(post.get("offset", "0") or 0)
        num = int(post.get("num", "0") or 0) or len(filtered)
        return filtered[offset:offset + num], len(filtered)

    def handle(self, req):
        if req.method != "POST":
            return Response(404, b"")
        post = nas_decode_form(req.body)
        path = self._dir(post.get("gamecd", ""))
        if path is None:
            return Response(403, b"")
        action = post.get("action", "").lower()
        headers = {"X-DLS-Host": "http://127.0.0.1/"}

        if action in ("count", "list"):
            listed, count = self._select(self._entries(path), post)
            if action == "count":
                return Response(200, str(count).encode(), headers)
            body = "".join("\t".join(e) + "\r\n" for e in listed) or "\r\n"
            log.info("DLS1 list for %s/%s attr1=%r -> %s", post.get("gamecd"),
                     post.get("rhgamecd", ""), post.get("attr1", ""), [e[0] for e in listed])
            return Response(200, body.encode("latin-1"), headers)

        if action == "contents":
            name = os.path.basename(post.get("contents", ""))
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
