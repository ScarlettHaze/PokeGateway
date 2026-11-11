"""Small helpers shared by the different servers."""

import base64
import secrets
import string
import urllib.parse

ALNUM = string.ascii_letters + string.digits


def random_str(length: int, alphabet: str = ALNUM) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --------------------------------------------------------------------------
# NAS (nas.nintendowifi.net/ac) encoding.
#
# Request and response values are base64 with a few characters swapped
# ('=' -> '*', and in requests also '+' -> '.'/'>' and '/' -> '-'/'?').
# --------------------------------------------------------------------------

def nas_decode_value(value: str) -> str:
    v = urllib.parse.unquote(value)
    v = v.replace("*", "=").replace("?", "/").replace(">", "+")
    v = v.replace(".", "+").replace("-", "/")
    try:
        return base64.b64decode(v).decode("latin-1")
    except Exception:
        return urllib.parse.unquote(value)


def nas_decode_form(body: bytes) -> dict:
    out = {}
    for part in body.decode("latin-1").strip().split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        out[key] = nas_decode_value(value)
    return out


def nas_encode_form(values: dict) -> bytes:
    parts = []
    for key, value in values.items():
        enc = base64.b64encode(str(value).encode("latin-1")).decode("ascii")
        parts.append("%s=%s" % (key, enc.replace("=", "*")))
    return ("&".join(parts) + "\r\n").encode("ascii")


# --------------------------------------------------------------------------
# GameSpy "\key\value\key\value\final\" messages.
# --------------------------------------------------------------------------

FINAL = "\\final\\"


def gs_parse(buffer: str):
    """Split a buffer into complete messages.

    Returns (messages, remainder) where each message is an ordered dict with
    "__cmd__" (first key) and "__cmd_val__" (its value).
    """
    messages = []
    while FINAL in buffer:
        raw, buffer = buffer.split(FINAL, 1)
        if not raw.startswith("\\"):
            continue
        parts = raw[1:].split("\\")
        if len(parts) % 2:
            parts.append("")
        msg = {}
        for i in range(0, len(parts), 2):
            key, value = parts[i], parts[i + 1]
            if i == 0:
                msg["__cmd__"] = key
                msg["__cmd_val__"] = value
            else:
                msg[key] = value
        messages.append(msg)
    return messages, buffer


def gs_build(cmd: str, cmd_val="", *pairs) -> bytes:
    """gs_build("lc", "2", ("sesskey", 1), ("id", 1))"""
    out = "\\%s\\%s" % (cmd, cmd_val)
    for key, value in pairs:
        out += "\\%s\\%s" % (key, value)
    out += FINAL
    return out.encode("latin-1")


def gs_b64encode(data: bytes) -> str:
    """GameSpy flavoured base64: +/= become []_."""
    return (base64.b64encode(data).decode("ascii")
            .replace("+", "[").replace("/", "]").replace("=", "_"))


def ip_to_int_le(ip: str) -> int:
    """IPv4 dotted string -> signed 32-bit int as the DS reports it
    (network byte order bytes read as little endian)."""
    b = bytes(int(x) for x in ip.split("."))
    return int.from_bytes(b, "little", signed=True)


def int_le_to_ip(value: int) -> str:
    b = (int(value) & 0xFFFFFFFF).to_bytes(4, "little")
    return ".".join(str(x) for x in b)


def friend_code(profile_id: int, game_id: str) -> int:
    """Compute the 12-digit friend code shown in-game for a profile ID.

    The upper bits are a CRC-8 (poly 0x07) over the little endian profile id
    followed by the 4-character game code."""
    data = profile_id.to_bytes(4, "little") + game_id.encode("ascii")
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return ((crc & 0x7F) << 32) | profile_id


def format_friend_code(fc: int) -> str:
    s = "%012d" % fc
    return "%s-%s-%s" % (s[0:4], s[4:8], s[8:12])
