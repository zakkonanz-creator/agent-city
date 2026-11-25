"""Load agents.toml.

Python 3.11+ has ``tomllib``; older Pythons (the stock macOS one is 3.9) fall back
to ``tomli`` if it is installed, and otherwise to the small parser below. It covers
the subset Agent City's config uses: ``[table]``, ``[[array.of.tables]]``, dotted
table names, ``key = value`` with strings, numbers, booleans and (multi-line) arrays,
and ``#`` comments. Anything fancier, install ``tomli`` or use Python 3.11+.
"""
import json
import os
import re

try:                                   # 3.11+
    import tomllib as _toml            # type: ignore
except ImportError:                    # pragma: no cover - depends on the interpreter
    try:
        import tomli as _toml          # type: ignore
    except ImportError:
        _toml = None


class ConfigError(Exception):
    pass


def load(path):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise ConfigError("no config at %s. Start one with: python3 scan.py --init   (or try the demo: python3 build.py --demo --open)" % path)
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    with open(path, "rb") as fh:
        raw = fh.read()
    if _toml is not None:
        try:
            return _toml.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ConfigError("%s: %s" % (path, e))
    return parse(raw.decode("utf-8"), path)


# ---------------------------------------------------------------------------
# minimal TOML subset parser
# ---------------------------------------------------------------------------

_KEY = r'(?:[A-Za-z0-9_\-]+|"[^"]*")'
_TABLE_RX = re.compile(r'^\[\s*(%s(?:\s*\.\s*%s)*)\s*\]$' % (_KEY, _KEY))
_AOT_RX = re.compile(r'^\[\[\s*(%s(?:\s*\.\s*%s)*)\s*\]\]$' % (_KEY, _KEY))
_KV_RX = re.compile(r'^(%s(?:\s*\.\s*%s)*)\s*=\s*(.*)$' % (_KEY, _KEY))


def _keys(s):
    return [k.strip().strip('"') for k in re.findall(_KEY, s)]


def _strip_comment(line):
    """Drop a # comment that is not inside a string."""
    out, q = [], None
    i = 0
    while i < len(line):
        c = line[i]
        if q:
            out.append(c)
            if c == "\\" and q == '"' and i + 1 < len(line):
                out.append(line[i + 1]); i += 2; continue
            if c == q:
                q = None
        elif c in "\"'":
            q = c; out.append(c)
        elif c == "#":
            break
        else:
            out.append(c)
        i += 1
    return "".join(out).rstrip()


def _brackets_open(s):
    depth, q, i = 0, None, 0
    while i < len(s):
        c = s[i]
        if q:
            if c == "\\" and q == '"':
                i += 2; continue
            if c == q:
                q = None
        elif c in "\"'":
            q = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        i += 1
    return depth > 0


def _value(s, where):
    s = s.strip()
    if not s:
        raise ConfigError("%s: missing value" % where)
    if s.startswith('"'):
        if not s.endswith('"') or len(s) < 2:
            raise ConfigError("%s: unterminated string" % where)
        return json.loads(s)           # basic strings share JSON's escapes
    if s.startswith("'"):
        if not s.endswith("'") or len(s) < 2:
            raise ConfigError("%s: unterminated string" % where)
        return s[1:-1]
    if s in ("true", "false"):
        return s == "true"
    if s.startswith("["):
        if not s.endswith("]"):
            raise ConfigError("%s: unterminated array" % where)
        return [_value(p, where) for p in _split_items(s[1:-1])]
    if s.startswith("{"):
        if not s.endswith("}"):
            raise ConfigError("%s: unterminated inline table" % where)
        out = {}
        for p in _split_items(s[1:-1]):
            m = _KV_RX.match(p.strip())
            if not m:
                raise ConfigError("%s: bad inline table entry %r" % (where, p))
            out[_keys(m.group(1))[-1]] = _value(m.group(2), where)
        return out
    try:
        return int(s.replace("_", ""))
    except ValueError:
        pass
    try:
        return float(s.replace("_", ""))
    except ValueError:
        raise ConfigError("%s: cannot read value %r" % (where, s))


def _split_items(s):
    items, cur, depth, q, i = [], [], 0, None, 0
    while i < len(s):
        c = s[i]
        if q:
            cur.append(c)
            if c == "\\" and q == '"' and i + 1 < len(s):
                cur.append(s[i + 1]); i += 2; continue
            if c == q:
                q = None
        elif c in "\"'":
            q = c; cur.append(c)
        elif c in "[{":
            depth += 1; cur.append(c)
        elif c in "]}":
            depth -= 1; cur.append(c)
        elif c == "," and depth == 0:
            items.append("".join(cur)); cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        items.append("".join(cur))
    return [x for x in items if x.strip()]


def parse(text, name="config"):
    root, cur = {}, None
    cur = root
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        n = i + 1
        line = _strip_comment(lines[i]).strip()
        i += 1
        if not line:
            continue
        where = "%s line %d" % (name, n)
        m = _AOT_RX.match(line)
        if m:
            ks = _keys(m.group(1))
            t = root
            for k in ks[:-1]:
                t = t.setdefault(k, {})
                if isinstance(t, list):
                    t = t[-1]
            arr = t.setdefault(ks[-1], [])
            if not isinstance(arr, list):
                raise ConfigError("%s: %s is not an array of tables" % (where, ks[-1]))
            arr.append({})
            cur = arr[-1]
            continue
        m = _TABLE_RX.match(line)
        if m:
            t = root
            for k in _keys(m.group(1)):
                t = t.setdefault(k, {})
                if isinstance(t, list):
                    t = t[-1]
            cur = t
            continue
        m = _KV_RX.match(line)
        if not m:
            raise ConfigError("%s: cannot read %r" % (where, line))
        val = m.group(2)
        while _brackets_open(val) and i < len(lines):     # multi-line array
            val += " " + _strip_comment(lines[i]).strip()
            i += 1
        ks = _keys(m.group(1))
        t = cur
        for k in ks[:-1]:
            t = t.setdefault(k, {})
        t[ks[-1]] = _value(val, where)
    return root
