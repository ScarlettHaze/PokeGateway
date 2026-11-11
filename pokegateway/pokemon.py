"""Binary structures used by the Gen 4/5 online services."""

import struct
import time

GTS_RECORD_SIZE_4 = 0x124   # 292
GTS_RECORD_SIZE_5 = 0x128   # 296
MAX_SPECIES = {4: 493, 5: 649}


def encode_name(gen: int, text: str, length_chars: int) -> bytes:
    """Encode a trainer/Pokemon name, 0xFFFF terminated and padded."""
    codes = []
    for ch in text[: length_chars - 1]:
        if gen == 5:
            codes.append(ord(ch))
        elif "0" <= ch <= "9":
            codes.append(0x0121 + ord(ch) - ord("0"))
        elif "A" <= ch <= "Z":
            codes.append(0x012B + ord(ch) - ord("A"))
        elif "a" <= ch <= "z":
            codes.append(0x0145 + ord(ch) - ord("a"))
        elif ch == "-":
            codes.append(0x01BE)
        else:
            codes.append(0x01DE)    # space
    codes.append(0xFFFF)
    codes += [0xFFFF] * (length_chars - len(codes))
    return struct.pack("<%dH" % length_chars, *codes)


def pack_timestamp(t) -> int:
    if t is None:
        return 0
    return (t.tm_year << 48) | (t.tm_mon << 40) | (t.tm_mday << 32) | \
        (t.tm_hour << 24) | (t.tm_min << 16) | (t.tm_sec << 8)


def levels_ok(min_level, max_level, level):
    if max_level == 0:
        max_level = 255
    return min_level <= level <= max_level


class GtsRecord:
    """A deposited GTS Pokemon (encrypted party Pokemon + metadata).

    Gen 4 (0x124 bytes) and Gen 5 (0x128 bytes) share the first 0x10C bytes:
      0x000  0xEC  encrypted Pokemon
      0x0EC  u16   species          0x0EE u8 gender     0x0EF u8 level
      0x0F0  u16   wanted species   0x0F2 u8 wanted gender
      0x0F3  u8    wanted min lvl   0x0F4 u8 wanted max lvl
      0x0F6  u8    trainer gender
      0x0F8  u64   time deposited   0x100 u64 time exchanged
      0x108  s32   pid
    Gen 4: 0x10C name, 0x11C OT, 0x11E country, 0x11F region, 0x120 class,
           0x121 is_exchanged, 0x122 version, 0x123 language
    Gen 5: 0x10C OT, 0x110 name, 0x120 country, 0x121 region, 0x122 class,
           0x123 is_exchanged, 0x124 version, 0x125 language, 0x126 badges,
           0x127 unity tower
    """

    def __init__(self, gen: int, raw: bytes):
        size = GTS_RECORD_SIZE_4 if gen == 4 else GTS_RECORD_SIZE_5
        if len(raw) != size:
            raise ValueError("bad GTS record size %d" % len(raw))
        self.gen = gen
        self.buf = bytearray(raw)
        self._country_off = 0x11E if gen == 4 else 0x120
        self._exch_off = 0x121 if gen == 4 else 0x123

    @property
    def raw(self) -> bytes:
        return bytes(self.buf)

    def _u16(self, off):
        return struct.unpack_from("<H", self.buf, off)[0]

    def _set_u16(self, off, v):
        struct.pack_into("<H", self.buf, off, v)

    species = property(lambda s: s._u16(0xEC), lambda s, v: s._set_u16(0xEC, v))
    gender = property(lambda s: s.buf[0xEE], lambda s, v: s.buf.__setitem__(0xEE, v))
    level = property(lambda s: s.buf[0xEF], lambda s, v: s.buf.__setitem__(0xEF, v))
    req_species = property(lambda s: s._u16(0xF0), lambda s, v: s._set_u16(0xF0, v))
    req_gender = property(lambda s: s.buf[0xF2], lambda s, v: s.buf.__setitem__(0xF2, v))
    req_min = property(lambda s: s.buf[0xF3], lambda s, v: s.buf.__setitem__(0xF3, v))
    req_max = property(lambda s: s.buf[0xF4], lambda s, v: s.buf.__setitem__(0xF4, v))

    @property
    def pid(self):
        return struct.unpack_from("<i", self.buf, 0x108)[0]

    @pid.setter
    def pid(self, v):
        struct.pack_into("<i", self.buf, 0x108, v)

    @property
    def country(self):
        return self.buf[self._country_off]

    @property
    def is_exchanged(self):
        return self.buf[self._exch_off]

    @is_exchanged.setter
    def is_exchanged(self, v):
        self.buf[self._exch_off] = v

    @property
    def time_deposited(self):
        return struct.unpack_from("<Q", self.buf, 0xF8)[0]

    @time_deposited.setter
    def time_deposited(self, t):
        struct.pack_into("<Q", self.buf, 0xF8, pack_timestamp(t))

    @property
    def time_exchanged(self):
        return struct.unpack_from("<Q", self.buf, 0x100)[0]

    @time_exchanged.setter
    def time_exchanged(self, t):
        struct.pack_into("<Q", self.buf, 0x100, pack_timestamp(t))

    def valid(self) -> bool:
        max_species = MAX_SPECIES[self.gen]
        if not 1 <= self.species <= max_species:
            return False
        if not 1 <= self.req_species <= max_species:
            return False
        if not 1 <= self.level <= 100:
            return False
        if self.req_min > 100 or self.req_max > 100:
            return False
        if self.gender not in (0, 1, 2, 3) or self.req_gender not in (0, 1, 2, 3):
            return False
        return True

    def can_trade(self, other: "GtsRecord") -> bool:
        """Can `self` (offered) be traded for `other` (listed)?"""
        if self.is_exchanged or other.is_exchanged:
            return False
        if self.species != other.req_species:
            return False
        if other.req_gender in (1, 2) and self.gender != other.req_gender:
            return False
        if not levels_ok(other.req_min, other.req_max, self.level):
            return False
        if other.species != self.req_species:
            return False
        if self.req_gender in (1, 2) and other.gender != self.req_gender:
            return False
        return levels_ok(self.req_min, self.req_max, other.level)

    def flag_traded(self, listed: "GtsRecord"):
        """Turn the offered Pokemon into the record delivered to the person
        who listed `listed`: keep our Pokemon data but take over the listing
        metadata and mark it exchanged."""
        self.species = listed.species
        self.gender = listed.gender
        self.level = listed.level
        self.req_species = listed.req_species
        self.req_gender = listed.req_gender
        self.req_min = listed.req_min
        self.req_max = listed.req_max
        self.buf[0xF8:0x100] = listed.buf[0xF8:0x100]
        self.time_exchanged = time.gmtime()
        self.pid = listed.pid
        self.is_exchanged = 1


# ---------------------------------------------------------------------------
# Battle Tower (Gen 4) / Battle Subway (Gen 5) filler opponents
# ---------------------------------------------------------------------------

# species, held item, moves, ability, nickname
_FILLER_TEAMS = [
    [(25, 270, (85, 98, 231, 86), 9, "PIKACHU"),        # Static, Life Orb
     (143, 234, (34, 156, 89, 242), 47, "SNORLAX"),     # Thick Fat, Leftovers
     (376, 275, (309, 89, 418, 153), 29, "METAGROSS")], # Clear Body, Focus Sash
    [(445, 270, (337, 89, 242, 424), 8, "GARCHOMP"),
     (448, 275, (396, 406, 245, 370), 80, "LUCARIO"),
     (130, 234, (127, 349, 89, 444), 22, "GYARADOS")],
]

_TRAINER_NAMES = ["Ace", "Riley", "Cynthia", "Volkner", "Flint", "Lucian", "Bertha"]


def _tower_pokemon(gen, species, item, moves, ability, nickname, ot):
    out = struct.pack("<HH4HIII", species, item, *moves, ot, 0x12345678 ^ species,
                      0x3FFFFFFF)                     # 31 IVs everywhere
    out += bytes([252, 252, 6, 0, 0, 0])              # EVs
    out += bytes([0, 2, ability, 255])                # PP ups, language, ability, friendship
    out += encode_name(gen, nickname, 11)
    if gen == 5:
        out += b"\x00" * 4
    return out


def _phrase(gen):
    return struct.pack("<4H", 5, 0, 0, 0) if gen == 4 else struct.pack("<4H", 0, 20, 0, 0)


def _tower_profile(gen, name, ot):
    version = 12 if gen == 4 else 20   # Platinum / White
    return (encode_name(gen, name, 8) + bytes([version, 2, 0, 0]) +
            struct.pack("<I", ot) + _phrase(gen) + bytes([0, 0]))


def fake_tower_record(gen, rank, index):
    team = _FILLER_TEAMS[(rank + index) % len(_FILLER_TEAMS)]
    ot = 10000 + rank * 100 + index
    body = b"".join(_tower_pokemon(gen, *mon, ot) for mon in team)
    body += _tower_profile(gen, _TRAINER_NAMES[index % len(_TRAINER_NAMES)], ot)
    body += _phrase(gen) * 3 + b"\x00\x00"
    return body


def fake_tower_leader(gen):
    return _tower_profile(gen, "-----", 0)
