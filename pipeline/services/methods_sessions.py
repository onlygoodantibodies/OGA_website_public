"""Sessions and result rows made from the published reports (1 Oct 2026).

`methods_backfill` fills what the lab records left blank. This module does
the half it deliberately did not: where a report describes a run that no
session holds, it **creates** the session and its result rows; where one
session holds two runs, it **splits** it; and where a session holds a run
but lacks a row for an antibody the report tested, it **adds the row**. The
owner asked for all three on 1 Oct 2026, with a note on each record saying
how it was made.

Cowork's extraction labelled each report run ``FILL``, ``SPLIT SESSION`` or
``CREATE SESSION``, and **those labels are not used to decide anything**:
read against the live rows they were wrong both ways. Nineteen of its 51
"create" runs already had a session holding the same antibodies — PLCG2's IF
session 327 carries the report's eleven antibodies with their concentrations
blank — so creating one would have put a second copy of the run beside the
first. And most of its 63 "split" runs named a session that only one report
ever described (CTSB session 286, PLEC session 181): the rows it called "the
other run" were rows where the lab had written a volume (``20 µl``) where
the report's sentence says ``2.0 µg``. A disagreeing value is not a second
experiment.

So the structure is decided from two pieces of evidence that have to agree:

* **The Access entry batch.** A run was keyed into Access as a block of
  consecutive ids, so a session's rows fall into batches (`BATCH_GAP`). A
  second run of the same gene was entered months or years later, in a block
  of its own.
* **Which report each batch fits** — per row, does that report list the
  antibody, and does its value agree (`_score`). Report *versions* (a
  ``_v5``, a combined Rab27A&B report) that fit a batch equally are one run.

A session is split only when two batches fit two different reports
decisively and the reports' claims do not overlap; a batch that fits both,
or neither, stays where it is. That is 7 sessions on the 1 Oct export, and
each one is named with its batches before anything moves.

Rules carried from CLAUDE.md:

* **Moved, never copied; nothing deleted.** A split moves result rows to a
  new session, readings untouched. A row with a file attached to it is not
  moved (the attachment names the old session too); the session is listed.
* **Fill-only-blank** on every existing record, as in `methods_backfill`.
* **Never invent a value; say what was supplied.** A created session has no
  bench date and no recorded experimenter. It is dated 1 January of the
  report's year, filed under the inactive member *From published report*,
  and its `comments` say exactly that, with the report it came from. Its
  rows carry the report's amount and say in their comments that nobody
  entered them at the bench.
* **A created row counts as a reading** (it holds a dilution), so the gene's
  progress strip ticks that application — true, since a figure was published
  from that run.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction

from pipeline.dedup_utils import normcat
from pipeline.services import methods_text

#: A gap in Access ids wider than this starts a new entry batch. Runs of one
#: gene entered on one occasion sit within a few dozen ids of each other; a
#: second run of it was entered hundreds of ids later.
BATCH_GAP = 150

#: Where a report's per-antibody value is written on a row it creates, and
#: which columns are compared when deciding whether a row fits a report.
WRITE_FIELDS = {"WB": ("dilution", "primary_ab_dilution"), "IP": ("amount_of_antibody",),
                "IF": ("best_concentration",), "FC": ("concentration",),
                "IHC": ("primary_ab_dilution",)}
COMPARE_FIELDS = {**WRITE_FIELDS, "IF": ("best_concentration", "concentration_1",
                                         "concentration_2")}
KIND = {"WB": "wb", "IP": "ip", "IF": "if", "FC": "fc", "IHC": "ihc"}
FIGURE_APP = {"WB": "WB", "IP": "IP", "IF": "ICC-IF", "FC": "FC", "IHC": "IHC"}

PLACEHOLDER_USERNAME = "report_import"
PLACEHOLDER_NAME = "From published report"

_DRAFT = re.compile(r"_v\d|draft| v\d", re.I)


def _models():
    from pipeline.models import FcResult, IfResult, IhcResult, IpResult, WbResult
    return {"WB": WbResult, "IP": IpResult, "IF": IfResult, "FC": FcResult, "IHC": IhcResult}


def norm_value(value) -> str:
    """One spelling for comparing amounts: ``1in1000``, ``1:1000`` and
    ``1/1 000`` are one dilution; ``2.0 µg`` and ``2 ug`` one amount."""
    v = str(value or "").lower().replace("µ", "u").replace("μ", "u")
    v = re.sub(r"[\s,]+", "", v)
    v = re.sub(r"^1(in|:)", "1/", v)
    v = re.sub(r"(\d)\.0+(?!\d)", r"\1", v)
    return v


@dataclass
class Group:
    """One run described by one or more versions of a report."""
    runs: list
    values: dict            # normcat -> antibody_values record (the chosen run's)

    @property
    def chosen(self):
        return self.runs[0]  # `_groups` orders them

    @property
    def label(self) -> str:
        files = sorted({r["report_file"] for r in self.runs})
        return " / ".join(files)

    @property
    def is_fill(self) -> bool:
        return any(r["action"] == "FILL" for r in self.runs)


@dataclass
class Plan:
    splits: list = field(default_factory=list)     # dicts
    creates: list = field(default_factory=list)    # dicts
    row_adds: list = field(default_factory=list)   # dicts (rows added to an existing session)
    session_fills: list = field(default_factory=list)  # (session, key, value)
    row_fills: list = field(default_factory=list)  # (row, col, value)
    left: dict = field(default_factory=dict)

    def leave(self, reason, sentence):
        self.left.setdefault(reason, []).append(sentence)


def _groups(runs, values_by_report, on_file=frozenset()) -> list[Group]:
    """Report versions that say the same thing about the same run are one
    run: the same antibodies at the same amounts. (FLT1's lysate report and
    its secreted-protein report list one panel at one set of dilutions; two
    sessions for them would be one run entered twice.)"""
    groups: list[Group] = []
    for run in sorted(runs, key=lambda r: r["report_id"]):
        vals = values_by_report.get(run["report_id"], {})
        sig = {k: norm_value(v["report_value"]) for k, v in vals.items()}
        for g in groups:
            gsig = {k: norm_value(v["report_value"]) for k, v in g.values.items()}
            if gsig == sig:
                g.runs.append(run)
                break
        else:
            groups.append(Group(runs=[run], values=dict(vals)))
    for g in groups:
        # The version to file it under: the one naming the most antibodies
        # this gene has on file (a combined Rab27A&B report lists the other
        # gene's too), then the usual order.
        g.runs.sort(key=lambda r: (len(set(values_by_report.get(r["report_id"], {})) & on_file),
                                   not _DRAFT.search(r["report_file"]),
                                   r.get("report_year") or "",
                                   r["report_file"].lower().endswith(".pdf")), reverse=True)
        g.values = dict(values_by_report.get(g.runs[0]["report_id"], {}))
    return groups


def _acc(row):
    """The Access id; flow cytometry and IHC rows never had one."""
    return getattr(row, "access_id", None)


def _batches(rows) -> list[list]:
    """A session's rows in Access entry order, cut where the ids jump. Rows
    entered in the app (no Access id) are a batch of their own."""
    keyed = sorted((r for r in rows if _acc(r) is not None), key=_acc)
    out: list[list] = []
    for row in keyed:
        if out and _acc(row) - _acc(out[-1][-1]) <= BATCH_GAP:
            out[-1].append(row)
        else:
            out.append([row])
    loose = sorted((r for r in rows if _acc(r) is None), key=lambda r: r.pk)
    if loose:
        out.append(loose)
    return out


def _value_for(group: Group, catalogue: str):
    """The report's value for a row's antibody: by catalogue number, or by
    the one report number the row's number extends ("ARP85343" for
    "ARP85343_P050")."""
    key = normcat(catalogue)
    if key in group.values:
        return group.values[key]
    loose = [k for k in group.values if len(k) >= 5 and key.startswith(k)]
    return group.values[loose[0]] if len(loose) == 1 else None


def _score(batch, group: Group, app: str) -> int:
    """+2 a row whose antibody the report lists at the value the row holds,
    +1 one it lists where the row holds no value, −1 one it lists at a
    different value, 0 one it does not list."""
    total = 0
    for row in batch:
        a = _value_for(group, row.antibody.catalogue_number)
        if a is None:
            continue
        lab = {norm_value(getattr(row, f)) for f in COMPARE_FIELDS[app]
               if str(getattr(row, f) or "").strip()}
        if not lab:
            total += 1
        elif norm_value(a["report_value"]) in lab:
            total += 2
        else:
            total -= 1
    return total


def _listed(batch, group) -> int:
    return sum(1 for r in batch if _value_for(group, r.antibody.catalogue_number) is not None)


def _assign(batches, groups, app):
    """Each batch's set of best-fitting groups, or ``None`` when none fits:
    a positive score, from a report that lists at least half the batch's
    antibodies (so a stray shared antibody does not claim a batch)."""
    out = []
    for batch in batches:
        scores = [_score(batch, g, app) if 2 * _listed(batch, g) >= len(batch) else 0
                  for g in groups]
        best = max(scores) if scores else 0
        if best <= 0:
            out.append(None)
        else:
            out.append(frozenset(i for i, s in enumerate(scores) if s == best))
    return out


def _decisive(assigned):
    """The claims that settle a batch: a set that strictly contains another
    batch's set is the overlap between two runs, not a run of its own."""
    sets = {a for a in assigned if a}
    return {a for a in sets if not any(b < a for b in sets)}


def _resolve_antibody(target, catalogue, fig_app, site_id, cache):
    from pipeline.models import Antibody, PublicationImage
    key = (target.pk, normcat(catalogue))
    if key not in cache:
        cache[key] = [ab for ab in Antibody.objects.filter(target=target)
                      if normcat(ab.catalogue_number) == key[1]]
    found = cache[key]
    if not found:
        # The report drops what the label adds: Aviva's "_P050", Proteintech's
        # "-Ig". A prefix that settles on exactly one product is that product.
        loose = [ab for ab in Antibody.objects.filter(target=target)
                 if normcat(ab.catalogue_number).startswith(key[1]) and len(key[1]) >= 5]
        found = loose if len({normcat(ab.catalogue_number) for ab in loose}) == 1 else []
    if len(found) > 1:
        published = set(PublicationImage.objects.filter(
            antibody__in=found, application_type=fig_app).values_list("antibody_id", flat=True))
        found = [ab for ab in found if ab.pk in published] or found
    if len(found) > 1 and site_id:
        found = [ab for ab in found if ab.site_id == site_id] or found
    return found[0] if len(found) == 1 else None


def _site_for(run, antibodies):
    from pipeline.models import Site
    name = (run.get("site") or "").strip()
    if name:
        site = Site.objects.filter(name__iexact=name).first()
        if site:
            return site
    counts = Counter(ab.site_id for ab in antibodies if ab and ab.site_id)
    if counts:
        return Site.objects.filter(pk=counts.most_common(1)[0][0]).first()
    return None


def condition_writes(app: str, run: dict, targets_map: dict):
    """What a run's conditions write: session keys, protein loading, and
    result-row columns — the same map `methods_backfill` fills from."""
    cleaned = {k: methods_text.clean(v) for k, v in run["conditions"].items()}
    session, row, loading = {}, {}, None
    for src_key, where_to in targets_map.get(app, {}).items():
        value = cleaned.get(src_key)
        if not value or not where_to:
            continue
        for dest in (d.strip() for d in where_to.replace("+", ";").split(";")):
            if dest.startswith("session_conditions."):
                session[dest.split(".", 1)[1]] = value
            elif dest == "protein_loading_ug":
                m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:µg|ug)?\s*", value)
                loading = m.group(1) if m else None
            elif "result." in dest:
                row[dest.split("result.", 1)[1]] = value
    return session, loading, row


def _report_source(target) -> str:
    return methods_text.source_for(list(target.reports.all())) if target else ""


def _created_note(group: Group, target, today) -> str:
    run = group.chosen
    source = _report_source(target)
    lines = (run["conditions"].get("cell_lines") or "").strip()
    return (f"Created {today:%-d %b %Y} from the published report "
            f"{run['report_file']}{f' ({source})' if source else ''} by load_methods_record. "
            "Nobody entered this session at the bench: its conditions and each antibody's "
            "amount are the report's, it is dated 1 January of the report's year because "
            "the bench date was not recorded, and the experimenter is not recorded."
            + (f" Cell lines in the report: {lines}." if lines else ""))


def _split_note(original, group: Group, rows, target, today) -> str:
    source = _report_source(target)
    ids = sorted(_acc(r) for r in rows if _acc(r) is not None)
    span = f"Access entries {ids[0]}–{ids[-1]}" if ids else f"{len(rows)} rows"
    return (f"Split {today:%-d %b %Y} from session #{original.pk} by load_methods_record. "
            f"Session #{original.pk} held two runs; these rows ({span}) match the published "
            f"report {group.chosen['report_file']}{f' ({source})' if source else ''} and the "
            "rest match another report. The rows were moved, not copied, and their readings "
            "are unchanged. The conditions are the report's; it is dated 1 January of the "
            "report's year and the experimenter is not recorded, as neither was. Cell lines "
            f"are copied from session #{original.pk}.")


ROW_NOTE = ("Created {day} from the published report {file} by load_methods_record; "
            "nobody entered this row at the bench.")


def plan(data: dict, gene: str | None = None, today: date | None = None) -> Plan:
    from pipeline.models import ExperimentSession
    from pipeline.services.methods_backfill import _target
    today = today or date.today()
    p = Plan()
    models = _models()
    targets_map = data["run_column_targets"]
    values_by_report: dict = defaultdict(dict)
    for a in data["antibody_values"]:
        if a.get("report_value", "").strip() and a["action"] != "ANTIBODY NOT IN DB":
            values_by_report[(a["report_id"], a["app"])][normcat(a["catalogue"])] = a

    runs_by: dict = defaultdict(list)
    for r in data["run_conditions"]:
        if gene and r["gene"].upper() != gene.upper():
            continue
        runs_by[(r["gene"].upper(), r["app"])].append(r)

    cache: dict = {}
    for (g, app), runs in sorted(runs_by.items()):
        if app not in models or all(r["action"] == "FILL" for r in runs):
            continue  # every run already has its session, and was filled
        target = _target(runs[0]["gene"])
        if target is None:
            p.leave("gene not on file", f"{g} {app}")
            continue
        fig_app = FIGURE_APP[app]
        on_file = frozenset(normcat(c) for c in target.antibodies.values_list(
            "catalogue_number", flat=True))
        groups = _groups(runs, {r["report_id"]: values_by_report.get((r["report_id"], app), {})
                                for r in runs}, on_file)
        model = models[app]
        sessions = list(ExperimentSession.objects.filter(target=target, procedure_type=app)
                        .order_by("pk"))
        homes: dict = defaultdict(list)  # group index -> [(session or None, rows)]
        session_rows: dict = {}
        claimed: set = set()

        for session in sessions:
            rows = list(model.objects.filter(session=session).select_related("antibody"))
            if not rows:
                continue
            session_rows[session] = rows
            batches = _batches(rows)
            assigned = _assign(batches, groups, app)
            decisive = _decisive(assigned)
            def best(claim):
                # Versions that fit equally are one run: work it once, under
                # the version whose antibodies are this gene's (not a
                # combined Rab27A&B report).
                return max(sorted(claim), key=lambda i: len(set(groups[i].values) & on_file)
                           - len(set(groups[i].values) - on_file))

            parts: dict = defaultdict(list)
            for batch, claim in zip(batches, assigned):
                parts[claim if claim in decisive else None].extend(batch)
            claims = [c for c in parts if c is not None]
            claimed.update(i for c in claims for i in c)
            disjoint = all(not (a & b) for a in claims for b in claims if a != b)
            # One row is not a run: a lone row that fits another report is as
            # likely a repeat of one antibody as a second experiment.
            if any(len(parts[c]) < 2 for c in claims):
                disjoint = False
            if len(claims) >= 2 and disjoint:
                # The earliest-entered run keeps the session, with whatever fits no
                # report decisively; every later run moves to a session of its own.
                first = min(claims, key=lambda c: min(_acc(r) or 10**9 for r in parts[c]))
                moving = {c: parts[c] for c in claims if c != first}
                if any(r.attachments.exists() for moved in moving.values() for r in moved):
                    p.leave("split refused — a file is attached to a row that would move",
                            f"session #{session.pk} {g} {app}")
                    for c in claims:
                        homes[best(c)].append((session, parts[c], rows))
                    continue
                stays = [r for c in parts if c not in moving for r in parts[c]]
                homes[best(first)].append((session, parts[first], stays))
                for claim, moved in moving.items():
                    gi = best(claim)
                    p.splits.append(dict(session=session, target=target, app=app,
                                         group=groups[gi], rows=moved,
                                         stays=stays,
                                         note=_split_note(session, groups[gi], moved, target, today)))
                    homes[gi].append(("split", moved, len(p.splits) - 1))
            else:
                # Its blanks are filled only on the rows that fit the report;
                # a row is added only for an antibody the session has nowhere.
                for claim in claims:
                    homes[best(claim)].append((session, parts[claim], rows))

        for gi, group in enumerate(groups):
            if group.is_fill and not homes.get(gi):
                continue  # Cowork placed it and it was filled; nothing here disagrees
            if gi in claimed and not homes.get(gi):
                continue  # another version of it was worked on that batch
            chosen = group.chosen
            sess_conds, loading, row_cols = condition_writes(app, chosen, targets_map)
            places = homes.get(gi) or []
            if not places:
                if all(r["action"] == "SPLIT SESSION" for r in group.runs):
                    p.leave("report fits no batch of the session that holds it",
                            f"{g} {app} ({chosen['report_file']}): session "
                            f"{chosen.get('session_ids') or '?'}")
                    continue
                held = [s for s in session_rows
                        if 2 * len({normcat(r.antibody.catalogue_number) for r in session_rows[s]}
                                   & set(group.values)) >= len(group.values)]
                if held:
                    # Most of the report's antibodies already sit in a session
                    # whose rows do not fit it: a second copy would be worse
                    # than a gap, so a person decides.
                    p.leave("a session holds most of the report's antibodies but does not fit it",
                            f"{g} {app} ({chosen['report_file']}): session "
                            + ", ".join(f"#{s.pk}" for s in held))
                    continue
                # A run no session holds: a session of its own.
                abs_ = {}
                for cat, a in group.values.items():
                    ab = _resolve_antibody(target, a["catalogue"], fig_app, None, cache)
                    if ab is None:
                        p.leave("antibody not resolved to one record",
                                f"{g} {app} {a['catalogue']} ({chosen['report_file']})")
                    else:
                        abs_[cat] = (ab, a)
                if not abs_:
                    p.leave("no antibody of the report on file", f"{g} {app} ({chosen['report_file']})")
                    continue
                site = _site_for(chosen, [ab for ab, _ in abs_.values()])
                year = (chosen.get("report_year") or "").strip()
                if site is None or not year.isdigit():
                    p.leave("no site or year to file it under", f"{g} {app} ({chosen['report_file']})")
                    continue
                p.creates.append(dict(
                    target=target, app=app, group=group, site=site, date=date(int(year), 1, 1),
                    conditions=sess_conds, loading=loading, row_cols=row_cols,
                    rows=[(ab, a) for ab, a in abs_.values()],
                    note=_created_note(group, target, today),
                    versions=len(group.runs)))
                continue
            for place in places:
                if place[0] == "split":
                    p.splits[place[2]].update(conditions=sess_conds, loading=loading,
                                              row_cols=row_cols)
                    _row_work(p, group, app, place[1], row_cols, target, fig_app, cache,
                              split_index=place[2], today=today)
                    continue
                session, rows, have_rows = place
                if not group.is_fill:
                    conds = session.session_conditions or {}
                    for key, value in sess_conds.items():
                        if not str(conds.get(key) or "").strip():
                            p.session_fills.append((session, key, value))
                _row_work(p, group, app, rows, row_cols, target, fig_app, cache,
                          session=session, today=today, have_rows=have_rows)
    # One fill per cell and per session key.
    p.row_fills = list({(type(r).__name__, r.pk, c): (r, c, v) for r, c, v in p.row_fills}.values())
    p.session_fills = list({(s.pk, k): (s, k, v) for s, k, v in reversed(p.session_fills)}.values())
    return p


def _row_work(p, group, app, rows, row_cols, target, fig_app, cache, *, today,
              session=None, split_index=None, have_rows=None):
    """Blank cells on the run's rows filled from its report, and a row added
    for each antibody the report tested that the session has no row for."""
    have = {normcat(r.antibody.catalogue_number) for r in (have_rows or rows)}
    have_ids = {r.antibody_id for r in (have_rows or rows)}
    for row in rows:
        a = _value_for(group, row.antibody.catalogue_number)
        if a is not None and not any(str(getattr(row, f) or "").strip()
                                     for f in COMPARE_FIELDS[app]):
            for f in WRITE_FIELDS[app]:
                p.row_fills.append((row, f, a["report_value"].strip()))
        for col, value in row_cols.items():
            if hasattr(row, col) and not str(getattr(row, col) or "").strip():
                p.row_fills.append((row, col, value))
    site_id = (session.site_id if session else
               p.splits[split_index]["session"].site_id)
    for cat, a in group.values.items():
        if cat in have:
            continue
        ab = _resolve_antibody(target, a["catalogue"], fig_app, site_id, cache)
        if ab is None:
            p.leave("antibody not resolved to one record",
                    f"{target.gene_name} {app} {a['catalogue']} ({group.chosen['report_file']})")
            continue
        if ab.pk in have_ids or normcat(ab.catalogue_number) in have:
            continue  # on file under its full catalogue number ("ARP85343_P050")
        have.add(cat)
        have_ids.add(ab.pk)
        p.row_adds.append(dict(session=session, split_index=split_index, app=app, antibody=ab,
                               value=a, row_cols=row_cols, report_file=group.chosen["report_file"]))


def placeholder_member():
    """The inactive member a created session is filed under — nobody signs in
    as it and no experimenter picker offers it (`members.experimenters`)."""
    from django.contrib.auth.models import User
    from pipeline.models import Member, Site
    user, made = User.objects.using("pipeline_db").get_or_create(
        username=PLACEHOLDER_USERNAME,
        defaults={"first_name": "From published", "last_name": "report", "is_active": False})
    if made:
        user.set_unusable_password()
        user.save(using="pipeline_db")
    member = Member.objects.filter(user_id=user.pk).first()
    if member is None:
        site = Site.objects.filter(name="McGill").first() or Site.objects.order_by("pk").first()
        member = Member.objects.create(user_id=user.pk, site_id=site.pk, role="experimenter",
                                       display_name=PLACEHOLDER_NAME, is_active=False)
    return member


def _new_row(app, session, antibody, value, row_cols, report_file, today):
    model = _models()[app]
    row = model(session=session, antibody=antibody)
    for f in WRITE_FIELDS[app]:
        setattr(row, f, value["report_value"].strip()[:_max(model, f)])
    for col, v in row_cols.items():
        if hasattr(row, col) and not str(getattr(row, col) or "").strip():
            setattr(row, col, v[:_max(model, col)])
    row.comments = ROW_NOTE.format(day=f"{today:%-d %b %Y}", file=report_file)
    row.save()
    return row


def _max(model, col):
    return model._meta.get_field(col).max_length or 10_000


def _session_from(target, app, site, day, member, conditions, loading, note, like=None):
    from pipeline.models import ExperimentSession
    return ExperimentSession.objects.create(
        procedure_type=app, target=target, site=site, date=day, experimenter=member,
        status="complete", session_conditions=dict(conditions),
        protein_loading_ug=loading or None, comments=note,
        cell_line_wt_id=getattr(like, "cell_line_wt_id", None),
        cell_line_ko_id=getattr(like, "cell_line_ko_id", None),
        fc_sub_protocol=getattr(like, "fc_sub_protocol", "na"))


@transaction.atomic(using="pipeline_db")
def apply(p: Plan, today: date | None = None) -> dict:
    from pipeline.models import ExperimentSession
    today = today or date.today()
    counts = dict(sessions_split=0, rows_moved=0, sessions_created=0, rows_created=0,
                  session_conditions=0, result_cells=0)
    if not (p.splits or p.creates or p.row_adds or p.session_fills or p.row_fills):
        return counts
    member = placeholder_member() if (p.splits or p.creates) else None
    models = _models()

    split_sessions = {}
    for i, s in enumerate(p.splits):
        original = ExperimentSession.objects.get(pk=s["session"].pk)
        year = (s["group"].chosen.get("report_year") or "").strip()
        day = date(int(year), 1, 1) if year.isdigit() else original.date
        new = _session_from(s["target"], s["app"], original.site, day, member,
                            s.get("conditions", {}), s.get("loading"), s["note"], like=original)
        moved = models[s["app"]].objects.filter(
            pk__in=[r.pk for r in s["rows"]], session_id=original.pk).update(session=new)
        split_sessions[i] = new
        counts["sessions_split"] += 1
        counts["rows_moved"] += moved

    for c in p.creates:
        new = _session_from(c["target"], c["app"], c["site"], c["date"], member,
                            c["conditions"], c["loading"], c["note"])
        for ab, value in c["rows"]:
            _new_row(c["app"], new, ab, value, c["row_cols"], c["group"].chosen["report_file"], today)
            counts["rows_created"] += 1
        counts["sessions_created"] += 1

    for add in p.row_adds:
        session = add["session"] or split_sessions[add["split_index"]]
        _new_row(add["app"], session, add["antibody"], add["value"], add["row_cols"],
                 add["report_file"], today)
        counts["rows_created"] += 1

    fresh = {s.pk: s for s in ExperimentSession.objects.filter(
        pk__in={s.pk for s, _, _ in p.session_fills})}
    for session, key, value in p.session_fills:
        s = fresh[session.pk]
        conds = dict(s.session_conditions or {})
        if not str(conds.get(key) or "").strip():
            conds[key] = value
            s.session_conditions = conds
            counts["session_conditions"] += 1
    for s in fresh.values():
        s.save(update_fields=["session_conditions"])

    for row, col, value in p.row_fills:
        cur = type(row).objects.get(pk=row.pk)
        if str(getattr(cur, col) or "").strip():
            continue
        setattr(cur, col, value[:_max(type(cur), col)])
        cur.save(update_fields=[col])
        counts["result_cells"] += 1
    return counts
