"""Configuration loading (INI file + sane defaults)."""

import configparser
import logging
import socket
from dataclasses import dataclass, field

log = logging.getLogger("config")

# Domains whose A records are answered with our own IP. Matching is by
# suffix, so "nintendowifi.net" also covers "nas.nintendowifi.net",
# "gpcm.gs.nintendowifi.net", "pokemondpds.master.gs.nintendowifi.net", ...
DEFAULT_REDIRECT_DOMAINS = [
    "nintendowifi.net",
    "nintendowifi.com",
    "gamespy.com",
    "gamespy.net",
    "pkgdsprod.nintendo.co.jp",
    "pkvldtprod.nintendo.co.jp",
    # Gen 5 Game Sync / Global Link (en-ds.pokemon-gl.com etc.). Kept local
    # so the game gets a clean error instead of reaching a third party.
    "pokemon-gl.com",
]

# GameSpy game name -> secret key. Needed by the QR server (heartbeat
# challenge) and the server browser (list encryption). Keys can be added in
# the [gamekeys] section of the config file.
#
#  pokemondpds   : Diamond/Pearl and HeartGold/SoulSilver friend matchmaking
#  pokemonplatds : Platinum (and the Wi-Fi Plaza in Pt/HGSS)
#  syachi2ds     : Black/White and Black 2/White 2 (both call
#                  DWC_InitFriendsMatch with product id 0x2FC6)
DEFAULT_GAME_KEYS = {
    "pokemondpds": "1vTlwb",
    "pokemonplatds": "IIup73",
    "syachi2ds": "tXH2sN",
}


@dataclass
class Config:
    public_ip: str = "auto"
    bind: str = "0.0.0.0"
    db_path: str = "data/pokegateway.db"
    log_level: str = "INFO"

    dns_enabled: bool = True
    dns_port: int = 53
    dns_upstream: str = "1.1.1.1"
    dns_upstream_port: int = 53
    dns_ttl: int = 300
    redirect_domains: list = field(default_factory=lambda: list(DEFAULT_REDIRECT_DOMAINS))

    http_port: int = 80

    gpcm_port: int = 29900
    gpsp_port: int = 29901
    qr_port: int = 27900
    natneg_port: int = 27901
    sb_port: int = 28910

    gt4_port: int = 12400
    gt5_port: int = 12411  # plain port behind the SSL proxy listening on 12401

    dls1_dir: str = "dlc"

    game_keys: dict = field(default_factory=lambda: dict(DEFAULT_GAME_KEYS))

    def resolved_public_ip(self) -> str:
        if self.public_ip and self.public_ip != "auto":
            return self.public_ip
        return detect_local_ip()


def detect_local_ip() -> str:
    """Return the LAN address of this machine (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def load_config(path=None) -> Config:
    cfg = Config()
    if not path:
        return cfg
    parser = configparser.ConfigParser()
    if not parser.read(path):
        log.warning("Config file %s not found, using defaults", path)
        return cfg

    def get(section, key, conv=str, attr=None):
        if parser.has_option(section, key):
            raw = parser.get(section, key)
            if conv is bool:
                value = parser.getboolean(section, key)
            else:
                value = conv(raw)
            setattr(cfg, attr or key, value)

    get("server", "public_ip")
    get("server", "bind")
    get("server", "db_path")
    get("server", "log_level")

    get("dns", "enabled", bool, "dns_enabled")
    get("dns", "port", int, "dns_port")
    get("dns", "upstream", str, "dns_upstream")
    get("dns", "upstream_port", int, "dns_upstream_port")
    get("dns", "ttl", int, "dns_ttl")
    if parser.has_option("dns", "extra_domains"):
        extra = [d.strip().lower().strip(".") for d in
                 parser.get("dns", "extra_domains").split(",") if d.strip()]
        cfg.redirect_domains.extend(extra)

    get("http", "port", int, "http_port")

    get("gamespy", "gpcm_port", int)
    get("gamespy", "gpsp_port", int)
    get("gamespy", "qr_port", int)
    get("gamespy", "natneg_port", int)
    get("gamespy", "sb_port", int)

    get("global_terminal", "gen4_port", int, "gt4_port")
    get("global_terminal", "gen5_port", int, "gt5_port")

    get("dls1", "dir", str, "dls1_dir")

    if parser.has_section("gamekeys"):
        for name, key in parser.items("gamekeys"):
            cfg.game_keys[name] = key.strip()

    return cfg
