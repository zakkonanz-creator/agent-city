"""Read job state off the machine. Every probe is read-only and best-effort:
a probe that cannot run says so and returns nothing, it never guesses.

    launchd    launchctl list + ~/Library/LaunchAgents/*.plist   (macOS)
    cron       crontab -l                                        (macOS, Linux)
    systemd    systemctl --user show <unit>                      (Linux)
    health     an HTTP GET; 2xx/3xx means up
    check      a shell command you wrote; exit 0 means healthy
    logs       mtime, size and tail of a log file
    ssh        the cron + logs probes, run on another machine in one ssh call
"""
import glob
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request

LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")
TAIL_LINES = 60


def _run(cmd, timeout=15, shell=False):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=shell)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return None, "", "not installed"
    except subprocess.TimeoutExpired:
        return None, "", "timed out after %ss" % timeout
    except Exception as e:                       # pragma: no cover
        return None, "", str(e)


# ---------------------------------------------------------------- launchd
_launchctl = None


def launchctl_list():
    """{label: (pid or None, last exit or None)}; {} off macOS."""
    global _launchctl
    if _launchctl is not None:
        return _launchctl
    _launchctl = {}
    code, out, _ = _run(["launchctl", "list"])
    if code != 0:
        return _launchctl
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid, status, label = parts
        _launchctl[label] = (int(pid) if pid.strip().lstrip("-").isdigit() and pid != "-" else None,
                             int(status) if status.strip().lstrip("-").isdigit() else None)
    return _launchctl


def launchd(label):
    plist = os.path.join(LAUNCH_AGENTS, label + ".plist")
    lst = launchctl_list()
    pid, status = lst.get(label, (None, None))
    return {"installed": os.path.exists(plist), "loaded": label in lst, "pid": pid,
            "last_exit": status, "plist": plist if os.path.exists(plist) else None}


def launchd_labels(prefixes):
    """Every launchd agent of yours (by label prefix), for discovery."""
    out = set()
    for p in glob.glob(os.path.join(LAUNCH_AGENTS, "*.plist")):
        lab = os.path.basename(p)[:-6]
        if any(lab.startswith(x) for x in prefixes):
            out.add(lab)
    for lab in launchctl_list():
        if any(lab.startswith(x) for x in prefixes):
            out.add(lab)
    return sorted(out)


# ---------------------------------------------------------------- cron
def parse_crontab(text):
    """Active crontab lines as (schedule, command) pairs."""
    rows = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line.split()[0] and not line.startswith("@"):
            continue                                # VAR=value
        if line.startswith("@"):
            sched, _, cmd = line.partition(" ")
        else:
            f = line.split(None, 5)
            if len(f) < 6:
                continue
            sched, cmd = " ".join(f[:5]), f[5]
        rows.append((sched, cmd))
    return rows


_crontab = None


def crontab():
    global _crontab
    if _crontab is None:
        code, out, _ = _run(["crontab", "-l"])
        _crontab = parse_crontab(out if code == 0 else "")
    return _crontab


def cron_match(needle, rows):
    for sched, cmd in rows:
        if needle in cmd:
            return {"installed": True, "schedule": sched, "command": cmd}
    return {"installed": False}


# ---------------------------------------------------------------- systemd
def systemd(unit):
    code, out, err = _run(["systemctl", "--user", "show", unit, "--no-pager",
                           "-p", "LoadState,ActiveState,SubState,Result,ExecMainStatus,"
                                 "ExecMainExitTimestamp,UnitFileState"])
    if code is None:
        return {"error": err}
    v = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    res = {"installed": v.get("LoadState") == "loaded",
           "loaded": v.get("ActiveState") in ("active", "activating", "reloading"),
           "active": v.get("ActiveState"), "result": v.get("Result")}
    st = v.get("ExecMainStatus")
    res["last_exit"] = int(st) if st and st.isdigit() else None
    # a .timer has no exit code of its own; the matching .service does
    if unit.endswith(".timer"):
        svc = systemd(unit[:-6] + ".service")
        res["last_exit"] = svc.get("last_exit")
        res["result"] = svc.get("result")
    return res


# ---------------------------------------------------------------- health + check
def health(url, timeout=8):
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "agent-city/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": 200 <= r.status < 400, "status": r.status,
                    "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "status": None, "error": type(e).__name__ + ": " + str(e)[:120]}


def check(cmd, timeout=30):
    code, out, err = _run(cmd, timeout=timeout, shell=True)
    first = (out.strip() or err.strip()).splitlines()
    return {"ok": code == 0, "exit": code, "note": first[0][:200] if first else ""}


# ---------------------------------------------------------------- logs
def tail_lines(path, n=TAIL_LINES, max_bytes=200_000):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def read_head(path, max_bytes):
    try:
        with open(path, "rb") as fh:
            return fh.read(max_bytes).decode("utf-8", "replace")
    except OSError:
        return ""


def log_facts(path, n=TAIL_LINES):
    """mtime / size / tail of one log. A glob picks the newest matching file."""
    path = os.path.expanduser(path)
    if any(c in path for c in "*?["):
        files = [p for p in glob.glob(path) if os.path.isfile(p)]
        if not files:
            return None
        files.sort(key=os.path.getmtime)
        path = files[-1]
        series = files
    else:
        series = None
    if not os.path.isfile(path):
        return None
    st = os.stat(path)
    recent = tail_lines(path, 100000)
    out = {"path": path, "mtime": st.st_mtime, "size": st.st_size,
           "tail": recent[-n:], "recent": recent}
    if series is not None:
        out["series"] = [os.path.getmtime(p) for p in series]
    return out


# ---------------------------------------------------------------- ssh
_SSH_SCRIPT = r'''
echo "@@CRON"; crontab -l 2>/dev/null
for f in "$@"; do
  for p in $(eval echo "$f"); do
    [ -f "$p" ] || continue
    m=$(stat -c %Y "$p" 2>/dev/null || stat -f %m "$p" 2>/dev/null)
    s=$(wc -c < "$p" 2>/dev/null | tr -d ' ')
    echo "@@LOG $f"; echo "$p"; echo "$m $s"
    tail -n {n} "$p" 2>/dev/null
  done
done
echo "@@PS"; ps -eo args 2>/dev/null
echo "@@END"
'''


def ssh_host(target, log_paths, timeout=25, n=2000):
    """Run the cron + logs probes on another machine in one ssh call.

    Returns {"ok", "crontab": rows, "logs": {configured path: facts}, "ps": [..]}.
    Paths may use ~ and globs; they are expanded on the remote side.
    """
    script = _SSH_SCRIPT.replace("{n}", str(n))
    args = " ".join(shlex.quote(p.replace("~", "$HOME", 1) if p.startswith("~") else p)
                    for p in log_paths)
    # paths are passed as words and re-split by the remote for/in, so ~ and globs expand there
    remote = "sh -c %s _ %s" % (shlex.quote(script), args)
    code, out, err = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, remote],
                          timeout=timeout)
    if code is None or "@@END" not in out:
        return {"ok": False, "error": (err or "no answer").strip()[:200]}
    res = {"ok": True, "crontab": [], "logs": {}, "ps": []}
    section, cron_txt, cur, buf = None, [], None, []

    def flush():
        if cur and len(buf) >= 2:
            key, path, meta = cur, buf[0], buf[1].split()
            try:
                mt, sz = float(meta[0]), int(meta[1])
            except (ValueError, IndexError):
                return
            prev = res["logs"].get(key)
            if not prev or mt > prev["mtime"]:
                res["logs"][key] = {"path": path, "mtime": mt, "size": sz,
                                    "tail": buf[2:][-TAIL_LINES:], "recent": buf[2:]}

    for line in out.splitlines():
        if line == "@@CRON":
            section = "cron"; continue
        if line.startswith("@@LOG "):
            flush(); section, cur, buf = "log", line[6:], []
            continue
        if line == "@@PS":
            flush(); section, cur = "ps", None; continue
        if line == "@@END":
            break
        if section == "cron":
            cron_txt.append(line)
        elif section == "log":
            buf.append(line)
        elif section == "ps":
            res["ps"].append(line)
    # remote paths were sent with $HOME; map results back to the configured spelling
    fixed = {}
    for p in log_paths:
        k = p.replace("~", "$HOME", 1) if p.startswith("~") else p
        if k in res["logs"]:
            fixed[p] = res["logs"][k]
    res["logs"] = fixed
    res["crontab"] = parse_crontab("\n".join(cron_txt))
    return res
