# The cell lines board — a guide

Every wild-type and knockout line the consortium holds, on one page. Search it,
edit it, and record what has actually arrived, been thawed, and been validated.

Where it lives: **<https://onlygoodantibodies.co.uk/pipeline/cell-lines/board/>**
(sign in first; it is also a tile on `/pipeline/start/`).

Everything about a cell line happens here: finding one, editing it, adding new
ones, and the round trip through Excel. There is no separate list page, detail
page or edit form — they were three screens for one job, and they are one grid
now.

---

## Editing

Click any cell with a dotted edit box. Type. Press **Enter**, or click away, and
it is saved. **Escape** cancels.

If a save fails you get a red banner saying why, and **the cell goes back to what
it held**. Site is matched by name, exactly; an unrecognised name is refused
rather than silently emptying the field.

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

## How the line grows

The **Growth** column, next to Medium, says whether the line is **adherent** or
grows in **suspension** — the thing you need to know before you thaw a vial, and
the reason it was asked for: a student looking a line up had nothing on the
screen telling them.

It is a type-ahead, not a fixed list. Adherent and suspension are what nearly
every row says, and a line somebody wants to describe more precisely can be.
It is in the download and in the blank template as **growth properties**, so it
goes out and comes back with everything else.

Blank cells read *adherent? suspension?* — that is a prompt, not a value. Many
rows have it recorded already; it came across with the Access import and, until
now, was in no column on any screen.

The **Species** column beside it says which animal the line came from. Nearly
every line on file is human — 604 of 616 — with a handful of mouse, rat, monkey
and dog. That is the reason it is drawn rather than assumed: if you never see
the column, you cannot spot the ones that are not human. Left blank on a new
row it is recorded as Human.

---

## The clone

**A gene and a background say what was knocked out and in what. The clone says
which one.** Two clones of one knockout are two different single-cell lines,
often made with different guides — so they are two rows here, not one row with a
note, and the CLONE column is what tells them apart.

Where a knockout has siblings the cell says so: *clone 2.3 · 1 of 7 clones*. A
knockout whose clone was never written down says **that**, rather than showing an
empty cell somebody would read as a filing error.

Two things follow, and both are deliberate:

- **Filtering to a gene can show several rows with the same name.** `HCT116`
  three times under ACSL5 is three clones, not one line listed three times —
  read the CLONE column.
- **Adding a knockout that already has clones on file asks which one this is.**
  It will not guess: it lists the clones already recorded and waits, because
  filling in a different clone's blanks — or hanging this one's vials off it — is
  how the clones were lost in the first place.

A C-number is a freeze-down batch **of a clone**, so the numbers listed under a
row are that clone's tubes.

---

## What you cannot change, and why

**Name, gene, genotype, clone and parent line.** These five are the structure,
not description.

A knockout line only means something as *this gene, knocked out, in that
parent* — and, where there is more than one, *this clone of it*. Every session
that used the line points at this row, and the entire interpretation of those
results depends on which of the two lines was the WT and which was the KO.
Renaming a line in a grid cell, flipping its genotype, or retyping its clone
would not correct history — it would rewrite it, silently, while every result
already recorded stayed attached.

So the grid refuses those five. **Click the line's name** — or its clone — to change them
deliberately: the dialog tells you what is already attached — how many sessions
used the line, how many knockouts were made from it — *before* you type, and
refuses a change that would leave a knockout without its parent.

**Supplier.** The same dialog. Suppliers go through a single resolver, so the same
vendor never ends up recorded under two spellings — "Horizon", "Horizon
Discovery", "horizon discovery ltd" — and a grid cell would defeat that. The
catalogue number next to it *is* editable in the grid.

---

## The three ticks

| Tick | Means |
|---|---|
| **KO confirmed** | The knockout has actually been confirmed, not just ordered. |
| **Received** | The vial has physically arrived. |
| **Thawed** | It has been brought up and is usable. |

Click any of them to toggle.

**KO confirmed is the one that matters scientifically.** An unconfirmed knockout
line makes every result that used it provisional — if the gene is still being
expressed, a "specific" band in the WT lane proves nothing. The box **underneath
the tick** is for *how* it was confirmed, or why it is not — it now says which,
prompting *"how was it confirmed?"* on a confirmed line and *"why not? add a
reason"* on one that is not. It is worth filling in: "no band by WB with a
validated antibody" and "sequencing confirmed indel" are different strengths of
evidence.

**The badge reads the tick and that box together.** They disagree on 54 of the
lines imported from Access — 6 ticked over a reason recording that the check
*failed*, and 48 the other way round, unticked over a reason reading
*Confirmed*. A green pill above the words "Failed-Decreased protein level" is
the worse half, so the badge on a row whose two halves disagree reads **check
this row** rather than either verdict, and nothing counts it as confirmed. One
click on the badge settles it.

**Received and thawed** are logistics, and they are the two that go stale
fastest. They are on the board precisely so they can be updated in three seconds
rather than through a form.

---

## Cellosaurus IDs are worth filling in

The Cellosaurus accession is the public identifier for a cell line — it is what
makes the line citable, and what lets someone else obtain the same one. Filling
it in is a small thing that turns an internal record into a reproducible one.
Search matches on it, so it is also the fastest way to find a line.

---

## Filters

- **Search** matches name, C-number, Cellosaurus ID, clone and gene.
- **Genotype** separates WT from KO.
- **KO confirmed** and **Received** are the two that answer "what still needs
  chasing" — filter for *not confirmed* or *not yet received* to get a to-do
  list. *Tick and reason disagree* is the third option, and it is the shortest
  useful list on the board: the rows where the box and the words next to it say
  different things.
- **Site** has a *No site recorded* option, for finding the rows that have no
  bench attached to them at all.

An edit can move a row out of the filter you are looking at. Filter for *not
confirmed*, tick a line as confirmed, and the row leaves the screen. It has not
been deleted; it no longer matches what you asked to see.

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

## WT lines are shared, KO lines are not

A wild-type parental line — HAP1, HeLa — is used across many targets, so it is
recorded once with no gene attached. A knockout belongs to exactly one gene, and
carries both that gene and the parent it came from.

That is why the Gene column is empty for WT lines. It is not missing data. The
row now says so itself — a WT line's Gene cell reads *"no gene — wild type"* —
because from the screen alone an empty cell reads as something to fill in, and
filling it in would create a second, wrong line.

This holds wherever you add a line, including the **Add cell lines** panel on a
gene's own page: leave a WT parent's gene column blank there and it stays blank,
even though every other row on that page belongs to that gene. (Until 2 Aug 2026
it did not — that panel stamped the page's gene onto the parent. If you added a WT
parent from a gene page before then, check its Gene cell.) Typing a gene onto a
*new* WT line is refused, with the same explanation the board gives you.

---

## Getting lines in and out

- **Download** gives you the rows on the screen — *the grid's own filters*, so a
  board narrowed to one gene downloads that gene and not everything.
- **Upload** takes the same file back, with the same per-row preview a paste
  gives. Nothing is written until you say so.
- **Downloads & uploads** (in Browse) is still there for the whole-dataset
  round-trip: cell lines alongside antibodies, targets and reports, as Excel *or*
  JSON. A blank never clears a field, and overwriting something already filled
  needs confirmation.

### Leave the site column alone

The download has a **site** column, and it is how the upload knows to update the
row it came from rather than make you a copy of another site's line at your own
bench.

Leave a cell **blank** and the row is recorded at your site. Put a name in and it
is recorded at that site; a name that is not a site on file is **refused by
name**, with the sites that do exist listed, rather than being quietly filed
under yours. The preview says which site every row would land at.

---

## Adding new ones

**Add cell lines** opens a panel on this page — you no longer go somewhere else to add
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
press the create button, and the check tells you which rows are new, which already exist and will be updated instead of duplicated, and which cannot be placed — usually a KO with no gene, or a row with no name.

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

Tell Harvinder. Worth including: the line name and its C-number, what you
expected, and what the red banner said if there was one. This page and the
emailed copy are the same file, so a correction lands in both.
