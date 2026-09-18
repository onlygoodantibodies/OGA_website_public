# The target board — a guide

The consortium's target list: every gene being pursued, at every site, on one
page. It holds what the master target spreadsheet holds, in the same columns,
and works several of them out from the records rather than asking anybody to
type them.

**Nobody has to stop using Excel.** Download the list, edit it in Excel, upload
it back. That path is supported permanently, not as a stepping stone.

Where it lives: **<https://onlygoodantibodies.co.uk/pipeline/targets/board/>**
(sign in first, then it is also the first tile on `/pipeline/start/`).

---

## What is where

| Where | What it is for |
|---|---|
| **Target board** | The list itself. Every target, every site. Edit anything by clicking it. |
| **Add targets** | A panel on the board, for putting genes on a site's list. It checks each one first — is another site already doing this, can a knockout line simply be bought — then adds the ones you keep. |
| **Add a gene** | The other door, in Browse. Look a gene up first — expression, knockout availability, how many antibodies exist — and add it from the same page once you have decided. Both doors write the same target and the same nomination. |
| **Portfolio** | For grant writing: how many targets are completed, broken down by protein class, site and funder. |

---

## Cells that offer what they will accept

Some cells give you a list rather than a blank box, and there are two kinds:

- **A dropdown** where the field has a fixed set of answers. Anything else would
  be refused when you save, so it is not offered.
- **A type-ahead** where the field is a convention rather than a rule. It offers
  what the column already holds, so you can pick the spelling everyone else is
  using — and you can still type something new, because a vocabulary nobody may
  add to stops describing the bench.

The type-ahead lists are built from the database each time the page loads, and
they fold case: where a column holds both `rabbit` and `Rabbit` you are offered
only the one already more used. Nothing is rewritten — picking from the list is
just how the split stops getting wider. A column with hundreds of different
values gets no list at all; that is free text, and a menu of hundreds is a
scroll rather than a reminder.

---

## Getting around

**The search box in the top bar covers the whole pipeline.** Type a gene name and
you land on that gene's page — everything recorded for it, and a strip across the
top saying where it has got to and what the next step is. Type a catalogue
number, an RRID, a cell line name or a clone and you get a page listing wherever
that string appears, with a link into the right board.

**A gene follows you between boards.** Once you are filtered to one gene, the
**Browse** menu keeps it: go from that gene's antibodies to its cell lines to its
sessions without typing the name again. All four boards use the same **Gene**
filter, and it is an exact match — `STMN2` does not also bring back `STMN1`. The
Search box next to it is the fuzzy one, for when you are not sure.

**Browse holds every page in the pipeline**, not only the boards: the Overview,
the Portfolio, Set recommendations, Publish figures, the Review queue,
Downloads & uploads, Add a gene and How it all works are all in there. Nothing is reachable from the task
hub alone.

**The grid shows a page at a time.** Fifty rows, with a pager underneath. The
count above the table is the **whole filtered set**, not what is on screen — so
a count of several thousand over fifty drawn rows means the filters matched
several thousand, not that something went wrong. A download is never paginated:
it carries the grid's filters and every row they match, whatever page you are
looking at. `?per_page=` in the address bar takes up to 500 if you want a longer
page.

---

## Working in Excel

1. **Download ▾ → Original layout** gives you columns A–T exactly as they are in
   the master spreadsheet, with the headers on row 5.
   (**Full sheet** gives the same plus the columns the app works out — which
   sites are pursuing each target, protein class, whether it is completed, and
   which applications have been run where.)
2. Edit it in Excel and save.
3. **Upload a sheet → Preview changes.** Nothing is saved at this point. You see
   how many targets and nominations would be created, and a list of anything in
   the file that disagrees with what the database already holds.
4. **Save these changes** when you are happy.

The master spreadsheet works as-is — its filters, its hidden rows and its
free-text dates all import without anybody tidying them up first.

### Four rules that make this safe

- **An empty cell never erases anything.** Clearing a cell in Excel does not
  clear it in the database.
- **Uploading the same file twice changes nothing the second time.**
- **Nothing is ever deleted by an upload.** Rows you delete from the spreadsheet
  are left alone, not removed.
- **Values that are already filled in are not overwritten** unless you tick
  *Update existing values*. Without the tick, an upload only fills blanks.

So the worst outcome of a mistaken upload is that something you meant to change
did not change — never that something was lost.

### The one thing an upload decides for you

If a row in your sheet has **no site column**, the app has to put the nomination
somewhere. The **"If a row has no site, treat it as"** box in the upload dialog is
what it uses, and it arrives set to **your own site**. Change it there if the sheet
belongs to somebody else, or set it to *"leave the site blank"* if you would rather
allocate afterwards on the board. Rows that *do* name a site always keep it.

It is a list of the real sites, so a name that is not one of them cannot be
saved by mistake.

---

## What is different from the spreadsheet

### It shows every site, not just McGill

This is the main reason for the change. Leicester has published MMP7 and
SERPINA1 and submitted PKN2, GNAQ and NR3C1; none of that appears in the
spreadsheet. Any target on more than one site's list is flagged **duplicate** on
the board and listed at the top of the Portfolio page.

### It warns you before you create a duplicate

**Add targets** takes a list of genes — typed a row at a time, or pasted — and
checks each one before anything is written, telling you:

- whether it is already on the list, and at which site;
- whether it has already been published;
- **who has done the rest of its family** — so a Rab nominated at Leicester comes
  back with *"McGill has 60 RAB targets (14 published) — they may be the right
  site to run this one"*;
- whether an off-the-shelf knockout line is already in the Horizon catalogue.

**Adding a gene records two things, not one.** It creates the target *and* a
nomination, marked **not funded** — because looking a gene up and adding it means
some site is pursuing it, and nobody has said it is funded. **Which site** is a
box on the panel, and it arrives set to your own: a coordinator can put a gene on
another bench's list without signing in as them, and an unrecognised site is
refused by name rather than quietly swapped for yours. The confirmation says what
was written. Both records are editable on the board straight away: click the
**Sites** cell to move it, click **Funded** to change it.

### Some columns fill themselves in

Protein name, alternative name, UniProt ID and molecular mass come from UniProt.
If the sheet's value differs from UniProt's, the upload **says so and leaves the
sheet's value alone** — it never silently overwrites.

### Status and Conclusion are notes now

The board works from two facts — *funded* and *completed* — and nothing else is
workflow state. The spreadsheet's Status and Conclusion text is still carried
through the file untouched, but nothing depends on it.

- **Funded** — set per nomination. Clicking the cell writes immediately; there is
  no confirmation step, and clicking it again puts it back.
- **Completed** — worked out for you: a target is completed once it has a Zenodo
  DOI or a published F1000 date. There is nothing to tick.
- **Applications** — which of WB / IP / IF / FC has actually been run, and at
  which site, taken from real sessions rather than a typed status. Many targets
  will only ever have some of the four, and the board shows that honestly.

### One gene can be on the list twice

SYNGAP1, ADNP, DYRK1A, STXBP1, ARID1B and TSC1 each appear twice in the imported
list — funded under one project and on the autism wish list under another. Both
are kept: one **target**, two **nominations**. There is nothing to tidy up here,
and unfunded nominations are exactly how a watch list is recorded.

### Protein classes, for grant writing

The Portfolio page counts completed targets by protein class — GPCRs, secreted,
nuclear, endosomal, kinases and so on — derived from UniProt keywords and GO
terms. Any target can be tagged by hand if the automatic ones miss something.

---

## What the board does not hold

The knockout-sourcing block (the Academia / Abcam / Horizon / Custom columns) and
the Zenodo view and download counts are not on the board — that was a deliberate
scope decision. The columns kept are A–T.

One piece of it survives in a different form. An unfunded wish list earns its keep
when a knockout line becomes available, so the board watches the Horizon catalogue
and **Add targets** says when a line already exists — the signal, without the
columns to maintain.

Bringing the sourcing columns back is blocked on one thing: nobody has decoded the
colour legend in the old sheet. The fill colours carry information no cell value
holds, and 37 rows record "we have this line in the lab" as a yellow fill and
nothing else. If you know what the colours meant, that is the missing piece.

**Deleting a gene**, too — and that is on purpose. It is done from the gene's own
page, not from a row here. Opening one gene's page is already the deliberate act;
a delete on every row of a 585-gene list is a delete beside every gene nobody was
thinking about. The panel there names everything that goes with it — its
antibodies, their results, their published figures — before anything happens, and
sends you back to this board afterwards saying what went.

---

## If something looks wrong

Nothing done in the spreadsheet can damage the database: every upload is previewed
first, and no upload deletes anything. If a preview shows something unexpected,
close it without saving and speak to Harvinder.

One thing that looks like a gap and is not: a target with **no nomination** shows
a dash in the **Funded** column rather than "not funded". Nothing has said which
site is pursuing it, so there is nothing to be funded — "not funded" would be a
claim nobody has made. The Portfolio says how many are in that state and links
straight to them.
