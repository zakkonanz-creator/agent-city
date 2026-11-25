"""How often a job is meant to run, from whatever describes its schedule.

Everything comes back as ``(runs_per_day, label)``. ``runs_per_day`` is None for
jobs with no clock (always-on services, things you run by hand); the scanner uses
it to decide when a quiet job has missed its runs.

Accepted forms:
    "*/15 * * * *"         a cron expression (5 fields)
    "every 30m" / "every 2h" / "every 1d" / "every 90s"
    "hourly" "daily" "weekly" "monthly"
    "always-on"            a resident service (no runs to miss; check it with a health URL)
    "manual"               run by hand
plus launchd plists (StartInterval / StartCalendarInterval) via ``from_plist``.
"""
import plistlib
import re

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
           "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
           "@annually": "0 0 1 1 *", "hourly": "0 * * * *", "daily": "0 0 * * *",
           "weekly": "0 0 * * 0", "monthly": "0 0 1 * *"}
RESIDENT = ("always-on", "always on", "resident", "service", "@reboot", "daemon")
MANUAL = ("manual", "by hand", "on demand", "on-demand", "adhoc", "ad hoc")

_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def _field(spec, lo, hi, names=None):
    """Expand one cron field into the set of values it matches."""
    out = set()
    for part in spec.lower().split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", "?"):
            a, b = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            a, b = _num(a, names), _num(b, names)
        else:
            a = _num(part, names)
            b = hi if step > 1 else a
        out.update(range(a, b + 1, step))
    if hi == 7:                         # cron allows 7 for Sunday
        out = {0 if v == 7 else v for v in out}
    return out


def _num(s, names):
    s = s.strip()
    if names and s[:3] in names:
        return names[s[:3]]
    return int(s)


def cron_runs_per_day(expr):
    f = expr.split()
    if len(f) != 5:
        raise ValueError("cron needs 5 fields: %r" % expr)
    mins = _field(f[0], 0, 59)
    hours = _field(f[1], 0, 23)
    dom, dow = f[2], f[4]
    days = 1.0
    if dom not in ("*", "?") and dow not in ("*", "?"):
        # cron ORs the two day fields when both are restricted
        days = min(1.0, len(_field(dom, 1, 31)) / 30.4 + len(_field(dow, 0, 7, _DOW)) / 7.0)
    elif dom not in ("*", "?"):
        days = len(_field(dom, 1, 31)) / 30.4
    elif dow not in ("*", "?"):
        days = len(_field(dow, 0, 7, _DOW)) / 7.0
    months = 1.0 if f[3] in ("*", "?") else len(_field(f[3], 1, 12, _MON)) / 12.0
    return len(mins) * len(hours) * days * months


def describe_cron(expr):
    f = expr.split()
    if f[0].startswith("*/") and f[1:] == ["*"] * 4:
        return "every %s min" % f[0][2:]
    if f[0] == "*" and f[1:] == ["*"] * 4:
        return "every minute"
    if f[1].startswith("*/") and f[2:] == ["*"] * 3 and f[0].isdigit():
        return "every %s h" % f[1][2:]
    if f[1] == "*" and f[2:] == ["*"] * 3 and f[0].isdigit():
        return "hourly at :%02d" % int(f[0])
    if f[0].isdigit() and f[1].isdigit() and f[2:4] == ["*", "*"]:
        t = "%02d:%02d" % (int(f[1]), int(f[0]))
        if f[4] == "*":
            return "daily %s" % t
        if f[4] in ("1-5", "mon-fri"):
            return "weekdays %s" % t
        return "%s (dow %s)" % (t, f[4])
    return "cron %s" % expr


def parse(text):
    """``(runs_per_day, label, kind)`` where kind is cron / every / resident / manual."""
    if text is None:
        return None, None, None
    s = str(text).strip()
    low = s.lower()
    if not s:
        return None, None, None
    if any(low.startswith(r) for r in RESIDENT):
        return None, s, "resident"
    if any(low.startswith(m) for m in MANUAL):
        return None, s, "manual"
    if low in ALIASES:
        expr = ALIASES[low]
        return cron_runs_per_day(expr), low, "cron"
    m = re.match(r"^every\s+(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)$", low)
    if m:
        n, unit = float(m.group(1)), m.group(2)[0]
        secs = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return 86400.0 / secs, "every %s%s" % (m.group(1), unit), "every"
    if len(s.split()) == 5:
        try:
            return cron_runs_per_day(s), describe_cron(s), "cron"
        except (ValueError, KeyError):
            pass
    return None, s, None


def from_plist(path):
    """``(runs_per_day, label)`` from a launchd plist, or (None, None)."""
    try:
        with open(path, "rb") as fh:
            p = plistlib.load(fh)
    except Exception:
        return None, None
    if p.get("StartInterval"):
        n = int(p["StartInterval"])
        return 86400.0 / n, ("every %d min" % (n // 60) if n >= 60 else "every %ds" % n)
    cal = p.get("StartCalendarInterval")
    if cal:
        items = cal if isinstance(cal, list) else [cal]
        per_day = 0.0
        for it in items:
            r = 1.0
            if "Minute" not in it:
                r *= 60
            if "Hour" not in it:
                r *= 24
            if "Weekday" in it:
                r /= 7.0
            if "Day" in it:
                r /= 30.4
            if "Month" in it:
                r /= 12.0
            per_day += r
        if len(items) == 1 and "Hour" in items[0]:
            it = items[0]
            return per_day, "%s%02d:%02d" % ("weekly " if "Weekday" in it else "daily ",
                                             it["Hour"], it.get("Minute", 0))
        return per_day, "%d launchd slots" % len(items)
    if p.get("KeepAlive"):
        return None, "always-on (KeepAlive)"
    if p.get("RunAtLoad"):
        return None, "at login"
    return None, None


def expected_gap_hours(runs_per_day):
    """How long a quiet spell can last before it counts as missed runs."""
    if not runs_per_day:
        return None
    gap = 24.0 / runs_per_day
    # two missed runs, plus slack for a machine that slept through one
    return max(gap * 2 + 1, gap + 3)
