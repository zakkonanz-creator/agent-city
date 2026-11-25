#!/usr/bin/env python3
"""Put the snapshot inside the page, producing one self-contained HTML file.

    python3 build.py               # data/city.json + city.html -> dist/index.html
    python3 build.py --demo        # a made-up city (demo.py) -> dist/demo.html
    python3 build.py --open        # ...and open it in your browser

The snapshot is inlined, not fetched, so the page works from file:// and from any
static host (and inside a sandboxed artifact, whose CSP blocks fetches).
"""
import argparse
import json
import os
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "city.html")
SNAP = os.path.join(HERE, "data", "city.json")
MARKER = "/* The snapshot is injected here by build.py as window.__CITY__. */"


def render(snap):
    with open(SRC, encoding="utf-8") as fh:
        html = fh.read()
    if MARKER not in html:
        sys.exit("city.html has lost its injection marker")
    # </script> inside the data would close the block early; \u2028/\u2029 break JS parsers
    blob = (json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
            .replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    html = html.replace(MARKER, "window.__CITY__ = " + blob + ";")
    name = snap.get("city_name") or "Agent City"
    return html.replace("<title>Agent City</title>", "<title>%s</title>" % _esc(name), 1)


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--demo", action="store_true", help="build the made-up demo city")
    ap.add_argument("--snapshot", default=SNAP)
    ap.add_argument("--out")
    ap.add_argument("--open", action="store_true", help="open the result in a browser")
    args = ap.parse_args(argv)

    if args.demo:
        sys.path.insert(0, HERE)
        import demo
        snap = demo.snapshot()
        out = args.out or os.path.join(HERE, "dist", "demo.html")
    else:
        if not os.path.exists(args.snapshot):
            sys.exit("no snapshot yet. Run: python3 scan.py   (or try: python3 build.py --demo)")
        with open(args.snapshot, encoding="utf-8") as fh:
            snap = json.load(fh)
        out = args.out or os.path.join(HERE, "dist", "index.html")

    html = render(snap)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    kb = os.path.getsize(out) / 1024
    print("wrote %s  (%.0f KB, %d agents, %d findings, scanned %s)"
          % (os.path.relpath(out), kb, len(snap["agents"]), len(snap["findings"]),
             snap.get("generated_local", "?")))
    if args.open:
        webbrowser.open("file://" + os.path.abspath(out))


if __name__ == "__main__":
    main()
