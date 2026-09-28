"""The cropper's line and panel controls, pinned where they fail silently.

Typed line boxes, keyboard nudging, the wider hit area and the panel list
(26 Sep 2026) all write into the same grid the save sends and
`commit.grid_cells` cuts from. The ways that goes wrong without a sound: a
typed value that moves the drawing but not the saved grid; a drag that also
toggles a panel; a list tick the figure does not see; and an old session
whose split fractions get rounded to whole pixels just by being opened and
saved again. Each test here asks the saved grid or the page state, not the
pixels. The broader feature tests are in `tests_browser_board.py`.
"""
from __future__ import annotations

import io
import re
import unittest

from pipeline.models import Antibody, Company
from pipeline.tests_browser_board import (CHROME, DB, HAVE_PLAYWRIGHT,
                                          _RealBrowserHarness)

GRID_KEYS = {"bounds", "hLines", "vLines", "bandLeft", "bandRight", "abOffset",
             "ihcLabels", "ihcScale", "ihcLayout", "ihcPage"}
GEOMETRY = ("bounds", "hLines", "vLines", "bandLeft", "bandRight")


@unittest.skipUnless(HAVE_PLAYWRIGHT and CHROME,
                     "needs playwright and the bundled Chromium")
class CropperLineAndPanelControlsInARealBrowserTests(_RealBrowserHarness):

    def _load(self, size=(400, 300), panels=4, abs=("ab111", "ab222")):
        from PIL import Image as _Image
        company = Company.objects.using(DB).create(name="Abcam")
        for cat in abs:
            Antibody.objects.using(DB).create(
                catalogue_number=cat, target=self.target, company=company,
                site=self.site)
        buf = io.BytesIO()
        _Image.new("RGB", size, (180, 90, 60)).save(buf, "PNG")
        self.page.goto(f"{self.live_server_url}/pipeline/cropper/?gene=SNCA")
        self.page.wait_for_selector('#onfile input.onfile', state="attached",
                                    timeout=20000)
        self.page.fill("#nrows", "1")
        self.page.dispatch_event("#nrows", "input")
        self.page.fill("#rowcount-0", str(panels))
        self.page.set_input_files("#file", files=[{
            "name": "fig.png", "mimeType": "image/png", "buffer": buf.getvalue()}])
        self.page.wait_for_selector(f"#line-row1-split{panels - 1}",
                                    state="attached", timeout=20000)

    def _type(self, sel, value):
        self.page.fill(sel, str(value))
        self.page.dispatch_event(sel, "change")

    def _screen(self, x, y):
        return self.page.evaluate(
            """([x, y]) => { const r = document.querySelector('#svgwrap svg')
                 .getBoundingClientRect();
               return [r.left + LINE_PAD + x*S.scale, r.top + LINE_PAD + y*S.scale]; }""",
            [x, y])

    def _assigned(self):
        return self.page.evaluate(
            "Object.keys(S.cellState).filter(k => S.cellState[k].assigned).sort()")

    def _save(self):
        self.page.wait_for_function("() => SESS.images.every(im => im.id)",
                                    timeout=20000)
        self.page.click("#save")
        self.page.wait_for_function(
            "document.querySelector('#savestatus').textContent.includes('saved')",
            timeout=20000)

    def _errors(self):
        return [e for e in self.errors if not re.match(r"^\d{3} ", e)]

    def test_a_typed_line_is_what_the_save_cuts(self):
        from pipeline.models import CropperImage
        from pipeline.services.cropper.commit import grid_cells
        self._load()
        self._type("#line-row1-split1", 150)
        self._type("#line-row1-left", 50)
        self._type("#line-top", 20)
        self._save()
        grid = CropperImage.objects.using(DB).get().grid
        # Same keys as before the boxes existed, splits still fractions.
        self.assertEqual(set(grid), GRID_KEYS)
        self.assertTrue(all(0 < f < 1 for f in grid["vLines"][0]))
        cells = {k: (l, t, r, b) for k, l, t, r, b in grid_cells(grid)}
        self.assertAlmostEqual(cells["0_0"][0], 50, places=6)
        self.assertAlmostEqual(cells["0_0"][1], 20, places=6)
        self.assertAlmostEqual(cells["0_1"][0], 150, places=6)
        self.assertAlmostEqual(cells["0_3"][2], 400, places=6)
        self.assertEqual(self._errors(), [])

    def test_a_selected_line_nudges_by_1_and_10_and_a_drag_assigns_nothing(self):
        self._load()
        x, y = self._screen(200, 150)
        self.page.mouse.click(x + 6, y)            # selects split 2, 6 px off
        self.assertIn("lp-on", self.page.get_attribute("#line-row1-split2", "class"))
        self.page.keyboard.press("ArrowRight")
        self.assertEqual(self.page.input_value("#line-row1-split2"), "201")
        self.page.keyboard.press("Shift+ArrowRight")
        self.assertEqual(self.page.input_value("#line-row1-split2"), "211")
        self.page.keyboard.press("Shift+ArrowLeft")
        self.assertEqual(self.page.input_value("#line-row1-split2"), "201")
        # The grid itself moved, not only the box.
        self.assertAlmostEqual(self.page.evaluate("cells()[2].rect.l"), 201, places=6)
        # Pressing on the line, and dragging it, toggle no panel.
        x, y = self._screen(201, 150)
        self.page.mouse.move(x + 5, y)
        self.page.mouse.down()
        self.page.mouse.move(x + 45, y, steps=5)
        self.page.mouse.up()
        self.assertGreater(int(self.page.input_value("#line-row1-split2")), 210)
        self.assertEqual(self._assigned(), [])
        # A drag over a panel toggles nothing either.
        cx, cy = self._screen(80, 150)
        self.page.mouse.move(cx, cy)
        self.page.mouse.down()
        self.page.mouse.move(cx + 25, cy + 15, steps=5)
        self.page.mouse.up()
        self.assertEqual(self._assigned(), [])
        self.assertEqual(self._errors(), [])

    def test_the_panel_list_and_the_figure_agree(self):
        self._load(panels=3)
        self.page.click("#celllist-open")
        self.page.check("#cell-0_1-toggle")
        self.assertIn("assigned", self.page.get_attribute("#cell-0_1", "class"))
        self.assertEqual(self._assigned(), ["0_1"])
        self.page.select_option("#cell-0_1-ab", "1")
        self.assertEqual(self.page.evaluate("S.cellState['0_1'].ab"), 1)
        # A real click on the figure ticks the list.
        cx, cy = self._screen(330, 150)
        self.page.mouse.click(cx, cy)
        self.assertTrue(self.page.is_checked("#cell-0_2-toggle"))
        self.assertEqual(self._assigned(), ["0_1", "0_2"])
        self.page.uncheck("#cell-0_2-toggle")
        self.assertEqual(self._assigned(), ["0_1"])
        self.page.click("#gen")
        self.assertEqual(self.page.get_attribute("#preview-summary", "data-crops"), "1")
        self.assertIn("SNCA_ab222_WB.png", self.page.text_content("#preview-summary"))
        self.assertEqual(self._errors(), [])

    def test_an_old_session_resumes_and_saves_back_unchanged(self):
        """A grid saved before the typed boxes: no ihc keys, and splits that
        are not whole pixels. Opening it and saving it again must not round
        them, and the preview must cut the panel it always cut."""
        from pipeline.models import CropperImage
        from pipeline.services.cropper.commit import grid_cells
        self._load(size=(350, 200), panels=3, abs=("ab111",))
        self._save()
        im = CropperImage.objects.using(DB).get()
        old = {"bounds": {"top": 10, "bottom": 190}, "hLines": [],
               "vLines": [[1 / 3, 0.61803]], "bandLeft": [5],
               "bandRight": [346], "abOffset": 0}
        im.grid = old
        im.mapping = {"0_1": {"assigned": True, "ab": 0, "manual": True}}
        im.save(using=DB)
        im.session.antibody_list = "ab111"
        im.session.save(using=DB)

        self.page.goto(f"{self.live_server_url}/pipeline/cropper/?gene=SNCA")
        self.page.wait_for_selector('#sessionpick option[value]:not([value=""])',
                                    state="attached", timeout=20000)
        self.page.select_option("#sessionpick", str(im.session_id))
        self.page.wait_for_function("() => S && S.img && S.scale", timeout=20000)
        drawn = self.page.evaluate(
            "cells().map(c => [c.key, c.rect.l, c.rect.t, c.rect.r, c.rect.bot])")
        expected = [list(c) for c in grid_cells(old)]
        self.assertEqual(len(drawn), len(expected))
        for d, e in zip(drawn, expected):
            self.assertEqual(d[0], e[0])
            for a, b in zip(d[1:], e[1:]):
                self.assertAlmostEqual(a, b, places=6)
        self.page.click("#gen")
        self.assertEqual(self.page.get_attribute("#preview-summary", "data-crops"), "1")
        self.assertIn("SNCA_ab111_WB.png", self.page.text_content("#preview-summary"))

        # Selecting a line by its box and leaving it untouched moves nothing.
        self.page.focus("#line-row1-split1")
        self.page.focus("#cellline")
        self.page.click("#save")
        self.page.wait_for_function(
            "document.querySelector('#savestatus').textContent.includes('saved')",
            timeout=20000)
        im.refresh_from_db()
        for key in GEOMETRY:
            self.assertEqual(im.grid[key], old[key], key)
        self.assertEqual(self._errors(), [])

    def test_a_line_is_let_go_and_an_edge_can_carry_its_splits(self):
        """Review fixes: a stale selection must not take the arrow keys, an
        edge that meets a split says so and has a way through, and a new
        session leaves no live boxes for a figure that is gone."""
        self._load()                                   # splits at 100/200/300
        x, y = self._screen(200, 150)
        self.page.mouse.click(x + 4, y)                # select split 2
        self.page.mouse.click(*self._screen(200, 400))  # below the figure
        self.page.keyboard.press("ArrowRight")
        self.assertEqual(self.page.input_value("#line-row1-split2"), "200")
        # Alt+Left is the browser's Back, never a nudge.
        self.page.mouse.click(x + 4, y)
        self.page.keyboard.press("Alt+ArrowLeft")
        self.assertEqual(self.page.input_value("#line-row1-split2"), "200")
        # A typed edge past a split is refused with the way through.
        self._type("#line-row1-left", 150)
        self.assertIn("Move that split first",
                      self.page.text_content("#line-row1-left-why"))
        self._type("#line-row1-left", 0)
        # Dragged past it, the edge stops and says why.
        lx, ly = self._screen(0, 150)
        self.page.mouse.move(lx, ly)
        self.page.mouse.down()
        self.page.mouse.move(*self._screen(150, 150), steps=5)
        self.page.mouse.up()
        self.assertEqual(self.page.input_value("#line-row1-left"), "99")
        self.assertIn("stopped at 99", self.page.text_content("#line-row1-left-why"))
        # With Shift the splits ride along, keeping their share of the row.
        lx, ly = self._screen(99, 150)
        self.page.keyboard.down("Shift")
        self.page.mouse.move(lx, ly)
        self.page.mouse.down()
        self.page.mouse.move(*self._screen(160, 150), steps=5)
        self.page.mouse.up()
        self.page.keyboard.up("Shift")
        left = int(self.page.input_value("#line-row1-left"))
        self.assertGreater(left, 140)
        self.assertGreater(int(self.page.input_value("#line-row1-split1")), left)
        # Space splits evenly re-spaces them between the edges.
        self.page.click("#line-row1-even")
        L, R = (self.page.evaluate(f"S.band{s}[0]") for s in ("Left", "Right"))
        xs = self.page.evaluate("cells().map(c => c.rect.l)")
        for k, x0 in enumerate(xs):
            self.assertAlmostEqual(x0, L + (R - L) * k / 4, places=6)
        self.assertEqual(self._assigned(), [])
        # A new session leaves no boxes, summary or Copy button behind.
        self.page.click("#newsession")
        if self.page.is_visible("#new-drop"):
            self.page.click("#new-drop")
        self.assertEqual(self.page.locator("#line-row1-left").count(), 0)
        self.assertEqual(self.page.text_content("#line-summary"), "")
        self.assertTrue(self.page.is_disabled("#line-copy"))
        self.assertEqual(self._errors(), [])

    def test_the_start_number_takes_two_digits_and_renumbers_every_panel(self):
        """TP53 run, 26 Sep 2026: typing 17 arrived as 71 (the box redrew on
        each keystroke and the caret jumped to the front), and a panel whose
        antibody had been picked by hand kept it when the start moved. Both
        map the wrong antibody with nothing on the page to say so."""
        self._load(panels=4, abs=[f"ab{100 + i}" for i in range(20)])
        self.page.click("#celllist-open")
        self.page.click("#cellrow-0-all")
        self.page.select_option("#cell-0_1-ab", "5")
        self.page.click("#aboff")
        self.page.keyboard.press("Control+A")
        self.page.keyboard.type("17")
        self.page.keyboard.press("Enter")
        self.assertEqual(self.page.input_value("#aboff"), "17")
        self.assertEqual(self.page.evaluate(
            "['0_0','0_1','0_2','0_3'].map(k => S.cellState[k].ab)"), [16, 17, 18, 19])
        self.assertEqual(self._errors(), [])

    def test_a_figure_s_reading_order_is_typed_as_a_list(self):
        """For a figure whose antibodies skip about in box 3's list: one line
        per assigned panel, an empty line for none, and a name the list does
        not hold refused by name rather than dropped."""
        self._load(panels=4, abs=("ab111", "ab222", "ab333", "ab444"))
        self.page.click("#celllist-open")
        self.page.click("#cellrow-0-all")
        names = self.page.evaluate("abList()")
        self.page.fill("#aborder", "zzz9")
        self.page.click("#aborder-apply")
        self.assertIn("not in box 3's list", self.page.text_content("#aborder-why"))
        self.page.fill("#aborder", f"{names[3]}\n\n{names[1]}")
        self.page.click("#aborder-apply")
        self.assertEqual(self.page.evaluate(
            "['0_0','0_1','0_2','0_3'].map(k => S.cellState[k].ab)"), [3, None, 1, None])
        self.assertIn("3 lines for 4 assigned panels",
                      self.page.text_content("#aborder-why"))
        self.assertEqual(self._errors(), [])

    def test_the_status_says_where_things_stand_in_words(self):
        self._load(panels=4, abs=("ab111", "ab222", "ab333", "ab444"))
        self.page.click("#celllist-open")
        self.page.click("#cellrow-0-all")
        text = self.page.text_content("#cropper-status")
        self.assertIn("Figure 1 of 1", text)
        self.assertIn("Box 5: 4 of 4 panels assigned · start 1", text)
        self.assertIn("Session: 1 figure, 4 panels assigned", text)
