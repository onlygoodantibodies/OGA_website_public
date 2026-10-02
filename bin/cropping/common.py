"""Shared by the zip-cropping tools (.claude/skills/zip-cropping/SKILL.md).

A browser signed in as the field-test account, the plan file every step
reads, and the contact sheets a person checks crops against.
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

SITE = os.environ.get("OGA_SITE", "https://onlygoodantibodies.co.uk")
MEDIA = "https://media.onlygoodantibodies.co.uk"

# The figure application a crop's filename uses: the database says ICC-IF,
# the filename says IF (CLAUDE.md, "A crop's object key").
FILE_APP = {"WB": "WB", "IP": "IP", "ICC-IF": "IF", "FC": "FC", "IHC": "IHC"}


def load_plan(path):
    plan_path = Path(path).resolve()
    plan = json.loads(plan_path.read_text())
    plan["_dir"] = plan_path.parent
    plan["_path"] = plan_path
    return plan


def save_plan(plan):
    out = {k: v for k, v in plan.items() if not k.startswith("_")}
    plan["_path"].write_text(json.dumps(out, indent=1) + "\n")


def work_dir(plan, name):
    d = plan["_dir"] / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def whole_figures(plan):
    """How many figures go on the gene's IHC page whole (box 6)."""
    return sum(1 for f in plan["figures"] if f.get("whole"))


def crop_files(plan):
    """``[(app, filename), ...]`` in the order the figures show them."""
    out = []
    for fig in plan["figures"]:
        app = FILE_APP[fig["app"]]
        out += [(app, f"{plan['gene']}_{cat}_{app}.png") for cat in fig["order"]]
    return out


def start(pw, w=1700, h=1100):
    """A headless browser signed in to the pipeline. Refuses loudly: arriving
    anywhere but /pipeline/start/ is not being signed in (bin/live_check.py)."""
    user, pwd = os.environ.get("OGA_TEST_USERNAME"), os.environ.get("OGA_TEST_PASSWORD")
    if not user or not pwd:
        sys.exit("OGA_TEST_USERNAME / OGA_TEST_PASSWORD are not set.")
    browser = pw.chromium.launch(executable_path=os.environ.get("OGA_CHROME") or None,
                                 args=shlex.split(os.environ.get("OGA_CHROME_ARGS", "")))
    page = browser.new_context(viewport={"width": w, "height": h}).new_page()
    dialogs, errors = [], []
    # The cropper has no native dialog by design; one appearing is a finding.
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(SITE + "/accounts/login/?next=/pipeline/start/")
    page.fill("input[name=login]", user)
    page.fill("input[name=password]", pwd)
    with page.expect_navigation():
        page.click("form button[type=submit]")
    if not page.url.endswith("/pipeline/start/"):
        sys.exit(f"Not signed in: landed on {page.url}")
    return browser, page, dialogs, errors


def pick_session(page, plan):
    """Resume the session ``build.py`` recorded, by its id — two sessions built
    in one minute carry the same label, and a plan from before ids were
    recorded still names one by label. Returns the label resumed."""
    want = str(plan.get("session") or "")
    if not want:
        sys.exit("No session recorded in the plan — run build.py first.")
    opts = page.evaluate("() => [...document.querySelectorAll('#sessionpick option')]"
                         ".map(o => [o.value, o.textContent.trim()])")
    hits = [o for o in opts if (o[0] == want if want.isdigit() else o[1] == want)]
    if len(hits) != 1:
        sys.exit(f"Session “{want}” is not exactly one entry in the list: {[o[1] for o in opts]}")
    page.select_option("#sessionpick", value=hits[0][0])
    return hits[0][1]


def txt(page, sel):
    loc = page.locator(sel)
    return loc.inner_text() if loc.count() else ""


def fetch(url, dest):
    """curl, not urllib: the edge turns Python's default user agent away (403)."""
    subprocess.run(["curl", "-sS", "-f", "-o", str(dest), url], check=True)


def sheets(src_dir, files, prefix, gene):
    """One contact sheet per application: every crop with the record it is
    filed on in a yellow label beneath, to read against the title printed in
    the crop. Returns the sheet paths."""
    from PIL import Image, ImageDraw, ImageFont

    tile = 330
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
    made = []
    for app in dict.fromkeys(a for a, _ in files):
        names = [f for a, f in files if a == app]
        cols = 7
        rows = -(-len(names) // cols)
        sheet = Image.new("RGB", (cols * tile, rows * (tile + 30)), "white")
        draw = ImageDraw.Draw(sheet)
        for i, name in enumerate(names):
            x, y = (i % cols) * tile, (i // cols) * (tile + 30)
            path = Path(src_dir) / name
            if path.exists():
                im = Image.open(path).convert("RGB")
                im.thumbnail((tile, tile))
                sheet.paste(im, (x, y))
            label = name[len(gene) + 1:-len(f"_{app}.png")]
            draw.rectangle([x, y + tile, x + tile - 4, y + tile + 28], fill=(255, 235, 120))
            draw.text((x + 4, y + tile + 3), f"{i + 1}. {label}" + ("" if path.exists() else " — MISSING"),
                      fill="black", font=font)
        out = Path(src_dir).parent / f"{prefix}_{app}.png"
        sheet.save(out)
        made.append(out)
    return made
