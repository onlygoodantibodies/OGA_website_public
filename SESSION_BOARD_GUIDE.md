# The sessions board — a guide

Every experiment session, across every site, on one page. Find one, change it,
open its results and change those — without walking through three screens to get
there.

Where it lives: **<https://onlygoodantibodies.co.uk/pipeline/sessions/board/>**
(sign in first; it is also a tile on `/pipeline/start/`).

**You do not have to work in the browser.** Download the sessions you are looking
at, edit them in Excel, upload the file back. That path is supported
permanently, not a stepping stone.

---

## The shape of a session

A session is *one application, on one gene, on one day, by one person*. "I ran a
WB screen on SOD1 on Tuesday" is one session. Inside it there is one **result
row per antibody** tested.

That two-level shape is why the board looks the way it does:

- **A row is a session.** Gene, application, date, who ran it, which site, its
  status, which WT and KO lines it compared.
- **The results live inside.** Click the results count — "6 results" — and the
  per-antibody rows open underneath.

**The count says rows and readings, because they are different numbers.**
Planning a session writes one blank row per antibody it will test, so a session
nobody has run yet reads *"22 antibodies · no readings recorded"* rather than
"22 results". A blank row is not a claim that the work was not done — gaps in a
results section are normal, and what settles whether a target is finished is a
published report — so the column counts what is written down and concludes
nothing from the rest.

The result columns are different for each application. Western blot records
exposure time and band pattern; immunofluorescence records microscope,
objective, plate and well; flow cytometry records median fluorescence. There are
16 columns for WB, 24 for IP, 24 for IF and 8 for FC, and almost nothing in
common between WB and IF. That is why results open per session rather than all
sitting in one flat sheet — a single grid would be mostly empty cells.

---

## Editing

Click any cell with a dotted edit box. Type. Press **Enter**, or click away, and
it is saved. **Escape** cancels.

There is no Save button and no draft state. If a save fails you get a red banner
saying why, and **the cell goes back to what it held** — nothing on screen ever
shows a value the database does not have.

### What you can change

Date, experimenter, site, status, the WT and KO lines, protein loading, and
comments. Plus any result field, once you have opened a session's results.

Experimenter, site and cell line are matched **by name, exactly**. Type
`McGill`, not `mcgill university`. If the name is not recognised the save is
refused and the banner names what it could not find — better than quietly
blanking the field.

### What you cannot change, and why

**Gene and application.** They are what the session *is*. Every result row
inside a session belongs to that gene and that application, and the result table
itself depends on which application it is. If either is wrong, the session was
created against the wrong target — delete it and make the right one, rather than
retyping the label on the wrong thing.

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

## Status

| Status | Means |
|---|---|
| **Planned** | Set up, not run yet. |
| **In progress** | Being run, or partly recorded. |
| **Complete** | Done and recorded. |
| **Failed** | Ran, did not work. Kept as a record. |
| **Repeat needed** | Ran, needs doing again. |
| **Cancelled** | Abandoned. Hidden from the board by default. |

Cancelled sessions are hidden unless you tick **Include cancelled**, or filter
for that status specifically. They are not deleted — an abandoned experiment is
part of the record.

---

## Working in Excel

1. **Download** gives you the sessions currently on screen, filtered exactly as
   you have filtered them. One sheet per application: WB, IP, IF, FC.
2. Edit it.
3. **Upload a sheet → Preview**. Nothing is written yet. You get a count of what
   would change, and a list of anything that disagrees with the database.
4. **Save these changes** applies it.

### Five rules that make this safe

1. **It only edits.** Nothing is created and nothing is deleted, ever. Adding a
   session is a separate, deliberate act.
2. **Rows are matched on `session_id` and `result_id`.** Those two columns are
   marked *do not edit* — they are how the file finds its way back to the right
   record. Change anything else and the row still lands correctly.
3. **A blank cell is ignored.** Clearing a cell in Excel never clears the field
   in the database. To empty something, do it on the board.
4. **A blank field fills freely; a filled one does not.** If the sheet disagrees
   with a value that is already there, it is listed in the preview and **left
   alone** unless you tick **Update existing values**.
5. **Re-uploading the same file changes nothing.** If the upload times out, just
   do it again — a second run of the same sheet is a no-op.

### The `session_` prefix

Session-level columns are named `session_date`, `session_status`,
`session_comments`, and so on. The prefix is not decoration: every result row
has its own `comments` too, separate from the session's. Two columns both called
"comments" is exactly how a session comment gets silently overwritten by an empty
result comment, so they are kept apart by name.

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

Search matches gene, protein name and session comments. **Gene** is an exact
match, for when search is too broad. The rest — application, site, experimenter,
status, a date range — narrow the list. **Apply** reloads.

Whatever is filtered is what **Download** gives you. The two cannot disagree.

One consequence worth knowing: if an edit moves a row out of the filter you are
looking at — you are filtering for Leicester and you change a session's site to
McGill — the row leaves the screen. It has not been deleted; it no longer matches
what you asked to see.

---

## What the board does not do

- **Adding an antibody to a session that already exists.** The grid edits result
  rows; it does not create them. Open the session's row and download **its own
  workbook** — a row for an antibody the session does not have yet is created when
  you upload it back. The Excel round-trip above is the same rule from the other
  side: a row with no `result_id` says so in the preview, and names the session to
  add the antibody to.
- **Deleting a single result row.** There is no surface for it. A whole session
  can be deleted, from its row, while the board is narrowed to one gene — and the
  panel lists everything that goes with it first.
- **Figures.** Cropping characterisation images is the cropper's job — **Publish
  figures** in Browse. Cropping does not publish: the crops wait in the **Review
  queue**, and releasing them there is what puts them on the public website.

---

## Adding new ones

**New session** opens a panel on this page — you no longer go somewhere else to add
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
press the create button, and the check tells you which antibodies are already on file and which would be added. A session also needs its header — gene, application, date, site, experimenter and the two cell lines — which sits above the table. Anything new, including the gene and the cell lines, is created with the session in one go, so a bad header creates nothing at all.

The table's columns are the same columns as the downloadable Excel template, in
the same order — so a file that works in one works in the other.

---

## If something looks wrong

Tell Harvinder. Worth including: which gene and which session, what you
expected, and what the red banner said if there was one. This page and the
emailed copy are the same file, so a correction lands in both.
