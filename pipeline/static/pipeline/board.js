/*
 * The board: a filterable grid you edit in place, download as Excel, and upload
 * back. Written once here so targets, sessions, antibodies and cell lines all
 * behave the same way rather than four times slightly differently.
 *
 * A page supplies what only it knows — the URLs, and how to draw one row — and
 * gets the rest: filtering, loading, inline editing, single-row redraw after a
 * save, and error handling that tells a hang apart from a gateway timeout apart
 * from an HTML error page.
 *
 * Two rules this file exists to enforce:
 *
 *   - A save redraws ONE row, from the row the save itself returned. Refetching
 *     the whole grid after every cell edit is what made the target board slow,
 *     and it scales with the dataset rather than the edit.
 *   - Nothing on screen may imply a save that did not happen. A failed edit puts
 *     the cell back to the value it held and says why, in the page.
 *
 * The CSRF token is always rendered server-side and passed in. It cannot be read
 * from document.cookie: CSRF_COOKIE_HTTPONLY is on, so the cookie is invisible to
 * JavaScript and every POST comes back 403.
 */
window.OGABoard = (function () {
  'use strict';

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  const TONES = {
    good: 'bg-emerald-100 text-emerald-800',
    warn: 'bg-amber-100 text-amber-800',
    flat: 'bg-gray-100 text-gray-700',
    cool: 'bg-ycharos-100 text-ycharos-900',
    bad:  'bg-red-100 text-red-800',
  };

  function pill(text, tone) {
    return `<span class="inline-block rounded-full px-2 py-0.5 text-xs font-semibold ${
      TONES[tone] || TONES.flat}">${esc(text)}</span>`;
  }

  /* Two placeholders, and the difference matters.
   *
   * DASH is markup, for a read-only cell drawn straight into innerHTML. EMPTY is
   * the same em dash as plain text, for anywhere it will be escaped or assigned
   * to textContent. Passing DASH into an escaping context printed
   * `<span class="text-gray-300">—</span>` as visible words on the target,
   * antibodies and cell-lines boards and on the session result cards — four
   * screens reading as half-built. An editable empty cell needs the text form
   * anyway: it is already greyed and dash-underlined by its own classes, and
   * showValue() puts it back through textContent. */
  const DASH = '<span class="text-gray-300">—</span>';
  const EMPTY = '—';

  /* Per-row verdicts for a paste preview.
   *
   * "3 new, 1 already known" is a count, and a count cannot be checked: the one
   * row about to be overwritten is the one you most need named. Both paste
   * endpoints already return a status per row — this draws them, so the same
   * table serves every board rather than each growing its own.
   *
   * `label(item)` gives the row its name, since what identifies a row differs by
   * entity (a catalogue number, a cell-line name). */
  const PREVIEW_STATUS = {
    'create': ['new', 'good'],
    'update': ['already on file — will be updated', 'cool'],
    'no-target': ['skipped — no matching gene', 'warn'],
    'blocked': ['skipped', 'warn'],
  };

  /* The supplier a preview row will actually be *saved* against.
   *
   * A vendor is matched against the suppliers already on file, so what is stored
   * can differ from what was typed — "Bio-Techne" resolves to one of two real
   * Bio-Techne brands by catalogue prefix. A preview that echoes the typed name
   * is telling you something that will not be true a second later. Run 3 caught
   * it on the antibodies board and it was fixed there; run 4 caught the same
   * thing on a gene's page, because the fix lived in one page's template instead
   * of here. One function, used by every preview that names a supplier. */
  /* ─────────────────────────────────────────────────────────────────────
   * Deleting a child record needs one gene in front of you.
   *
   * Owner's call after using the feature: the warning box was right, the reach
   * was too wide. An antibody, a cell line and a session all belong to a gene,
   * and the boards that list them are thousands of rows long — so a Delete on
   * every row is a Delete beside every record you were not thinking about, and
   * "impossible to trigger accidentally" is not a property of the confirmation
   * alone. Narrowed to one gene, the grid in front of you is the thing you came
   * for, and the control is where you are already looking.
   *
   * Read from the **form**, not from a value the server rendered, and read on
   * every draw. The Gene box can be cleared without pressing Apply, and the
   * grid redraws after an add or a delete using whatever the form says — so a
   * page rendered narrowed could otherwise repaint the whole dataset with a
   * Delete on all of it. `q` does not count: it is the fuzzy box, and "the
   * search happens to show one gene" is not the same statement.
   *
   * The gate is announced, never silent — a control that is simply absent is a
   * feature a reader concludes does not exist (`geneGate` writes the sentence
   * next to the filters). Same rule as a save button disabled with a reason.
   */
  const GENE_GATE = 'Deleting is off until you pick a single gene. '
    + 'Type one in the Gene box above and press Apply — the grid then shows '
    + 'that gene\'s records, and each row gets a Delete.';

  /* **The sentence is per board, because the answer is.** One shared string
   * said "pick a single gene" on all three, and what that leaves out is
   * different on each. Two boards read `NA` (`targets.gene_q_with_na`) and one
   * does not, so the word is offered on exactly the two where it reaches
   * something: on **cell lines** it is necessary — a wild type has no gene, so a
   * gene filter can never reach one, and on live (2 Sep 2026) 148 lines are
   * reachable only that way; on **antibodies** it is supported and currently
   * matches nothing, which is right to offer anyway, since it is the shape a
   * blank gene cell takes and a field test already typed it here after learning
   * it one board over. The **sessions** board filters with `gene_q` alone and
   * every session has a gene, so saying nothing there would leave the word
   * looking broken rather than inapplicable.
   *
   * The other half is where a record this page cannot delete *is* deleted: a
   * reader who finds no Delete concludes the app cannot do it.
   *
   * `geneGate` takes the sentence, so the *mechanism* stays in one place and
   * only the wording varies — the mistake to avoid is three boards each growing
   * their own gate. */
  const GENE_GATE_ANTIBODIES = 'Deleting is off until you pick a single gene. '
    + 'Type one in the Gene box above and press Apply — or NA for the antibodies '
    + 'with no gene recorded. The grid then shows those records, and each row '
    + 'gets a Delete. Deleting a gene itself is on that gene\'s own page.';
  const GENE_GATE_CELL_LINES = 'Deleting is off until you pick a single gene. '
    + 'Type one in the Gene box above and press Apply — or type NA for the lines '
    + 'with no gene recorded, which is how you reach a wild type, since a gene '
    + 'filter never can. The grid then shows those records, and each row gets a '
    + 'Delete.';
  const GENE_GATE_SESSIONS = GENE_GATE
    + ' Every session has a gene, so NA finds nothing here.';

  /* The genes a `?gene=` filter names — the mirror of
   * `services/targets.py::gene_terms`, and it has to stay a mirror: the server
   * narrows on this list and `oneGene` gates deleting on its length, so the two
   * disagreeing means a Delete beside rows the reader did not ask for. */
  function geneTerms(value) {
    const seen = new Set(), out = [];
    String(value == null ? '' : value).trim().split(/[,\s]+/).forEach(t => {
      if (!t) return;
      const key = t.toUpperCase();
      if (seen.has(key)) return;
      seen.add(key);
      out.push(t);
    });
    return out;
  }

  /* **Exactly one**, not "anything in the box". The gene filter carries a list
   * now — adding six genes lands you on the board narrowed to those six — and
   * "the board is narrowed to a gene" was the whole gate on deleting. A
   * six-gene filter is six genes' worth of records with a Delete on every row,
   * which is the situation the gate exists to prevent, one comma away. */
  function oneGene(form) {
    const box = (form || document).querySelector('[name="gene"]');
    return geneTerms(box && box.value).length === 1;
  }

  function geneGate(form, el, message) {
    const on = oneGene(form);
    if (el) {
      el.textContent = on ? '' : (message || GENE_GATE);
      el.classList.toggle('hidden', on);
    }
    return on;
  }

  function supplierLabel(item) {
    const row = item.row || {};
    const typed = (item.company || row.company || '').trim();
    const saved = (item.company_resolved || typed).trim();
    if (saved && typed && saved !== typed) return `${saved} (you typed "${typed}")`;
    return saved;
  }

  /* What a checked list of genes would actually write, and why it might write
   * nothing.
   *
   * There are two doors to adding a gene — the target board's Add panel and the
   * feasibility box — and CLAUDE.md's rule for this pair is that one check sits
   * behind both. The *arithmetic* had already been written twice, with comments
   * on each copy saying it must match the other; the *refusal* had been written
   * once. So the feasibility door greyed its button over a list that would
   * create nothing, and the board's panel armed `Create them` over the same
   * list.
   *
   * The case that makes it more than tidiness is a UniProt outage, which is a
   * documented, transient, ordinary condition here (`bulk_targets` has a lookup
   * deadline precisely because UniProt is slow). Every row comes back
   * `unchecked`, which refuses to create — the preview says so and says to press
   * Check these again — and next to that sentence sat a live save button. Press
   * it and you get "0 target(s) added", which reads as the feature being broken
   * rather than as the network being slow. The feasibility door greyed the
   * button correctly and then named the wrong reason ("already on your list"),
   * which is the same mistake one layer down.
   *
   * `nominating` deliberately excludes rows that are also being created: a new
   * target's own nomination is already counted by `creating`, so including it
   * would count each new gene twice. Both doors put this number on the button,
   * so they can only agree by sharing it. */
  function targetAddSummary(d, site) {
    const rows = (d && d.rows) || [];
    const by = s => rows.filter(r => r.status === s).length;
    const creating = by('new');
    const nominating = rows.filter(
      r => r.will_nominate && r.status !== 'new').length;
    const total = creating + nominating;
    const unchecked = by('unchecked'), notFound = by('not_found');
    const whose = site || (d && d.site) || 'your site';
    let why = '';
    if (!total && rows.length) {
      if (unchecked) {
        why = `${plural(unchecked, 'gene')} could not be checked — UniProt did `
            + 'not answer in time. Press Check these again; nothing can be '
            + 'added until it does.';
      } else if (notFound) {
        why = 'None of these genes could be confirmed in UniProt, so there is '
            + 'nothing to add. Check the spelling.';
      } else {
        why = `Nothing to add — every gene checked is already on ${whose}'s list.`;
      }
    }
    return {rows, creating, nominating, total, unchecked, notFound, whose, why,
            label: total ? `Add ${total} to pipeline` : ''};
  }

  /* Where a bulk add of genes should take you, and whether it should take you
   * there on its own.
   *
   * The owner's ask: a successful bulk add lands on the **targets board,
   * filtered to the genes it just added** — one or fifty, always the board.
   * Adding targets used to end where every other paste ends, on the panel you
   * pressed with a green line under it, and the one link offered went to the
   * *whole* board: 585 rows with yours somewhere in them, which is the same as
   * not being told where they went. Filtering to what landed is the difference
   * between a receipt and a place.
   *
   * The board is right even for a single gene, deliberately. A paste is a batch
   * whatever its length, and landing on one gene's page after pasting one gene
   * makes the destination depend on how much you happened to type. The gene's
   * own page is where the *single* Add on the feasibility page goes — that one
   * is about one gene from the start.
   *
   * Written once because there are two doors to adding a gene and they have
   * disagreed about every other thing they share: the count on the button, the
   * refusal under it, and whether a nomination is a record. `d.landed` is the
   * one key it reads — `bulk_targets.apply` builds it, with the target's own
   * `gene_name`, so a paste of the synonym `PARK8` filters on **LRRK2** rather
   * than on a spelling nothing is stored under, which would land you on an
   * empty board after a successful save.
   *
   * **`quiet` is the whole of the safety.** `CLAUDE.md`: a page must not reload
   * itself out from under its own result line, because the result line is often
   * the only place a *dropped* value is named — a gene UniProt refused, a gene
   * that was already yours, a nomination that only gained a funder. Navigating
   * is exactly that reload wearing a different hat, so it happens only when the
   * result has nothing else to say: everything checked was added, and nothing
   * was refused, skipped or merely amended. Otherwise the caller shows the
   * result and offers `url` as a button — one click, and nothing disappears on
   * its own.
   *
   * The path is a literal here for the same reason both templates already write
   * theirs as literals: board.js is a static file with no `{% url %}`.
   * `tests_navigation.py` asserts it is what Django reverses to. */
  function targetAddDestination(d) {
    const landed = (d && d.landed) || [];
    if (!landed.length) return null;
    const genes = landed.map(t => t.gene).filter(Boolean);
    const nothingElse = !((d.not_found || []).length || (d.errors || []).length
                          || (d.skipped || []).length
                          || (d.funding_filled || []).length);
    return {
      url: `/pipeline/targets/board/?gene=${encodeURIComponent(genes.join(','))}`,
      genes,
      count: landed.length,
      quiet: nothingElse,
      label: `See ${plural(landed.length, 'gene')} on the targets board →`,
    };
  }

  /* Go, or offer to go — the caller's half of `targetAddDestination`, written
   * once so the two doors cannot differ about when a page moves under somebody.
   * `where` is where the button is appended when the result has something to
   * say that navigating away would destroy. */
  function targetAddGo(d, where) {
    const dest = targetAddDestination(d);
    if (!dest) return null;
    if (dest.quiet) { window.location.href = dest.url; return dest; }
    if (where) {
      where.insertAdjacentHTML('beforeend',
        `<p class="mt-2"><a href="${dest.url}" class="inline-flex items-center gap-1 px-3 py-1.5
           bg-ycharos-700 text-white text-sm font-semibold rounded-full hover:bg-ycharos-800"
           >${esc(dest.label)}</a></p>`);
    }
    return dest;
  }

  /* What a board does once a row has been destroyed.
   *
   * Written six times across four templates, and all six removed the `<tr>` and
   * decremented the count by one. That is right for a grid holding everything
   * and wrong for these, because **a board draws a page**: take a row out of a
   * 50-row page and 49 remain, with the row that should have moved up still
   * sitting on the next page. Delete a few and the page quietly shrinks — and
   * deleting the last row of the last page leaves an empty grid, which after a
   * successful delete reads as data loss.
   *
   * `board.load()` refetches **the page**, so the cost is the page and not the
   * dataset. The rule it looks like it breaks — never refetch the grid after a
   * save — is about a *cell edit*, where nothing has changed about which rows
   * belong here. A delete changes exactly that, including whether the page
   * still exists (`board_page` clamps past the end to the last page).
   *
   * The banner goes **after** the await: `load` clears messages, so that a
   * stale error cannot survive a reload — which means a fresh one must not be
   * posted before it. */
  function rowDeleted(board, errors) {
    return async (id, d) => {
      await board.load();
      errors.show(`Deleted ${d.label}.`);
    };
  }

  /* A concentration whose unit cannot be converted is refused per row and the
   * row is still written — deliberately, since an odd concentration is no reason
   * to discard an otherwise good vial. What was missing was the *count*, in both
   * places a count belongs: the preview's summary, and the line the save prints.
   * Run 8 read the per-row refusal on `3 mM`, pressed Create them with nothing
   * else said, and got an antibody with no concentration at all.
   *
   * Here rather than in a board template, because three panels paste antibodies
   * and a preview rule fixed on one page is a rule for one page. */
  function concentrationWarning(summary) {
    const n = (summary || {}).no_concentration || 0;
    if (!n) return '';
    return `<li class="text-amber-800">${n} concentration${n === 1 ? '' : 's'}
      cannot be converted and ${n === 1 ? 'will' : 'will'} not be stored — the rest of
      ${n === 1 ? 'that row' : 'those rows'} is saved. Fix the cell and check again to keep
      ${n === 1 ? 'it' : 'them'}.</li>`;
  }

  function concentrationSaved(result) {
    const dropped = (result || {}).no_concentration || [];
    if (!dropped.length) return '';
    const listed = dropped.slice(0, 5).map(
      d => `${esc(d.catalogue || '(row)')} (${esc(d.typed || '')})`).join(', ');
    return `<br><span class="text-amber-800">${dropped.length}
      row${dropped.length === 1 ? '' : 's'} saved without a concentration: ${listed}${
      dropped.length > 5 ? ', …' : ''}. Add it on the board in ${esc('µg/mL')}.</span>`;
  }

  /* The same pair, for the cell-lines column that had the same bug.
   *
   * `c number` is an integer with a `C-` prefix for display, and the paste path
   * read it with a bare digit search — so `C-RUN11-01` was filed as **11**, on a
   * preview that said the row was fine. It is refused per cell now and the row
   * is still written, which is the right bargain and exactly why it has to be
   * counted at the save too: the eleventh field test read a per-row refusal,
   * pressed the button, and got two knockouts carrying no C-number at all. */
  function cNumberWarning(summary) {
    const n = (summary || {}).no_c_number || 0;
    if (!n) return '';
    return `<li class="text-amber-800">${n} C-number${n === 1 ? '' : 's'}
      cannot be read and ${n === 1 ? 'will' : 'will'} not be stored — the rest of
      ${n === 1 ? 'that row' : 'those rows'} is saved. A C-number is a plain number,
      optionally written C-631; a batch label that is not belongs in the comments
      column.</li>`;
  }

  function cNumberSaved(result) {
    const dropped = (result || {}).no_c_number || [];
    if (!dropped.length) return '';
    const listed = dropped.slice(0, 5).map(
      d => `${esc(d.name || '(row)')} (${esc(d.typed || '')})`).join(', ');
    return `<br><span class="text-amber-800">${dropped.length}
      row${dropped.length === 1 ? '' : 's'} saved without a C-number: ${listed}${
      dropped.length > 5 ? ', …' : ''}. Add it on the board as a plain number.</span>`;
  }

  /* A number the app gave a record is a thing the app did, so it says so.
   *
   * The first field test on A-numbers: *"the new antibody was auto-assigned A-1
   * even though I left ab # blank — probably deliberate, but nothing in the
   * preview or the save message mentions that a lab reference number has been
   * minted."* Correct on both counts. A number written on a freezer box is
   * exactly the kind of value a person needs to know exists, and the app was
   * inventing one in silence.
   *
   * The check says a number *will* be given and does not promise which. It
   * cannot: the number depends on what else lands in the same batch, and a
   * preview that names A-3085 and then writes A-3087 is worse than one that
   * names nothing. The save reports what was actually issued, which is the
   * moment the answer is real. */
  /* Two boards, two opposite behaviours, so two keys.
   *
   * A **cell line** is numbered when it is created: a C-number is issued per
   * site in freeze order and a freeze-down batch takes the next one, so there is
   * nothing to defer. An **antibody** is not, since 2 Sep 2026 — the A-number
   * decides which freezer box a vial goes in, and numbering on arrival scatters
   * a protein across as many boxes as it had deliveries. So the antibody
   * message names the *absence* and says where the number comes from instead.
   *
   * `will_be_numbered` and `will_be_unnumbered` are deliberately different keys
   * rather than one flag read two ways: this is one writer for both panels, and
   * a key meaning opposite things on two boards is how a message ends up true
   * of neither. */
  function labNumberNote(summary) {
    const s = summary || {};
    const numbered = s.will_be_numbered || 0;
    const unnumbered = s.will_be_unnumbered || 0;
    let out = '';
    if (numbered) {
      out += `<li>${plural(numbered, 'new record')} will be given this site's
        next lab number. Type one in the number column to use your own
        instead.</li>`;
    }
    if (unnumbered) {
      out += `<li>${plural(unnumbered, 'new antibody', 'new antibodies')} will be
        added <b>without an A-number</b>, so you can deal the numbers out once
        the whole delivery is in and keep each protein's vials together — press
        <b>Assign numbers</b> on the antibodies board when you are ready. Type a
        number in the <b>ab #</b> column to set one now. Planning a session gives
        a number to anything that still has none.</li>`;
    }
    return out;
  }

  /* What a save left deliberately blank. The counterpart to `labNumbersIssued`:
     a number the app declined to give is as much a thing the app did as one it
     gave, and a save that silently produced no A-number reads as a save that
     went wrong — the mirror image of run 8's finding, which was a number
     appearing with nothing to announce it. */
  function labNumbersDeferred(result) {
    const n = (result || {}).left_unnumbered || 0;
    if (!n) return '';
    return `<br><span>${plural(n, 'antibody', 'antibodies')} added with
      <b>no A-number</b> — that is deliberate. Press <b>Assign numbers</b> when
      the delivery is complete and they will be dealt out in gene order, so each
      protein's vials sit together in the freezer. Nothing is numbered until you
      say so, or until a session is planned.</span>`;
  }

  function labNumbersIssued(result) {
    const issued = (result || {}).numbers_issued || [];
    if (!issued.length) return '';
    const listed = issued.slice(0, 5).map(
      d => `${esc(d.number)} ${esc(d.name || '(row)')}`).join(', ');
    return `<br><span>Lab ${issued.length === 1 ? 'number' : 'numbers'} given:
      ${listed}${issued.length > 5 ? ', …' : ''}. Write ${
      issued.length === 1 ? 'it' : 'them'} on the ${
      issued.length === 1 ? 'box' : 'boxes'}.</span>`;
  }

  /* One writer for everything a check and a save have to add beyond their own
   * counts, because there are seven places these are rendered and the last
   * three messages of this kind each had to be wired into all of them by hand.
   * A result that carries none of these renders nothing, so both are safe on
   * every panel — an antibody panel cannot miss a cell-line note by being an
   * antibody panel. */
  function checkNotes(summary) {
    return concentrationWarning(summary) + cNumberWarning(summary)
         + labNumberNote(summary);
  }

  function saveNotes(result) {
    return concentrationSaved(result) + cNumberSaved(result)
         + labNumbersIssued(result) + labNumbersDeferred(result);
  }

  /* Which sheet an upload was read from, when there was a choice.
   *
   * Blank for the ordinary one-sheet file. A workbook with a second tab gets a
   * sentence, because the reader used to take whichever tab Excel had selected
   * on save and say nothing at all about it — so a `Notes` tab left in front
   * replaced the data and the upload appeared to do nothing. */
  /* "1 rows read." and "4 gene(s) checked." — a count with a hedge beside it.
   *
   * Next to a number is where a grammar slip costs most: it makes a careful
   * reader distrust the number. `(s)` is the same slip written down in advance. */
  function plural(n, one, many) {
    return `${n} ${Number(n) === 1 ? one : (many || one + 's')}`;
  }

  function sheetLine(d) {
    const note = (d || {}).sheet_note || '';
    return note ? `<span class="block font-normal text-xs text-amber-800 mb-1">${esc(note)}</span>` : '';
  }

  /* Blank *lines* off the top and bottom of a paste — never the leading tab of
   * a line, which is a column.
   *
   * This was `textarea.value.trim()`, and `trim()` does not know the difference:
   * a tab is whitespace, so it ate the leading tab of line 1 and only line 1.
   * Every cell on that row then slid one column left while the rows underneath
   * read perfectly — the shape that makes it so hard to see, because a paste
   * either works or it doesn't, and this one did both at once.
   *
   * It is the normal case, not an edge case: a gene's own page says "leave the
   * gene column blank — every row here is TRPA1", so every row of every paste
   * made through that panel starts with an empty first column, and following the
   * instruction is what triggers it. The seventh field test caught it only
   * because the shift pushed a concentration into `site` and sites are
   * validated: `There is no site called '0.8'`. One column either way and three
   * antibodies would have been created with the catalogue number stored as the
   * gene and the company as the catalogue, silently, with a preview agreeing.
   *
   * A line that is all tabs is still dropped — that is a row with nothing in any
   * column, not a row whose first column is blank. */
  function pastedBlock(raw) {
    const lines = String(raw == null ? '' : raw).replace(/\r/g, '').split('\n');
    while (lines.length && !lines[0].trim()) lines.shift();
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    return lines.join('\n');
  }

  /* A row stopped because its gene is not a target yet gets the way out, not a
   * description of it.
   *
   * "gene 'TRPA1' not in pipeline — tick “create targets”" named a control that
   * does not exist under that name, on a panel whose checkbox reads "Add the
   * gene as a new target if it isn't one yet". The wording is fixed at the
   * source; this is the other half — a gene belongs on the target board, so the
   * preview offers the trip there with the gene already filled in. The server
   * supplies the URL (`add_target_url`), because this file must not know how
   * Django spells a route. */
  function addTargetLink(it) {
    const url = (it || {}).add_target_url || '';
    if (!url) return '';
    const gene = (it.gene || it.target_gene || '').trim();
    return ` <a href="${esc(url)}" class="font-semibold text-ycharos-700 hover:underline whitespace-nowrap"
       >Add ${esc(gene) || 'it'} to the pipeline →</a>`;
  }

  /* A download of everything is asked for, not assumed.
   *
   * The owner's review: *"I accidently downloaded all the sessions, that should
   * not happen I should by default download a gene."* Every board's Download
   * takes the grid's filters, which is right — and with no filters set that is
   * the whole dataset, from a button one click away from the one you wanted.
   *
   * So an unfiltered export says how much it is about to hand over and waits.
   * A filtered one never asks: the whole point of the filters is that you have
   * already said what you meant.
   */
  function confirmWholeDownload(form, count, noun) {
    const filtered = form && [...new FormData(form).entries()]
      .some(([, v]) => String(v).trim() !== '');
    if (filtered) return true;
    const n = Number(count) || 0;
    return window.confirm(
      `No filters are set, so this downloads all ${n.toLocaleString()} ` +
      `${noun}${n === 1 ? '' : 's'}.\n\n` +
      `To get one gene, type it in the Gene box first and download again.`);
  }

  function previewRows(items, label) {
    if (!items || !items.length) return '';
    const rows = items.map((it, i) => {
      const [text, tone] = PREVIEW_STATUS[it.status] || [it.status || '?', 'flat'];
      return `<tr>
        <td class="px-2 py-1 text-right text-gray-300 select-none">${i + 1}</td>
        <td class="px-2 py-1 font-medium text-gray-800">${
          esc(label(it)) || '<span class="text-gray-400">(blank row)</span>'}</td>
        <td class="px-2 py-1 whitespace-nowrap">${pill(text, tone)}</td>
        <td class="px-2 py-1 text-gray-500">${esc(it.note || '')}${addTargetLink(it)}</td>
      </tr>`;
    }).join('');
    return `<div class="mt-3 border border-gray-200 rounded-lg overflow-hidden">
      <div class="max-h-64 overflow-y-auto overflow-x-auto">
        <table class="text-xs w-full">
          <thead class="bg-gray-50 text-gray-500 uppercase tracking-wide sticky top-0">
            <tr><th class="px-2 py-1"></th>
                <th class="px-2 py-1 text-left font-semibold">Row</th>
                <th class="px-2 py-1 text-left font-semibold">What will happen</th>
                <th class="px-2 py-1 text-left font-semibold"></th></tr>
          </thead>
          <tbody class="divide-y divide-gray-100">${rows}</tbody>
        </table></div></div>`;
  }

  /* An editable cell. `extra` becomes data- attributes and is posted along with
   * the edit, so a board can carry whatever its patch endpoint needs (a
   * nomination id, a result id) without this file knowing what those mean.
   * Use dashed keys — `{'nomination-id': 3}` posts as `nomination_id`. */
  /* opts.empty: what an empty cell shows instead of a bare dash.
   *
   * An empty editable cell used to render exactly like the read-only dashes
   * beside it, so there was nothing to say it could be typed into — the field
   * test tried to allocate a site and reported the cell as not editable. Empty
   * cells now carry a dashed underline, and can name what they are for. The
   * placeholder is never a value: data-empty tells the editor the cell is blank,
   * so the prompt text can't be saved as if someone had typed it. */
  /* opts.display: what the cell *shows*, where that differs from what it stores.
   *
   * A field with a closed set has two spellings — the code the server keeps
   * (`in_progress`) and the label a person reads (`In Progress`) — and the
   * editor has handled both since it was written, as its `{value, label}`
   * options. The closed cell could not, so the sessions board drew the status
   * twice: a pill reading "Planned" with the raw `planned` underneath it as the
   * editable copy. One column, two copies of one fact, and no other column on
   * the board does it. Now the cell carries the code in `data-value` and prints
   * the label, so the pill *is* the control.
   *
   * opts.classes: extra classes for the closed cell, so a cell can be drawn as a
   * pill. They come off for the duration of an edit — a <select> inside a
   * rounded, coloured pill is not a text box — and go back on afterwards. */
  function cell(rowId, field, value, extra, opts) {
    const attrs = Object.entries(extra || {})
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => ` data-${k}="${esc(v)}"`).join('');
    const display = (opts && opts.display) || value;
    const shown = esc(display);
    const blank = !esc(value);
    const hint = (opts && opts.empty) || EMPTY;
    const label = (opts && opts.label) || field.replace(/_/g, ' ');
    const extraClass = (opts && opts.classes) || '';
    return `<span class="edit cursor-text hover:bg-yellow-50 rounded px-1 -mx-1 inline-block min-w-[2rem]${
              blank ? ' text-gray-400 border-b border-dashed border-gray-400' : ''}${
              extraClass ? ' ' + extraClass : ''}"
              data-target="${esc(rowId)}" data-field="${esc(field)}"${attrs}${
              blank ? ' data-empty="1"' : ` data-value="${esc(value)}"`}${
              !blank && display !== value ? ` data-display="${esc(display)}"` : ''}${
              extraClass ? ` data-cellclass="${esc(extraClass)}"` : ''}
              tabindex="0" role="button"
              aria-label="${esc(label)}${value ? `: ${shown}` : ' — empty'}. Click to edit."
              title="Click to edit — ${esc(label)}">${blank ? esc(hint) : shown}</span>`;
  }

  /* A DOI, editable, with the link beside it and its date underneath.
   *
   * It lived in the target board's own template, and it is on two surfaces now —
   * the board and the gene's own page — so it is here, because a preview rule
   * fixed in one template is a rule for one page and this file already carries
   * three that learned it the hard way.
   *
   * It draws a link *beside* an editable cell, never instead of one: drawing the
   * link alone as soon as the field had a value is how a typo became permanent
   * on the one column somebody is trying to correct. `href` is empty when the
   * stored value is not an absolute address, and that is the harmful case — the
   * browser resolves a relative one against this site and the Not Found page it
   * lands on reads as the record having been lost rather than the link being
   * wrong — so it says so instead of linking.
   *
   * The **date is the other half, and it was drawn nowhere at all.** A gene
   * counts as completed on a Zenodo DOI or an F1000 *date*, so recording an
   * F1000 paper by its DOI alone leaves the publication date blank, and the one
   * field that says when the work came out was reachable only through a
   * spreadsheet upload. It sits under the DOI it belongs to — one fact, one
   * place — rather than in a column of its own on a grid that is eleven wide. */
  function doiCell(rowId, spec) {
    const editable = cell(rowId, spec.field, spec.text, {},
                          {label: spec.label, empty: 'add DOI'});
    const dated = spec.dateField
      ? `<span class="block text-xs text-gray-400 mt-0.5">${
           cell(rowId, spec.dateField, spec.date, {},
                {label: spec.dateLabel || 'date', empty: 'add date'})}</span>`
      : '';
    if (!spec.text) return editable + dated;
    const suffix = spec.href
      ? `<a href="${esc(spec.href)}" target="_blank" rel="noopener"
            class="ml-1 text-ycharos-700 hover:underline"
            aria-label="Open ${esc(spec.label)} ${esc(spec.text)} in a new tab">↗</a>`
      : `<span class="block text-xs text-amber-700">not a link — give a DOI or
           a full https:// address</span>`;
    return `<span class="break-all">${editable}</span>${suffix}${dated}`;
  }

  /* An ISO date as a person writes it. Display only — every date *cell* keeps
   * the ISO spelling, because that is one `target_list_io.parse_date` reads back
   * and a surface must not print a value its own parser would refuse. */
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function dateText(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
    return m ? `${+m[3]} ${MONTHS[+m[2] - 1]} ${m[1]}` : String(iso || '');
  }

  /* What you type into a cell, when the field has known values.
   *
   * A grid cell was always a bare text box, and for two fields that was simply
   * wrong. `status` is one of six codes — the Plan a session header and this
   * board's own filter both draw it as a dropdown, and only the grid let you
   * type `done` into it and take a refusal from the server afterwards. `rating`
   * is free text with no vocabulary written down anywhere, so whatever a person
   * invents in that box is what a generated Data Note then prints.
   *
   * Two kinds, and the difference is whether an unlisted value is legitimate:
   *
   *   strict — a `<select>`. The field has a closed set (a model's `choices`),
   *            so anything else would be refused on save anyway and offering it
   *            is offering a mistake.
   *   loose  — an `<input list=…>` and a `<datalist>`. The values are only what
   *            is already on file, so the list is a reminder and not a rule —
   *            the same trade `newEntry`'s `suggestions` makes one surface over,
   *            and for the same reason: a vocabulary nobody may add to is a
   *            vocabulary that stops describing the bench.
   *
   * A value the list does not carry is kept as an option of its own rather than
   * quietly swapped for the first one — including the empty cell, which gets a
   * blank option so that opening a dropdown and closing it again cannot write a
   * status nobody chose. */
  let editSeq = 0;
  const EDIT_CLASS = 'border border-ycharos-400 rounded px-1 py-0.5 text-sm w-full min-w-[10rem]';

  /* The plain values out of a `cellChoices` entry, for a surface that wants the
   * same vocabulary in a different control.
   *
   * A board's grid cell and its Add panel offer the same fields — site, role,
   * clonality — and the two draw them differently on purpose: a grid cell may be
   * a `<select>`, while an Add-panel cell stays a text input because `newEntry`
   * serialises the grid to TSV through the same parser a paste uses. Without
   * this each template mapped the list itself, which is three copies of one
   * transformation and three chances for a panel to offer something the cell
   * beside it does not. The blank option is dropped: it exists so a dropdown can
   * *clear* a value, and there is nothing to clear on a row being created. */
  function optionValues(spec) {
    return ((spec && spec.values) || [])
      .map(v => (typeof v === 'string' ? v : (v && v.value) || ''))
      .filter(Boolean);
  }

  function editorHtml(spec, original) {
    const opts = ((spec && spec.values) || []).map(v => (typeof v === 'string'
      ? {value: v, label: v}
      : {value: v.value, label: v.label || v.value}));
    if (!opts.length) {
      return `<input class="${EDIT_CLASS}" value="${esc(original)}">`;
    }
    const same = o => String(o.value) === String(original);
    if (spec.strict) {
      const head = opts.some(same) ? '' : `<option value="${esc(original)}" selected>${
        original ? `${esc(original)} — not a listed value` : '— choose —'}</option>`;
      return `<select class="${EDIT_CLASS}">${head}${opts.map(o =>
        `<option value="${esc(o.value)}"${same(o) ? ' selected' : ''}>${esc(o.label)}</option>`
      ).join('')}</select>`;
    }
    const listId = `oga-cell-choices-${++editSeq}`;
    return `<input class="${EDIT_CLASS}" value="${esc(original)}" list="${listId}">
      <datalist id="${listId}">${opts.map(o =>
        `<option value="${esc(o.value)}"${o.label !== o.value ? ` label="${esc(o.label)}"` : ''}></option>`
      ).join('')}</datalist>`;
  }

  /* Put a saved (or reverted) value back into a cell, keeping the empty-cell
   * styling and prompt in step with whether there is now a value. */
  /* `labelOf` turns the stored code back into what a reader sees, for a cell
   * drawn with `opts.display`. Without it, cancelling an edit on the sessions
   * board's status put the raw `in_progress` back on screen where the pill had
   * been — the very stutter the display option exists to remove. */
  function showValue(span, value, hint, labelOf) {
    const shown = value && labelOf ? labelOf(value) : value;
    // Keep the accessible name in step with the value, or a screen reader goes
    // on reading out what the cell used to hold.
    const label = (span.getAttribute('aria-label') || '').split(':')[0]
      .replace(/ — empty$/, '');
    span.setAttribute('aria-label',
      `${label}${value ? `: ${shown}` : ' — empty'}. Click to edit.`);
    if (value) {
      delete span.dataset.empty;
      span.dataset.value = value;
      span.classList.remove('text-gray-400', 'border-b', 'border-dashed',
                            'border-gray-400');
      span.textContent = shown;
      return;
    }
    span.dataset.empty = '1';
    delete span.dataset.value;
    span.classList.add('text-gray-400', 'border-b', 'border-dashed',
                       'border-gray-400');
    span.textContent = hint;
  }

  /* dataset gives camelCase; the server speaks snake_case. */
  const snake = s => s.replace(/[A-Z]/g, c => '_' + c.toLowerCase());
  const RESERVED = new Set(['target', 'field', 'value', 'editing', 'row', 'empty',
                            'busy']);

  const formQuery = form =>
    new URLSearchParams(new FormData(form)).toString();

  /* Scroll something into view, but only when it is actually out of sight —
   * yanking the page under somebody who is already reading the message is its
   * own bug. Lifted out of `banner` because the pop-outs need the same thing:
   * `uploadPanel` writes its result into a `fixed inset-0 overflow-y-auto`
   * modal, which is a tall scroller for exactly the same reason the grid is. */
  function bringIntoView(el) {
    if (!el) return;
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > (window.innerHeight || 0)) {
      el.scrollIntoView({block: 'center', behavior: 'smooth'});
    }
  }

  /* A message area that distinguishes the failure modes. Returns {show, clear}.
   *
   * It scrolls itself into view, and says so out loud.
   *
   * The banner sits above the grid, and the grid is a tall scroller: edit a row
   * forty down and the message can appear entirely off-screen. What you see then
   * is the cell reverting to its old value with no explanation — and a refused
   * edit that looks like an accepted one is worse than an error, because you walk
   * away believing the site was changed. The fourth field test reported exactly
   * that on the target board, where the server had answered with the right
   * sentence and the tester never saw it.
   *
   * `role="alert"` is set here rather than in four templates, so a screen reader
   * announces it wherever it is on the page. */
  function banner(boxId, textId, closeId) {
    const box = document.getElementById(boxId);
    const text = document.getElementById(textId);
    const close = closeId ? document.getElementById(closeId) : null;
    if (box) {
      box.setAttribute('role', 'alert');
      box.setAttribute('aria-live', 'assertive');
    }
    const clear = () => { if (box) { box.classList.add('hidden'); } if (text) text.textContent = ''; };
    const show = msg => {
      if (text) text.textContent = msg;
      if (!box) return;
      box.classList.remove('hidden');
      bringIntoView(box);
    };
    if (close) close.addEventListener('click', clear);
    return {show, clear};
  }

  /* One careful fetch. Reads the body as text before parsing, so an HTML error
   * page reports as an error rather than throwing inside JSON.parse and leaving
   * a "saving…" that never resolves. Returns the parsed body, or null.
   *
   * `kind` says whether this request was writing or only reading, because the
   * honest sentence differs and getting it wrong is worse than saying less. A
   * failed *write* cannot promise nothing was saved. A failed *read* can: it
   * changed nothing, and telling someone to "check the board to see whether it
   * was saved" when the board is the thing that just failed sends them in a
   * circle. The field test hit exactly that — a 500 on the cell-lines rows fetch
   * reported as a save that might have happened. */
  async function requestJson(url, options, onError, kind) {
    const reading = kind === 'read';
    const outcome = reading
      ? 'Nothing has been changed.'
      : 'Check the board to see whether it was saved.';
    let res;
    try {
      res = await fetch(url, options);
    } catch (e) {
      onError(reading
        ? `Could not reach the server (${e.message}). Nothing has been changed.`
        : `Could not reach the server (${e.message}). Nothing was saved.`);
      return null;
    }
    const text = await res.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) { /* not JSON — handled below */ }
    if (data === null) {
      // Don't claim nothing was saved — a reply we cannot read is a reply we
      // cannot draw that conclusion from. It used to say "Nothing was saved" on
      // a duplicate submit whose twin had just written four records.
      onError(res.status === 502 || res.status === 504
        ? `That took too long to answer. ${outcome}`
        : `The server returned ${res.status}${
             reading ? ' and these rows could not be loaded' : ''}. ${outcome}`);
      return null;
    }
    // `ok === false` is a refusal. A 200 with no `ok` field at all is success —
    // the older bulk endpoints predate that convention and return their payload
    // bare, and they are shared with other pages, so this side gives way.
    if (!res.ok || data.ok === false) {
      // Endpoints disagree about whether a refusal is `error` or `errors`.
      onError(data.error || (data.errors || []).join('; ')
              || `The server returned ${res.status}. ${
                   reading ? 'Nothing has been changed.' : 'Nothing was saved.'}`);
      return null;
    }
    return data;
  }

  /*
   * cfg:
   *   grid            tbody element rows are drawn into
   *   filtersForm     the filter <form>; its fields become the query string
   *   countEl         element showing "N things"
   *   errors          {show, clear} from banner()
   *   csrf            token rendered server-side
   *   urls            {rows, patch}
   *   colspan         column count, for the empty/loading row
   *   emptyMessage    shown when nothing matches
   *   countLabel(n)   text for countEl
   *   rowHtml(row)    a <tr data-row="..."> for one row  — the board-specific bit
   *   cellChoices     {field: {values: [value | {value, label}], strict: bool}}
   *                   what that field's editor offers — see editorHtml(). Read
   *                   on every edit, so a board may add to it as it learns (the
   *                   sessions board does, per procedure, when a row is opened).
   *   afterLoad(data) optional; called with the whole rows payload
   *   afterPatch(data) optional; called with the whole patch payload
   */
  function create(cfg) {
    const {grid, filtersForm, countEl, errors, csrf, urls} = cfg;
    const colspan = cfg.colspan || 10;
    const countLabel = cfg.countLabel || (n => `${n} row${n === 1 ? '' : 's'}`);
    const emptyRow = `<tr><td colspan="${colspan}" class="px-3 py-8 text-center text-gray-400">${
      esc(cfg.emptyMessage || 'Nothing matches these filters.')}</td></tr>`;
    const loadingRow = `<tr><td colspan="${colspan}" class="px-3 py-8 text-center text-gray-400">Loading…</td></tr>`;
    /* A failed load is not an empty result, and must never be drawn as one. The
     * cell-lines board spent this release telling people "No cell lines match
     * these filters" over 559 intact rows, because the rows fetch 500'd and the
     * grid fell back to the empty message — so everyone went and adjusted their
     * filters. */
    const failedRow = `<tr><td colspan="${colspan}" class="px-3 py-8 text-center text-red-700">
      These rows could not be loaded — the reason is in the message above.
      Your data has not been changed.</td></tr>`;

    let count = 0;

    /* One page at a time.
     *
     * Every board returned **every matching row** and drew the lot in one
     * `innerHTML`. On the antibodies board that is 3,225 rows of fifteen cells,
     * and the personal review got *"page unresponsive"* — a browser dialog, on
     * the app's largest and most-used table. It is not the extension and it is
     * not the network: the cost is building and painting the whole dataset when
     * nobody can read more than a screenful of it.
     *
     * `page` is board state, deliberately **not** a field on the filter form.
     * The form is what `formQuery` serialises into the rows fetch, the patch
     * query string *and* every download href — so a page number living there
     * would silently scope an export to fifty rows. A download stays the whole
     * filtered set; this only ever narrows what is drawn.
     */
    let page = 1;
    let pages = 1;
    /* `?per_page=` in the address bar is honoured, because the rows endpoint
     * honours it and a page that ignores what the URL asked for is the failure
     * the `?site=` and `?gene=` rules are about — resolving a parameter
     * server-side is only half of it. The direction here is milder (the URL
     * says 10, the board drew 50) but it is the same silence.
     *
     * Not a form field: `formQuery` feeds the patch query string and every
     * download href as well as the rows fetch, so a page size living there
     * would scope an export to a screenful. Board state, read once. */
    const asked = parseInt(
      new URLSearchParams(location.search).get('per_page'), 10);
    const perPage = (asked > 0 ? Math.min(asked, 500) : 0) || cfg.perPage || 50;
    const query = () => (filtersForm ? formQuery(filtersForm) : '');
    const pagedQuery = () => {
      const q = query();
      return `${q}${q ? '&' : ''}page=${page}&per_page=${perPage}`;
    };

    function setCount(n) {
      count = n;
      if (countEl) countEl.textContent = countLabel(n);
    }

    /* What you are looking at, and of how much.
     *
     * "1–50 of 3,225" rather than a bare page number: a reader who has filtered
     * to one gene needs to know whether the screen is the answer or the first
     * slice of it, and a page number alone does not say. Hidden entirely when
     * everything fits, so the small boards look exactly as they did.
     */
    function renderPager() {
      const el = cfg.pagerEl;
      if (!el) return;
      if (pages <= 1) { el.innerHTML = ''; el.classList.add('hidden'); return; }
      el.classList.remove('hidden');
      const from = (page - 1) * perPage + 1;
      const to = Math.min(count, page * perPage);
      const btn = (label, target, disabled) =>
        `<button type="button" data-page="${target}" ${disabled ? 'disabled' : ''}
           class="px-2.5 py-1 rounded border text-sm font-semibold ${disabled
             ? 'border-gray-200 text-gray-300 cursor-not-allowed'
             : 'border-gray-300 text-gray-700 hover:bg-gray-50'}">${label}</button>`;
      el.innerHTML = `
        <span class="text-sm text-gray-600">${from.toLocaleString()}–${to.toLocaleString()}
          of ${count.toLocaleString()}</span>
        <span class="flex items-center gap-1">
          ${btn('‹ Previous', page - 1, page <= 1)}
          <span class="text-sm text-gray-500 px-1">page ${page} of ${pages}</span>
          ${btn('Next ›', page + 1, page >= pages)}
        </span>`;
    }

    if (cfg.pagerEl) {
      cfg.pagerEl.addEventListener('click', e => {
        const b = e.target.closest('button[data-page]');
        if (!b || b.disabled) return;
        page = Math.max(1, Math.min(pages, +b.dataset.page));
        load();
        // The grid is a long way down by the time you reach the pager.
        grid.closest('table')?.scrollIntoView({block: 'start', behavior: 'smooth'});
      });
    }

    /* A filter change starts again at page 1. Staying on page 12 of a result set
     * that now has three pages asks the server for nothing and draws an empty
     * grid over a filter that matches plenty. */
    if (filtersForm) {
      filtersForm.addEventListener('change', () => { page = 1; });
      filtersForm.addEventListener('submit', () => { page = 1; });
    }

    function render(rows) {
      grid.innerHTML = rows.length ? rows.map(r => cfg.rowHtml(r)).join('') : emptyRow;
    }

    async function load() {
      grid.innerHTML = loadingRow;
      // A fresh load clears a message left over from an earlier request. Without
      // this a stale save error sat on top of a successful reload, which is how
      // the field test saw a green success and a red 500 for one click.
      errors.clear();
      const data = await requestJson(`${urls.rows}?${pagedQuery()}`,
        {headers: {'X-Requested-With': 'fetch'}}, errors.show, 'read');
      // Not setCount(0) — "0 cell lines" is as false as "nothing matches".
      if (!data) {
        grid.innerHTML = failedRow;
        if (countEl) countEl.textContent = 'could not be counted';
        if (cfg.pagerEl) cfg.pagerEl.innerHTML = '';
        return;
      }
      if (cfg.afterLoad) cfg.afterLoad(data);
      setCount(data.count);
      /* **A board whose server does not paginate has no pager**, and must not
       * be given one. The first version of this fell back to
       * `ceil(count / perPage)` when `pages` was absent, which invents
       * pagination: the server sends every row, the grid draws every row, and
       * the pager underneath says "1–50 of 74" over 74 visible rows and offers
       * a Next that changes nothing. Two things on one screen disagreeing about
       * how much you are looking at is the failure this file exists to stop.
       * Absent means one page of whatever arrived. */
      pages = data.pages || 1;
      page = data.page || 1;
      render(data.rows);
      renderPager();
    }

    /* Go to the page a row is on, and say whether it was found.
     *
     * **Pagination breaks every link that points at a row**, and the sessions
     * board has two: `?open=<id>`, which is where a bookmark of the retired
     * session page lands, and "Record its results here" after a save. Both
     * looked the row up in the drawn grid, so with fifty rows drawn instead of
     * all of them anything further down came back missing — under a message
     * saying it was *"outside the current filters — clear them to see it"*,
     * which was false and, worse, advice that makes it harder to find: clearing
     * the filters lengthens the list and pushes the row further back.
     *
     * The server answers where the row is (`?locate=`), because only it knows
     * the ordering. `false` means genuinely not in this filtered set, which is a
     * different sentence and the caller writes it.
     */
    async function locate(id) {
      if (!id) return false;
      grid.innerHTML = loadingRow;
      errors.clear();
      const q = query();
      const data = await requestJson(
        `${urls.rows}?${q}${q ? '&' : ''}per_page=${perPage}&locate=${encodeURIComponent(id)}`,
        {headers: {'X-Requested-With': 'fetch'}}, errors.show, 'read');
      if (!data) {
        grid.innerHTML = failedRow;
        if (countEl) countEl.textContent = 'could not be counted';
        return false;
      }
      if (cfg.afterLoad) cfg.afterLoad(data);
      setCount(data.count);
      pages = data.pages || 1;
      page = data.page || 1;
      render(data.rows);
      renderPager();
      return data.located !== false;
    }

    async function patch(body) {
      errors.clear();
      return requestJson(`${urls.patch}?${query()}`, {
        method: 'POST',
        headers: {'X-CSRFToken': csrf, 'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams(body).toString(),
      }, errors.show);
    }

    /* A redraw that arrived while another cell in the same row was open, held
     * until that cell is done with. Keyed by row id. */
    const heldRedraw = new Map();

    /* Which snapshot each row is currently showing.
     *
     * Every patch reply carries a whole row read on the server, so two saves on
     * one row produce two snapshots and the screen showed whichever *reply*
     * landed last — not whichever *write* did. Holding a redraw made that
     * deterministic rather than merely racy: a payload parked because another
     * cell was open is replayed at the end of that cell's save, which is after
     * the newer payload has already been painted, so the older one always wins.
     * Edit two cells in a row quickly and the first edit reappears with its old
     * value, saved and invisible. Stamped and dropped if stale instead. */
    let seq = 0;
    const painted = new Map();

    /* Redraw one row from what the save returned. An edit can push a row out of
     * the active filter — the server says so and the row goes. */
    function replaceRow(id, data, n) {
      // A caller with no stamp — the identity dialog, which redraws after its
      // own modal save — counts as the newest thing that has happened, so it
      // paints and any cell reply still in flight is stale against it.
      if (n === undefined) n = ++seq;
      if (n <= (painted.get(String(id)) || 0)) return;
      painted.set(String(id), n);
      const tr = grid.querySelector(`tr[data-row="${id}"]`);
      if (!tr) { load(); return; }
      // Never redraw a row while one of its other cells is open for editing.
      // The redraw replaces the whole <tr>, which destroys that open editor —
      // so the click that opened it is swallowed and the cell has to be clicked
      // a second time. Hold the redraw until the open cell finishes instead.
      if (tr.querySelector('.edit[data-editing]')) {
        heldRedraw.set(String(id), {data, n});
        return;
      }
      if (data.matches === false) {
        // A board may hang a detail row off a row (results, history) keyed by
        // data-for. It goes with its parent, never orphaned.
        grid.querySelectorAll(`tr[data-for="${id}"]`).forEach(el => el.remove());
        tr.remove();
        setCount(Math.max(0, count - 1));
        if (!grid.querySelector('tr[data-row]')) grid.innerHTML = emptyRow;
        return;
      }
      if (cfg.afterPatch) cfg.afterPatch(data);
      tr.outerHTML = cfg.rowHtml(data.row);
    }

    /* Apply a redraw that was held back while a cell was open — unless a newer
     * one has been painted since, in which case it is stale and goes. */
    function flushRedraw(id) {
      const held = heldRedraw.get(String(id));
      if (!held) return;
      heldRedraw.delete(String(id));
      replaceRow(id, held.data, held.n);
    }

    async function save(body, id) {
      // Stamped before the await, so the order the saves were *started* in is
      // what decides which snapshot is newer.
      const n = ++seq;
      const data = await patch(body);
      if (data) replaceRow(id, data, n);
      return data;
    }

    /* Everything the element carries goes to the server: the row id under the
     * name this board's endpoint expects, the field, the value, and any extra
     * data- attributes the board attached (nomination id, result id, …). */
    function bodyFor(el, value) {
      const body = {};
      for (const [k, v] of Object.entries(el.dataset)) {
        if (!RESERVED.has(k)) body[snake(k)] = v;
      }
      body[cfg.rowParam || 'target_id'] = el.dataset.target;
      body.field = el.dataset.field;
      body.value = value;
      return body;
    }

    /* Show that a save landed. The only feedback on the boards was the cell's
     * own hover highlight, which the field test read as a save flash — it is
     * still there when the pointer has not moved, and gone the moment it does.
     * A real one, on the row, so a toggle that changes a pill somewhere in the
     * row is as visible as a cell that changes its text. */
    function flash(el) {
      if (!el) return;
      el.classList.add('bg-yellow-100', 'transition-colors', 'duration-700');
      setTimeout(() => el.classList.remove('bg-yellow-100'), 700);
    }

    /* Which editable cell did this click mean?
     *
     * Only the text of a value was clickable, so a short value in a wide column
     * — "Leicester" under SITES — was a target a few characters wide with dead
     * space either side. The field test clicked it twice, decided the cell was
     * read-only, and reported that there is no way to move a target between
     * sites. A click anywhere in a cell that holds exactly one editable value
     * means that value. Two of them (funder over project) stays ambiguous, so it
     * is left alone rather than guessed. */
    function editableFor(e) {
      const direct = e.target.closest('.edit');
      if (direct) return direct;
      const td = e.target.closest('td');
      if (!td || e.target.closest('a, button, input, select, textarea, details')) return null;
      const spans = td.querySelectorAll('.edit');
      return spans.length === 1 ? spans[0] : null;
    }

    /* Click a cell, type, Enter or blur to save, Escape to cancel. Buttons
     * marked .toggle save a fixed value from their own data attributes. */
    /* Cell editing is *delegated*, and that is what a pop-out has to be
     * told about.
     *
     * Both handlers hung off `grid`, so anything drawn outside the table
     * has dead cells — and silently: the markup is identical, the click
     * simply reaches no listener. The sessions board's results move out of
     * the grid into a drawer, which is exactly that case, so the binding is
     * a function and any number of roots can have it. `cfg.extraRoots` is
     * how a page names them; `bindEditing` is on the returned object for a
     * panel built after the board.
     *
     * A root is bound once — binding twice would save every edit twice. */
    const boundRoots = new WeakSet();

    function bindEditing(root) {
      if (!root || boundRoots.has(root)) return;
      boundRoots.add(root);

      root.addEventListener('keydown', e => {
        const span = e.target.closest && e.target.closest('.edit');
        if (!span || span.dataset.editing) return;
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); span.click(); }
      });

      root.addEventListener('click', async e => {
        const toggle = e.target.closest('.toggle');
        if (toggle) {
          // One click, one save. Without this a second click lands while the
          // first is still in flight and sends the same change twice.
          if (toggle.dataset.busy) return;
          toggle.dataset.busy = '1';
          const id = toggle.dataset.target;
          try {
            const data = await save(bodyFor(toggle, toggle.dataset.value), id);
            // Toggling funded wrote to the server and changed a pill, with nothing
            // to say a save had happened at all.
            if (data) flash(grid.querySelector(`tr[data-row="${id}"]`));
          } finally {
            delete toggle.dataset.busy;
          }
          return;
        }

        const span = editableFor(e);
        if (!span || span.dataset.editing) return;
        // data-empty, not a text comparison: an empty cell may show a prompt
        // ("set site") rather than a dash, and a prompt is not a value.
        const hint = span.textContent.trim();
        /* data-value first: a cell drawn with `opts.display` shows the label
         * and stores the code, and editing the label would post `In Progress`
         * to a field whose six answers are all snake_case. Every other cell has
         * no data-value and reads its own text, exactly as before. */
        const original = span.dataset.empty ? ''
          : (span.dataset.value !== undefined ? span.dataset.value : hint);
        /* What a stored code reads as, for putting the cell back on cancel or
         * on a save the row does not redraw — built from the same closed set
         * the editor's <select> is built from, so the two cannot disagree. */
        const choices = (cfg.cellChoices || {})[span.dataset.field];
        /* What this cell was showing before the edit. `choices` answers for a
         * closed set — a session status, where every code has a label — and
         * cannot answer for an **open** one: the antibodies board draws a
         * received date as `Aug 2026` over a stored `2026-08`, and there is no
         * list of every date to look it up in. Cancelling or a refused save
         * both put `original` back, so the label it was drawn with is exactly
         * the right answer for both, and it is on the span already. Without
         * this the cell came back reading `2026-08` — a valid spelling of the
         * same day, which is what makes it worth fixing: nothing looks broken,
         * the column just quietly stops matching the rows around it. */
        const shownOriginal = span.dataset.display || '';
        const labelOf = v => {
          const opt = ((choices && choices.values) || []).find(
            o => o && typeof o === 'object' && String(o.value) === String(v));
          if (opt) return opt.label || opt.value;
          if (shownOriginal && String(v) === String(original)) return shownOriginal;
          return v;
        };
        // A pill's own styling is wrong around a text box or a dropdown.
        const cellClass = span.dataset.cellclass;
        if (cellClass) span.classList.remove(...cellClass.split(/\s+/));
        span.dataset.editing = '1';
        /* The input was w-full of a span sized to its own contents, so the box you
         * type into was as wide as the value already there — about 2rem for an
         * empty cell, which is where a session's SIGNAL field showed
         * "single band ~2(" while it was being typed. The span grows for the
         * duration of the edit instead; the surrounding td caps it, so nothing
         * bursts the layout. min-w-0 lets it shrink again on a narrow screen. */
        span.classList.add('block', 'w-full', 'min-w-0');
        /* `cfg.cellChoices` is read at edit time, not at board creation: the
         * sessions board learns a procedure's result vocabulary when a row is
         * opened, and merges it into the same object. */
        span.innerHTML = editorHtml((cfg.cellChoices || {})[span.dataset.field],
                                    original);
        const input = span.querySelector('input, select');
        input.focus();
        // A <select> has no select(); only a text box has text to highlight.
        if (input.select) input.select();

        const finish = async (shouldSave) => {
          if (!span.dataset.editing) return;
          delete span.dataset.editing;
          span.classList.remove('block', 'w-full', 'min-w-0');
          if (cellClass) span.classList.add(...cellClass.split(/\s+/));
          const value = input.value;
          const id = span.dataset.target;
          if (!shouldSave || value === original) {
            showValue(span, original, hint, labelOf);
            flushRedraw(id);
            return;
          }
          span.textContent = 'saving…';
          const data = await save(bodyFor(span, value), id);
          // Failed save: put the cell back, so nothing on screen claims a value
          // the database does not have.
          if (!data) { showValue(span, original, hint, labelOf); flushRedraw(id); return; }
          // Redrawing the row usually destroys this span. A cell in a detail
          // panel survives, and must show what was saved rather than "saving…".
          if (span.isConnected) { showValue(span, value, hint, labelOf); flash(span); }
          else flash(grid.querySelector(`tr[data-row="${id}"] .edit[data-field="${
            span.dataset.field}"]`));
          // This cell is closed now, so any redraw held back for its row can go.
          flushRedraw(id);
        };

        input.addEventListener('keydown', ev => {
          if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
          if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
        });
        // Picking from a dropdown *is* the edit — waiting for a blur means a
        // click straight back onto the grid saves, and a click on the page
        // chrome looks like it did nothing. `finish` guards on data-editing, so
        // the blur that follows is a no-op rather than a second save.
        if (input.tagName === 'SELECT') {
          input.addEventListener('change', () => finish(true));
        }
        input.addEventListener('blur', () => finish(true));
      });
    }

    bindEditing(grid);
    (cfg.extraRoots || []).forEach(bindEditing);


    return {load, locate, patch, save, replaceRow, render, setCount, query,
            bindEditing,
            get count() { return count; },
            get page() { return page; },
            get pages() { return pages; }};
  }

  /* ─────────────────────────────────────────────────────────────────────
   * New entries: a pop-out with two ways in, one way out.
   *
   *   Table — an empty grid with the right headings, for typing structured
   *           data. Keyboard-navigable like a spreadsheet, and a block pasted
   *           from Excel fills the cells rather than landing in one of them.
   *   Paste — a textarea, for when the data is already in a message or a
   *           supplier page and just needs pasting.
   *
   * Both serialise to the same tab-separated text and go through the same
   * server parser, so the two modes cannot behave differently. The header row
   * is always sent: bulk_cell_lines.parse requires one (without it every line
   * is skipped and you get a silent zero-row import), and sending it means the
   * columns map by name rather than by position.
   *
   * cfg:
   *   mount            element to render the panel into
   *   columns          [string] headings — take these from the same constant
   *                    the Excel template uses, never a fresh list
   *   example          [string] optional placeholder row
   *   csrf, urls       {preview, commit}
   *   rows             initial blank row count (default 5)
   *   intro            html shown above the tabs
   *   extraHtml        html for fields this entity needs (a gene, a date…)
   *   suggestions      {columnName: [value | {value, hint}]} — a dropdown on
   *                    that column's cells, from what the page already knows is
   *                    on file. Still a text input: a value the list does not
   *                    carry is typed as before, which is what keeps a C-number
   *                    or another site's line reachable.
   *   extraValues()    -> object merged into every request body
   *   buildBody(tsv, extra, phase) -> object  request body; phase is 'preview'
   *                    or 'commit'. Some endpoints default to a dry run and
   *                    need telling explicitly which one this is — a commit
   *                    that forgets silently previews again and creates nothing.
   *   renderPreview(data) -> html
   *   renderResult(data)  -> html
   *   onDone()         called after a successful create
   */
  /* Every panel instance gets its own id namespace.
   *
   * These ids were a fixed `ne-` prefix, which is fine while a page has one
   * panel and silently fatal the moment it has two. The gene page has two — Add
   * antibodies and Add cell lines — so twelve ids appeared twice, getElementById
   * returned the first every time, and *every* handler on the cell-lines panel
   * bound to the antibodies one. Its grid opened with zero rows, "+ Add row" did
   * nothing, its Paste tab never activated, and "Create them" was wired to the
   * wrong grid. There is no way to enter a cell line from a gene page.
   *
   * A counter rather than a caller-supplied prefix, so a third panel cannot
   * reintroduce this by forgetting to pass one. Ids in `extraHtml` belong to the
   * caller and are untouched — the boards look those up by name. */
  let panelSeq = 0;

  /* Escape dismisses an open pop-out.
   *
   * Escape already cancelled an inline cell edit, which is the habit it teaches:
   * the fourth field test pressed it twice on the identity dialog, watched it
   * stay open, and had to find Cancel. The same key, on the same board, doing two
   * different things is the kind of inconsistency people stop trusting.
   *
   * Registered per panel and guarded on visibility, so Escape only closes
   * something that is actually on screen, and a stack of them closes the topmost
   * first (last registered wins because the handler returns after acting).
   * `dismiss` is the panel's own close function — a dialog mid-save closes the
   * same way Cancel does, writing nothing. */
  function escapeCloses(isOpen, dismiss) {
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape' || !isOpen()) return;
      e.preventDefault();
      dismiss();
    });
  }

  /* The example, as a row of its own that cannot be typed into.
   *
   * It has been two things at once and neither worked. As **placeholder text on
   * row 0** it read as a filled-in first row — the personal review said it was
   * never clear you were meant to type over it — and it vanished the moment that
   * cell had a value, so the one statement of what a column wants disappeared as
   * you used it and rows 2 and beyond never had it at all. As a **legend printed
   * underneath** it was the same content a second time, which is the duplication
   * the review then flagged.
   *
   * One example, pinned in the header above row 1, greyed and labelled `e.g.`,
   * outside the tab order and outside `tsv()` — which reads `body.children`, so
   * a header row cannot be saved as a record. Row 1 is unambiguously yours and
   * empty. The workbooks do the same thing on their own row 2
   * (`views/imports.py`), and both are skipped by one reader
   * (`services/example_row.py`), so what the screen calls an example and what
   * the parser discards cannot drift apart.
   */
  function exampleRow(cols, example) {
    if (!example || !example.length) return '';
    /* Not aria-hidden. It is the only statement of the conventions in the grid,
     * and hiding it from a screen reader would leave that reader with the column
     * names alone — the state this replaced. It reads as "e.g., HAP1, SNCA,
     * KO, …", which is what it is. */
    return `<tr class="bg-gray-50 border-t border-gray-100">
      <td class="px-2 py-1 text-[10px] font-bold uppercase tracking-wide text-gray-400 text-right select-none">e.g.</td>
      ${cols.map((_c, i) => `<td class="px-2 py-1 text-xs italic text-gray-400 whitespace-nowrap select-none">${
        esc((example[i] || '').trim())}</td>`).join('')}
    </tr>`;
  }

  /* The blank spreadsheet, offered where the columns are.
   *
   * `views/imports.py::import_template` builds it from `columns_and_example` —
   * the same list behind this grid's headings and behind the paste parser — and
   * was **routed but reachable from nowhere**: no template and no script
   * mentioned the URL. So a board could be filled in by hand or pasted into, and
   * the file most people would actually want to work in existed and could not
   * be got at. It is the mirror of the rule about an export with no importer:
   * a routed endpoint is not a reachable one.
   *
   * It is a receipt-bearing link, not a bare href, for the same reason every
   * other download here is: the page has to say the file arrived.
   */
  function templateLink(cfg, receiptId) {
    if (!cfg.templateUrl) return '';
    return `<p class="mt-2 text-sm">
      <a href="${cfg.templateUrl}" data-receipt="${receiptId}"
         class="font-semibold text-ycharos-700 hover:underline">⬇ Blank Excel template</a>
      <span class="text-gray-500">— the same columns as the table below, with an
      example row. Fill it in, then bring it back with the upload button.</span>
    </p>
    <p id="${receiptId}" class="text-sm" role="status" aria-live="polite"></p>`;
  }

  /* A column whose value is usually something already on file, offered as a
   * dropdown instead of remembered.
   *
   * The gene page listed a site's wild-type parentals *in prose* — "A549, HAP1,
   * HCT116, …" — under a grid where you then retyped one into the `parent` cell
   * from memory, on the one column where getting it wrong attaches a knockout to
   * another institution's line. A list you read and a box you type into are two
   * halves of a job the page can simply do.
   *
   * A `<datalist>` rather than a `<select>` on purpose: the cell stays a text
   * input, so `tsv()` serialises it unchanged, the keyboard grid still works, and
   * a value the list does not carry — a C-number, another site's line, one added
   * five minutes ago on another screen — is still typed exactly as before.
   * Narrowing the column to a fixed list would take those away.
   */
  function suggestionLists(cfg, cols, id) {
    const by = {};
    Object.entries(cfg.suggestions || {}).forEach(([name, values]) => {
      const i = cols.findIndex(c => String(c).toLowerCase() === String(name).toLowerCase());
      if (i >= 0 && (values || []).length) by[i] = values;
    });
    const html = Object.entries(by).map(([i, values]) => `
      <datalist id="${id('sug-' + i)}">
        ${values.map(v => {
          const value = typeof v === 'string' ? v : (v.value || '');
          const hint = typeof v === 'string' ? '' : (v.hint || '');
          return `<option value="${esc(value)}"${hint ? ` label="${esc(hint)}"` : ''}></option>`;
        }).join('')}
      </datalist>`).join('');
    return {html, listFor: i => (by[i] ? ` list="${id('sug-' + i)}"` : '')};
  }

  function newEntry(cfg) {
    const cols = cfg.columns || [];
    const startRows = cfg.rows || 5;
    const ns = `ne${++panelSeq}`;
    const id = n => `${ns}-${n}`;
    const suggest = suggestionLists(cfg, cols, id);

    cfg.mount.innerHTML = `
      <div class="border border-gray-200 rounded-xl bg-white p-4 mt-4">
        <div class="flex items-start justify-between gap-4">
          <div class="text-sm text-gray-600 max-w-2xl">${cfg.intro || ''}</div>
          <button type="button" id="${id('close')}"
                  class="text-gray-400 hover:text-gray-700 font-bold text-lg leading-none">&times;</button>
        </div>
        ${templateLink(cfg, id('template-receipt'))}
        ${suggest.html}
        ${cfg.extraHtml ? `<div class="mt-3">${cfg.extraHtml}</div>` : ''}
        <div class="mt-3 flex gap-1 border-b border-gray-200">
          <button type="button" id="${id('tab-table')}"
                  class="px-3 py-2 text-sm font-semibold border-b-2 border-ycharos-600 text-ycharos-700">Table</button>
          <button type="button" id="${id('tab-paste')}"
                  class="px-3 py-2 text-sm font-semibold border-b-2 border-transparent text-gray-500 hover:text-gray-800">Paste</button>
        </div>

        <div id="${id('table-mode')}" class="mt-3">
          <div class="overflow-x-auto border border-gray-200 rounded-lg">
            <table class="text-sm" id="${id('grid')}">
              <thead class="bg-gray-50">
                <tr>
                  <th class="px-2 py-1 w-8"></th>
                  ${cols.map(c => `<th class="px-2 py-1 text-left text-xs font-bold uppercase tracking-wide text-gray-500 whitespace-nowrap">${esc(c)}</th>`).join('')}
                </tr>
                ${exampleRow(cols, cfg.example)}
              </thead>
              <tbody></tbody>
            </table>
          </div>
          <div class="mt-2 flex flex-wrap items-center gap-3">
            <button type="button" id="${id('add-row')}"
                    class="text-sm font-semibold text-ycharos-700 hover:underline">+ Add row</button>
            ${cols.length > 6 ? `<span class="text-xs text-gray-500">${cols.length} columns —
              scroll the table sideways for the rest: ${esc(cols.slice(-3).join(', '))}</span>` : ''}
            <span class="text-xs text-gray-400">
              Tab or arrow keys to move · Enter moves down a row, same column ·
              click a cell and type to replace it · paste a block from Excel to fill the grid
            </span>
          </div>
        </div>

        <div id="${id('paste-mode')}" class="mt-3 hidden">
          <textarea id="${id('paste')}" rows="8" spellcheck="false"
                    class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono"
                    placeholder="${esc(cols.join('\t'))}"></textarea>
          <p class="text-xs text-gray-400 mt-1">
            One row per line, columns separated by tabs — paste straight from Excel.
            Expected order: ${esc(cols.join(' · '))}
          </p>
        </div>

        <div class="mt-3 flex flex-wrap items-center gap-3">
          <button type="button" id="${id('preview')}"
                  class="px-4 py-2 rounded-lg bg-gray-900 text-white font-semibold text-sm">Check these</button>
          ${/* aria-describedby, not the reason as the button's name. The reason
                is a whole sentence, and a screen reader that takes it as the
                label announces the error where the action should be — run 11
                read the button's accessible name as "The check did not pass, so
                nothing can be created yet…". Pointing at the note beside it
                keeps the name "Create them" and the reason as its description,
                which is what the note is already for. */''}
          <button type="button" id="${id('commit')}" disabled
                  title="Check the rows first — this turns on once the check has run"
                  aria-describedby="${id('why')}"
                  class="px-4 py-2 rounded-lg bg-ycharos-600 text-white font-semibold text-sm opacity-50 cursor-not-allowed">Create them</button>
          <button type="button" id="${id('clear')}"
                  class="text-sm text-gray-500 hover:text-gray-800">Clear</button>
          ${/* The reason the save is greyed, in text. It was on `title` alone —
                so on a touch screen, and for anyone who does not think to hover a
                button that looks broken, a disabled button was the whole message.
                A refusal everywhere else in this build names itself on the page;
                this one only did so to a mouse. */''}
          <span id="${id('why')}" class="text-xs text-gray-500"
                role="status" aria-live="polite">Check the rows first.</span>
        </div>
        <div id="${id('out')}" class="mt-3 text-sm"></div>
      </div>`;

    const el = n => document.getElementById(id(n));
    const body = el('grid').querySelector('tbody');
    const out = el('out');
    // This panel's markup is built after any page-load pass, so its own
    // download link has to be wired here or it is a plain href — the silent
    // download this whole mechanism exists to stop.
    downloadReceipts(cfg.mount);

    const cellHtml = (r) => `
      <tr>
        <td class="px-2 py-1 text-xs text-gray-300 text-right select-none">${r + 1}</td>
        ${/* No placeholders. The example is its own pinned row above (see
              `exampleRow`): a placeholder on row 0 reads as a filled-in first
              row you are not sure whether to type over, and disappears exactly
              when it is being used. */''}
        ${cols.map((_c, i) => `<td class="p-0">
          <input data-r="${r}" data-c="${i}" spellcheck="false"${suggest.listFor(i)}
                 class="w-full min-w-[8rem] px-2 py-1 text-sm border-0 focus:ring-2 focus:ring-ycharos-400 focus:bg-yellow-50">
        </td>`).join('')}
      </tr>`;

    function addRow(n = 1) {
      for (let k = 0; k < n; k++) {
        body.insertAdjacentHTML('beforeend', cellHtml(body.children.length));
      }
    }
    const at = (r, c) => body.querySelector(`input[data-r="${r}"][data-c="${c}"]`);

    /* Type-over replaces, the way a spreadsheet does.
     *
     * Tab already selected the cell's contents; a mouse click did not — it put a
     * caret mid-string, so typing inserted. The field test produced HHAP1AP1 and
     * STMNSTMN22 that way and had to learn Ctrl+A. A click into a cell that was
     * not already focused selects all of it; a second click inside the focused
     * cell still places a caret, so editing part of a value is not lost. */
    let selectOnClick = false;
    body.addEventListener('mousedown', e => {
      selectOnClick = e.target.tagName === 'INPUT'
                      && e.target !== document.activeElement;
    });
    body.addEventListener('mouseup', e => {
      if (selectOnClick && e.target.tagName === 'INPUT') {
        e.preventDefault();
        e.target.select();
      }
      selectOnClick = false;
    });

    function focusCell(r, c) {
      while (r >= body.children.length) addRow();
      const input = at(r, c);
      if (input) { input.focus(); input.select(); }
    }

    /* Excel-ish keyboard. Left/Right only jump cells at the ends of the text,
     * so ordinary editing inside a cell still works. */
    body.addEventListener('keydown', e => {
      const t = e.target;
      if (t.tagName !== 'INPUT') return;
      const r = +t.dataset.r, c = +t.dataset.c;
      const last = t.value.length;
      if (e.key === 'ArrowDown' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault(); focusCell(r + 1, c);
      } else if (e.key === 'ArrowUp' || (e.key === 'Enter' && e.shiftKey)) {
        e.preventDefault(); if (r > 0) focusCell(r - 1, c);
      } else if (e.key === 'ArrowRight' && t.selectionStart === last && c < cols.length - 1) {
        e.preventDefault(); focusCell(r, c + 1);
      } else if (e.key === 'ArrowLeft' && t.selectionStart === 0 && c > 0) {
        e.preventDefault(); focusCell(r, c - 1);
      } else if (e.key === 'Tab' && !e.shiftKey && c === cols.length - 1
                 && r === body.children.length - 1) {
        // Tabbing off the last cell grows the grid rather than leaving it.
        e.preventDefault(); focusCell(r + 1, 0);
      }
    });

    /* A block pasted from Excel fills the grid from the focused cell, instead
     * of dumping every tab and newline into one input. */
    body.addEventListener('paste', e => {
      const t = e.target;
      if (t.tagName !== 'INPUT') return;
      const text = (e.clipboardData || window.clipboardData).getData('text') || '';
      if (!/[\t\n\r]/.test(text)) return;
      e.preventDefault();
      const r0 = +t.dataset.r, c0 = +t.dataset.c;
      const lines = text.replace(/\r/g, '').split('\n').filter(l => l !== '');
      lines.forEach((line, dr) => {
        line.split('\t').forEach((val, dc) => {
          const c = c0 + dc;
          if (c >= cols.length) return;
          while (r0 + dr >= body.children.length) addRow();
          const input = at(r0 + dr, c);
          if (input) input.value = val.trim();
        });
      });
      focusCell(r0 + lines.length - 1, c0);
    });

    el('add-row').addEventListener('click', () => {
      addRow();
      focusCell(body.children.length - 1, 0);
    });

    // Tabs
    function mode(which) {
      const table = which === 'table';
      el('table-mode').classList.toggle('hidden', !table);
      el('paste-mode').classList.toggle('hidden', table);
      el('tab-table').className = `px-3 py-2 text-sm font-semibold border-b-2 ${
        table ? 'border-ycharos-600 text-ycharos-700' : 'border-transparent text-gray-500 hover:text-gray-800'}`;
      el('tab-paste').className = `px-3 py-2 text-sm font-semibold border-b-2 ${
        table ? 'border-transparent text-gray-500 hover:text-gray-800' : 'border-ycharos-600 text-ycharos-700'}`;
    }
    el('tab-table').addEventListener('click', () => mode('table'));
    el('tab-paste').addEventListener('click', () => mode('paste'));

    /* Does the first pasted line name columns, or is it data?
     *
     * This used to be "any column name appears anywhere in line 1", which a data
     * row can satisfy by accident — a comment containing the word "site" was
     * enough — and line 1 was then eaten as headings with nothing said about it.
     * Now most of line 1's cells have to *be* column names, and either way the
     * reading is reported before anything is created, because a paste silently
     * losing its first row is the worst outcome here. */
    function headerReading(pasted) {
      const fields = (pasted.split('\n')[0] || '')
        .split('\t').map(s => s.trim().toLowerCase()).filter(Boolean);
      const want = new Set(cols.map(c => c.toLowerCase()));
      const hits = fields.filter(f => want.has(f)).length;
      const isHeader = fields.length === 1
        ? hits === 1
        : hits >= 2 && hits * 2 >= fields.length;
      return {
        isHeader,
        note: isHeader
          ? `Line 1 read as column headings (${hits} of ${fields.length} cells `
            + 'match a column name), so it will not be saved as a record.'
          : 'No headings found, so every line is read as data in the column '
            + `order shown: ${cols.join(' · ')}.`,
      };
    }

    /* What the paste box will say about how it read the text. Set by tsv(),
     * shown with the Check result — never after the create, which would be too
     * late to be any use. */
    let pasteNote = '';

    /* Whichever mode is showing, the server sees the same thing: a header row
     * followed by tab-separated data rows. */
    function tsv() {
      if (!el('paste-mode').classList.contains('hidden')) {
        // `pastedBlock`, never `.trim()` — see its own note. A leading tab is an
        // empty first column, and this panel's own instruction asks for one.
        const pasted = pastedBlock(el('paste').value);
        pasteNote = '';
        if (!pasted) return '';
        const reading = headerReading(pasted);
        pasteNote = reading.note;
        return reading.isHeader ? pasted : [cols.join('\t'), pasted].join('\n');
      }
      pasteNote = '';
      const rows = [];
      for (let r = 0; r < body.children.length; r++) {
        const vals = cols.map((_c, i) => (at(r, i)?.value || '').trim());
        if (vals.some(v => v)) rows.push(vals.join('\t'));
      }
      return rows.length ? [cols.join('\t'), ...rows].join('\n') : '';
    }

    /* One click, one request. A second click on Create used to send the whole
     * batch again: the first save succeeded and the duplicate came back a 500,
     * so the panel showed a success and a failure for the same action and there
     * was no way to tell what had been written. The buttons go dead while a
     * request is in flight, so the first click is the only one that counts. */
    let inFlight = false;
    const BUTTONS = ['preview', 'commit', 'clear'];

    /* Create them is *disabled* until a check has run, not absent.
     *
     * It used to be `hidden`, so a first-time user faced a filled grid offering
     * only Check these and Clear, with nothing to suggest a save button existed
     * at all. The sixth field test only pressed Check because an earlier panel
     * had taught it. A greyed button with a reason on hover says there are two
     * steps and which one you are on — and it stays greyed after a check that
     * failed, so nothing invites a save of rows the server refused. */
    let armed = false;

    function setBusy(on) {
      inFlight = on;
      BUTTONS.forEach(n => {
        const b = el(n);
        if (!b) return;
        const off = on || (n === 'commit' && !armed);
        b.disabled = off;
        b.classList.toggle('opacity-50', off);
        b.classList.toggle('cursor-not-allowed', off);
      });
    }

    /* **A count on a save button is every record the press will write.**
     *
     * The feasibility door learned this after run 8 and reads `Add 5 to
     * pipeline`; the targets board's Add panel, posting to the same endpoint,
     * still said a generic `Create them` — so the counting fix landed on one of
     * two doors, which is the shape this file exists to stop. A board supplies
     * `cfg.commitLabel(d)` and gets the same sentence. Returning nothing keeps
     * the default, so the three boards that have no meaningful count are
     * unaffected. */
    const COMMIT_DEFAULT = el('commit').textContent;

    function setCommitLabel(d) {
      const label = cfg.commitLabel ? cfg.commitLabel(d) : '';
      el('commit').textContent = label || COMMIT_DEFAULT;
    }

    function arm(on, why) {
      armed = on;
      if (!on) setCommitLabel(null);
      const reason = why
        || (on ? '' : 'Check the rows first — this turns on once the check has run.');
      el('commit').title = on
        ? 'Create the rows exactly as checked above'
        : reason;
      const note = el('why');
      if (note) {
        note.textContent = on ? '' : reason;
        note.classList.toggle('hidden', on);
      }
      setBusy(inFlight);
    }

    async function post(url, busy, phase) {
      const text = tsv();
      if (!text) { out.innerHTML = '<p class="text-gray-500">Nothing entered yet.</p>'; return null; }
      out.innerHTML = `<span class="text-gray-500">${esc(busy)}</span>`;
      const extra = cfg.extraValues ? cfg.extraValues() : {};
      const onError = msg => { out.innerHTML = `<p class="text-red-700">${esc(msg)}</p>`; };
      // Most endpoints take a JSON POST. buildRequest is the escape hatch for
      // the ones that don't — the targets check is GET-only, and it is shared
      // with the target board's own panel, so it is not ours to change.
      if (cfg.buildRequest) {
        const req = cfg.buildRequest(phase, text, extra);
        // A board can refuse before anything is sent — e.g. the table changed
        // after it was checked, so the checked rows no longer describe it.
        if (req.error) { onError(req.error); return null; }
        return requestJson(req.url, req.options || {}, onError);
      }
      const payload = cfg.buildBody(text, extra, phase);
      return requestJson(url, {
        method: 'POST',
        headers: {'X-CSRFToken': cfg.csrf, 'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      }, onError);
    }

    el('preview').addEventListener('click', async () => {
      if (inFlight) return;
      setBusy(true);
      try {
        // **"Nothing entered" and "the check failed" are two states**, and this
        // collapsed them into one. `post` writes *Nothing entered yet.* into the
        // output panel for an empty grid and returns null, and the branch below
        // then overwrote the reason beside the button with *Fix the rows above
        // and check again* — pointing at rows that do not exist. Run 18 hit it
        // by pressing Check with the Paste tab open and empty, and the message
        // named nothing it could act on. Asked here, before the request, so the
        // sentence beside the button matches the one in the panel.
        if (!tsv()) {
          out.innerHTML = '<p class="text-gray-500">Nothing entered yet.</p>';
          arm(false, 'There is nothing to check yet — type a row into the table '
                   + 'above, or paste one into the Paste tab.');
          return;
        }
        const d = await post(cfg.urls.preview, 'Checking…', 'preview');
        if (!d) {
          // A check the server refused leaves the save greyed — say which of the
          // two states you are in, or a disabled button after a visible error
          // reads as the error being unrelated to it.
          arm(false, 'The check did not pass, so nothing can be created yet. '
                   + 'Fix the rows above and check again.');
          return;
        }
        out.innerHTML = (pasteNote
          ? `<p class="mb-2 text-xs text-gray-500">${esc(pasteNote)}</p>` : '')
          + cfg.renderPreview(d);
        // **A check that succeeds is not a check that found something to
        // write.** A board that can tell says so, and the save stays greyed
        // with the reason on the page — the same state a failed check leaves
        // it in, because pressing it would have the same result. Without this
        // the targets panel armed `Create them` over a list of genes UniProt
        // had not answered for, directly under its own "press Check these
        // again", and the press reported "0 target(s) added".
        const nothing = cfg.whyNotCommit ? cfg.whyNotCommit(d) : '';
        if (nothing) { arm(false, nothing); return; }
        arm(true);
        // After `arm`, not before: arming resets the label to the default, so a
        // count set first would be wiped by the line that turns the button on.
        setCommitLabel(d);
      } finally {
        setBusy(false);
      }
    });

    el('commit').addEventListener('click', async () => {
      if (inFlight) return;
      setBusy(true);
      try {
        const d = await post(cfg.urls.commit, 'Creating…', 'commit');
        if (!d) return;
        out.innerHTML = cfg.renderResult(d);
        // A page that needs to catch up with the write adds its own control
        // here rather than reloading itself out from under the reader — the
        // result line is often the only place a dropped value is named.
        if (cfg.afterResult) cfg.afterResult(out, d);
        // Disarmed so the checked rows cannot be sent a second time — the next
        // create goes through Check first.
        arm(false);
        if (cfg.onDone) cfg.onDone();
      } finally {
        setBusy(false);
      }
    });

    function clear() {
      body.innerHTML = '';
      addRow(startRows);
      el('paste').value = '';
      out.innerHTML = '';
      arm(false);
    }
    el('clear').addEventListener('click', clear);

    addRow(startRows);
    const panel = cfg.mount;
    panel.classList.add('hidden');
    el('close').addEventListener('click', () => panel.classList.add('hidden'));
    escapeCloses(() => !panel.classList.contains('hidden'),
                 () => panel.classList.add('hidden'));

    /* Open with row 1 already carrying the cells that make it the kind of record
     * the button said it would add.
     *
     * "Add a wild type" and "add cell lines" are the same grid and the same
     * endpoint — what differs is two cells, and a page that knows which button
     * was pressed can fill them rather than printing the rule and hoping. The
     * rule in question is the one this repo has been bitten by four times: a
     * wild type has **no gene**, so its gene cell is `NA` and its genotype is
     * `WT`, and a knockout row that leaves the genotype blank is silently
     * written as a parental.
     *
     * Prefilled, not fixed: every cell is still a cell, so a person who opened
     * the wrong one types over it. Columns are matched by name, so a template
     * that gains a column cannot shift the prefill onto its neighbour.
     */
    function fill(values) {
      Object.entries(values || {}).forEach(([name, value]) => {
        const c = cols.findIndex(
          col => String(col).toLowerCase() === String(name).toLowerCase());
        const input = c >= 0 && at(0, c);
        if (input) input.value = value;
      });
    }

    return {
      open(prefill) {
        panel.classList.remove('hidden');
        // Every existing caller does `addEventListener('click', entry.open)`,
        // so the first argument here is usually a MouseEvent. Its own
        // properties live on the prototype, so `fill` would quietly do nothing
        // — quietly being the problem. Say which arguments are values.
        if (prefill && typeof prefill === 'object' && !(prefill instanceof Event)) {
          fill(prefill);
        }
        focusCell(0, 0);
      },
      close() { panel.classList.add('hidden'); },
      clear,
      fill,
    };
  }

  /* ─────────────────────────────────────────────────────────────────────
   * The other half of the round trip: put an edited sheet back.
   *
   * The target and sessions boards had download *and* upload; antibodies and
   * cell lines had download only, and pointed at the whole-dataset tool for the
   * way back — a different page, a different shape, and every record in the
   * consortium rather than the rows you had filtered to.
   *
   * Written once here for the same reason `create` and `newEntry` are: four
   * boards, one behaviour. It reuses the endpoints that already exist — the
   * upload parses to the same preview the paste box returns, and the commit is
   * the same bulk_*_commit the pop-out posts to. No second write path.
   *
   * cfg:
   *   mount, csrf
   *   urls          {preview, commit}
   *   intro         html above the file input
   *   extraFields() -> object  merged into the *upload* form data
   *   extraBody()   -> object  merged into the commit JSON
   *   label(item)   -> string  how one row is named in the preview
   *   renderResult(data) -> html
   *   onDone()      called after a successful save
   *
   * Two opt-outs, for the per-gene bench workbook, whose importer creates
   * sessions rather than editing rows:
   *   fileCommit    the commit re-posts the *file*, because that endpoint reads
   *                 the workbook rather than the TSV a paste produces
   *   renderPreview(data) -> html   replaces the row table
   *   canCommit(data) -> bool       whether there is anything to save; false
   *                 leaves the button hidden while the preview still explains why
   *   overwrite:false  hides the "also overwrite" tick — an import that only
   *                 ever creates has nothing to overwrite, and offering the
   *                 choice implies it might edit something
   */
  function uploadPanel(cfg) {
    // Namespaced per instance for the same reason newEntry is: two on one page
    // would silently share ids and the second would be dead.
    const ns = `up${++panelSeq}`;
    const id = n => `${ns}-${n}`;
    cfg.mount.innerHTML = `
      <div class="fixed inset-0 bg-black/40 z-50 flex items-start justify-center p-4 overflow-y-auto hidden"
           id="${id('scrim')}">
        <div class="bg-white rounded-xl shadow-xl max-w-3xl w-full mt-10 p-6">
          <div class="flex items-start justify-between gap-4">
            <div class="text-sm text-gray-600">${cfg.intro || ''}</div>
            <button type="button" id="${id('close')}"
                    class="text-gray-400 hover:text-gray-700 text-xl leading-none">&times;</button>
          </div>
          ${templateLink(cfg, id('template-receipt'))}
          <div class="mt-4 space-y-3">
            <input type="file" id="${id('file')}" accept=".xlsx,.xlsm,.csv,.tsv"
                   class="block w-full text-sm border border-gray-300 rounded-lg p-2">
            <label class="flex items-start gap-2 text-sm text-gray-700${
                     cfg.overwrite === false ? ' hidden' : ''}">
              <input type="checkbox" id="${id('overwrite')}" class="mt-1">
              <span>Also overwrite values that already exist
                <span class="block text-xs text-gray-500">Off by default: blanks are
                filled and what is already there is left alone.</span>
              </span>
            </label>
            ${cfg.extraHtml || ''}
            <div class="flex flex-wrap items-center gap-3">
              <button type="button" id="${id('preview')}"
                      class="bg-gray-900 text-white rounded-lg px-4 py-2 text-sm font-semibold">Preview changes</button>
              <button type="button" id="${id('commit')}"
                      class="hidden bg-ycharos-600 text-white rounded-lg px-4 py-2 text-sm font-semibold">Save these changes</button>
              ${/* Why the Preview button is greyed, in text beside it — the rule
                    the Add grid's save already follows: a button is disabled
                    with a reason, and the reason is on the page, not on a
                    `title` that a touch screen never shows. */''}
              <span id="${id('why')}" class="text-xs text-gray-500"
                    role="status" aria-live="polite"></span>
            </div>
            ${/* `role="status"`, and the receipt scrolls itself in — this sits at
                  the bottom of a `fixed inset-0 overflow-y-auto` modal, which is
                  the same tall scroller the grid banner is, and the twelfth field
                  test pressed Save here and reported seeing nothing at all. */''}
            <div id="${id('out')}" class="text-sm" role="status" aria-live="polite"></div>
          </div>
        </div>
      </div>`;

    const el = n => document.getElementById(id(n));
    const out = () => el('out');
    /* A refusal that shows nothing reads as an accepted save — the same reason
     * the grid's banner scrolls itself in. */
    const fail = msg => {
      out().innerHTML = `<p class="text-red-700">${esc(msg)}</p>`;
      bringIntoView(out());
    };
    // Same reason as in `newEntry`: this markup postdates any page-load pass.
    downloadReceipts(cfg.mount);

    // The text the preview was built from, kept so the save commits exactly what
    // was shown rather than re-reading a file that may have been swapped.
    let previewed = '';
    let busy = false;
    // A save has landed and its receipt is on screen, so the file in the input
    // is spent. See `setButtons`.
    let spent = false;

    function setButtons() {
      const p = el('preview'), c = el('commit');
      /* **Pressing Preview again must not quietly delete the receipt.** That is
       * one click from the state a reader reaches when they think the save did
       * nothing — and it is worse than silence, because the same file previews
       * a second time as *"0 new, 2 already known (will be updated)"*, which is
       * the panel contradicting the receipt it printed a moment earlier. So a
       * spent file disarms the button and says why, and choosing another file
       * arms it again. */
      const why = busy ? ''
        : (spent ? 'Saved — choose another file to upload again.' : '');
      p.disabled = busy || spent;
      p.classList.toggle('opacity-50', p.disabled);
      p.classList.toggle('cursor-not-allowed', p.disabled);
      p.title = why;
      el('why').textContent = why;
      if (c) { c.disabled = busy; c.classList.toggle('opacity-50', busy); }
    }

    function setBusy(on) { busy = on; setButtons(); }

    /* A new file is a deliberate fresh start, so the old receipt goes with it —
     * but only then, and never on its own. */
    el('file').addEventListener('change', () => {
      spent = false;
      previewed = '';
      el('commit').classList.add('hidden');
      out().innerHTML = '';
      setButtons();
    });

    el('preview').addEventListener('click', async () => {
      if (busy) return;
      const file = el('file').files[0];
      if (!file) { out().textContent = 'Choose a file first.'; return; }
      setBusy(true);
      try {
        const fd = new FormData();
        fd.append('file', file);
        Object.entries(cfg.extraFields ? cfg.extraFields() : {})
          .forEach(([k, v]) => fd.append(k, v));
        out().innerHTML = '<span class="text-gray-500">Reading the file…</span>';
        const d = await requestJson(cfg.urls.preview,
          {method: 'POST', headers: {'X-CSRFToken': cfg.csrf}, body: fd},
          fail, 'read');
        if (!d) return;
        if (cfg.renderPreview) {
          /* A preview that renders nothing reads as a click that did nothing.
           * That is exactly what run 11's Excel-saved workbook produced: a 200,
           * a clean console, and a panel that looked untouched. Whatever the
           * reason, say something — and prepend the sheet line here so every
           * board gets it without each renderPreview remembering to. */
          const body = cfg.renderPreview(d) || '';
          out().innerHTML = sheetLine(d) + (body ||
            `<p class="text-amber-800">Nothing was read from that file, so there is
             nothing to save.</p>`);
          // Whether there is anything to save is a separate question from what
          // the preview says: a refusal has to render its reason *and* keep the
          // button hidden, so the two cannot be the same return value.
          const ready = cfg.canCommit ? cfg.canCommit(d) : true;
          previewed = ready ? (cfg.fileCommit ? 'file' : (d.text || '')) : '';
          el('commit').classList.toggle('hidden', !previewed);
          return;
        }
        previewed = d.text || '';
        const s = d.summary || {};
        /* The same two refusals the paste box counts. A preview rule fixed in
         * one surface is a rule for one surface — these were on the Add panels
         * and not on the upload panel, which is the same file. */
        const dropped = checkNotes(s);
        out().innerHTML = `
          <div class="border border-gray-200 rounded-lg p-3 bg-gray-50">
            <p class="font-semibold text-gray-900">${sheetLine(d)}${plural(s.rows || 0, 'row')} read.</p>
            <p class="text-gray-700">${s.create || 0} new,
               ${s.update || 0} already known${s.update ? ' (will be updated)' : ''}${
                 s.blocked ? `, ${s.blocked} skipped` : ''}.</p>
            ${dropped ? `<ul class="mt-1 list-disc list-inside">${dropped}</ul>` : ''}
          </div>
          ${previewRows(d.items, cfg.label)}`;
        el('commit').classList.remove('hidden');
      } finally { setBusy(false); }
    });

    el('commit').addEventListener('click', async () => {
      if (busy || !previewed) return;
      setBusy(true);
      try {
        out().innerHTML = '<span class="text-gray-500">Saving…</span>';
        let d;
        if (cfg.fileCommit) {
          // The workbook itself, again: this endpoint reads the sheet rather
          // than a TSV, so there is no parsed text to hand back to it.
          const file = el('file').files[0];
          if (!file) { fail('Choose a file first.'); return; }
          const fd = new FormData();
          fd.append('file', file);
          Object.entries(cfg.extraFields ? cfg.extraFields() : {})
            .forEach(([k, v]) => fd.append(k, v));
          d = await requestJson(cfg.urls.commit,
            {method: 'POST', headers: {'X-CSRFToken': cfg.csrf}, body: fd}, fail);
        } else {
          const body = Object.assign(
            {text: previewed, dry_run: false,
             overwrite: el('overwrite').checked},
            cfg.extraBody ? cfg.extraBody() : {});
          d = await requestJson(cfg.urls.commit, {
            method: 'POST',
            headers: {'X-CSRFToken': cfg.csrf, 'Content-Type': 'application/json'},
            body: JSON.stringify(body),
          }, fail);
        }
        if (!d) return;
        out().innerHTML = cfg.renderResult(d);
        /* The same hook `newEntry` has, and for the same reason: a page with no
         * grid to redraw offers a button instead of reloading itself out from
         * under its own result line — the line that names a dropped
         * concentration is the one an auto-reload destroys. It was missing
         * here, so a gene page mounting this panel could configure it and get
         * silence: a config key nothing reads is the shape that shipped a
         * released defect once already. */
        if (cfg.afterResult) cfg.afterResult(out(), d);
        /* And then say it happened, where the reader is. The receipt is the
         * bottom element of a modal that scrolls, so a long preview can leave
         * it below the fold — which is a save that wrote and announced nothing,
         * the shape the twelfth field test reported: *"no Saved, no row count,
         * no show them on this page"* about two uploads that had both written
         * perfectly. */
        bringIntoView(out());
        // Hidden again so the same file cannot be saved twice by clicking on.
        el('commit').classList.add('hidden');
        previewed = '';
        spent = true;
        if (cfg.onDone) cfg.onDone();
      } finally { setBusy(false); }
    });

    const scrim = el('scrim');
    const dismiss = () => scrim.classList.add('hidden');
    el('close').addEventListener('click', dismiss);
    scrim.addEventListener('click', e => {
      if (e.target === scrim) dismiss();
    });
    escapeCloses(() => !scrim.classList.contains('hidden'), dismiss);
    return {
      open() { scrim.classList.remove('hidden'); },
      close() { scrim.classList.add('hidden'); },
    };
  }

  /* ─────────────────────────────────────────────────────────────────────
   * Changing what a row *is*.
   *
   * The boards refuse identity in a cell, and should: retyping a catalogue
   * number in a grid turns the row into a different antibody while every result
   * recorded against it stays attached. But a refusal needs somewhere to send
   * people, and until now that was the legacy edit page kept alive for this one
   * job. This is the replacement, and it is deliberately not an inline cell:
   *
   *   - it opens as a dialog, so the change is a decision, not a stray click;
   *   - it says what is attached *before* you type, because "eleven results and
   *     two figures follow this row" is the difference between renaming a thing
   *     and rewriting the history of eleven experiments;
   *   - it saves nothing until the fields differ from what was loaded.
   *
   * cfg:
   *   mount, csrf
   *   urls        {load, save}
   *   idParam     name of the row-id parameter the endpoints expect
   *   fields      [{name, label, hint, options?}] what identity means here
   *   describe(identity) -> html   the "what follows this" sentence
   *   query()     -> string        current filters, so the redraw honours them
   *   onSaved(data)                given the patch payload, to redraw the row
   */
  /* Deleting one record — a pop-out of its own, mountable by any board.
   *
   * It lived inside `identityDialog`, which gave the two boards that *have* an
   * identity dialog a delete button and left the other two with none — and
   * targets, the thing actually asked for ("we created a mock gene USP8 that
   * could be removed"), were one of the two without. A pop-out any board can
   * mount is the fix, and it keeps one implementation of the flow rather than a
   * second copy on the boards that were missed.
   *
   * Three steps, none of them a reflex: shut, then a preview that names what
   * would go, then the record's own name typed out. No `confirm()` — one
   * reflexive click is exactly what this replaces. The server re-asks both
   * questions on the commit, because a preview is not a permission slip.
   *
   * cfg: mount, csrf, kind, urls {preview, commit}, onDeleted(id, data)
   */
  function deleteDialog(cfg) {
    const ns = `del${++panelSeq}`;
    const id = n => `${ns}-${n}`;
    cfg.mount.innerHTML = `
      <div id="${id('scrim')}" class="hidden fixed inset-0 bg-black/40 z-50 flex items-start justify-center p-4 overflow-y-auto">
        <div class="bg-white rounded-xl shadow-xl w-full max-w-lg mt-16 p-5" role="dialog" aria-modal="true">
          <div class="flex items-start justify-between gap-4">
            <h2 class="text-lg font-bold text-gray-900">Delete this record</h2>
            <button type="button" id="${id('close')}"
                    class="text-gray-400 hover:text-gray-700 font-bold text-xl leading-none">&times;</button>
          </div>
          <div id="${id('why')}" class="mt-3 text-sm"></div>
          ${/* A warning box with a second, explicit press — the owner's call
                after using it. The first press opens this and asks the server;
                the second is the one that writes. Typing the record's own name
                was the earlier design and was more friction than it bought:
                nothing here can be reached without reading a panel that names
                what goes. */''}
          <div id="${id('confirm')}" class="hidden mt-3">
            <label class="flex items-start gap-2 text-sm text-gray-800">
              <input type="checkbox" id="${id('ack')}" class="mt-0.5 rounded border-gray-300">
              <span>I understand this deletes
                <span id="${id('word')}" class="font-bold"></span>
                and cannot be undone from the app.</span>
            </label>
          </div>
          <div class="mt-4 flex items-center gap-3">
            <button type="button" id="${id('go')}" disabled
                    class="hidden bg-red-700 text-white rounded-lg px-4 py-2 text-sm font-semibold">Delete permanently</button>
            <button type="button" id="${id('cancel')}"
                    class="text-sm text-gray-500 hover:text-gray-800">Cancel</button>
          </div>
        </div>
      </div>`;

    const el = n => document.getElementById(id(n));
    // `agreed` is the number this panel put on the button. The commit sends it
    // back and the server refuses if what is behind the record has changed
    // since — re-asking "are you still allowed?" would answer yes while the
    // count grew from 3 to 30.
    let rowId = null, confirmWord = '', agreed = null;
    const close = () => el('scrim').classList.add('hidden');
    el('close').addEventListener('click', close);
    el('cancel').addEventListener('click', close);
    escapeCloses(() => !el('scrim').classList.contains('hidden'), close);

    const post = (url, body) => requestJson(url, {
      method: 'POST',
      headers: {'X-CSRFToken': cfg.csrf,
                'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams(body).toString(),
    }, msg => { el('why').innerHTML = `<p class="text-red-700">${esc(msg)}</p>`; });

    async function open(recordId) {
      rowId = recordId;
      el('why').innerHTML = '<p class="text-gray-400">Checking…</p>';
      el('confirm').classList.add('hidden');
      el('go').classList.add('hidden');
      el('ack').checked = false;
      confirmWord = '';
      agreed = null;
      el('scrim').classList.remove('hidden');
      const d = await post(cfg.urls.preview, {kind: cfg.kind, id: recordId});
      if (!d) return;
      const p = d.plan || {};
      if (!p.allowed) {
        el('why').innerHTML = `<p class="text-red-800">${esc(p.why)}</p>`;
        return;
      }
      if (p.overriding) {
        /* **A superuser deleting through the refusals is a different act, and
         * must not look like the ordinary one.** The counts come from Django's
         * own collector, so they are transitive — deleting a gene takes its
         * antibodies, and those take their results and their published figures.
         * Listed line by line with a total, because "22 antibodies" understates
         * it by two levels and a person cannot agree to what they have not been
         * shown. */
        const list = rows => rows.map(
          c => `<li>${c.count.toLocaleString()} ${esc(c.noun)}</li>`).join('');
        const goes = p.cascade_total || 0;
        const left = p.orphaned_total || 0;
        /* **The rows that survive, broken, are the half a delete count cannot
         * see.** Nothing cascades off a wild type, so a panel built from
         * `cascade` alone says "will also delete nothing else" over three
         * sessions about to lose the control line they were run against. */
        const orphaned = left ? `
            <p class="mt-2 text-red-900">And <b>${left.toLocaleString()}</b>
              record${left === 1 ? '' : 's'} would be left on file with a gap
              where this used to be:</p>
            <ul class="mt-1 ml-5 list-disc text-red-900">${list(p.orphaned)}</ul>` : '';
        el('why').innerHTML = `
          <div class="border-2 border-red-600 bg-red-50 rounded-lg p-3">
            <p class="font-bold text-red-900">${esc(p.label)} has work behind it.</p>
            ${goes ? `<p class="mt-1 text-red-900">Deleting it will also permanently
              delete <b>${goes.toLocaleString()}</b> other record${
                goes === 1 ? '' : 's'}:</p>
            <ul class="mt-1 ml-5 list-disc text-red-900">${list(p.cascade)}</ul>`
              : '<p class="mt-1 text-red-900">Nothing else would be deleted.</p>'}
            ${orphaned}
            <p class="mt-2 text-red-900">This cannot be undone from the app —
              Render's point-in-time recovery is the only way back.</p>
          </div>`;
        confirmWord = p.confirm_with;
        agreed = goes + left;
        el('word').textContent = p.label;
        el('confirm').classList.remove('hidden');
        el('go').classList.remove('hidden');
        el('go').disabled = true;
        el('go').classList.add('opacity-50');
        el('go').textContent = goes
          ? `Delete it and ${goes.toLocaleString()} other record${goes === 1 ? '' : 's'}`
          : 'Delete permanently';
        return;
      }
      el('go').textContent = 'Delete permanently';
      const also = (p.owned || []).length
        ? `<p class="mt-1">Its own records go too: ${
            p.owned.map(o => `${o.count} ${esc(o.noun)}`).join(', ')}.</p>` : '';
      el('why').innerHTML =
        `<p class="text-gray-800">Nothing else points at <b>${esc(p.label)}</b>,
          so it can be removed.${also}
          <span class="block mt-1 text-gray-500">This cannot be undone from the
          app.</span></p>`;
      confirmWord = p.confirm_with;
      el('word').textContent = p.label;
      el('confirm').classList.remove('hidden');
      el('go').classList.remove('hidden');
      el('go').disabled = true;
      el('go').classList.add('opacity-50');
    }

    // The tick arms the button; nothing else does. Two deliberate presses with
    // a manifest between them.
    el('ack').addEventListener('change', () => {
      el('go').disabled = !el('ack').checked;
      el('go').classList.toggle('opacity-50', !el('ack').checked);
    });

    el('go').addEventListener('click', async () => {
      if (!el('ack').checked) return;
      const body = {kind: cfg.kind, id: rowId, confirm: confirmWord};
      if (agreed !== null) body.agreed = agreed;
      const d = await post(cfg.urls.commit, body);
      if (!d) return;
      close();
      if (cfg.onDeleted) cfg.onDeleted(rowId, d);
    });

    return {open, close};
  }

  function identityDialog(cfg) {
    const ns = `idty${++panelSeq}`;
    const id = n => `${ns}-${n}`;
    cfg.mount.innerHTML = `
      <div id="${id('scrim')}" class="hidden fixed inset-0 bg-black/40 z-50 flex items-start justify-center p-4 overflow-y-auto">
        <div class="bg-white rounded-xl shadow-xl max-w-lg w-full mt-16 p-6">
          <div class="flex items-start justify-between gap-4">
            <h2 class="text-lg font-bold text-gray-900">Change what this is</h2>
            <button type="button" id="${id('close')}"
                    class="text-gray-400 hover:text-gray-700 text-xl leading-none">&times;</button>
          </div>
          <div id="${id('warn')}" class="mt-3 text-sm"></div>
          <div id="${id('fields')}" class="mt-4 space-y-3"></div>
          <div id="${id('out')}" class="mt-3 text-sm"></div>
          <div class="mt-4 flex items-center gap-3">
            <button type="button" id="${id('save')}"
                    class="bg-ycharos-600 text-white rounded-lg px-4 py-2 text-sm font-semibold">Save the change</button>
            <button type="button" id="${id('cancel')}"
                    class="text-sm text-gray-500 hover:text-gray-800">Cancel</button>
        </div>
      </div>`;

    const el = n => document.getElementById(id(n));
    let rowId = null, loaded = {};

    const close = () => el('scrim').classList.add('hidden');
    el('close').addEventListener('click', close);
    el('cancel').addEventListener('click', close);
    el('scrim').addEventListener('click', e => {
      if (e.target === el('scrim')) close();
    });
    escapeCloses(() => !el('scrim').classList.contains('hidden'), close);

    function drawFields(identity) {
      el('fields').innerHTML = cfg.fields.map(f => {
        const value = identity[f.name] || '';
        const input = f.options
          ? `<select data-f="${f.name}" class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
               ${f.options.map(o => `<option value="${esc(o)}"${
                 o === value ? ' selected' : ''}>${esc(o)}</option>`).join('')}
             </select>`
          : `<input data-f="${f.name}" value="${esc(value)}"
                    class="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">`;
        return `<label class="block">
          <span class="block text-xs font-semibold text-gray-500 mb-1">${esc(f.label)}</span>
          ${input}
          ${f.hint ? `<span class="block text-xs text-gray-400 mt-1">${f.hint}</span>` : ''}
        </label>`;
      }).join('');
    }

    async function open(targetId) {
      rowId = targetId;
      el('out').innerHTML = '';
      el('warn').innerHTML = '<span class="text-gray-500">Loading…</span>';
      el('fields').innerHTML = '';
      el('scrim').classList.remove('hidden');
      const d = await requestJson(
        `${cfg.urls.load}?${cfg.idParam}=${encodeURIComponent(targetId)}`,
        {headers: {'X-Requested-With': 'fetch'}},
        msg => { el('warn').innerHTML = `<p class="text-red-700">${esc(msg)}</p>`; },
        'read');
      if (!d) return;
      loaded = d.identity;
      el('warn').innerHTML = cfg.describe(loaded);
      drawFields(loaded);
    }

    async function saveChange() {
      const body = {};
      body[cfg.idParam] = rowId;
      let changed = false;
      cfg.fields.forEach(f => {
        const node = el('fields').querySelector(`[data-f="${f.name}"]`);
        body[f.name] = node ? node.value : '';
        if ((body[f.name] || '') !== (loaded[f.name] || '')) changed = true;
      });
      if (!changed) {
        el('out').innerHTML = '<p class="text-gray-500">Nothing is different — nothing saved.</p>';
        return;
      }
      el('out').innerHTML = '<span class="text-gray-500">Saving…</span>';
      const d = await requestJson(`${cfg.urls.save}?${cfg.query ? cfg.query() : ''}`, {
        method: 'POST',
        headers: {'X-CSRFToken': cfg.csrf,
                  'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams(body).toString(),
      }, msg => { el('out').innerHTML = `<p class="text-red-700">${esc(msg)}</p>`; });
      if (!d) return;
      close();
      if (cfg.onSaved) cfg.onSaved(rowId, d);
    }

    el('save').addEventListener('click', saveChange);

    /* Enter saves, as it does in every cell on every board.
     *
     * These are bare inputs in plain divs, with no form and no keydown handler,
     * so Enter did nothing at all here — no save, no close, no message. Exactly
     * the shape of the Escape gap already fixed on this dialog: the grid taught
     * the habit and the one pop-out that mattered most ignored it. Scoped to the
     * text inputs so a <select> keeps its own keyboard behaviour. */
    el('fields').addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      if (!e.target.matches || !e.target.matches('input[data-f]')) return;
      e.preventDefault();
      saveChange();
    });

    return {open, close};
  }

  /* ─────────────────────────────────────────────────────────────────────
   * Raw data against a session — the gel scan, the Ponceau, the .fcs the
   * readings were taken off.
   *
   * It lived inside `session_board.html`, reachable only by opening a session's
   * drawer on the sessions board — and it is the panel that decides what a
   * Zenodo deposit actually contains: `services/deposit.py` packages every
   * `FileAttachment` on the gene into `<GENE>_underlying_data.zip`. So the
   * deposit button sat on the gene's own page saying "0 raw file(s)" with no
   * way, on that page, to make it say anything else. A record whose underlying
   * data is an empty zip is the shape of published work with nothing under it.
   *
   * It is here rather than copied into the gene page for the reason this repo
   * keeps relearning one surface at a time: a preview rule fixed in one
   * template is a rule for one template. Two panels writing raw lab data
   * through the same endpoints would drift on which categories they offer, on
   * whether an armed Remove disarms, and on whether a failed read draws "no
   * files yet" — and the reader has no way to tell which of the two is the
   * broken one.
   *
   * `cfg`:
   *   root      element the panels live inside; the click handler is delegated
   *             from it, because a panel redraws itself and a listener bound to
   *             the markup stops firing the first time it does
   *   csrf      the token
   *   urls      {list, upload, remove}
   *   onChange  optional (sessionId, {files}) after a file is attached or
   *             removed — a count drawn elsewhere on the page is stale
   *             otherwise, and two numbers disagreeing on one screen reads as
   *             data loss
   * ───────────────────────────────────────────────────────────────────── */
  function filesPanel(cfg) {
    const root = cfg.root;
    const urls = cfg.urls || {};

    /* A small file is reported in bytes, not as "0 KB".
     *
     * Rounding to KB prints `0 KB` for anything under half a kilobyte, which
     * reads as a file that failed to upload — and the row it sits on is the
     * app's only evidence that the upload worked. The empty case is refused at
     * the server, so a zero here would always be a lie. */
    function fileSize(bytes) {
      if (!(bytes > 0)) return '';
      if (bytes < 1024) return `${bytes} bytes`;
      const kb = bytes / 1024;
      return kb < 1024 ? `${Math.round(kb)} KB` : `${(kb / 1024).toFixed(1)} MB`;
    }

    function panelFor(sessionId) {
      return root.querySelector(`.files-panel[data-session="${sessionId}"]`);
    }

    /* The shell. Drawn with whatever is around it and filled in by its own
     * fetch, because attaching a file changes no result row: an upload redraws
     * the files and nothing else, the same reason a cell edit redraws one row
     * rather than the grid. */
    function shell(sessionId) {
      return `<div class="bg-white border border-gray-200 rounded-lg p-3 files-panel"
                   data-session="${esc(sessionId)}">
        <p class="text-xs font-bold uppercase tracking-wide text-gray-600">Raw data &amp; files</p>
        <p class="text-sm text-gray-600 mt-1">
          The scan, the plate overview, the instrument file &mdash; what these
          readings were taken off. Kept against this session, so a result can be
          traced back to the image behind it, and gathered into the gene's
          Zenodo deposit as its underlying data.</p>
        <div class="files-body mt-2 text-sm">
          <p class="text-gray-400">Loading files&hellip;</p>
        </div>
      </div>`;
    }

    /* One stored file. The name is the download and it announces itself, like
     * every other download in the app — a plain <a href> writes the file and
     * the page does not move, which a field test read as a broken feature. */
    function fileRow(sessionId, f) {
      const bits = [f.category_label, fileSize(f.size_bytes),
                    f.result_label, f.uploaded_by, f.uploaded_at]
        .filter(Boolean).map(esc).join(' &middot; ');
      return `<li class="flex items-start justify-between gap-3 py-1.5 border-b border-gray-100 last:border-0">
        <div class="min-w-0">
          <a href="${esc(f.download_url)}" data-receipt="files-receipt-${esc(sessionId)}"
             class="font-mono text-sm text-ycharos-700 hover:underline break-all">${esc(f.filename)}</a>
          <p class="text-xs text-gray-500">${bits}</p>
          ${f.description ? `<p class="text-xs text-gray-600">${esc(f.description)}</p>` : ''}
        </div>
        <button type="button" class="file-remove shrink-0 text-xs font-semibold text-red-700 hover:underline"
                data-id="${esc(f.id)}" data-name="${esc(f.filename)}">Remove</button>
      </li>`;
    }

    /* What the panel says when there is nothing yet: an instruction, not a
     * value — the same rule as an empty cell's prompt. "No files" is a
     * statement of fact that tells nobody what to do about it. */
    function bodyHtml(sessionId, d) {
      const rows = d.rows || [];
      const list = rows.length
        ? `<ul class="mb-3">${rows.map(f => fileRow(sessionId, f)).join('')}</ul>
           <p class="text-xs text-gray-500 mb-2">${
             plural(rows.length, 'file')} on this session.</p>`
        : `<p class="text-gray-500 mb-3">No files yet &mdash; attach the scan or
           instrument file these readings came from.</p>`;

      const cats = (d.categories || []).map(
        c => `<option value="${esc(c.key)}">${esc(c.label)}</option>`).join('');
      /* Which result row, if any. "The whole session" is first and is the
       * default, because a Ponceau or a plate overview is about the run rather
       * than about one antibody, and guessing a row for it would file the image
       * under a reagent it is not evidence about.
       *
       * The options are the server's (`attachments.result_options`), not
       * whatever rows the calling page has drawn. */
      const rowOpts = ['<option value="">The whole session</option>'].concat(
        (d.results || []).map(
          r => `<option value="${esc(r.id)}">${esc(r.label)}</option>`)
      ).join('');

      const form = d.may_write ? `
        <div class="border-t border-gray-100 pt-2 grid gap-2 sm:grid-cols-2">
          <label class="text-xs text-gray-500">Kind of file
            <select class="file-category mt-0.5 w-full border border-gray-300 rounded-lg px-2 py-1 text-sm">${cats}</select></label>
          <label class="text-xs text-gray-500">What it shows
            <select class="file-result mt-0.5 w-full border border-gray-300 rounded-lg px-2 py-1 text-sm">${rowOpts}</select></label>
          <label class="text-xs text-gray-500 sm:col-span-2">File
            <input type="file" class="file-input mt-0.5 block w-full text-sm"></label>
          <label class="text-xs text-gray-500 sm:col-span-2">Description (optional)
            <input type="text" class="file-description mt-0.5 w-full border border-gray-300 rounded-lg px-2 py-1 text-sm"
                   placeholder="e.g. 10 s exposure, full uncropped membrane"></label>
          <div class="sm:col-span-2 flex items-center gap-3">
            <button type="button" class="file-attach px-3 py-1.5 rounded-lg bg-ycharos-600 text-white text-sm font-semibold">Attach</button>
            <span class="text-xs text-gray-400">Up to ${esc(d.max_mb)} MB per file.</span>
          </div>
        </div>`
        /* Greyed with the reason on the page, not hidden and not on `title`:
         * a control that is simply absent is a feature a reader concludes does
         * not exist, and a tooltip explains nothing on a touch screen. */
        : `<div class="border-t border-gray-100 pt-2">
             <button type="button" disabled
                     class="px-3 py-1.5 rounded-lg bg-gray-200 text-gray-500 text-sm font-semibold cursor-not-allowed">Attach</button>
             <p class="text-xs text-amber-800 mt-1">${esc(d.why_not || '')}</p>
           </div>`;

      return `${list}
        <p id="files-receipt-${esc(sessionId)}" class="text-sm mb-2" role="status" aria-live="polite"></p>
        ${form}
        <div class="files-out mt-2 text-sm" role="alert"></div>`;
    }

    async function load(sessionId) {
      let panel = panelFor(sessionId);
      if (!panel) return null;
      const body = panel.querySelector('.files-body');
      /* A failed read is not an empty result. Without the fourth argument this
       * would fall back to drawing "No files yet", which is an assertion the
       * page has no basis for — and the one it would make about a session whose
       * files exist and could not be fetched. */
      const d = await requestJson(
        `${urls.list}?session_id=${encodeURIComponent(sessionId)}`,
        {headers: {'X-Requested-With': 'fetch'}},
        msg => { body.innerHTML = `<p class="text-red-700">${esc(msg)}</p>`; },
        'read');
      if (!d || d.ok === false) return null;
      /* Re-found after the await, never reused: the drawer may have been closed
       * or switched to another session while this was in flight, in which case
       * the panel this started in is detached and writing to it paints a screen
       * nobody is looking at. */
      panel = panelFor(sessionId);
      if (!panel) return null;
      panel.querySelector('.files-body').innerHTML = bodyHtml(sessionId, d);
      downloadReceipts(panel);
      return d;
    }

    /* Attaching and removing. Delegated, because the panel redraws itself. */
    root.addEventListener('click', async e => {
      const panel = e.target.closest('.files-panel');
      if (!panel || !root.contains(panel)) return;
      const id = panel.dataset.session;
      const out = panel.querySelector('.files-out');
      const say = (html, tone) => { if (out) out.innerHTML = `<p class="${tone}">${html}</p>`; };
      const fail = msg => say(esc(msg), 'text-red-700');

      /* An armed Remove disarms the moment attention moves. A button left
       * saying "press again to remove" while you do something else is a delete
       * waiting for an unrelated click. */
      const clicked = e.target.closest('.file-remove');
      panel.querySelectorAll('.file-remove[data-armed="1"]').forEach(b => {
        if (b === clicked) return;
        b.dataset.armed = ''; b.textContent = 'Remove';
      });

      const after = (verb, name, tone) => {
        const box = panelFor(id) && panelFor(id).querySelector('.files-out');
        if (box) box.innerHTML =
          `<p class="${tone}">${verb} <span class="font-mono">${esc(name)}</span>.</p>`;
      };

      const attach = e.target.closest('.file-attach');
      if (attach) {
        const input = panel.querySelector('.file-input');
        const file = input && input.files[0];
        if (!file) { fail('Choose a file first.'); return; }
        const fd = new FormData();
        fd.append('session_id', id);
        fd.append('file', file);
        fd.append('category', panel.querySelector('.file-category').value);
        fd.append('result_id', panel.querySelector('.file-result').value);
        fd.append('description', panel.querySelector('.file-description').value);
        attach.disabled = true;
        say('Attaching&hellip;', 'text-gray-500');
        const d = await requestJson(
          urls.upload,
          {method: 'POST', headers: {'X-CSRFToken': cfg.csrf}, body: fd}, fail);
        attach.disabled = false;
        if (!d) return;
        if (d.ok === false) { fail((d.errors || []).join(' ')); return; }
        /* Redrawn in place. Never "reload the page to see it" — and never a
         * reload done for them either, which would take this line with it. */
        const fresh = await load(id);
        after('Attached', file.name, 'text-emerald-700');
        if (cfg.onChange) cfg.onChange(id, {files: (fresh && fresh.rows || []).length});
        return;
      }

      /* Remove is two clicks and no red manifest: nothing points at an
       * attachment, and `services/deletion.py`'s panel is for a delete that
       * reaches other rows. Using it here is how it stops meaning anything. */
      const rm = clicked;
      if (!rm) return;
      if (rm.dataset.armed !== '1') {
        rm.dataset.armed = '1';
        rm.textContent = 'Press again to remove';
        return;
      }
      const fd = new FormData();
      fd.append('id', rm.dataset.id);
      say('Removing&hellip;', 'text-gray-500');
      const d = await requestJson(
        urls.remove,
        {method: 'POST', headers: {'X-CSRFToken': cfg.csrf}, body: fd}, fail);
      if (!d) return;
      if (d.ok === false) { fail((d.errors || []).join(' ')); return; }
      const fresh = await load(id);
      after('Removed', d.filename || '', 'text-gray-700');
      if (cfg.onChange) cfg.onChange(id, {files: (fresh && fresh.rows || []).length});
    });

    return {shell, load};
  }

  /* ─────────────────────────────────────────────────────────────────────
   * A download that says it happened.
   *
   * A plain <a href> to an export is invisible: the browser writes the file to
   * the Downloads folder and the page does not move, flash or say anything. The
   * sixth field test pressed Generate Report, watched nothing happen, and filed
   * the app's best output as a broken feature — with a directory listing open.
   * It got the same silence from the bench workbook. If a tester with a file
   * system to check reads it as failure, a scientist certainly will.
   *
   * So the click fetches the bytes itself and hands them to the browser as a
   * blob. That way the page knows the file actually arrived and can name it and
   * its size, rather than announcing a download it only asked for. A failure
   * says so in the page — never an alert, and the detail goes to the console for
   * whoever is debugging, not to the scientist.
   *
   * One download per real click, so this is not the unattended `a.click()` loop
   * Chrome throttles.
   *
   * Mark the anchor `data-receipt="<id of the element to write into>"`; every
   * such anchor on the page is wired by `downloadReceipts()`.
   */
  function humanSize(bytes) {
    if (!(bytes > 0)) return '';
    const kb = bytes / 1024;
    return kb < 1024 ? ` (${Math.round(kb)} KB)` : ` (${(kb / 1024).toFixed(1)} MB)`;
  }

  function filenameFrom(disposition, fallback) {
    const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition || '');
    return m ? decodeURIComponent(m[1]) : fallback;
  }

  async function downloadWithReceipt(a, out) {
    const say = (html, tone) => {
      if (out) out.innerHTML = `<span class="${tone}">${html}</span>`;
    };
    say('Preparing the file…', 'text-gray-500');
    let resp;
    try {
      resp = await fetch(a.href, {headers: {'X-Requested-With': 'fetch'}});
    } catch (e) {
      console.error('download failed', e);
      resp = null;
    }
    if (!resp || !resp.ok) {
      if (resp) console.error('download failed', resp.status, resp.statusText);
      say('That file could not be built, so nothing was downloaded. ' +
          'Nothing has been changed — try again, and tell whoever looks after ' +
          'the site if it keeps happening.', 'text-red-700');
      return;
    }
    const blob = await resp.blob();
    const name = filenameFrom(resp.headers.get('Content-Disposition'),
                              a.dataset.filename || 'download');
    const url = URL.createObjectURL(blob);
    const tmp = document.createElement('a');
    tmp.href = url;
    tmp.download = name;
    document.body.appendChild(tmp);
    tmp.click();
    tmp.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    say(`Downloaded <span class="font-mono">${esc(name)}</span>${
      esc(humanSize(blob.size))} — it is in your Downloads folder.`,
        'text-emerald-700');
  }

  /* `data-confirm` makes one press deliberate without taking the control away.
   *
   * The sibling of `confirmWholeDownload`, and the same trade: a download
   * writes nothing, so gating it the way a save is gated would remove an output
   * somebody legitimately wants — but a file that comes back looking like a
   * finding and is mostly placeholder is worth naming first. Generate Report on
   * a gene with no readings is the case it was written for. Absent, the click
   * is unchanged. */
  function downloadReceipts(root) {
    (root || document).querySelectorAll('a[data-receipt]').forEach(a => {
      if (a.dataset.receiptWired) return;
      a.dataset.receiptWired = '1';
      a.addEventListener('click', ev => {
        ev.preventDefault();
        if (a.dataset.confirm && !window.confirm(a.dataset.confirm)) return;
        downloadWithReceipt(a, document.getElementById(a.dataset.receipt));
      });
    });
  }


  /* ── Assign numbers ────────────────────────────────────────────────────────
   *
   * Assign or rearrange a bench's A-numbers, as a whole mapping rather than a
   * cell. The A-number decides which freezer box a vial goes in, so a bench
   * receiving reagents over weeks wants to group a protein's antibodies
   * together *after* they have all arrived — which is why uOttawa stopped
   * logging into the portal and kept a spreadsheet instead.
   *
   * **Two pages mount this, and that is why it lives here.** The antibodies
   * board points it at its filter form; a gene's own page points it at that
   * gene. A second copy in the second template is how the two would come to
   * refuse different things, print different counts, or arm the button on
   * different conditions — the failure this repo records more than any other,
   * and one this panel would show off badly, because the thing it writes is a
   * number somebody puts on a freezer box.
   *
   * `cfg.query()` is the only difference between the two: it returns the query
   * string naming the rows this page means, and the server resolves them from
   * it, so a paginated board cannot renumber half of them.
   *
   * Two presses, deliberately. The first writes nothing and draws every
   * old → new pair; the second sends the count *and* the mapping's stamp it was
   * shown, and the server refuses a set that moved in between.
   */
  function renumberPanel(cfg) {
    const mount = cfg.mount;
    const urls = cfg.urls || {};
    let plan = null;

    const el = id => document.getElementById(id);

    function row(it) {
      if (it.held) {
        return `<tr class="text-gray-500">
          <td class="px-2 py-1">${esc(it.label)}</td>
          <td class="px-2 py-1 whitespace-nowrap">${esc(it.old) || DASH}</td>
          <td class="px-2 py-1 whitespace-nowrap">${pill('keeps it', 'warn')}</td>
          <td class="px-2 py-1 text-xs">${esc(it.held)}</td></tr>`;
      }
      return `<tr${it.moves ? '' : ' class="text-gray-400"'}>
        <td class="px-2 py-1">${esc(it.label)}</td>
        <td class="px-2 py-1 whitespace-nowrap">${esc(it.old) || '<span class="text-gray-400">not numbered</span>'}</td>
        <td class="px-2 py-1 whitespace-nowrap font-semibold">${esc(it.new)}</td>
        <td class="px-2 py-1 text-xs">${it.moves ? '' : 'unchanged'}</td></tr>`;
    }

    function draw(d) {
      const out = el('rn-out');
      if (d.refusal) {
        out.innerHTML = `<p class="text-red-800">${esc(d.refusal)}</p>`;
        arm(false, '');
        bringIntoView(out);
        return;
      }
      const held = (d.items || []).filter(i => i.held).length;
      out.innerHTML = `
        <p class="font-semibold text-gray-900">${plural(d.moving, 'number')} would change${
          held ? `, and ${plural(held, 'antibody', 'antibodies')} keep${held === 1 ? 's' : ''} what it has` : ''}.</p>
        ${held ? `<p class="text-xs text-gray-500 mt-1">An antibody that has been used
          is left alone — a reading, a published figure, a queued crop, or a
          planned experiment. Its number is on a bench sheet or under a figure by
          now, and moving it would file a later reading against a different
          antibody.</p>` : ''}
        <div class="mt-2 border border-gray-200 rounded-lg overflow-hidden">
          <div class="max-h-72 overflow-y-auto">
            <table class="text-xs w-full">
              <thead class="bg-gray-50 text-gray-500 uppercase tracking-wide sticky top-0">
                <tr><th class="px-2 py-1 text-left font-semibold">Antibody</th>
                    <th class="px-2 py-1 text-left font-semibold">Now</th>
                    <th class="px-2 py-1 text-left font-semibold">Would become</th>
                    <th class="px-2 py-1"></th></tr>
              </thead>
              <tbody class="divide-y divide-gray-100">${(d.items || []).map(row).join('')}</tbody>
            </table></div></div>`;
      arm(d.moving > 0, d.moving > 0 ? '' :
        'Nothing would change — these antibodies already have these numbers.');
      bringIntoView(out);
    }

    function arm(on, why) {
      const go = el('rn-go');
      go.disabled = !on;
      go.classList.toggle('opacity-50', !on);
      go.classList.toggle('cursor-not-allowed', !on);
      go.textContent = on && plan
        ? `Change ${plural(plan.moving, 'number')}`
        : 'Change the numbers';
      el('rn-why').textContent = on ? '' : (why || '');
    }

    function open() {
      plan = null;
      mount.innerHTML = `
        <div class="border border-gray-200 rounded-xl bg-white p-4 mt-4">
          <div class="flex items-start justify-between gap-4">
            <div class="text-sm text-gray-600 max-w-2xl">
              <b class="text-gray-900">Assign or rearrange A-numbers.</b>
              This works on <b>${esc(cfg.scope)}</b>, in the order they are shown —
              gene, then supplier, then catalogue — so numbering them as shown puts
              each protein's antibodies together.
              <span class="block mt-2 text-gray-500">Leave the first number blank
              to carry on from this bench's highest. Type one to re-deal a block
              you have already used — the numbers move together, in one go, so
              two vials are never both holding the same one.</span>
              <span class="block mt-2 text-amber-800">Only do this while the
              numbers are not yet written on the tubes. Nothing in the app can
              know whether they are.</span>
              <span class="block mt-2 text-gray-500">Your own bench's antibodies,
              unless you are a superuser.</span>
            </div>
            <button type="button" id="rn-close"
                    class="text-gray-400 hover:text-gray-700 font-bold text-lg leading-none">&times;</button>
          </div>
          <div class="mt-3 flex flex-wrap items-end gap-3">
            <label class="block">
              <span class="block text-xs font-semibold text-gray-500 mb-1">First number</span>
              <input type="text" id="rn-start" placeholder="this bench's next"
                     class="w-44 border border-gray-300 rounded-lg px-3 py-2 text-sm">
            </label>
            <button type="button" id="rn-check"
                    class="px-4 py-2 rounded-lg bg-gray-900 text-white font-semibold text-sm">Check these</button>
            <button type="button" id="rn-go" disabled aria-describedby="rn-why"
                    class="px-4 py-2 rounded-lg bg-ycharos-600 text-white font-semibold text-sm opacity-50 cursor-not-allowed">Change the numbers</button>
            <span id="rn-why" class="text-xs text-gray-500"></span>
          </div>
          <div id="rn-out" class="mt-3 text-sm" role="alert"></div>
        </div>`;

      el('rn-close').addEventListener('click', () => { mount.innerHTML = ''; });

      el('rn-check').addEventListener('click', async () => {
        const body = new URLSearchParams({start: el('rn-start').value});
        const d = await requestJson(`${urls.plan}?${cfg.query()}`,
          {method: 'POST',
           headers: {'X-CSRFToken': cfg.csrf,
                     'Content-Type': 'application/x-www-form-urlencoded'},
           body: body.toString()},
          msg => { el('rn-out').innerHTML = `<p class="text-red-700">${esc(msg)}</p>`;
                   arm(false, ''); },
          'read');
        if (!d) return;
        plan = d;
        draw(d);
      });

      el('rn-go').addEventListener('click', async () => {
        if (!plan) return;
        /* The count *and* the mapping the button offered go with the press. A
           count alone is not consent: one row leaving the set as another joins
           keeps the count and changes which antibodies move. */
        const body = new URLSearchParams({
          start: el('rn-start').value,
          consented_count: String(plan.moving),
          stamp: plan.stamp || '',
        });
        const d = await requestJson(`${urls.apply}?${cfg.query()}`,
          {method: 'POST',
           headers: {'X-CSRFToken': cfg.csrf,
                     'Content-Type': 'application/x-www-form-urlencoded'},
           body: body.toString()},
          msg => { el('rn-out').innerHTML = `<p class="text-red-700">${esc(msg)}</p>`; });
        if (!d) return;
        const changed = d.changed || [];
        el('rn-out').innerHTML = `
          <div class="border border-emerald-200 bg-emerald-50 rounded-lg p-3 text-emerald-900">
            <b>${plural(changed.length, 'number')} changed at ${esc(d.site)}.</b>
            ${changed.length ? `<ul class="mt-1 list-disc list-inside text-xs">${
              changed.slice(0, 12).map(c =>
                `<li>${esc(c.was) || 'not numbered'} → <b>${esc(c.now)}</b></li>`).join('')}${
              changed.length > 12 ? `<li>and ${changed.length - 12} more</li>` : ''}</ul>` : ''}
            ${cfg.afterNote ? cfg.afterNote(d) : ''}
          </div>`;
        plan = null;
        arm(false, 'Done — press Check these again to look at another set.');
        bringIntoView(el('rn-out'));
        if (cfg.onDone) cfg.onDone(d);
      });

      bringIntoView(mount);
    }

    /* Registered once, not inside `open` — `escapeCloses` adds a document
       listener and the panel is rebuilt on every press of the button, so
       registering there stacks a fresh listener per open. It takes a
       *predicate*, not an element: the mount is empty exactly when the panel is
       closed. */
    escapeCloses(() => mount.innerHTML !== '', () => { mount.innerHTML = ''; });
    return {open};
  }

  return {esc, pill, cell, doiCell, dateText, formQuery, banner, requestJson, create, newEntry,
          previewRows, supplierLabel, uploadPanel, identityDialog, bringIntoView,
          concentrationWarning, concentrationSaved, targetAddSummary, rowDeleted,
          labNumberNote, labNumbersIssued, checkNotes, saveNotes,
          targetAddDestination, targetAddGo,
          oneGene, geneGate, geneTerms, GENE_GATE,
          GENE_GATE_ANTIBODIES, GENE_GATE_CELL_LINES, GENE_GATE_SESSIONS,
          cNumberWarning, cNumberSaved, sheetLine, plural, pastedBlock,
          optionValues,
          downloadWithReceipt, downloadReceipts, confirmWholeDownload,
          deleteDialog, filesPanel, renumberPanel,
          // Exported because a page mounting its own dialog needs the same
          // Escape behaviour the three built-in pop-outs have. A page-local
          // copy is how one surface comes to dismiss differently.
          escapeCloses,
          DASH, EMPTY, TONES};
})();
