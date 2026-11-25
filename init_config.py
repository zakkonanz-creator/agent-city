"""Write a starter agents.toml from the jobs already on this machine.

    python3 scan.py --init

Reads your crontab, your own launchd agents (~/Library/LaunchAgents, Apple's and
other vendors' left out) and your systemd user timers. Each becomes an [[agent]]
with how to find it, its schedule and, where the job says, its log file. Purposes,
districts, reads and writes are left for you: the scan can't know what a job is for.
"""
import glob
import os
import plistlib
import re
import shlex

import probes

VENDOR = ("com.apple.", "com.google.", "com.microsoft.", "com.adobe.", "com.docker.",
          "homebrew.", "com.dropbox.", "com.spotify.", "us.zoom.", "com.zoom.",
          "com.oracle.", "org.mozilla.", "com.jetbrains.", "keybase.", "com.grammarly.")


def _id(text, taken):
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"
    i, out = 2, base
    while out in taken:
        out, i = "%s-%d" % (base, i), i + 1
    taken.add(out)
    return out


def _cron_jobs():
    jobs = []
    for sched, cmd in probes.crontab():
        log = None
        m = re.search(r">>?\s*(\S+)", cmd)
        if m and m.group(1) not in ("/dev/null", "&1"):
            log = m.group(1)
        try:
            words = shlex.split(cmd.split(">")[0].split("&&")[-1])
        except ValueError:
            words = cmd.split()
        script = next((w for w in words if re.search(r"\.(py|sh|js|ts|rb|mjs)$", w)), None)
        name = os.path.splitext(os.path.basename(script))[0] if script else (words[0] if words else "cron-job")
        needle = os.path.basename(script) if script else cmd[:40]
        jobs.append({"name": name, "cron": needle, "schedule": sched if not sched.startswith("@") else sched,
                     "log": log})
    return jobs


def _launchd_jobs():
    jobs = []
    for p in sorted(glob.glob(os.path.expanduser("~/Library/LaunchAgents/*.plist"))):
        label = os.path.basename(p)[:-6]
        if label.startswith(VENDOR):
            continue
        try:
            with open(p, "rb") as fh:
                pl = plistlib.load(fh)
        except Exception:
            continue
        jobs.append({"name": label.split(".")[-1], "launchd": label,
                     "log": pl.get("StandardOutPath"), "error_log": pl.get("StandardErrorPath")})
    return jobs


def _systemd_jobs():
    code, out, _ = probes._run(["systemctl", "--user", "list-timers", "--all", "--no-legend", "--plain"])
    if code != 0:
        return []
    jobs = []
    for line in out.splitlines():
        m = re.search(r"(\S+\.timer)", line)
        if m:
            jobs.append({"name": m.group(1)[:-6], "systemd": m.group(1)})
    return jobs


def _q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def starter(home=os.path.expanduser("~")):
    jobs = _cron_jobs() + _launchd_jobs() + _systemd_jobs()
    taken = set()
    out = ['# Starter config written by `python3 scan.py --init`.',
           '# Fill in purpose, district, tier, reads/writes and talks_to; see',
           '# examples/agents.example.toml for every key. Delete jobs you don\'t want on the map.',
           '', '[city]', 'name = "My Agent City"', '',
           '[[district]]', 'id = "main"', 'name = "Main Street"', '',
           '# Add districts and move agents into them as the city grows, e.g.:',
           '# [[district]]', '# id = "inbox"', '# name = "Inbox"', '']
    if not jobs:
        out += ['# No cron, launchd or systemd jobs found on this machine.',
                '# Add agents by hand, e.g.:', '# [[agent]]', '# id = "my-agent"',
                '# schedule = "every 1h"', '# log = "~/agents/logs/my-agent.log"', '']
    for j in jobs:
        out += ['[[agent]]', 'id = %s' % _q(_id(j["name"], taken)), 'district = "main"',
                'purpose = ""']
        for k in ("cron", "launchd", "systemd", "schedule", "log", "error_log"):
            v = j.get(k)
            if v:
                if k in ("log", "error_log") and v.startswith(home):
                    v = "~" + v[len(home):]
                out.append("%s = %s" % (k, _q(v)))
        out += ['tier = 2', '# writes = []', '# reads = []', '# talks_to = []', '']
    out += ['[discover]', 'cron = true', '',
            '[yards]', 'enabled = true', '# projects = ["~/code/*"]', 'claude_sessions = true',
            'plans = true', 'exclude = []', '']
    return "\n".join(out), len(jobs)


def write(path):
    if os.path.exists(path):
        raise SystemExit("%s already exists; not overwriting it" % path)
    text, n = starter()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return n
