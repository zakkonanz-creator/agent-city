"""Strip anything secret-shaped from text the scanner copies off the machine.

Log tails, README excerpts and probe output end up in ``data/city.json`` and in the
built page, and you may publish that page. Every such string passes through
``scrub`` first. The rule is blunt on purpose: better to redact a harmless line
than to publish a token.

Add your own patterns in agents.toml:

    [redact]
    patterns = ["acme-internal-[0-9]+", "customer #[0-9]+"]
"""
import os
import re

PATTERNS = [
    ("private key",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)")),
    ("slack token",   re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai key",    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("stripe key",    re.compile(r"\b[rsp]k_(live|test)_[A-Za-z0-9]{16,}")),
    ("bearer token",  re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*")),
    ("basic auth",    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@")),
    ("aws key",       re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google key",    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("github token",  re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("jwt",           re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("secret param",  re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd)=([^\s&\"']{6,})")),
    ("email",         re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # 40+ chars of key alphabet with both letters and digits and no path separator;
    # a slash would make every deep file path look like a secret
    ("long secret",   re.compile(r"(?<![\w/.-])(?=[A-Za-z0-9+_-]*\d)(?=[A-Za-z0-9+_-]*[A-Za-z])[A-Za-z0-9+_-]{40,}={0,2}(?![\w/])")),
    # international numbers only: a bare digit run is far more often a timestamp or id
    ("phone",         re.compile(r"(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}\b")),
]

_extra = []
_hits = []
HOME = os.path.expanduser("~")
USER = os.path.basename(HOME)


def add_patterns(patterns):
    """Extra regexes from the config's [redact] table."""
    for p in patterns or []:
        _extra.append(("custom", re.compile(p)))


def scrub(text):
    """Return ``text`` with every secret-shaped run replaced, recording what went."""
    if not text:
        return text
    text = str(text)
    for label, rx in PATTERNS + _extra:
        def sub(m, label=label):
            _hits.append((label, m.group(0)[:6] + "..."))
            return "[redacted:%s]" % label.split()[0]
        text = rx.sub(sub, text)
    # your home folder and user name say who you are; ~ says the same thing to you
    if HOME and HOME != "/":
        text = text.replace(HOME, "~")
    if USER and len(USER) > 2:
        text = re.sub(r"(?<=/home/)%s\b|(?<=/Users/)%s\b" % (re.escape(USER), re.escape(USER)),
                      "you", text)
    return text


def report():
    """What was stripped this run (label and the first few characters)."""
    return list(_hits)


def summary():
    counts = {}
    for label, _ in _hits:
        counts[label] = counts.get(label, 0) + 1
    return counts
