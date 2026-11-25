#!/usr/bin/env python3
"""A made-up city, so you can see Agent City before wiring up your own agents.

Nothing here is real. It builds a throwaway folder of fake logs, notes, project
folders and Claude Code sessions, then runs the real scanner over it, so the demo
goes through exactly the code path your own city will.

    python3 build.py --demo --open
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

H = 3600
D = 86400

DISTRICTS = [
    {"id": "inbox", "name": "Inbox", "blurb": "Email, chat and everything that talks to a person."},
    {"id": "research", "name": "Research Lab", "blurb": "Reads the outside world and writes it down."},
    {"id": "studio", "name": "Studio", "blurb": "Drafts, clips and posts."},
    {"id": "ledger", "name": "Ledger", "blurb": "Invoices, receipts and the bank."},
    {"id": "ops", "name": "Ops Yard", "blurb": "Backups, servers and the plumbing."},
    {"id": "civic", "name": "Watchtower", "blurb": "The jobs that watch the other jobs."},
]

# id, district, schedule, interval (s) or None, how it behaves, tier, extras
# behaviour: live | fail | fail0 (exit 0, error in log) | quiet (stopped days ago)
#            scaffold (never installed) | paused | manual | nolog
AGENTS = [
    ("email-triage", "inbox", "every 15m", 15 * 60, "live", 4,
     dict(purpose="Sorts new email into reply / read later / ignore and writes the shortlist.",
          writes=["notes/inbox/triage.md"], talks_to=["Gmail"], watched="output")),
    ("reply-drafter", "inbox", "every 30m", 30 * 60, "live", 3,
     dict(purpose="Drafts replies for everything triage marked 'reply'. Never sends.",
          reads=["notes/inbox/triage.md", "notes/people/"], writes=["Gmail drafts"],
          talks_to=["Gmail"], watched="runs")),
    ("meeting-prep", "inbox", "every 1h", H, "fail0", 4,
     dict(purpose="A one-page brief before every calendar event with an outside guest.",
          reads=["notes/people/", "reports/market.md"], writes=["Slack DMs"],
          talks_to=["Calendar", "Slack"], watched="runs",
          error="ERROR calendar API: 401 Unauthorized (token expired)")),
    ("slack-digest", "inbox", "30 7 * * *", D, "live", 3,
     dict(purpose="Morning digest of the channels you skimmed past yesterday.",
          writes=["reports/slack-digest.md"], talks_to=["Slack"])),
    ("follow-up-nudger", "inbox", "0 16 * * 1-5", D, "scaffold", 3,
     dict(purpose="Reminds you about threads where you promised something.",
          writes=["Slack DMs"], talks_to=["Slack", "Gmail"])),
    ("market-scan", "research", "0 6 * * *", D, "live", 4,
     dict(purpose="Reads the news, filings and forums you follow; writes what changed.",
          writes=["reports/market.md"], talks_to=["Web"], watched="output")),
    ("competitor-watch", "research", "weekly", 7 * D, "quiet", 3,
     dict(purpose="Diffs competitors' pricing pages and changelogs every week.",
          writes=["reports/competitors.md"], talks_to=["Web"], quiet_days=40)),
    ("paper-digest", "research", "0 5 * * *", D, "live", 2,
     dict(purpose="Summarises new papers from your saved searches.",
          writes=["notes/reading/"], talks_to=["Web"])),
    ("trend-radar", "research", "every 6h", 6 * H, "live", 3,
     dict(purpose="Tracks search and social volume for a list of topics.", host="homelab",
          writes=["reports/trends.md"], talks_to=["Web"])),
    ("question-bank", "research", "manual", None, "manual", 2,
     dict(purpose="You ask it a research question; it files a cited answer.",
          reads=["notes/"], writes=["notes/answers/"])),
    ("blog-drafter", "studio", "0 9 * * *", D, "live", 3,
     dict(purpose="Turns the market report into a first draft in your voice.",
          reads=["reports/market.md", "notes/reading/"], writes=["drafts/blog/"],
          talks_to=["Notion"])),
    ("clip-cutter", "studio", "every 2h", 2 * H, "live", 4,
     dict(purpose="Finds the best minute of each new recording and cuts a vertical clip.",
          host="cloud", writes=["clips/"], talks_to=["YouTube"], watched="output")),
    ("social-scheduler", "studio", "0 10 * * *", D, "paused", 3,
     dict(purpose="Queues approved clips and posts across your social accounts.",
          talks_to=["YouTube", "LinkedIn"], paused="waiting on the new brand guidelines",
          paused_since="2026-01-05", resume="crontab -e and uncomment social_scheduler")),
    ("thumbnail-maker", "studio", "every 1h", H, "scaffold", 2,
     dict(purpose="Generates three thumbnail options per clip.", writes=["clips/thumbs/"])),
    ("newsletter", "studio", "0 8 * * 5", 7 * D, "live", 4,
     dict(purpose="Friday newsletter assembled from the week's drafts and reports.",
          reads=["drafts/blog/", "reports/market.md", "reports/trends.md"],
          writes=["drafts/newsletter.md"], talks_to=["Email"], watched="runs")),
    ("invoice-chaser", "ledger", "0 9 * * 1-5", D, "live", 4,
     dict(purpose="Finds overdue invoices and drafts a polite chaser for each.",
          writes=["reports/receivables.md"], talks_to=["Stripe", "Gmail"], watched="output")),
    ("receipt-filer", "ledger", "every 1h", H, "fail", 3,
     dict(purpose="Pulls receipts out of email and files them with the right category.",
          writes=["notes/receipts/"], talks_to=["Gmail", "Drive"],
          error="Traceback (most recent call last):\n  File \"filer.py\", line 88, in <module>\n"
                "KeyError: 'category'")),
    ("spend-report", "ledger", "0 7 * * 1", 7 * D, "live", 3,
     dict(purpose="Weekly spend by category, with anything unusual called out.",
          reads=["notes/receipts/", "reports/receivables.md"], writes=["reports/spend.md"])),
    ("bank-sync", "ledger", "every 4h", 4 * H, "live", 4,
     dict(purpose="Pulls balances and transactions from the bank feed.", host="cloud",
          writes=["reports/cash.md"], talks_to=["Bank"])),
    ("backup", "ops", "0 2 * * *", D, "live", 5,
     dict(purpose="Nightly encrypted backup of notes, reports and drafts.", host="homelab",
          reads=["notes/", "reports/", "drafts/"], writes=["backups/"], watched="output")),
    ("uptime-pinger", "ops", "every 5m", 5 * 60, "live", 3,
     dict(purpose="Pings every public endpoint and alerts on two misses in a row.", host="cloud",
          talks_to=["Slack"], watched="runs")),
    ("dependency-bumper", "ops", "0 4 * * 0", 7 * D, "live", 2,
     dict(purpose="Opens a pull request when a dependency has a security fix.",
          talks_to=["GitHub"])),
    ("log-rotator", "ops", "0 3 * * *", D, "live", 1,
     dict(purpose="Compresses and prunes old logs.")),
    ("disk-cleaner", "ops", "0 3 * * *", D, "nolog", 1,
     dict(purpose="Clears caches when the disk passes 85%.")),
    ("watchdog", "civic", "every 15m", 15 * 60, "live", 5,
     dict(purpose="Checks every other agent ran on time and that its output looks right.",
          role="watchdog", reads=["reports/", "notes/inbox/triage.md"], writes=["Slack alerts"],
          talks_to=["Slack"], watched="output")),
    ("cost-tracker", "civic", "0 23 * * *", D, "live", 3,
     dict(purpose="Adds up what every agent spent on AI and APIs today.",
          writes=["reports/costs.md"])),
]

STORES = [("notes", "Notes", "what you and your agents know"),
          ("reports", "Reports", "generated digests and briefs"),
          ("drafts", "Drafts", "work waiting for a human")]

PROJECTS = [  # name, code files, idle days, checklist (done, total) or None
    ("invoice-portal", 24, 2, (7, 11)),
    ("voice-notes-to-tasks", 9, 5, None),
    ("podcast-pipeline", 15, 21, (3, 9)),
    ("crm-cleanup", 6, 35, None),
    ("expense-ocr", 4, 70, None),
    ("landing-page-v2", 31, 95, (12, 14)),
]
PLANS = [  # title, idle days
    ("Weekly review agent", 3),
    ("Contract clause checker", 18),
    ("Price-change alerts for suppliers", 45),
    ("Home lab dashboard", 60),
]
SESSIONS = [  # pid, project, title, idle hours, phase-ish
    (90001, "invoice-portal", "Add Stripe webhook retries", 0.3, "built"),
    (90002, "agent-city", "Make the Yards cranes slower", 6, "built"),
    (90003, "notes", "Plan a reading-list agent", 150, "planned, nothing built"),
]


def _ts_line(t, text):
    return "%s %s" % (datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S"), text)


def _touch(path, t, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.utime(path, (t, t))


def _write_log(path, now, interval, behaviour, extra):
    lines, last = [], now - interval * 0.3
    if behaviour == "quiet":
        last = now - extra.get("quiet_days", 30) * D
    start = last - 7 * D
    t, runs = last, []
    while t > start and len(runs) < 2100:
        runs.append(t)
        t -= interval
    for t in reversed(runs):
        lines.append(_ts_line(t, "=== run start ==="))
        lines.append(_ts_line(t + 3, "done: ok"))
    if behaviour in ("fail", "fail0"):
        lines = lines[:-1] + [_ts_line(last + 2, l) for l in extra["error"].splitlines()]
    _touch(path, last, "\n".join(lines) + "\n")


def build_world(root, now):
    """Fake logs, stores, projects and sessions under ``root``; returns a config dict."""
    logs = os.path.join(root, "logs")
    agents = []
    for aid, dist, sch, interval, behaviour, tier, extra in AGENTS:
        spec = {"id": aid, "district": dist, "schedule": sch, "tier": tier,
                "purpose": extra["purpose"], "run_marker": "=== run start"}
        for k in ("writes", "reads", "talks_to", "watched", "role", "paused",
                  "paused_since", "resume"):
            if k in extra:
                spec[k] = extra[k]
        spec["host"] = extra.get("host", "local")
        # writes/reads that are folders live under the fake root, so roads and clouds connect
        for k in ("writes", "reads"):
            if k in spec:
                spec[k] = [os.path.join(root, p) if "/" in p or p.endswith(".md") else p
                           for p in spec[k]]
        if behaviour == "scaffold":
            spec["launchd"] = "com.example.agent-city-demo.%s" % aid   # never installed
        elif behaviour != "nolog":
            spec["log"] = os.path.join(logs, aid + ".log")
            if behaviour != "manual":
                _write_log(spec["log"], now, interval or D, behaviour, extra)
            else:
                _touch(spec["log"], now - 3 * D, _ts_line(now - 3 * D, "=== run start ===") + "\n")
        if behaviour == "nolog":
            spec["purpose"] += " (No log configured, so the city can't tell if it runs.)"
        agents.append(spec)

    # the stores: files of mixed ages, so the clouds show some going cold
    for sid, label, blurb in STORES:
        for k in range(36):
            age = (k * k * 0.9) * D
            _touch(os.path.join(root, sid, "item-%02d.md" % k), now - age, "x")
    for sub in ("inbox/triage.md", "people/ada.md", "people/grace.md", "reading/today.md",
                "receipts/2026-01.md"):
        _touch(os.path.join(root, "notes", sub), now - 2 * H, "x")
    for f in ("market.md", "trends.md", "receivables.md", "spend.md", "costs.md",
              "competitors.md", "slack-digest.md", "cash.md"):
        _touch(os.path.join(root, "reports", f), now - 5 * H, "x")
    _touch(os.path.join(root, "drafts", "blog", "post.md"), now - 9 * H, "x")

    # project folders for the Yards
    for name, code, idle, chk in PROJECTS:
        d = os.path.join(root, "projects", name)
        for k in range(code):
            _touch(os.path.join(d, "src", "mod_%02d.py" % k), now - (idle + k * 0.2) * D, "pass\n")
        if chk:
            done, total = chk
            body = "# %s\n\n" % name + "".join("- [%s] step %d\n" % ("x" if i < done else " ", i + 1)
                                               for i in range(total))
            _touch(os.path.join(d, "PLAN.md"), now - idle * D, body)
    claude = os.path.join(root, "claude")
    for title, idle in PLANS:
        slug = title.lower().replace(" ", "-")
        _touch(os.path.join(claude, "plans", slug + ".md"), now - idle * D,
               "# %s\n\n- [ ] decide scope\n- [ ] build\n- [ ] ship\n" % title)
    for pid, project, title, idle_h, phase in SESSIONS:
        sid = "demo-%d" % pid
        cwd = os.path.join(root, "projects", project)
        _touch(os.path.join(claude, "sessions", "%d.json" % pid), now - idle_h * H,
               json.dumps({"pid": pid, "cwd": cwd, "sessionId": sid, "name": title}))
        slug = "".join(c if c.isalnum() else "-" for c in cwd)
        ts = datetime.utcfromtimestamp(now - idle_h * H).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        recs = [{"type": "user", "timestamp": ts, "message": {"role": "user", "content": title}},
                {"type": "assistant", "timestamp": ts, "message": {
                    "role": "assistant", "usage": {"input_tokens": 40000, "output_tokens": 90000,
                                                   "cache_read_input_tokens": 9000000,
                                                   "cache_creation_input_tokens": 300000},
                    "content": [{"type": "tool_use", "name": "Write" if phase == "built" else "Read",
                                 "input": {"file_path": os.path.join(cwd, "src", "main.py")}}]}},
                {"type": "ai-title", "aiTitle": title}]
        _touch(os.path.join(claude, "projects", slug, sid + ".jsonl"), now - idle_h * H,
               "\n".join(json.dumps(r) for r in recs) + "\n")

    return {
        "city": {"name": "Agent City (demo)"},
        "district": DISTRICTS,
        "agent": agents,
        "store": [{"id": sid, "label": label, "blurb": blurb, "path": os.path.join(root, sid)}
                  for sid, label, blurb in STORES],
        "hosts": {},
        "discover": {"cron": False},
        "yards": {"projects": [os.path.join(root, "projects", "*")], "claude_home": claude,
                  "exclude": []},
    }


def snapshot():
    import scan
    now = time.time()
    root = tempfile.mkdtemp(prefix="agent-city-demo-")
    try:
        cfg = build_world(root, now)
        snap = scan.scan(cfg, now=now, alive_pids={p for p, *_ in SESSIONS}, quiet=True,
                         snapshots_dir=os.path.join(root, "snapshots"))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    # make the throwaway paths read like a real setup
    blob = json.dumps(snap).replace(root.replace("\\", "\\\\"), "~/agents")
    snap = json.loads(blob)
    snap["demo"] = True
    snap["changed"] = [{"agent": "receipt-filer", "what": "state", "detail": "live → failing",
                        "since": "yesterday"},
                       {"agent": "competitor-watch", "what": "state", "detail": "live → ruin",
                        "since": "last week"}]
    return snap


if __name__ == "__main__":
    s = snapshot()
    print(json.dumps(s["counts"]), len(s["findings"]), "findings,", len(s["sites"]), "plots")
