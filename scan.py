#!/usr/bin/env python3
"""Read every agent in agents.toml off the machine and write the city snapshot.

    python3 scan.py                       # agents.toml -> data/city.json (+ snapshots/)
    python3 scan.py --config other.toml
    python3 scan.py --no-sessions         # skip the Yards (Claude Code sessions, projects)
    python3 scan.py --redact-report       # list what redaction stripped
    python3 scan.py --quiet

Nothing about a job's state lives in the config. The config says what each agent
is and where to look; whether it is installed, when it last ran, and whether that
run went wrong are read at scan time. The scan never writes outside this folder.
"""
import argparse
import datetime as dt
import glob
import json
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config as config_mod  # noqa: E402
import probes  # noqa: E402
import redact  # noqa: E402
import schedule as sched  # noqa: E402

SNAP = os.path.join(HERE, "data", "city.json")
SNAPSHOTS = os.path.join(HERE, "snapshots")

TIER_NAMES = {1: "shed", 2: "block", 3: "offices", 4: "landmark", 5: "civic monument"}
# use in the last 7 days -> architecture tier (footprint, floors, cornices, spire)
USE_STEPS = [(300, 5), (60, 4), (10, 3), (1, 2), (0, 1)]
DEFAULT_ERRORS = (r"Traceback \(most recent call last\)|\b(ERROR|FATAL|CRITICAL)\b|"
                  r"\bUnhandled\b|\bpanic:|\bfailed with exit code [1-9]|"
                  r"\b(401|403) (Unauthorized|Forbidden)\b|credit balance is too low")
TS_RX = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")
DISTRICT_TINTS = ["#2a3350", "#2c2a4d", "#1f3a34", "#3a2a3e", "#3a3524", "#243545",
                  "#2b3a3a", "#3e3026", "#2d3346", "#332a40"]
SLOTS = [(i, j) for j in range(4) for i in range(3)]   # 12 plots per district, 3 x 4
SLOTS.sort(key=lambda s: (s[0] + s[1], s[1]))         # back of the block first


def now_ts():
    return time.time()


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def norm_path(p):
    p = str(p).strip()
    if p.startswith("store:"):
        return p
    return os.path.normpath(os.path.expanduser(p)) + ("/" if p.endswith("/") else "")


def paths_meet(a, b):
    """True when a written path and a read path refer to the same thing: equal, or
    one is a folder (ends in /) that contains the other. A folder that is a bare
    top-level name would match half the disk, so it must be at least two deep or
    be named exactly."""
    a, b = norm_path(a), norm_path(b)
    if a.rstrip("/") == b.rstrip("/"):
        return True
    for d, f in ((a, b), (b, a)):
        if d.endswith("/") and f.startswith(d) and d.strip("/").count("/") >= 1:
            return True
    return False


def looks_like_path(s):
    s = str(s)
    return ("/" in s or re.search(r"\.[a-z0-9]{1,5}$", s, re.I)) and " " not in s.strip()


# ---------------------------------------------------------------------------
# one agent
# ---------------------------------------------------------------------------

def _untimed(line):
    """An error line without its leading timestamp (the page shows when it ran)."""
    return re.sub(r"^\[?\d{4}-\d{2}-\d{2}[T ][\d:.,]+(Z|[+-]\d{2}:?\d{2})?\]?\s*[-|:]?\s*", "", line)


def run_starts(tail, marker, since):
    """Count run markers with a timestamp after ``since`` in a log tail."""
    rx = re.compile(marker)
    n = 0
    for line in tail:
        if not rx.search(line):
            continue
        m = TS_RX.search(line)
        if not m:
            n += 1
            continue
        try:
            t = dt.datetime.strptime("%s %s:%s:%s" % (m.group(1), m.group(2), m.group(3),
                                                       m.group(4) or "00"),
                                     "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if t >= since:
            n += 1
    return n


def last_run_section(tail, marker):
    """The lines of the latest run, when the log marks where runs start."""
    if not marker:
        return tail
    rx = re.compile(marker)
    idx = [i for i, l in enumerate(tail) if rx.search(l)]
    return tail[idx[-1]:] if idx else tail


def probe_agent(spec, ctx):
    """Everything the machine says about one agent. Pure reads."""
    now = ctx["now"]
    host_name = spec.get("host", "local")
    remote = ctx["hosts"].get(host_name)
    kind = "cloud" if host_name == "cloud" else ("server" if remote is not None or
                                                host_name not in ("local", "laptop") else "local")
    a = {"installed": None, "loaded": None, "pid": None, "last_exit": None,
         "schedule": None, "schedule_source": None, "notes": [], "errors": [],
         "last_run": None, "log": None, "err": None, "health": None, "check": None,
         "kind_host": kind, "where": host_name if kind == "server" else None,
         "unreachable": False}

    runs_per_day, label, skind = sched.parse(spec.get("schedule"))

    # where it is installed ---------------------------------------------------
    if spec.get("launchd"):
        ld = probes.launchd(spec["launchd"])
        a.update(installed=ld["installed"], loaded=ld["loaded"], pid=ld["pid"],
                 last_exit=ld["last_exit"])
        if ld["plist"]:
            a["schedule_source"] = "~/Library/LaunchAgents/%s.plist" % spec["launchd"]
            if not spec.get("schedule"):
                runs_per_day, label = sched.from_plist(ld["plist"])
                skind = "resident" if (label or "").startswith("always-on") else "launchd"
    if spec.get("cron"):
        rows = remote["probe"].get("crontab", []) if remote else probes.crontab()
        if remote and not remote["probe"].get("ok"):
            a["unreachable"] = True
        else:
            cm = probes.cron_match(spec["cron"], rows)
            a["installed"] = cm["installed"] if a["installed"] is None else (a["installed"] or cm["installed"])
            if cm["installed"]:
                a["loaded"] = True
                a["schedule_source"] = "crontab" + (" on %s" % host_name if remote else "")
                if not spec.get("schedule"):
                    runs_per_day, label, skind = sched.parse(cm["schedule"])
    if spec.get("systemd"):
        sd = probes.systemd(spec["systemd"])
        if "error" not in sd:
            a.update(installed=sd["installed"], loaded=sd["loaded"])
            if sd.get("last_exit") is not None:
                a["last_exit"] = sd["last_exit"]
            if sd.get("result") not in (None, "", "success"):
                a["errors"].append("systemd result: %s" % sd["result"])
            a["schedule_source"] = "systemd --user %s" % spec["systemd"]
        else:
            a["notes"].append("systemctl unavailable: %s" % sd["error"])
    if spec.get("process"):
        procs = remote["probe"].get("ps", []) if remote else ctx["ps"]()
        running = any(spec["process"] in p for p in procs)
        a["loaded"] = running if a["loaded"] is None else (a["loaded"] and running)
        if not running:
            a["errors"].append("no running process matches %r" % spec["process"])

    a["runs_per_day"], a["schedule"], a["schedule_kind"] = runs_per_day, label, skind

    # what it left behind -----------------------------------------------------
    marker = spec.get("run_marker")
    err_rx = re.compile(spec.get("error_pattern") or DEFAULT_ERRORS)
    for key, field in (("log", "log"), ("error_log", "err")):
        p = spec.get(key)
        if not p:
            continue
        if remote:
            f = remote["probe"].get("logs", {}).get(p) if remote["probe"].get("ok") else None
            if not remote["probe"].get("ok"):
                a["unreachable"] = True
        else:
            f = probes.log_facts(p)
        if f:
            a[field] = f
    if a["log"]:
        a["last_run"] = a["log"]["mtime"]
        sect = last_run_section(a["log"].get("recent") or a["log"]["tail"], marker)[-200:]
        hits = [i for i, l in enumerate(sect) if err_rx.search(l)]
        if hits:
            line = sect[hits[-1]].strip()
            if "Traceback" in line:
                # the exception itself is the first unindented line after the traceback
                after = [l.strip() for l in sect[hits[-1] + 1:] if l.strip() and not l[:1].isspace()]
                line = after[0] if after else line
            a["errors"].append(_untimed(line)[:220])
    if a["err"] and a["err"]["size"] > 0:
        # an error log counts only when it moved during the latest run
        if a["last_run"] is None or a["err"]["mtime"] >= a["last_run"] - 300:
            # stderr also carries warnings and progress, so only error-shaped lines count
            hits = [l.strip() for l in a["err"]["tail"] if err_rx.search(l)]
            if hits:
                a["errors"].append(_untimed(hits[-1])[:220])
        a["last_run"] = max(a["last_run"] or 0, a["err"]["mtime"])

    # is it up -----------------------------------------------------------------
    if spec.get("health_url"):
        h = probes.health(spec["health_url"])
        a["health"] = h
        if h["ok"]:
            if skind in ("resident", None):
                a["last_run"] = max(a["last_run"] or 0, now)
        else:
            a["errors"].append("health check %s" % (("HTTP %s" % h["status"]) if h.get("status")
                                                      else h.get("error", "failed")))
    if spec.get("check"):
        c = probes.check(spec["check"], timeout=int(spec.get("check_timeout", 30)))
        a["check"] = c
        if not c["ok"]:
            a["errors"].append("check exited %s%s" % (c["exit"], (": " + c["note"]) if c["note"] else ""))
        elif skind in ("resident", None) and not a["log"]:
            a["last_run"] = max(a["last_run"] or 0, now)

    if a["last_exit"] not in (None, 0) and not spec.get("ignore_exit"):
        a["errors"].insert(0, "last exit code %s" % a["last_exit"])

    # how much it does in a week ----------------------------------------------
    a["use"], a["use_how"] = None, None
    if a["log"] and marker:
        n = run_starts(a["log"].get("recent") or a["log"]["tail"], marker, now - 7 * 86400)
        a["use"], a["use_how"] = n, "%d run%s counted in its log (last 7 days)" % (n, "" if n == 1 else "s")
    elif a["log"] and a["log"].get("series"):
        n = sum(1 for t in a["log"]["series"] if t >= now - 7 * 86400)
        a["use"], a["use_how"] = n, "%d dated log file%s in 7 days" % (n, "" if n == 1 else "s")
    elif runs_per_day and a["last_run"] and now - a["last_run"] < 7 * 86400:
        n = int(round(runs_per_day * 7))
        a["use"], a["use_how"] = n, "estimated from its schedule (%s)" % label
    if spec.get("use") is not None:
        a["use"], a["use_how"] = int(spec["use"]), "declared in agents.toml"
    return a


def state_of(spec, a, now):
    """One word for the building, and the reason, in priority order."""
    if spec.get("paused"):
        return "paused"
    until = spec.get("until")
    if until:
        try:
            if dt.date.fromisoformat(str(until)) < dt.date.fromtimestamp(now):
                return "dormant"
        except ValueError:
            pass
    if a["unreachable"]:
        return "unknown"
    if a["installed"] is False:
        return "scaffold"
    if spec.get("not_live"):
        return "scaffold"
    if a["schedule_kind"] == "manual":
        return "failing" if a["errors"] else "manual"
    if a["errors"]:
        return "failing"
    if a["last_run"] is None:
        if a["loaded"]:
            return "live" if a["schedule_kind"] == "resident" else "unknown"
        return "unknown"
    gap = sched.expected_gap_hours(a["runs_per_day"])
    hours = (now - a["last_run"]) / 3600
    if gap and hours > gap:
        return "ruin"
    if not gap and a["schedule_kind"] != "resident" and hours > float(spec.get("stale_after_hours", 24 * 14)):
        return "ruin"
    return "live"


# ---------------------------------------------------------------------------
# the whole city
# ---------------------------------------------------------------------------

def layout(agents, districts):
    """Seat each district's agents tallest-at-the-back, three districts to a row.
    A district with more than 12 agents spills into a second block."""
    out_d = []
    k = 0
    by_d = {}
    for a in agents:
        by_d.setdefault(a["district"], []).append(a)
    for d in districts:
        members = by_d.get(d["id"], [])
        members.sort(key=lambda a: (-a["floors"], -a["value_tier"], a["id"]))
        chunks = [members[i:i + len(SLOTS)] for i in range(0, max(1, len(members)), len(SLOTS))]
        for ci, chunk in enumerate(chunks):
            did = d["id"] if ci == 0 else "%s-%d" % (d["id"], ci + 1)
            col, row = k % 3, k // 3
            nd = {"id": did, "name": d["name"] + ("" if ci == 0 else " %s" % ("II", "III", "IV", "V")[min(ci - 1, 3)]),
                  "ox": col * 8, "oy": row * 9,
                  "tint": d.get("tint") or DISTRICT_TINTS[k % len(DISTRICT_TINTS)],
                  "blurb": d.get("blurb", "")}
            out_d.append(nd)
            for a, (i, j) in zip(chunk, SLOTS):
                a["district"], a["i"], a["j"] = did, i, j
            k += 1
    return out_d


def store_facts(st, now, stale_days):
    root = os.path.expanduser(st["path"])
    files = []
    skip = {".git", "node_modules", "__pycache__", ".venv", ".next", "dist", "build"}
    cap = int(st.get("max_files", 20000))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for f in filenames:
            if f.startswith("."):
                continue
            try:
                files.append(os.path.getmtime(os.path.join(dirpath, f)))
            except OSError:
                pass
            if len(files) >= cap:
                break
        if len(files) >= cap:
            break
    n = len(files)
    stale = sum(1 for m in files if now - m > stale_days * 86400)
    return {"files": n, "stale": stale, "capped": n >= cap,
            "rot": round(stale / n, 3) if n else 0.0,
            "newest_h": round((now - max(files)) / 3600, 1) if files else None,
            "oldest_d": int((now - min(files)) / 86400) if files else None,
            "exists": os.path.isdir(root)}


def scan(cfg, now=None, with_sessions=True, alive_pids=None, claude_home=None, quiet=False,
         snapshots_dir=None, base_dir=None):
    """Build the snapshot dict from a loaded config."""
    now = now or now_ts()
    base_dir = base_dir or HERE
    redact.add_patterns((cfg.get("redact") or {}).get("patterns"))
    city = cfg.get("city") or {}
    say = (lambda *x: None) if quiet else (lambda *x: print(*x, file=sys.stderr))

    # hosts reached over ssh: one call each, before any agent is looked at
    hosts = {}
    for name, h in (cfg.get("hosts") or {}).items():
        if not isinstance(h, dict) or not h.get("ssh"):
            continue
        logs = sorted({p for a in cfg.get("agent", []) if a.get("host") == name
                       for p in (a.get("log"), a.get("error_log")) if p})
        say("probing %s over ssh..." % name)
        hosts[name] = {"probe": probes.ssh_host(h["ssh"], logs), "label": h.get("label", name)}
        if not hosts[name]["probe"].get("ok"):
            say("  %s unreachable: %s" % (name, hosts[name]["probe"].get("error")))

    ps_cache = {}

    def ps():
        if "rows" not in ps_cache:
            code, out, _ = probes._run(["ps", "-eo", "args"])
            ps_cache["rows"] = out.splitlines() if code == 0 else []
        return ps_cache["rows"]

    ctx = {"now": now, "hosts": hosts, "ps": ps}
    districts_cfg = cfg.get("district") or []
    if not districts_cfg:
        districts_cfg = [{"id": "main", "name": city.get("name", "Agents")}]
    known_d = {d["id"] for d in districts_cfg}

    agents = []
    seen = set()
    for spec in cfg.get("agent", []):
        aid = spec.get("id")
        if not aid or aid in seen:
            raise config_mod.ConfigError("every [[agent]] needs a unique id (problem: %r)" % aid)
        seen.add(aid)
        d = spec.get("district") or districts_cfg[0]["id"]
        if d not in known_d:
            raise config_mod.ConfigError("agent %s: unknown district %r" % (aid, d))
        p = probe_agent(spec, ctx)
        state = state_of(spec, p, now)
        tier = int(spec.get("tier", 2))
        tier = max(1, min(5, tier))
        use_tier = next(t for lim, t in USE_STEPS if p["use"] >= lim) if p["use"] is not None else None
        writes = [str(w) for w in spec.get("writes", [])]
        # architecture: how much it does (use), how much it matters (value), or both
        size_by = city.get("size_by", "blend")
        if use_tier and size_by == "use":
            arch = use_tier
        elif use_tier and size_by == "blend":
            arch = int(math.ceil((use_tier + tier) / 2.0))
        else:
            arch = tier
        floors = max(2, min(9, 1 + 2 * arch - (1 if arch > 2 else 0)))
        hours_ago = round((now - p["last_run"]) / 3600, 2) if p["last_run"] else None
        log = p["log"] or {}
        err = p["err"] or {}
        tail = None
        if state == "failing":
            src = err.get("tail") if err.get("size") else log.get("tail")
            tail = redact.scrub("\n".join((src or [])[-40:])) or None
        readme = None
        if spec.get("readme"):
            rp = os.path.expanduser(spec["readme"])
            readme = redact.scrub(probes.read_head(rp, 3000)) or None
        notes = list(p["notes"])
        if p["health"]:
            h = p["health"]
            notes.append("health %s%s" % (("HTTP %s" % h["status"]) if h.get("status") else h.get("error", ""),
                                          (" in %d ms" % h["ms"]) if h.get("ms") is not None else ""))
        if p["check"] and p["check"]["note"]:
            notes.append("check: " + p["check"]["note"])
        sched_txt = p["schedule"] or ("always-on" if p["schedule_kind"] == "resident" else "not scheduled")
        if p["kind_host"] == "server" and hosts.get(spec.get("host")):
            sched_txt += " on " + hosts[spec["host"]]["label"]
        a = {
            "id": aid,
            "name": spec.get("name") or aid,
            "district": d,
            "purpose": spec.get("purpose", ""),
            "role": spec.get("role"),
            "writes": writes,
            "reads": [str(r) for r in spec.get("reads", [])],
            "host": p["kind_host"],
            "where": (hosts.get(spec.get("host"), {}).get("label") or spec.get("host"))
                     if p["kind_host"] == "server" else None,
            "installed": p["installed"],
            "loaded": p["loaded"],
            "pid": p["pid"],
            "last_exit": p["last_exit"],
            "state": state,
            "schedule": sched_txt,
            "schedule_source": spec.get("defined_in") or p["schedule_source"],
            "runs_per_day": round(p["runs_per_day"], 3) if p["runs_per_day"] else None,
            "last_run_iso": iso(p["last_run"]) if p["last_run"] else None,
            "hours_ago": hours_ago,
            "last_run_note": redact.scrub("; ".join(notes)) if notes else None,
            "log_path": redact.scrub(log.get("path")) if log else None,
            "log_size": log.get("size"),
            "err_log": redact.scrub(err.get("path")) if err else None,
            "err_size": err.get("size"),
            "run_errors": [redact.scrub(e) for e in p["errors"]][:4],
            "log_tail": tail,
            "readme": readme,
            "readme_path": redact.scrub(spec.get("readme")) if spec.get("readme") else None,
            "tier": arch,
            "value_tier": tier,
            "tier_name": TIER_NAMES[tier],
            "size_by": size_by,
            "use": p["use"],
            "use_tier": use_tier,
            "use_how": p["use_how"] or "not measurable here (give it a run_marker or a schedule)",
            "floors": floors,
            "sources": [str(s) for s in spec.get("talks_to", [])],
            "watch": spec.get("watched", "none"),
            "paused": ({"since": str(spec.get("paused_since", "")), "why": str(spec["paused"]),
                        "resume": str(spec.get("resume", ""))} if spec.get("paused") else None),
        }
        if a["state"] == "unknown" and p["unreachable"]:
            a["last_run_note"] = "%s did not answer over ssh" % spec.get("host")
        agents.append(a)

    districts = layout(agents, districts_cfg)

    # dependency roads: A writes something B reads -----------------------------
    edges = []
    for A in agents:
        for B in agents:
            if A is B:
                continue
            for w in A["writes"]:
                for r in B["reads"]:
                    if paths_meet(w, r):
                        edges.append({"from": A["id"], "to": B["id"], "path": w})
    feeds = {}
    for e in edges:
        feeds.setdefault(e["from"], set()).add(e["to"])
    for a in agents:
        a["feeds"] = sorted(feeds.get(a["id"], ()))
        a["fed_by"] = sorted({e["from"] for e in edges if e["to"] == a["id"]})

    # the brain: shared stores, drawn as clouds --------------------------------
    stale_days = int((cfg.get("brain") or {}).get("stale_days", 30))
    stores, links = [], []
    min_ox = min((d["ox"] for d in districts), default=0)
    for k, st in enumerate(cfg.get("store", []) or []):
        f = store_facts(st, now, int(st.get("stale_days", stale_days)))
        sid = st.get("id") or os.path.basename(os.path.normpath(st["path"]))
        s = dict(f, id=sid, label=st.get("label", sid), blurb=st.get("blurb", ""),
                 path=redact.scrub(st["path"]), color=st.get("color"),
                 gx=min_ox - 9.5, gy=-2.0 + k * 6.5)
        stores.append(s)
        spath = norm_path(st["path"]).rstrip("/") + "/"
        for a in agents:
            hits = [r for r in a["reads"] if r in ("store:" + sid, sid) or paths_meet(r, spath)]
            if hits:
                links.append({"store": sid, "agent": a["id"], "paths": len(hits)})
            wh = [w for w in a["writes"] if w in ("store:" + sid, sid) or paths_meet(w, spath)]
            pl = a.setdefault("plumbing", {"reads": 0, "writes": 0, "read_paths": [], "write_paths": []})
            pl["reads"] += len(hits)
            pl["writes"] += len(wh)
            pl["read_paths"] += [redact.scrub(h) for h in hits]
            pl["write_paths"] += [redact.scrub(w) for w in wh]
    for a in agents:
        a.setdefault("plumbing", {"reads": 0, "writes": 0, "read_paths": [], "write_paths": []})
    readers = {l["agent"] for l in links}
    writers = {a["id"] for a in agents if a["plumbing"]["writes"]}
    brain = {"stores": stores, "links": links, "stale_days": stale_days,
             "totals": {"files": sum(s["files"] for s in stores),
                        "stale": sum(s["stale"] for s in stores),
                        "readers": len(readers), "writers": len(writers),
                        "agents": len(agents),
                        "unplumbed": sum(1 for a in agents if a["id"] not in readers | writers)},
             "note": "Reads and writes are what agents.toml declares."}
    if not stores:
        brain["totals"] = {}

    # orphans: files written that nothing reads --------------------------------
    orphans = []
    for a in agents:
        for w in a["writes"]:
            if not looks_like_path(w):
                continue
            if any(paths_meet(w, r) for b in agents if b is not a for r in b["reads"]):
                continue
            orphans.append({"agent": a["id"], "path": w})

    # discovery: jobs running here that the config does not mention ------------
    unknown = discover(cfg, agents, hosts)

    findings = make_findings(agents, edges, orphans, unknown, brain, now)

    snap = {
        "generated": iso(now),
        "generated_local": dt.datetime.fromtimestamp(now).strftime("%d %b %H:%M"),
        "city_name": city.get("name", "Agent City"),
        "districts": districts,
        "agents": agents,
        "findings": findings,
        "unknown": unknown,
        "edges": edges,
        "orphans": orphans,
        "brain": brain,
        "counts": counts_of(agents),
    }

    if with_sessions and (cfg.get("yards") or {}).get("enabled", True):
        import sessions
        prev = previous_snapshot(snapshots_dir or SNAPSHOTS)
        snap.update(sessions.build(cfg.get("yards") or {}, districts, now=now,
                                   alive_pids=alive_pids, claude_home=claude_home,
                                   prev_sites=(prev or {}).get("sites"), base_dir=base_dir))
    snap["changed"] = changes(previous_snapshot(snapshots_dir or SNAPSHOTS), snap)
    return snap


def discover(cfg, agents, hosts):
    dcfg = cfg.get("discover") or {}
    out = []
    claimed_cron = [a for a in cfg.get("agent", []) if a.get("cron")]
    if dcfg.get("cron", True):
        rows = probes.crontab()
        for sch, cmd in rows:
            if any(a["cron"] in cmd for a in claimed_cron if a.get("host", "local") in ("local", "laptop")):
                continue
            if any(x in cmd for x in dcfg.get("ignore", [])):
                continue
            out.append({"where": "crontab", "what": redact.scrub(cmd[:160]), "schedule": sch})
        for name, h in hosts.items():
            if not h["probe"].get("ok"):
                continue
            for sch, cmd in h["probe"].get("crontab", []):
                if any(a["cron"] in cmd for a in claimed_cron if a.get("host") == name):
                    continue
                if any(x in cmd for x in dcfg.get("ignore", [])):
                    continue
                out.append({"where": "crontab on " + h["label"], "what": redact.scrub(cmd[:160]),
                            "schedule": sch})
    prefixes = dcfg.get("launchd_prefixes") or []
    if prefixes:
        claimed = {a.get("launchd") for a in cfg.get("agent", [])}
        for lab in probes.launchd_labels(prefixes):
            if lab not in claimed and not any(x in lab for x in dcfg.get("ignore", [])):
                out.append({"where": "launchd", "what": lab, "schedule": None})
    return out


def make_findings(agents, edges, orphans, unknown, brain, now):
    f = []
    by_feeds = {}
    for e in edges:
        by_feeds.setdefault(e["from"], set()).add(e["to"])
    for a in agents:
        down = len(by_feeds.get(a["id"], ()))
        if a["state"] == "failing":
            why = (a["run_errors"] or ["it reported a problem"])[0]
            f.append({"sev": "crit" if (a["value_tier"] >= 3 or down) else "warn",
                      "title": "%s is failing" % a["name"],
                      "evidence": why + ("  (it feeds %d other agent%s)" % (down, "" if down == 1 else "s")
                                         if down else ""),
                      "agent": a["id"]})
            if a["last_exit"] == 0 and a["run_errors"]:
                f.append({"sev": "warn", "title": "%s exits 0 but logged an error" % a["name"],
                          "evidence": "A scheduler that only checks exit codes would call this healthy.",
                          "agent": a["id"]})
        elif a["state"] == "ruin":
            f.append({"sev": "crit" if a["value_tier"] >= 4 or down else "warn",
                      "title": "%s has gone quiet" % a["name"],
                      "evidence": "last ran %s, scheduled %s" % (_ago(a["hours_ago"]), a["schedule"]),
                      "agent": a["id"]})
        elif a["state"] == "scaffold" and a["value_tier"] >= 3:
            f.append({"sev": "warn", "title": "%s was built and never switched on" % a["name"],
                      "evidence": "value tier %d, but nothing on this machine runs it" % a["value_tier"],
                      "agent": a["id"]})
        elif a["state"] == "unknown":
            f.append({"sev": "info", "title": "Cannot tell whether %s runs" % a["name"],
                      "evidence": a.get("last_run_note") or "give it a log, a health_url or a check",
                      "agent": a["id"]})
        if a["state"] in ("live", "failing") and a["watch"] == "none" and a["value_tier"] >= 4:
            f.append({"sev": "warn", "title": "Nobody watches %s" % a["name"],
                      "evidence": "value tier %d and nothing would tell you if it stopped" % a["value_tier"],
                      "agent": a["id"]})
    if unknown:
        f.append({"sev": "warn", "title": "%d job%s running that %s not on the map"
                  % (len(unknown), "" if len(unknown) == 1 else "s", "is" if len(unknown) == 1 else "are"),
                  "evidence": "; ".join("%s: %s" % (u["where"], u["what"][:70]) for u in unknown[:5])
                              + (" and %d more" % (len(unknown) - 5) if len(unknown) > 5 else ""),
                  "agent": None})
    for o in orphans[:12]:
        f.append({"sev": "info", "title": "Written, never read: %s" % o["path"],
                  "evidence": "%s writes it every run and no agent reads it (fine if a person does)" % o["agent"],
                  "agent": o["agent"]})
    for s in brain.get("stores", []):
        if not any(l["store"] == s["id"] for l in brain["links"]):
            f.append({"sev": "info", "title": "Nothing reads %s" % s["label"],
                      "evidence": "%d files, %d%% untouched for %d days" % (s["files"], round(s["rot"] * 100),
                                                                          brain["stale_days"]),
                      "agent": None})
    order = {"crit": 0, "warn": 1, "info": 2}
    f.sort(key=lambda x: order[x["sev"]])
    return f


def _ago(h):
    if h is None:
        return "never"
    if h < 48:
        return "%dh ago" % round(h)
    return "%dd ago" % round(h / 24)


def counts_of(agents):
    c = {"total": len(agents)}
    for k in ("live", "failing", "scaffold", "unknown", "paused", "manual"):
        c[k] = sum(1 for a in agents if a["state"] == k)
    c["dark"] = sum(1 for a in agents if a["state"] in ("ruin", "dormant"))
    c["cloud"] = sum(1 for a in agents if a["host"] == "cloud")
    c["server"] = sum(1 for a in agents if a["host"] == "server")
    return c


def previous_snapshot(folder):
    files = sorted(glob.glob(os.path.join(folder, "city-*.json")))
    if not files:
        return None
    try:
        with open(files[-1], encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def changes(prev, snap):
    if not prev:
        return []
    since = (prev.get("generated") or "")[:10]
    old = {a["id"]: a for a in prev.get("agents", [])}
    out = []
    for a in snap["agents"]:
        o = old.get(a["id"])
        if not o:
            out.append({"agent": a["id"], "what": "new", "detail": "first seen", "since": since})
            continue
        for k in ("state", "last_exit", "schedule", "host"):
            if o.get(k) != a.get(k):
                out.append({"agent": a["id"], "what": k,
                            "detail": "%s → %s" % (o.get(k), a.get(k)), "since": since})
    for aid in old:
        if aid not in {a["id"] for a in snap["agents"]}:
            out.append({"agent": aid, "what": "removed", "detail": "no longer in agents.toml",
                        "since": since})
    return out


def write(snap, out=SNAP, snapshots_dir=SNAPSHOTS):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    os.makedirs(snapshots_dir, exist_ok=True)
    blob = json.dumps(snap, indent=1, ensure_ascii=False)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(blob)
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    with open(os.path.join(snapshots_dir, "city-%s.json" % stamp), "w", encoding="utf-8") as fh:
        fh.write(blob)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.path.join(HERE, "agents.toml"))
    ap.add_argument("--out", default=SNAP)
    ap.add_argument("--no-sessions", action="store_true", help="skip the Yards")
    ap.add_argument("--redact-report", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--init", action="store_true",
                    help="write a starter agents.toml from this machine's cron/launchd/systemd jobs")
    args = ap.parse_args(argv)
    if args.init:
        import init_config
        n = init_config.write(args.config)
        print("wrote %s with %d job%s found on this machine." % (os.path.relpath(args.config),
                                                                 n, "" if n == 1 else "s"))
        print("Add a purpose (and reads/writes) to each, then: python3 scan.py && python3 build.py --open")
        return
    try:
        cfg = config_mod.load(args.config)
        snap = scan(cfg, with_sessions=not args.no_sessions, quiet=args.quiet)
    except config_mod.ConfigError as e:
        sys.exit("agents.toml: %s" % e)
    write(snap, args.out)
    c = snap["counts"]
    if not args.quiet:
        print("%d agents: %d live, %d failing, %d gone quiet, %d never installed, %d unknown"
              % (c["total"], c["live"], c["failing"], c["dark"], c["scaffold"], c["unknown"]))
        print("%d dependency roads, %d findings (%d need a decision)"
              % (len(snap["edges"]), len(snap["findings"]),
                 sum(1 for f in snap["findings"] if f["sev"] == "crit")))
        if snap.get("sites") is not None:
            print("Yards: %d projects, %d open terminals" % (len(snap.get("sites", [])),
                                                             (snap.get("terminal_counts") or {}).get("live", 0)))
        print("wrote %s -> now run: python3 build.py" % os.path.relpath(args.out, os.getcwd()))
    if args.redact_report:
        for label, frag in redact.report():
            print("  redacted %-14s %s" % (label, frag))
        if not redact.report():
            print("  nothing redacted")


if __name__ == "__main__":
    main()
