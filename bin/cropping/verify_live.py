#!/usr/bin/env python3
"""After release: fetch every published crop the plan made, for checking.

    python bin/cropping/verify_live.py PLAN.json

Read-only, and needs no sign-in. Reads the gene's public page and, for each
planned crop, the figure drawn in that antibody's row for that application;
downloads it into ``live/`` and draws ``live_<APP>.png`` contact sheets
labelled with the record whose row shows it, to be read against the title
printed inside every crop. Also the answer key for a rehearsal on a gene
already public (compare ``preview_<APP>.png`` with ``live_<APP>.png``).

Check the live files, not the preview: the server cuts the saved crops
itself, so a preview that was right does not by itself prove the release is.
"""
import argparse
import re
import subprocess
import sys

from common import SITE, crop_files, load_plan, sheets, work_dir


APP_BOX = {"wb": "WB", "ip": "IP", "icc_if": "IF", "fc": "FC", "ihc": "IHC"}


def published(html):
    """``{(catalogue, APP): image url}`` — read off the page's own rows
    (``div.row-box``: the antibody's catalogue in its heading, then one
    ``experiment-box`` per application), so a figure is tied to the record
    whose row draws it, whatever its file is called. PPP2R5D's WB, IP and IF
    figures predate the cropper and are named ``experiments/PPP2RSD-WB-…``;
    matching on the cropper's filename pattern missed all fifteen."""
    out = {}
    for chunk in html.split('<div class="row-box"')[1:]:
        cat = re.search(r"<h3>\s*([^<]+?)\s*</h3>", chunk)
        if not cat:
            continue
        for app, url in re.findall(r'data-app="([a-z_]+)"[^>]*>\s*<img src="([^"]+)"', chunk):
            out[(cat.group(1).lower(), APP_BOX.get(app, app.upper()))] = url
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    opts = ap.parse_args()
    plan = load_plan(opts.plan)
    gene = plan["gene"]
    html = subprocess.run(["curl", "-sS", "-f", f"{SITE}/antibodies/{gene}/"],
                          capture_output=True, text=True, check=True).stdout
    live = published(html)
    out = work_dir(plan, "live")
    missing, legacy = [], []
    files = crop_files(plan)
    for app, name in files:
        cat = name[len(gene) + 1:-len(f"_{app}.png")]
        url = live.get((cat.lower(), app))
        if url is None:
            missing.append(name)
            continue
        if not url.rsplit("/", 1)[-1] == name:
            legacy.append(f"{name} ← {url.rsplit('/', 1)[-1]}")
        dest = out / name
        if not dest.exists():
            subprocess.run(["curl", "-sS", "-f", "-o", str(dest), url], check=True)
    print(f"{len(files) - len(missing)} of {len(files)} planned crops have a published figure "
          f"in their antibody's row on {gene}'s page.")
    if legacy:
        print(f"{len(legacy)} of them are under another file name (an older crop, not the "
              "cropper's):", *legacy, sep="\n  ")
    if missing:
        print("No published figure in that antibody's row:", missing)
    for s in sheets(out, files, "live", gene):
        print("sheet:", s)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
