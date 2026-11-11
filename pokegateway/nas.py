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
        ret["datetime"] = time.strftime("%Y%m%d%H%M%S")
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
        ret = {"retry": "0", "returncd": "007", "statusdata": "Y"}
        svc = post.get("svc", "")
        if svc in ("9000", "9001"):
            ret["svchost"] = (self.public_host or req.host or "n/a")
            ret["token" if svc == "9000" else "servicetoken"] = token
        else:
            ret["svchost"] = "n/a"
            ret["servicetoken"] = token
        return ret

    def profanity(self, post):
        # Accept every word ("0" == not profane) for every region.
        count = len(post.get("words", "").split("\t"))
        words = "0" * count
        ret = {"prwords": words, "returncd": "000"}
        for region in "ACEJKP":
            ret["prwords" + region] = words
        return ret
