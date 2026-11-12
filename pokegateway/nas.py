"""nas.nintendowifi.net - Nintendo authentication server (/ac, /pr) and the
conntest.nintendowifi.net connection test page."""

import logging
import time

from .http_server import Response
from .util import nas_decode_form, nas_encode_form, random_str

log = logging.getLogger("nas")

NAS_HEADERS = {"NODE": "wifiappe1", "Server": "Nintendo Wii (http)"}

CONNTEST_BODY = (b'<html><head><title>HTML Page</title></head>'
                 b'<body bgcolor="#FFFFFF">This is test.html page</body></html>')


def conntest(_req):
    return Response(200, CONNTEST_BODY,
                    {"Content-Type": "text/html", "X-Organization": "Nintendo",
                     "Server": "BigIP"})


class NAS:
    def __init__(self, db, public_host=None):
        self.db = db
        self.public_host = public_host

    def handle(self, req):
        if req.method != "POST":
            return Response(200, b"ok", {"Content-Type": "text/html",
                                         "X-Organization": "Nintendo"})
        post = nas_decode_form(req.body)
        post["ipaddr"] = req.client_ip
        if req.path == "/pr":
            ret = self.profanity(post)
        else:
            action = post.get("action", "").lower()
            handler = {"acctcreate": self.acctcreate, "login": self.login,
                       "svcloc": self.svcloc}.get(action)
            if handler is None:
                log.warning("Unknown NAS action %r", action)
                ret = {"retry": "1", "returncd": "109"}
            else:
                ret = handler(post, req)
        # 12-char yyMMddHHmmss, the format the DS itself uses (devtime) and
        # that the Entralinked Gen 5 server returns -- not a 4-digit year.
        ret["datetime"] = time.strftime("%y%m%d%H%M%S")
        return Response(200, nas_encode_form(ret), dict(NAS_HEADERS))

    def acctcreate(self, post, _req):
        if self.db.is_banned(post.get("userid", ""), post.get("macadr", "")):
            return {"retry": "1", "returncd": "3913", "locator": "gamespy.com",
                    "reason": "User banned."}
        userid = self.db.next_userid()
        log.info("Account created: userid %s (%s)", userid, post.get("gsbrcd", "?"))
        return {"retry": "0", "returncd": "002", "userid": userid}

    def login(self, post, _req):
        userid = post.get("userid", "")
        if self.db.is_banned(userid, post.get("macadr", "")):
            return {"retry": "1", "returncd": "3914", "locator": "gamespy.com",
                    "reason": "User banned."}
        challenge = random_str(8)
        post["challenge"] = challenge
        token = self.db.create_authtoken(post)
        log.info("NAS login: userid %s game %s name %r", userid,
                 post.get("gsbrcd", "?"), post.get("ingamesn", post.get("devname", "")))
        return {"retry": "0", "returncd": "001", "locator": "gamespy.com",
                "challenge": challenge, "token": token}

    def svcloc(self, post, req):
        token = self.db.create_authtoken(post)
        svc = post.get("svc", "")
        log.info("NAS svcloc: svc=%s gamecd=%s", svc, post.get("gamecd"))
        # Minimal response, matching the Entralinked Gen 5 server: no "retry"
        # and the credential only under "servicetoken" (extra fields appear to
        # make the Gen 5 game reject the service-location step).
        if svc in ("9000", "9001"):
            svchost = self.public_host or "dls1.nintendowifi.net"   # DLS1 / Mystery Gift
        elif svc == "0000":
            svchost = "external"                                    # Global Link / Game Sync
        else:
            svchost = "n/a"
        # returncd 001 (not the Gen 4 convention 007): the Gen 5 game stops
        # right after svcloc otherwise, for every service type. (Entralinked.)
        ret = {"returncd": "001", "statusdata": "Y", "svchost": svchost,
               "servicetoken": token}
        return ret

    def profanity(self, post):
        # Accept every word ("0" == not profane) for every region.
        count = len(post.get("words", "").split("\t"))
        words = "0" * count
        ret = {"prwords": words, "returncd": "000"}
        for region in "ACEJKP":
            ret["prwords" + region] = words
        return ret
