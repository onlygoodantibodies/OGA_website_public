# The antibodies board — a guide

Every antibody the consortium holds, on one page. Search it, edit it, and mark
what each one is actually good for.

Where it lives: **<https://onlygoodantibodies.co.uk/pipeline/antibodies/board/>**
(sign in first; it is also a tile on `/pipeline/start/`).

Everything about an antibody happens here: finding one, editing it, adding new
ones, and the round trip through Excel. There is no separate search page, detail
page, edit form or paste page — they were four screens for one job, and they are
one grid now.

---

## Editing

Click any cell with a dotted edit box. Type. Press **Enter**, or click away, and
it is saved. **Escape** cancels.

If a save fails you get a red banner saying why, and **the cell goes back to what
it held**. Nothing on screen ever shows a value the database does not have.

**Some cells give you a list rather than a blank box**, and there are two kinds:

- **A dropdown** where the field has a fixed set of answers — clonality, site
  and storage. Anything else would be refused on save, so it is not offered.
- **A type-ahead** where the field is a convention rather than a rule — host,
  isotype and reactivity. It offers what the column already holds, so you can
  pick the spelling everyone else is using, and you can still type something
  new. A vocabulary nobody may add to stops describing the bench.

The type-ahead lists are built from the database each time the page loads, and
they fold case: if the column holds both `rabbit` and `Rabbit`, you are offered
only the one already more used. Nothing is rewritten — picking from the list is
just how the split stops getting wider.

Site is matched **by name, exactly**. If the name is not recognised the save is
refused and the banner says so, rather than quietly emptying the field.

---

## The two columns that matter most

The board puts **OGA recommends** and **Supplier claims** side by side, as
clickable ticks. That adjacency is the point.

| Column | What it means |
|---|---|
| **OGA recommends** | What *we* found the antibody works for, from knockout-controlled testing. Our verdict. |
| **Supplier claims** | What the *catalogue* says it works for. Their claim. |

They are recorded separately because they frequently disagree, and the whole
purpose of YCharOS is to be able to say so. A supplier claiming WB, IP, IF and FC
while our testing recommends none of them is not a data-entry error — it is a
finding, and the board should show it plainly.

Click any application — WB, IP, IF, FC — in either column to toggle it.

---

## What you cannot change, and why

**Catalogue number, supplier and gene.** These three are the antibody — the
product you can order. The same catalogue number from a different supplier is a
different antibody.

Retyping one part of that in a grid cell would silently turn the row into a
different antibody — while every result, every figure and every recommendation
already recorded against it stayed attached. So the grid refuses it.

**Click the catalogue number** to change them deliberately. The dialog names what
is already attached to the row *before* you type, and refuses a change that would
collide with a vial already on file — so the consequence is visible at the moment
you make it, which is the whole difference between this and a grid cell.

**Lot and site are not identity — they say which vial was tested,** and you can
edit both here. A row is one vial of one product: the same catalogue number
tested at two sites, or from two lots, is two rows, because a different lot of
the same antibody can behave differently and that is worth recording separately.

**RRID.** The Research Resource Identifier is stored as a bare `AB_` number plus
a link to the registry, and the two are written together. Typing either half by
hand is how they end up pointing at different things, so **the board cell** shows
it as a link only and cannot be edited. (Giving one when you *add* an antibody is
fine and encouraged — see below.)

A new antibody added on the board starts with **no RRID** — the column shows `—`,
and that is expected. Looking one up means calling the Antibody Registry, which
is slow and often wrong about clonality, so it is not done while you wait.

**If you already know the RRID, type it into the `rrid` column** of the Add
antibodies grid, or of your paste or sheet, and it is kept. That column is there
on purpose; it is only the inline cell on the board that is closed, so that the
number and its link are always written as a pair.

If you do not, the RRID stays empty until someone runs the registry backfill
(`python manage.py backfill_rrid_from_registry`). **It is a hand-run command, not
a scheduled job**, so an antibody added today has no RRID tomorrow unless
somebody typed one or ran it.

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

## Filters

- **Search** matches catalogue number, RRID, clone ID, gene and supplier — so a
  single box handles "find me 12A8", "find me everything from Proteintech" and
  "find me the SOD1 antibodies".
- **Gene** is an exact match, for when search is too broad.
- **Recommended for** narrows to antibodies we recommend for one application.
- **OGA recommended** separates "we have a positive verdict" from "we do not" —
  useful for finding the gaps.
- **Lab number** finds the rows with no A-number yet, or the ones that have one.
  This is how you get back to a batch you have just logged.

An edit can move a row out of the filter you are looking at. Filter for
Leicester, change a row's site to McGill, and the row leaves the screen. It has
not been deleted; it no longer matches what you asked to see.

---

## Getting antibodies in and out

- **Download** gives you the rows on the screen — *the grid's own filters*, so a
  board narrowed to one gene downloads that gene and not the whole consortium.
- **Upload** takes the same file back. You get the same per-row preview a paste
  gives, and nothing is written until you say so.
- **Downloads & uploads** (in Browse) is still there for the whole-dataset
  round-trip: every antibody, cell line, target and report in one file, as Excel
  *or* JSON. Same safety rules everywhere — a blank never clears a field, and
  overwriting something already filled needs confirmation.

### Leave the site column alone

The download has a **site** column, and it matters more than it looks. A row is
one *vial*, and site is half of what makes it that vial, so the column is how the
upload knows to update McGill's row rather than make you a Leicester copy of it.

Leave a cell **blank** and the row is recorded at your own site — right for
reagents you are entering for your own bench. Put a name in and it is recorded at
that site; a name that is not a site on file is **refused by name**, with the
sites that do exist listed, rather than being quietly filed under yours.

The preview says which site every row would land at, and the summary lists them —
so if a sheet spans two benches you can see that before saving.

---

## Isotype and reactivity

**Isotype** sits under Clonality, with the clone ID — `IgG`, `IgG1`, `IgG2a` and
so on. **Reactivity (supplier)** is its own column beside Supplier recommends,
and that placement is the point: it is what the *datasheet* says the antibody
cross-reacts with, not something OGA tested. The lab writes it as initials —
`H, M, R` for human, mouse, rat.

Both have been in the database since the Access import — about 2,800 antibodies
carry each — and until now neither was on any screen or in any sheet. If you
kept them in a spreadsheet of your own because the portal appeared not to have
them, the columns will now upload.

---

## Lot numbers are worth filling in

An antibody that worked in one lot and failed in the next is one of the more
common ways a published result stops reproducing. The lot column is the only
place that gets recorded. If you know the lot on the tube, put it in.

---

## Where the vial is, and when it turned up

**Storage**, **Box** and **Received** are the last three columns before Comments.
They are editable in the grid and they are in the download and the upload, so a
sheet can carry them either way.

**Storage** is the freezer: `4C`, `-20`, `-80`, `LN2` or `RT`. It takes what
people actually write — `(-20°C)`, `-20C`, `fridge` — and refuses anything it
cannot place, listing what it takes.

**Box** is free text. Whatever is written on the box: a number, a name, your own
scheme. Set a storage temperature as well, or the record says which box with no
freezer to look in — and the board will say so if you try to remove one and leave
the other.

**A vial can be recorded in two places** — a −20 °C working aliquot and a −80 °C
backup is the ordinary reason. Both are shown, and those two cells are then
read-only, because one cell cannot say which of the two you meant. Django admin
edits them individually.

**Received takes as much of the date as you actually know.** A full date
(`2026-08-14`), a month (`Aug 2026`, `August 2026`, `2026-08`) or a year. A month
stays a month: nothing turns it into the 1st, and the column prints `Aug 2026`.
Download it and you get `2026-08` back, which is what makes the round trip safe.

A slashed date whose two numbers could each be the month — `01/02/2026` — is
**refused**, because it is 1 February to some of us and 2 January to others and
nothing in the cell says which. Write it as `2026-02-01` or `1 Feb 2026`.

---

## In kind or purchased

The **Acquisition** column, next to Received, says whether the supplier
contributed the vial **in kind** or the lab **purchased** it — the distinction
that matters when a top-cited antibody has to be bought to finish a study.

It is a dropdown with three answers: *In Kind*, *Purchased* and *Unknown*.
Anything else is refused, so the column stays countable.

This was already recorded for effectively every antibody on file — it came
across with the Access import years ago and had simply never been shown on a
screen or carried in a sheet. So expect to find it mostly filled in already,
and to be correcting it rather than starting from nothing.

In the download and the template the column is called **acquisition** and holds
`in_kind`, `purchased` or `unknown`. Typing *In Kind* is read the same way, so
a sheet edited from what the board displays still comes back correctly.

---

## Assigning and rearranging A-numbers

**Adding an antibody no longer gives it a number.** That is deliberate: the
A-number decides which freezer box a vial goes in, and reagents for one protein
arrive over weeks — numbering each one as it turns up scatters that protein
across as many boxes as it had deliveries. So the number waits until you are
ready to deal a set out.

Two things follow. The board tells you how many of your bench's antibodies have
no number yet, and the link takes you to them. And **planning a session gives a
number to anything on it that still has none** — the bench sheet prints the
`ab #` column, and a blank there is a tube nobody can identify.

**If your bench numbers a vial the day it arrives, tick "Give these an A-number
now"** on the Add panel, on a gene's own page, or on the Upload panel. That is
the way it always worked, and it still works — the box is just off unless you
ask for it.

You can still type a number in the **ab #** column at any point, on the board or
in a sheet. A number you typed always wins, tick or no tick.

**Assign numbers** is the button beside Upload a sheet — and there is one on
each gene's own page too, which does the same job for that gene's antibodies at
your bench. It works on **every row
the filters above are showing**, in the order they are shown — gene, then
supplier, then catalogue — so numbering them as shown puts each protein's
antibodies together. That is the point of it: if the number decides which box a
vial goes in, numbering in order of arrival scatters one protein across five
boxes.

It is two presses. **Check these** writes nothing and draws every old → new pair,
including the rows that would not move. **Change the numbers** then writes the
whole set in one go.

**Leave "First number" blank** to carry on from your bench's highest. **Type
one** to re-deal a block you have already used — the numbers move together, so
two vials are never both holding the same one at any point. That is why this is a
panel and not the number cell: swapping A-1 and A-2 by retyping cells is
impossible, because whichever you type first collides with the one still holding
it.

**Some rows keep the number they have, and the preview names each one.** An
antibody with a reading recorded against it, a published figure, or a crop
waiting for review has had its number leave the app — it is on a bench sheet or
under a figure — and moving it could file a later reading against a different
antibody. Their numbers are also kept out of the way, so nothing else lands on
one.

**The app cannot see your freezer.** Only do this while the numbers are not yet
written on the tubes. Nothing in the portal knows whether they are.

If a number you are aiming at is already held by an antibody that is *not* in the
filter, the whole batch is refused and the message names the record holding it.
Start higher, or widen the filter so that row moves too. Two sites at once is
refused as well — McGill's A-1 and Leicester's A-1 are different antibodies.

**Your own bench's antibodies**, unless you are a superuser — the same rule as
deleting. These numbers are written on somebody's freezer boxes.

---

## Adding new ones

**Add antibodies** opens a panel on this page — you no longer go somewhere else to add
things and then come back to look at them.

It has two tabs, because there are two ways data arrives:

- **Table** — an empty grid with the right headings, for typing deliberately. It
  works like a spreadsheet: **Tab** and the **arrow keys** move between cells,
  **Enter** drops **down one row in the same column** and adds a row when you
  reach the bottom, **clicking a cell and typing replaces** what is in it (click a
  second time inside the same cell if you want to edit part of it), and if you
  copy a block of cells out of Excel and paste, it **fills the grid** rather than
  dumping everything into one box. There are more columns than fit — the table
  scrolls sideways, and the note under it says how many.
- **Paste** — a plain text box, for a list that already exists in an email, a
  supplier page or a spreadsheet. One row per line, columns separated by tabs.

Either way, **Check these** first. Nothing is written until you press it and then
press the create button, and the check tells you which rows are new, which already exist and will be updated instead of duplicated, and which name a gene that isn't a target yet.

Each checked row is named by its **whole identity — catalogue · supplier · gene ·
lot · site** — and a row that will be updated says which vial it matched. That
matters when two rows differ only in their lot: one is a new vial and one is an
update, and naming them by catalogue and gene alone would print the two of them
identically.

The table's columns are the same columns as the downloadable Excel template, in
the same order — so a file that works in one works in the other.

---

## Deleting

**A row can only be deleted while the board is narrowed to one gene.** Put a
gene in the **Gene** box and press Apply, and a delete appears on each row;
clear it and they go again. That is deliberate — a control on every row is a
control beside every record nobody was thinking about — and the board says so
where the buttons would be.

**You get a list before anything goes.** The panel names every record that will
be destroyed with it, and every record that will be left pointing at nothing,
and the button carries the number it just showed you. If that number has changed
by the time you press it, the delete is refused rather than done — so a second
person adding a session while you are reading cannot widen what you agreed to.

You can delete your own site's records; a superuser can delete anything. Someone
else's row refuses by name and says whose it is.

---

## If something looks wrong

Tell Harvinder. Worth including: the catalogue number and supplier, what you
expected, and what the red banner said if there was one. This page and the
emailed copy are the same file, so a correction lands in both.
