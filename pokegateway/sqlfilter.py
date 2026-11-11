"""Tiny, safe evaluator for GameSpy server-browser filter strings such as

    dwc_mver = 90 and dwc_pid != 123 and maxplayers = 2 and numplayers < 2
    and dwc_mtype = 0 and dwc_mresv != dwc_pid and (dwc_pid = 5 or dwc_pid = 7)

Identifiers resolve to the server's reported keys. Unknown identifiers are
treated as empty strings / 0. Nothing is ever passed to eval().
"""

import re

_TOKEN = re.compile(r"""
    \s*(?:
        (?P<num>-?\d+(?:\.\d+)?)
      | '(?P<sq>[^']*)'
      | "(?P<dq>[^"]*)"
      | (?P<op><>|!=|>=|<=|==|=|<|>|&|\||\(|\))
      | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
    )""", re.VERBOSE)


class FilterError(ValueError):
    pass


def tokenize(text):
    pos = 0
    out = []
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise FilterError("bad filter near %r" % text[pos:pos + 10])
        pos = m.end()
        if m.group("num") is not None:
            out.append(("num", m.group("num")))
        elif m.group("sq") is not None:
            out.append(("str", m.group("sq")))
        elif m.group("dq") is not None:
            out.append(("str", m.group("dq")))
        elif m.group("op") is not None:
            out.append(("op", m.group("op")))
        else:
            word = m.group("id")
            if word.lower() in ("and", "or", "not", "like"):
                out.append(("kw", word.lower()))
            else:
                out.append(("id", word))
    return out


class _Parser:
    def __init__(self, tokens, server):
        self.t = tokens
        self.i = 0
        self.s = server

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        if not self.t:
            return True
        v = self.or_expr()
        if self.i != len(self.t):
            raise FilterError("trailing tokens")
        return bool(v)

    def or_expr(self):
        v = self.and_expr()
        while self.peek() == ("kw", "or"):
            self.take()
            r = self.and_expr()
            v = bool(v) or bool(r)
        return v

    def and_expr(self):
        v = self.not_expr()
        while self.peek() == ("kw", "and"):
            self.take()
            r = self.not_expr()
            v = bool(v) and bool(r)
        return v

    def not_expr(self):
        if self.peek() == ("kw", "not"):
            self.take()
            return not self.not_expr()
        return self.cmp_expr()

    def cmp_expr(self):
        left = self.bit_expr()
        kind, val = self.peek()
        if kind == "op" and val in ("=", "==", "!=", "<>", "<", ">", "<=", ">="):
            self.take()
            right = self.bit_expr()
            return _compare(left, val, right)
        if (kind, val) == ("kw", "like"):
            self.take()
            right = self.bit_expr()
            pattern = "^" + re.escape(str(right)).replace("%", ".*").replace("_", ".") + "$"
            return re.match(pattern, str(left), re.I) is not None
        return left

    def bit_expr(self):
        v = self.atom()
        while self.peek() in (("op", "&"), ("op", "|")):
            _, op = self.take()
            r = self.atom()
            a, b = _to_int(v), _to_int(r)
            v = (a & b) if op == "&" else (a | b)
        return v

    def atom(self):
        kind, val = self.take()
        if kind == "op" and val == "(":
            v = self.or_expr()
            if self.take() != ("op", ")"):
                raise FilterError("missing )")
            return v
        if kind == "num":
            return float(val) if "." in val else int(val)
        if kind == "str":
            return val
        if kind == "id":
            return self.s.get(val, self.s.get(val.lower(), ""))
        raise FilterError("unexpected token %r" % (val,))


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _compare(a, op, b):
    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    na, nb = num(a), num(b)
    if na is not None and nb is not None:
        a, b = na, nb
    else:
        a, b = str(a), str(b)
    if op in ("=", "=="):
        return a == b
    if op in ("!=", "<>"):
        return a != b
    try:
        return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
    except TypeError:
        return False


def matches(filter_text: str, server: dict) -> bool:
    return _Parser(tokenize(filter_text), server).parse()
