"""GameSpy Presence Connection Manager (gpcm.gs.nintendowifi.net:29900).

Handles login with the NAS authtoken, friend lists ("buddies"), presence
status and buddy messages. Friend codes are derived from the profile ID,
so registering friends in-game works once both players have logged in.
"""

import asyncio
import hashlib
import logging
import time

from .util import gs_b64encode, gs_build, gs_parse, ip_to_int_le, random_str

log = logging.getLogger("gpcm")


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("latin-1")).hexdigest()


def login_response(server_chal, nas_chal, client_chal, authtoken):
    """Expected client \\response\\ value."""
    h = _md5(nas_chal)
    return _md5(h + " " * 48 + authtoken + client_chal + server_chal + h)


def login_proof(server_chal, nas_chal, client_chal, authtoken):
    """\\proof\\ the server sends back (challenges swapped)."""
    h = _md5(nas_chal)
    return _md5(h + " " * 48 + authtoken + server_chal + client_chal + h)


class GPCMServer:
    def __init__(self, db):
        self.db = db
        self.sessions = {}          # profileid -> GPCMClient

    async def handle(self, reader, writer):
        client = GPCMClient(self, reader, writer)
        await client.run()

    def online(self):
        return [{"profileid": pid, "ip": c.ip, "status": c.status,
                 "statstring": c.statstring, "gameid": c.gameid}
                for pid, c in self.sessions.items()]


class GPCMClient:
    def __init__(self, server, reader, writer):
        self.server = server
        self.db = server.db
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        self.ip = peer[0]
        self.challenge = random_str(10, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        self.profileid = None
        self.gameid = ""
        self.sesskey = None
        self.status = "0"
        self.statstring = "Offline"
        self.locstring = ""
        self.last_seen = time.time()

    def send(self, data: bytes):
        try:
            self.writer.write(data)
        except Exception:
            pass

    async def run(self):
        self.send(gs_build("lc", "1", ("challenge", self.challenge), ("id", "1")))
        buffer = ""
        try:
            while True:
                chunk = await asyncio.wait_for(self.reader.read(4096), 180)
                if not chunk:
                    break
                self.last_seen = time.time()
                buffer += chunk.decode("latin-1")
                messages, buffer = gs_parse(buffer)
                for msg in messages:
                    handler = getattr(self, "cmd_" + msg["__cmd__"], None)
                    if handler is None:
                        log.debug("Unhandled GPCM command %s", msg["__cmd__"])
                        continue
                    handler(msg)
                await self.writer.drain()
                if len(buffer) > 16384:
                    break
        except (asyncio.TimeoutError, ConnectionError):
            pass
        except Exception:
            log.exception("GPCM client error")
        finally:
            await self.disconnect()

    async def disconnect(self):
        if self.profileid is not None and self.server.sessions.get(self.profileid) is self:
            del self.server.sessions[self.profileid]
            self.status, self.statstring, self.locstring = "0", "Offline", ""
            self.broadcast_status()
            log.info("Profile %d logged out", self.profileid)
        try:
            self.writer.close()
        except Exception:
            pass

    def error(self, err, msg, fatal=True, id_="1"):
        pairs = [("err", err)]
        if fatal:
            pairs.append(("fatal", ""))
        pairs += [("errmsg", msg), ("id", id_)]
        self.send(gs_build("error", "", *pairs))

    # --------------------------------------------------------------- login
    def cmd_login(self, msg):
        authtoken = msg.get("authtoken", "")
        nas = self.db.get_authtoken(authtoken)
        if nas is None:
            self.error(266, "There was an error validating the pre-authentication.")
            return
        expected = login_response(self.challenge, nas["challenge"],
                                  msg.get("challenge", ""), authtoken)
        if msg.get("response") != expected:
            log.warning("GPCM login response mismatch for userid %s", nas.get("userid"))
            self.error(260, "The password provided is incorrect.")
            return
        userid = nas.get("userid", "")
        gsbrcd = nas.get("gsbrcd", "")
        if self.db.is_banned(userid, nas.get("macadr", "")):
            self.error(256, "Login failed.")
            return
        profile = self.db.get_or_create_profile(userid, gsbrcd, nas.get("macadr", ""),
                                                nas.get("ingamesn", ""))
        self.profileid = profile["profileid"]
        self.gameid = gsbrcd[:4]
        self.sesskey = str(int.from_bytes(random_str(4).encode(), "little") % 1000000000)
        old = self.server.sessions.get(self.profileid)
        if old is not None and old is not self:
            old.profileid = None
            try:
                old.writer.close()
            except Exception:
                pass
        self.server.sessions[self.profileid] = self
        proof = login_proof(self.challenge, nas["challenge"], msg.get("challenge", ""),
                            authtoken)
        self.send(gs_build(
            "lc", "2",
            ("sesskey", self.sesskey), ("proof", proof), ("userid", userid),
            ("profileid", self.profileid), ("uniquenick", profile["uniquenick"]),
            ("lt", gs_b64encode(random_str(16).encode())), ("id", msg.get("id", "1"))))
        log.info("Profile %d logged in (userid %s, game %s, %s)", self.profileid, userid,
                 gsbrcd, self.ip)
        for m in self.db.pop_messages(self.profileid):
            self.send(m["msg"])
        self.send_pending_requests()
        self.pull_friend_status()
        self.broadcast_status()

    def cmd_logout(self, msg):
        try:
            self.writer.close()
        except Exception:
            pass

    def cmd_ka(self, msg):
        self.send(gs_build("ka", ""))

    # --------------------------------------------------------------- profile
    def cmd_getprofile(self, msg):
        try:
            target = int(msg.get("profileid", "0"))
        except ValueError:
            return
        profile = self.db.get_profile(target)
        if profile is None:
            return
        pairs = [("profileid", profile["profileid"]), ("nick", profile["uniquenick"]),
                 ("userid", profile["userid"]), ("email", profile["email"]),
                 ("sig", random_str(32, "0123456789abcdef")),
                 ("uniquenick", profile["uniquenick"]), ("pid", "11")]
        if profile["firstname"]:
            pairs.append(("firstname", profile["firstname"]))
        if profile["lastname"]:
            pairs.append(("lastname", profile["lastname"]))
        pairs += [("lon", "0.000000"), ("lat", "0.000000"), ("loc", ""),
                  ("id", msg.get("id", "1"))]
        self.send(gs_build("pi", "", *pairs))

    def cmd_updatepro(self, msg):
        if self.profileid is None:
            return
        for key, value in msg.items():
            self.db.update_profile(self.profileid, key, value)

    def cmd_updateui(self, msg):
        pass

    # --------------------------------------------------------------- presence
    def status_message(self) -> str:
        if self.status == "0" and self.statstring == "Offline":
            return "|s|0|ss|Offline"
        return "|s|%s|ss|%s|ls|%s|ip|%d|p|0|qm|0" % (
            self.status, self.statstring, self.locstring, ip_to_int_le(self.ip))

    def cmd_status(self, msg):
        if self.profileid is None:
            return
        self.status = msg.get("__cmd_val__", "0")
        self.statstring = msg.get("statstring", "")
        self.locstring = msg.get("locstring", "")
        self.send_pending_requests()
        self.send_new_authorizations()
        self.broadcast_status()

    def broadcast_status(self, only=None):
        """Send our status to every mutual friend who is online."""
        if self.profileid is None and only is None:
            return
        pid = self.profileid
        if pid is None:
            return
        msg = gs_build("bm", "100", ("f", pid), ("msg", self.status_message()))
        targets = [only] if only is not None else \
            [b["buddy"] for b in self.db.buddy_list(pid)]
        for target in targets:
            peer = self.server.sessions.get(target)
            if peer is not None and self.db.has_buddy(target, pid):
                peer.send(msg)

    def pull_friend_status(self, only=None):
        """Ask for the status of our friends (sent to us)."""
        buddies = self.db.buddy_list(self.profileid)
        for b in buddies:
            if only is not None and b["buddy"] != only:
                continue
            if b["status"] != 1:
                continue
            peer = self.server.sessions.get(b["buddy"])
            text = peer.status_message() if peer is not None else "|s|0|ss|Offline"
            self.send(gs_build("bm", "100", ("f", b["buddy"]), ("msg", text)))

    # --------------------------------------------------------------- buddies
    def send_buddy_request(self, target_session, from_pid, when=None):
        sig = random_str(32, "0123456789abcdef")
        target_session.send(gs_build(
            "bm", "2", ("f", from_pid), ("date", when or int(time.time())),
            ("msg", "\r\n\r\n|signed|" + sig)))

    def send_pending_requests(self):
        for req in self.db.pending_requests_for(self.profileid):
            # Requests from someone we already have as a friend are mutual:
            # authorize automatically (that's how DS friend codes work).
            if self.db.has_buddy(self.profileid, req["owner"]):
                self.db.auth_buddy(req["owner"], self.profileid)
                self.db.auth_buddy(self.profileid, req["owner"])
                self.send_bm4(req["owner"])
                continue
            self.send_buddy_request(self, req["owner"], req["time"])

    def send_new_authorizations(self):
        for b in self.db.unnotified_auths(self.profileid):
            self.send(gs_build("bm", "1", ("f", b["buddy"]),
                               ("msg", "I have authorized your request to add me to your list")))

    def send_bm4(self, other):
        self.send(gs_build("bm", "4", ("f", other), ("date", int(time.time())), ("msg", "")))

    def cmd_addbuddy(self, msg):
        if self.profileid is None:
            return
        try:
            new = int(msg.get("newprofileid", "0"))
        except ValueError:
            return
        if new == self.profileid or new <= 0:
            return
        self.db.add_buddy(self.profileid, new)
        peer = self.server.sessions.get(new)
        if self.db.has_buddy(new, self.profileid):
            # They registered us too -> mutual friendship.
            self.db.auth_buddy(self.profileid, new)
            self.db.auth_buddy(new, self.profileid)
            self.send_bm4(new)
            if peer is not None:
                peer.send_bm4(self.profileid)
                peer.broadcast_status(only=self.profileid)
            self.broadcast_status(only=new)
            self.pull_friend_status(only=new)
        elif peer is not None:
            self.send_buddy_request(peer, self.profileid)
        log.info("Profile %d added %d as a friend", self.profileid, new)

    def cmd_authadd(self, msg):
        if self.profileid is None:
            return
        try:
            src = int(msg.get("fromprofileid", "0"))
        except ValueError:
            return
        self.db.auth_buddy(src, self.profileid)
        self.send_bm4(src)
        peer = self.server.sessions.get(src)
        if peer is not None:
            peer.send_new_authorizations()
            self.broadcast_status(only=src)
            peer.broadcast_status(only=self.profileid)

    def cmd_delbuddy(self, msg):
        if self.profileid is None:
            return
        try:
            self.db.del_buddy(self.profileid, int(msg.get("delprofileid", "0")))
        except ValueError:
            pass

    def cmd_bm(self, msg):
        if self.profileid is None:
            return
        kind = msg.get("__cmd_val__", "")
        if kind not in ("1", "5", "102", "103"):
            return
        try:
            dest = int(msg.get("t", "0"))
        except ValueError:
            return
        out = gs_build("bm", "1", ("f", self.profileid), ("msg", msg.get("msg", "")))
        peer = self.server.sessions.get(dest)
        if peer is not None:
            peer.send(out)
        elif kind == "1":
            self.db.save_message(self.profileid, dest, out)
        else:
            self.error(2307, "The buddy to send a message to is offline.", fatal=False)


async def start(db, bind, port):
    server = GPCMServer(db)
    srv = await asyncio.start_server(server.handle, bind, port)
    log.info("GPCM listening on %s:%d", bind, port)
    return server, srv
