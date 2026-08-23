#!/usr/bin/env python3
"""Stage a loadable dev copy of the extension and serve a test paper.

The shipped extension only runs on journal domains, which makes it awkward to
try against a local file. This stages a copy that also matches localhost, serves
the test fixture, and tells you what to click. Nothing it changes is shipped —
the real manifest stays clean.

    python tools/dev_extension.py

    # try it against the real dataset rather than the bundled seed
    python tools/dev_extension.py --index https://onlygoodantibodies.co.uk/extension/index.json

Leave it running, load the printed directory as an unpacked extension, then open
the printed URL. Ctrl-C when done.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import shutil
import socketserver
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = os.path.join(ROOT, "browser-extension")
STAGE = os.path.join(ROOT, "dist", "extension-dev")
FIXTURES = os.path.join(EXT, "test", "fixtures")

LOCAL_MATCHES = ["http://localhost/*", "http://127.0.0.1/*"]


def stage(index_source: str | None) -> str:
    if os.path.exists(STAGE):
        shutil.rmtree(STAGE)
    shutil.copytree(
        EXT, STAGE,
        ignore=shutil.ignore_patterns("node_modules", "__pycache__", "*.pyc"),
    )

    manifest_path = os.path.join(STAGE, "manifest.json")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    matches = manifest["content_scripts"][0]["matches"]
    for m in LOCAL_MATCHES:
        if m not in matches:
            matches.append(m)
    manifest["name"] = manifest["name"] + " (dev)"

    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    if index_source:
        dest = os.path.join(STAGE, "data", "index.json")
        if index_source.startswith(("http://", "https://")):
            print(f"fetching index from {index_source} ...")
            with urllib.request.urlopen(index_source, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        else:
            with open(index_source, encoding="utf-8") as fh:
                data = json.load(fh)
        with open(dest, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        counts = data.get("counts", {})
        print(
            f"  using {counts.get('antibodies', '?')} antibodies across "
            f"{counts.get('genes_with_antibody_records', '?')} genes"
        )

    with open(os.path.join(STAGE, "data", "index.json"), encoding="utf-8") as fh:
        counts = json.load(fh).get("counts", {})
    if counts.get("genes_with_antibody_records", 0) < 10:
        print(
            "\n  NOTE: this is the seed index (one gene). Most papers will show nothing.\n"
            "  Pass --index with the live URL to test against the real dataset.",
            file=sys.stderr,
        )

    return STAGE


def serve(port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=FIXTURES)

    class Reuse(socketserver.TCPServer):
        allow_reuse_address = True

    with Reuse(("127.0.0.1", port), handler) as httpd:
        actual = httpd.server_address[1]
        print("\n" + "─" * 68)
        print("  1. Chrome:  chrome://extensions → Developer mode → Load unpacked")
        print(f"     Firefox: about:debugging → Load Temporary Add-on → manifest.json")
        print(f"\n     {STAGE}")
        print(f"\n  2. Open:    http://127.0.0.1:{actual}/paper.html")
        print("\n  Reload the extension AND the page after any code change.")
        print("─" * 68 + "\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", help="URL or path to an index.json to use instead of the bundled one")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--no-serve", action="store_true", help="stage only, do not serve the fixture")
    args = ap.parse_args()

    path = stage(args.index)
    print(f"staged {path}")

    if args.no_serve:
        return 0
    serve(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
