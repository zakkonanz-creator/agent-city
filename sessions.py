#!/usr/bin/env python3
"""The Yards: work in progress, laid out south of the city on three streets.

A plot is one piece of unfinished work, from any of:
  * a project folder you list in [yards] projects (globs allowed)
  * a plan Claude Code wrote to ~/.claude/plans/
  * an open Claude Code terminal (~/.claude/sessions + its transcript)

Each plot lands on a street, so the ground answers one question:
  finish  - a terminal is open on it, or its code moved in the last 14 days
  decide  - idle 2 to 8 weeks
  let go  - code idle past 8 weeks, or a plan with nothing built after 4

    python3 sessions.py                           # list terminals + plots
    python3 sessions.py --street let_go           # what is on one street, with ids
    python3 sessions.py --let-go plan:foo.md --why "superseded"

--let-go records the decision in data/let-go.md and the scanner stops drawing that
plot. It deletes nothing, and it never closes a terminal: it prints the command.

Everything here reads your own Claude Code files and stays on this machine unless
you publish the built page. List anything private in [yards] exclude.
"""
import argparse
import collections
import datetime as dt
import glob
import json
import math
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LET_GO = os.path.join(HERE, "data", "let-go.md")
TAIL_BYTES = 400_000
CODE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".sh", ".rb", ".go", ".rs",
            ".java", ".kt", ".swift", ".c", ".cpp", ".cs", ".php", ".html", ".css", ".sql")
SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", "__pycache__", ".venv",
             "venv", "target", ".cache"}
CHECK_RX = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\[([ xX])\]", re.M)
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
GATE_TOOLS = ("AskUserQuestion", "ExitPlanMode")
STREETS = [("finish", "Finish", "a terminal is open on it, or its code moved in the last two weeks"),
           ("decide", "Decide", "idle two to eight weeks — finish it or let it go"),
           ("let_go", "Let go", "idle past eight weeks, or a plan with nothing built after four")]
# Default per-million-token rates for the sunk-cost estimate. Set your own in
# [yards.rates]; the page always labels the result an estimate.
DEFAULT_RATES = {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}


# ---------------------------------------------------------------- helpers
def _ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read_tail(path, n=TAIL_BYTES):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > n:
                fh.seek(size - n)
                fh.readline()
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def slug_for(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def live_claude_pids():
    out = set()
    try:
        r = subprocess.run(["ps", "-eo", "pid,comm"], capture_output=True, text=True, timeout=10)
    except Exception:
        return out
    for line in r.stdout.splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip().endswith("claude"):
            try:
                out.add(int(parts[0]))
            except ValueError:
                pass
    return out


def excluded(name, exclude):
    n = (name or "").strip().lower()
    return any(n == e.lower() or n.replace("-", " ") == e.lower().replace("-", " ") for e in exclude)


# ---------------------------------------------------------------- terminals
def transcript_facts(path):
    """Title, last prompt, mode, last activity and turn count from the tail."""
    f = {"title": None, "last_prompt": None, "mode": None, "permission_mode": None,
         "last_activity": None, "turns": 0, "waiting_on_you": None}
    last_role = None
    for line in _read_tail(path).splitlines():
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t = r.get("type")
        if t == "ai-title" and r.get("aiTitle"):
            f["title"] = r["aiTitle"]
        elif t == "last-prompt" and r.get("lastPrompt"):
            f["last_prompt"] = r["lastPrompt"]
        elif t == "mode":
            f["mode"] = r.get("mode")
        elif t == "permission-mode":
            f["permission_mode"] = r.get("permissionMode")
        elif t in ("user", "assistant"):
            last_role = t
            f["turns"] += 1
            if r.get("timestamp"):
                f["last_activity"] = r["timestamp"]
    f["waiting_on_you"] = last_role == "assistant"
    return f


def session_detail(path, rates):
    """What a session did, streamed from the whole transcript (a substring test
    skips most lines before any JSON parsing, so big transcripts stay cheap)."""
    d = {"first_prompt": None, "files": [], "tool_calls": 0, "plan": None, "phase": None,
         "top_tools": [], "span_h": None, "bash_calls": 0, "files_built": 0, "tokens": {},
         "cost_usd": 0.0}
    if not os.path.exists(path):
        return d
    tools, tok = collections.Counter(), collections.Counter()
    files, first_ts, last_ts, last_gate = [], None, None, False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.search(r'"timestamp":"([^"]+)"', line)
            if m:
                first_ts = first_ts or m.group(1)
                last_ts = m.group(1)
            if d["first_prompt"] is None and '"type":"user"' in line.replace(" ", ""):
                try:
                    c = json.loads(line).get("message", {}).get("content")
                    if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                        d["first_prompt"] = c.strip()[:260]
                except ValueError:
                    pass
            if '"usage"' in line:
                try:
                    u = json.loads(line).get("message", {}).get("usage") or {}
                    tok["in"] += u.get("input_tokens", 0) or 0
                    tok["out"] += u.get("output_tokens", 0) or 0
                    tok["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
                    tok["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
                except ValueError:
                    pass
            if '"tool_use"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            gate = False
            for b in (r.get("message", {}).get("content") or []):
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                n = b.get("name") or "?"
                tools[n] += 1
                d["tool_calls"] += 1
                inp = b.get("input") or {}
                if n in WRITE_TOOLS and inp.get("file_path"):
                    files.append(inp["file_path"])
                    if "/.claude/plans/" in inp["file_path"]:
                        d["plan"] = os.path.basename(inp["file_path"])
                if n in GATE_TOOLS:
                    gate = True
            last_gate = gate
    home = os.path.expanduser("~")
    seen, out = set(), []
    for p in files:
        if p not in seen:
            seen.add(p)
            out.append(p.replace(home, "~"))
    real = [p for p in out if "/.claude/plans/" not in p]
    d["files"], d["files_built"] = out, len(real)
    d["top_tools"] = [n for n, _ in tools.most_common(5)]
    d["bash_calls"] = tools.get("Bash", 0)
    d["tokens"] = dict(tok)
    d["cost_usd"] = round((tok["in"] * rates["input"] + tok["out"] * rates["output"]
                           + tok["cache_read"] * rates["cache_read"]
                           + tok["cache_write"] * rates["cache_write"]) / 1e6, 2)
    a, b = _ts(first_ts), _ts(last_ts)
    if a and b:
        d["span_h"] = round((b - a) / 3600, 1)
    # a lot of real work goes through shell heredocs that Write/Edit never sees
    if last_gate:
        d["phase"] = "waiting on a decision"
    elif real:
        d["phase"] = "built"
    elif tools.get("Bash", 0) >= 25:
        d["phase"] = "worked via shell"
    elif d["plan"]:
        d["phase"] = "planned, nothing built"
    elif d["tool_calls"]:
        d["phase"] = "investigated"
    else:
        d["phase"] = "just talked"
    return d


def status_of(r):
    if not r["alive"]:
        return "closed"
    if r["idle_h"] > 24 * 5:
        return "abandoned"
    if r["idle_h"] > 24:
        return "stale"
    if r["idle_h"] > 2:
        return "parked"
    return "waiting on you" if r.get("waiting_on_you") else "active"


def terminals(claude_home, exclude, rates, now, alive_pids=None):
    sess_dir = os.path.join(claude_home, "sessions")
    proj_dir = os.path.join(claude_home, "projects")
    if not os.path.isdir(sess_dir):
        return []
    alive = live_claude_pids() if alive_pids is None else set(alive_pids)
    rows = []
    for fn in sorted(os.listdir(sess_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(sess_dir, fn), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        pid, cwd, sid = d.get("pid"), d.get("cwd") or "", d.get("sessionId") or ""
        if not (pid and sid):
            continue
        project = os.path.basename(cwd.rstrip("/")) or cwd
        if excluded(project, exclude):
            continue
        tpath = os.path.join(proj_dir, slug_for(cwd), sid + ".jsonl")
        facts = transcript_facts(tpath) if os.path.exists(tpath) else {}
        detail = session_detail(tpath, rates)
        mt = _ts(facts.get("last_activity"))
        if mt is None:
            mt = os.path.getmtime(tpath) if os.path.exists(tpath) else os.path.getmtime(
                os.path.join(sess_dir, fn))
        r = {"pid": pid, "alive": pid in alive, "project": project, "cwd": cwd.replace(
                 os.path.expanduser("~"), "~"),
             "session": sid, "name": d.get("name"),
             "started": d.get("procStart") or d.get("startedAt"),
             "size_mb": round(os.path.getsize(tpath) / 1e6, 1) if os.path.exists(tpath) else 0,
             "idle_h": round((now - mt) / 3600, 1),
             "title": facts.get("title"), "last_prompt": (facts.get("last_prompt") or "")[:260] or None,
             "mode": facts.get("mode"), "permission_mode": facts.get("permission_mode"),
             "waiting_on_you": facts.get("waiting_on_you"), "turns": facts.get("turns", 0)}
        r.update({k: detail[k] for k in ("first_prompt", "files_built", "tool_calls", "plan",
                                         "phase", "span_h", "top_tools", "bash_calls",
                                         "tokens", "cost_usd")})
        r["files"] = detail["files"][:10]
        r["status"] = status_of(r)
        r["nudge"] = bool(r["alive"] and r["waiting_on_you"] and 2 <= r["idle_h"] <= 24)
        rows.append(r)
    rows.sort(key=lambda r: (not r["alive"], r["idle_h"]))
    return rows


# ---------------------------------------------------------------- plots
def _walk(root, cap=4000):
    """(code file count, newest code mtime, any mtime)."""
    n, newest, any_m = 0, None, None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            try:
                m = os.path.getmtime(os.path.join(dirpath, f))
            except OSError:
                continue
            any_m = max(any_m or 0, m)
            if f.endswith(CODE_EXT):
                n += 1
                newest = max(newest or 0, m)
        if n >= cap:
            break
    return n, newest, any_m


def _checklist(paths):
    done = total = 0
    where = []
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                marks = CHECK_RX.findall(fh.read())
        except OSError:
            continue
        if marks:
            done += sum(1 for m in marks if m in "xX")
            total += len(marks)
            where.append(os.path.basename(p))
    return done, total, where


def progress_of(site, files):
    """(0-1, receipt): a checklist with 3+ items wins; otherwise a stage estimate."""
    done, total, where = _checklist(files)
    if total >= 3:
        return round(done / total, 3), "%d of %d checklist items done (%s)" % (done, total, ", ".join(where))
    code = site.get("code_files") or 0
    if code:
        p = min(0.6, 0.1 + 0.5 * math.log10(1 + code) / math.log10(61))
        return round(p, 3), "%d code files, no checklist (stage estimate)" % code
    return 0.05, "a plan, nothing built yet (stage estimate)"


def street_of(s, cfg):
    idle = s.get("idle_days") or 0
    if s.get("state") == "open":
        return "finish"
    if (s.get("code_files") or 0) > 0:
        if idle < cfg["finish_days"]:
            return "finish"
        return "let_go" if idle > cfg["decide_days"] else "decide"
    return "let_go" if idle > cfg["plan_days"] else "decide"


def let_go_ids():
    if not os.path.exists(LET_GO):
        return set()
    with open(LET_GO, encoding="utf-8") as fh:
        return set(re.findall(r"<!-- id:(.+?) -->", fh.read()))


def from_projects(patterns, exclude, now):
    out = []
    for pat in patterns or []:
        for d in sorted(glob.glob(os.path.expanduser(pat))):
            if not os.path.isdir(d) or excluded(os.path.basename(d), exclude):
                continue
            n, newest, any_m = _walk(d)
            ref = newest or any_m or os.path.getmtime(d)
            docs = [os.path.join(d, f) for f in os.listdir(d)
                    if re.match(r"(?i).*(plan|readme|todo|checklist|handoff|roadmap).*\.md$", f)]
            out.append({"id": "project:" + os.path.basename(d), "name": os.path.basename(d),
                        "source": "projects", "session": None,
                        "path": d.replace(os.path.expanduser("~"), "~"),
                        "idle_days": round((now - ref) / 86400, 1), "cost_usd": 0.0, "turns": 0,
                        "code_files": n, "has_plan": bool(docs), "plan": None,
                        "evidence": ("%d code files, last touched %s" % (n, _days(now - ref)))
                                    if n else "no code yet",
                        "_docs": docs, "_dir": d})
    return out


def from_plans(claude_home, exclude, now, seen_plans):
    pdir = os.path.join(claude_home, "plans")
    out = []
    if not os.path.isdir(pdir):
        return out
    for p in sorted(glob.glob(os.path.join(pdir, "*.md"))):
        name = os.path.basename(p)
        if name in seen_plans:
            continue
        title = name[:-3].replace("-", " ")
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
        except OSError:
            pass
        if excluded(title, exclude):
            continue
        m = os.path.getmtime(p)
        out.append({"id": "plan:" + name, "name": title[:80], "source": "plan", "session": None,
                    "path": None, "idle_days": round((now - m) / 86400, 1), "cost_usd": 0.0,
                    "turns": 0, "code_files": 0, "has_plan": True, "plan": name,
                    "evidence": "a written plan, nothing built", "_docs": [p]})
    return out


def _days(secs):
    d = secs / 86400
    return "today" if d < 1 else ("%dd ago" % round(d))


def layout(sites, districts):
    """Three streets, one under the other, under the city."""
    cols = 7
    y0 = max((d["oy"] for d in districts), default=0) + 12
    x0 = max(0, (max((d["ox"] for d in districts), default=0) + 6) // 2 - cols)
    y, streets = y0, []
    for key, label, blurb in STREETS:
        rows = [s for s in sites if s["street"] == key]
        rows.sort(key=lambda s: (s["state"] != "open", -(s["cost_usd"] or 0),
                                 -(s["code_files"] or 0), -(s["idle_days"] or 0)))
        n_rows = max(1, math.ceil(len(rows) / cols))
        for i, s in enumerate(rows):
            s["gx"] = x0 + (i % cols) * 2
            s["gy"] = y + (i // cols) * 2
        streets.append({"key": key, "label": label, "blurb": blurb, "y": y - 1,
                        "rows": n_rows, "count": len(rows)})
        y += n_rows * 2 + 1
    return {"x": x0 - 1, "y": y0 - 1, "w": cols * 2, "h": y - 1 - y0, "cols": cols,
            "streets": streets}


def build(ycfg, districts, now=None, alive_pids=None, claude_home=None, prev_sites=None,
          base_dir=HERE):
    """The Yards part of the snapshot: terminals, sites, site_block, counts."""
    now = now or time.time()
    cfg = {"finish_days": int(ycfg.get("finish_days", 14)),
           "decide_days": int(ycfg.get("decide_days", 56)),
           "plan_days": int(ycfg.get("plan_days", 28))}
    exclude = [str(x) for x in ycfg.get("exclude", [])]
    rates = dict(DEFAULT_RATES, **(ycfg.get("rates") or {}))
    home = os.path.expanduser(claude_home or ycfg.get("claude_home") or "~/.claude")

    terms = terminals(home, exclude, rates, now, alive_pids) if ycfg.get("claude_sessions", True) else []
    sites = from_projects(ycfg.get("projects"), exclude, now)
    if ycfg.get("plans", True):
        sites += from_plans(home, exclude, now, set())

    # a live terminal working inside a listed project claims it: that plot is "open"
    live = [t for t in terms if t["alive"]]
    claimed = set()
    for s in sites:
        for t in live:
            if s.get("_dir") and os.path.expanduser(t["cwd"]).rstrip("/") == s["_dir"].rstrip("/"):
                s.update(session=t["session"], pid=t["pid"], state="open",
                         cost_usd=round(s["cost_usd"] + t["cost_usd"], 2), turns=t["turns"])
                claimed.add(t["session"])
            elif s["source"] == "plan" and t.get("plan") == s["plan"]:
                s.update(session=t["session"], pid=t["pid"], state="open",
                         code_files=t["files_built"], cost_usd=t["cost_usd"], turns=t["turns"],
                         evidence=t["phase"])
                claimed.add(t["session"])
    for t in live:
        if t["session"] in claimed:
            continue
        sites.append({"id": "terminal:%s" % t["pid"], "name": t["title"] or t["name"] or t["project"],
                      "source": "terminal", "session": t["session"], "path": t["cwd"],
                      "idle_days": round(t["idle_h"] / 24, 1), "cost_usd": t["cost_usd"],
                      "turns": t["turns"], "code_files": t["files_built"],
                      "has_plan": bool(t["plan"]), "plan": t["plan"],
                      "evidence": t["phase"] or "open terminal", "pid": t["pid"], "state": "open",
                      "_docs": [os.path.join(home, "plans", t["plan"])] if t.get("plan") else []})
    gone = let_go_ids()
    sites = [s for s in sites if s["id"] not in gone]

    prev = {s["id"]: s for s in (prev_sites or [])}
    for s in sites:
        if "state" not in s:
            s["state"] = ("stalled" if s["idle_days"] > 30 else "started") if s["code_files"] else "planned"
        s["street"] = street_of(s, cfg)
        p = prev.get(s["id"])
        s["let_go_streak"] = ((p or {}).get("let_go_streak", 0) + 1) if s["street"] == "let_go" else 0
        s["progress"], s["progress_receipt"] = progress_of(s, s.pop("_docs", []))
        s.pop("_dir", None)
        s.setdefault("pid", None)
    block = layout(sites, districts)
    streets = collections.Counter(s["street"] for s in sites)
    counts = {"total": len(sites),
              "open": sum(1 for s in sites if s["state"] == "open"),
              "started": sum(1 for s in sites if s["state"] == "started"),
              "stalled": sum(1 for s in sites if s["state"] == "stalled"),
              "planned": sum(1 for s in sites if s["state"] == "planned"),
              "sunk": round(sum(s["cost_usd"] or 0 for s in sites), 2),
              "streets": {k: streets.get(k, 0) for k, _, _ in STREETS},
              "let_go_twice": sum(1 for s in sites if s["let_go_streak"] >= 2)}
    if prev:
        new = [s for s in sites if s["id"] not in prev]
        counts["delta"] = {"then": len(prev), "now": len(sites), "new": len(new),
                           "since": (prev_sites[0].get("_since") if prev_sites else None) or "last scan"}
    for s in sites:
        s["_since"] = dt.date.fromtimestamp(now).isoformat()
    live_t = [t for t in terms if t["alive"]]
    tcounts = {"total": len(terms), "live": len(live_t),
               "active_today": sum(1 for t in live_t if t["idle_h"] <= 24),
               "stale": sum(1 for t in live_t if 24 < t["idle_h"] <= 120),
               "abandoned": sum(1 for t in live_t if t["idle_h"] > 120),
               "waiting": sum(1 for t in live_t if t.get("waiting_on_you")),
               "projects": len({t["project"] for t in live_t})}
    return {"terminals": live_t, "terminal_counts": tcounts, "sites": sites,
            "site_block": block if sites else None, "site_counts": counts}


def let_go(site_id, why):
    os.makedirs(os.path.dirname(LET_GO), exist_ok=True)
    new = not os.path.exists(LET_GO)
    with open(LET_GO, "a", encoding="utf-8") as fh:
        if new:
            fh.write("# Let go\n\nWork deliberately dropped. Remove a line to bring the plot back.\n\n")
        fh.write("- %s · `%s` · %s <!-- id:%s -->\n" % (dt.date.today().isoformat(), site_id,
                                                                 why or "no reason given", site_id))


def main(argv=None):
    sys.path.insert(0, HERE)
    import config as config_mod
    ap = argparse.ArgumentParser(description="The Yards: terminals and work in progress")
    ap.add_argument("--config", default=os.path.join(HERE, "agents.toml"))
    ap.add_argument("--street", choices=["finish", "decide", "let_go"])
    ap.add_argument("--let-go", dest="let_go")
    ap.add_argument("--why")
    args = ap.parse_args(argv)
    if args.let_go:
        let_go(args.let_go, args.why)
        print("recorded in %s; the plot is gone from the next scan" % os.path.relpath(LET_GO))
        if args.let_go.startswith("terminal:"):
            print("the terminal is still open; close it yourself:  kill %s" % args.let_go.split(":", 1)[1])
        return
    cfg = {}
    if os.path.exists(args.config):
        cfg = config_mod.load(args.config).get("yards") or {}
    y = build(cfg, [{"ox": 0, "oy": 0}])
    print("%-6s %-15s %-26s %s" % ("PID", "STATUS", "PROJECT", "WORKING ON"))
    for t in y["terminals"]:
        print("%-6s %-15s %-26s %s" % (t["pid"], t["status"], t["project"][:26],
                                       (t["title"] or t["name"] or "(untitled)")[:50]))
    print()
    for s in y["sites"]:
        if args.street and s["street"] != args.street:
            continue
        print("%-8s %-9s %-40s %s" % (s["street"], s["state"], s["id"][:40], s["evidence"][:60]))


if __name__ == "__main__":
    main()
