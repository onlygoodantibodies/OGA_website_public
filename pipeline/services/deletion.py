"""Removing a record, in a way that cannot happen by accident.

Carl Laflamme: *"we created a mock gene USP8 that could be removed (not sure how
to remove a target entry)"*. He was not missing a button — **nothing in the app
can delete anything**, so an honest mistake by a scientist needed a developer and
a Render shell. For a five-site consortium where people will try things out,
that is a real hole.

The reason it was left out is visible in the schema, and it is a good reason.
Every foreign key into these models is ``CASCADE`` or ``SET_NULL``:

    Target    <- Report, TargetAssignment, TargetNomination,
                 TargetClassification, Antibody, ReagentRequest,
                 ExperimentSession   (all CASCADE)
    Antibody  <- WbResult, IpResult, IfResult, FcResult, PublicationImage,
                 InventoryLocation   (all CASCADE)
    CellLine  <- CellLineVial, InventoryLocation, CellCultureEvent, Sample
                 (CASCADE); ExperimentSession.cell_line_wt/ko and
                 CellLine.parent_line (SET_NULL)

So ``target.delete()`` on a gene with work behind it removes every antibody,
every session and every reading, silently and in one statement — and the
``SET_NULL`` ones are worse, because they leave a session that still exists and
no longer knows which cell line it was run against.

Three rules make this safe, and they are the whole module.

**You may delete your own bench's records, and a superuser may delete any.**
Site is half of what makes a row the row it is everywhere else in this app, and
"who noticed it first" is the wrong answer to "whose record is this" with six
institutions on file. Another site's record is a refusal naming the site, not a
warning — and so is a delete whose *cascade* would reach one, which is a
different question and is asked separately (``_cascade_trespass``): a target
Leicester nominated can carry McGill's antibodies two levels down.

**Work behind a record is a manifest, not a wall** (owner's decision, 3 Aug).
The first design refused outright, which meant a real gene could not be removed
at any permission level and an honest mistake still needed a developer and a
shell. What replaced it is the thing that makes deletion survivable: before
anything is written you are shown **everything that goes**, transitively, from
Django's own ``Collector`` — and everything that would be left behind *broken*
rather than gone, which the ``SET_NULL`` relations do and which no count of
deleted rows can see.

**A record's own parts go with it, and are counted first.** A target's
nominations, an antibody's storage locations, a cell line's vials, a session's
result rows: these are not independent records, and refusing on them would mean
nothing could ever be deleted. They are listed in the confirmation with counts,
so "delete this session" reads as "delete this session and its 4 readings".

**The confirmation is two deliberate presses with the manifest between them** —
open, read what it says goes, tick the box, then press a button that names the
number. Typing the record's own name was the earlier design; it was more
friction than it bought, because nothing here is reachable without first reading
a panel that lists the casualties. The typed word is still *sent* and still
checked server-side, so a stray POST with no dialog behind it is refused.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Q

from pipeline.models import (Antibody, CellLine, CellLineVial, CellCultureEvent,
                             ExperimentSession, FileAttachment,
                             InventoryLocation, PublicationImage, ReagentRequest,
                             Report, Sample, Site, Target, TargetAssignment,
                             TargetClassification, TargetNomination)
from pipeline.services import targets as target_svc

DB = "pipeline_db"


class Refused(Exception):
    """A deletion that would take something with it, explained in a sentence."""


@dataclass
class Plan:
    """What deleting this record would do, before it is done."""
    label: str = ""
    confirm_with: str = ""
    owned: list = field(default_factory=list)     # [(noun, count)] — goes too
    blockers: list = field(default_factory=list)  # [(noun, count)] — refuses
    kind: str = ""
    pk: int = 0
    # Set by `plan()`: whose record this is, when it is not yours.
    not_yours: str = ""
    # Set when the refusals are about to be overridden. `cascade` is everything
    # that goes with it and `orphaned` everything left behind broken — both from
    # Django's own collector, so both are transitive and cannot drift from the
    # schema.
    overriding: bool = False
    cascade: list = field(default_factory=list)
    orphaned: list = field(default_factory=list)

    @property
    def blocking(self) -> list:
        """The blockers that actually block — the ones with a count.

        `blockers` is every *question* asked, zeros included, so that adding a
        relation to the list is a one-line change. Reading its length instead of
        its counts made nothing deletable at all, which the tests caught on the
        first run: the same count-versus-list confusion this codebase keeps
        paying for elsewhere.
        """
        return [(n, c) for n, c in self.blockers if c]

    @property
    def allowed(self) -> bool:
        if self.overriding:
            return True
        return not self.blocking and not self.not_yours

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "id": self.pk, "label": self.label,
            "confirm_with": self.confirm_with,
            "owned": [{"noun": n, "count": c} for n, c in self.owned if c],
            "blockers": [{"noun": n, "count": c} for n, c in self.blockers if c],
            "allowed": self.allowed,
            "why": self.why,
            "overriding": self.overriding,
            "cascade": [{"noun": n, "count": c} for n, c in self.cascade],
            "cascade_total": sum(c for _n, c in self.cascade),
            "orphaned": [{"noun": n, "count": c} for n, c in self.orphaned],
            "orphaned_total": sum(c for _n, c in self.orphaned),
        }

    @property
    def why(self) -> str:
        if self.overriding:
            listed = ", ".join(f"{c} {n}" for n, c in self.cascade)
            left = ", ".join(f"{c} {n}" for n, c in self.orphaned)
            return (f"{self.label} has work behind it. Deleting it will also "
                    f"delete {listed or 'nothing else'}"
                    + (f", and leave {left}" if left else "")
                    + ". It cannot be undone from the app — Render's "
                      "point-in-time recovery is the only way back.")
        if self.allowed:
            return ""
        if self.not_yours:
            return self.not_yours
        listed = ", ".join(f"{c} {n}" for n, c in self.blocking)
        return (f"{self.label} cannot be deleted: {listed} point at it. "
                f"Records with work behind them are never removed from a page — "
                f"move or merge them with the management commands, which have a "
                f"dry run and a backup in front of them.")


def companions(kind: str, obj) -> list:
    """Records that are **part of what is being deleted** and that the cascade
    cannot reach, because the foreign key pointing at it is ``SET_NULL``.

    One case, and it took a real deletion to find it. ``CellLine.target`` is
    ``SET_NULL``, so deleting a gene did not delete its knockouts — it blanked
    their gene and left them on the board. The dialog said so (``orphans()``
    named them, which is why that half exists) and saying so was not enough,
    because what is left behind is not a lesser version of the row: **a cell
    line with no gene is what a wild type is** everywhere else in this app. The
    orphan is therefore not findable by the gene it was made against, reads as a
    parental line while still flagged genotype KO, and collides by name with the
    knockout somebody adds when they put the gene back — which produced the one
    message a reader cannot act on: *"'U2OS TRPA1 KO A1' matches 2 cell lines:
    U2OS TRPA1 KO A1 — Leicester, U2OS TRPA1 KO A1 — Leicester."*

    So a knockout goes with its gene. **Wild types deliberately do not**: a
    parental is shared between every knockout made from it and is recorded once
    with no gene, and 96 of them point at the Access-era placeholder target
    called ``NA`` — cascading off that row would delete most of the consortium's
    parental lines in one press. Only lines that are *not* wild types go, which
    is exactly the set that cannot mean anything once the gene is gone.

    Returned as instances rather than a queryset because they are walked by the
    collector and deleted by ``delete()``, and both want the same rows.
    """
    if kind == "target":
        return list(CellLine.objects.using(DB)
                    .filter(target_id=obj.pk)
                    .exclude(genotype=CellLine.Genotype.WILD_TYPE))
    return []


def _walk(obj, extra=()):
    """One read-only ``Collector.collect()``, shared by all three questions.

    ``collect()`` is not free — on a gene with 22 antibodies it reaches every
    reading and every published figure, and the preview asks three things about
    the same walk: what gets deleted, what gets nulled, and whether either
    reaches another site. Three separate walks is three times the queries on the
    one endpoint a person waits in front of before a destructive act.

    ``extra`` is ``companions()`` — collected into the *same* walk so the
    manifest, the orphan list and the cross-site check all account for them
    without any of the three being told about the special case. Collected in a
    second call because ``Collector.collect()`` reads the model off the first
    object and assumes the batch is homogeneous.

    ``delete()`` is never called here. ``collect()`` only reads.
    """
    from django.db.models.deletion import Collector

    collector = Collector(using=DB)
    collector.collect([obj])
    if extra:
        collector.collect(list(extra))
    return collector


def cascade(obj, collector=None) -> list:
    """Everything a delete would actually take, asked of Django itself.

    **A hand-written list of children is the wrong tool the moment a superuser
    can override the refusals.** Deleting a target cascades to its antibodies,
    and each of those cascades to its results and its published figures — so
    "22 antibodies" understates it by two levels, and a person reading that
    number would not know what they were agreeing to.

    ``Collector`` is the thing that performs the delete, so what it reports is
    what will happen, transitively, including relations added to the schema
    after this was written.

    Returns ``[(noun, count)]``, the record itself excluded — it is named
    separately and does not want counting as one of its own casualties. The
    noun **agrees with its count**: this was `verbose_name_plural` whatever the
    number, which read *"1 cell lines"* on the panel a person studies
    immediately before destroying something. Next to a number is where a grammar
    slip costs most — it makes a careful reader distrust the number, which is
    the one thing on this screen they have to trust.
    """
    collector = collector or _walk(obj)

    counts = {}
    for model, instances in collector.data.items():
        n = len(instances)
        if model is type(obj):
            n -= 1                      # the row itself
        if n > 0:
            counts[model] = counts.get(model, 0) + n

    # **`collector.data` is not the whole cascade.** Django routes any relation
    # it can remove with a single query — no signals, no cascades of its own —
    # into `fast_deletes` as a queryset, and those never appear in `data`. On a
    # target that is the nominations, the classifications and the published
    # figures: a manifest built from `data` alone silently *undercounts*, which
    # on the one screen whose entire job is to say what will be destroyed is the
    # worst possible direction to be wrong in. Found by a test noticing a
    # nomination missing from the list.
    for qs in collector.fast_deletes:
        n = qs.count()
        if n:
            counts[qs.model] = counts.get(qs.model, 0) + n

    return sorted(((_noun(m, n), n) for m, n in counts.items()),
                  key=lambda kv: -kv[1])


def _noun(model, n) -> str:
    meta = model._meta
    return str(meta.verbose_name if n == 1 else meta.verbose_name_plural)


# How to say what a `SET_NULL` costs. The fallback is derived and correct; these
# are the five that a scientist reads on a real screen, so they are written out.
#
# The first one used to say that deleting a gene turned its knockouts into
# parental lines — true, said plainly in the dialog, and **not enough**: it
# happened for real, and the row it left behind was unusable rather than merely
# incomplete. Knockouts go with the gene now (`companions`), so what this line
# describes is the case that remains: a wild type pointing at the Access-era
# `NA` placeholder, for which losing a gene it never had costs nothing.
#
# `(singular, plural)`, because every one of these is printed with a count in
# front of it and a count is often 1 — see `_noun` above. One pair per key
# rather than a shared phrase with an `(s)` in it, which is the same slip
# written down in advance.
_ORPHAN_WORDING = {
    ("cellline", "target"): (
        "wild type that would be left with no gene recorded — which is what a "
        "wild type is anyway, so nothing about it changes on screen",
        "wild types that would be left with no gene recorded — which is what a "
        "wild type is anyway, so nothing about them changes on screen"),
    ("cellline", "parent_line"): (
        "knockout that would be left with no parent line recorded",
        "knockouts that would be left with no parent line recorded"),
    ("cellline", "arrived_with_ko"): (
        "wild type that would no longer say which knockout it shipped with",
        "wild types that would no longer say which knockout they shipped with"),
    ("experimentsession", "cell_line_wt"): (
        "session that would no longer say which wild type it was run against",
        "sessions that would no longer say which wild type they were run against"),
    ("experimentsession", "cell_line_ko"): (
        "session that would no longer say which knockout it was run against",
        "sessions that would no longer say which knockout they were run against"),
}


def _rows_of(objs) -> list:
    """`field_updates` holds querysets on some paths and lists on others."""
    return list(objs)


def orphans(obj, collector=None) -> list:
    """What a delete leaves behind **broken rather than gone**.

    The counterpart to ``cascade`` and the half a deleted-row count structurally
    cannot see. Five relations into these models are ``SET_NULL``
    (``_ORPHAN_WORDING`` names them, and a test checks that list against
    Django's metadata), and ``SET_NULL`` is worse here than a cascade: the
    knockout survives with no parent, the session survives no longer knowing
    what it was run against, and deleting a *gene* leaves its knockouts with no
    gene at all — which is what a wild type is. Silently, in every case, with
    nothing on any screen afterwards to say a value used to be there.

    Deleting a wild type is exactly this case, and it is the one where the
    manifest alone lies: nothing cascades off a WT, so a dialog built from
    ``cascade`` says *"will also delete nothing else"* over three sessions about
    to lose their control line. Same shape as the ``fast_deletes`` undercount
    above — the count was honest about the question it was asked, and the
    question was the wrong one.

    Read from ``Collector.field_updates``, so it is the collector's own account
    of what it is about to null, not a hand-written list of SET_NULL fields that
    a schema change would leave behind.
    """
    collector = collector or _walk(obj)

    # **A row that is being deleted is not a row left behind broken.** The two
    # lists overlap the moment anything is collected beyond the record itself:
    # deleting a gene now takes its knockouts (`companions`), and nulling a
    # knockout's `cell_line_ko` on the way out reaches the very sessions that
    # gene is about to cascade to. Counting those twice would put the same
    # sessions under "will delete" and "will leave broken" in one panel, which
    # is the two-disagreeing-answers failure on the one screen that must not
    # have it.
    going = {(model._meta.model_name, row.pk)
             for model, rows in collector.data.items() for row in rows}

    seen = {}
    for (fld, value), instance_lists in collector.field_updates.items():
        if value is not None:
            continue
        key = (fld.model._meta.model_name, fld.name)
        pks = seen.setdefault(key, (fld, set()))[1]
        for objs in instance_lists:
            pks.update(row.pk for row in _rows_of(objs)
                       if (key[0], row.pk) not in going)

    out = []
    for key, (fld, pks) in seen.items():
        if not pks:
            continue
        n = len(pks)
        written = _ORPHAN_WORDING.get(key)
        noun = (written[0] if n == 1 else written[1]) if written else (
            f"{_noun(fld.model, n)} that would lose "
            f"{'its' if n == 1 else 'their'} {fld.verbose_name}")
        out.append((str(noun), n))
    return sorted(out, key=lambda kv: -kv[1])


def _count(model, **kw) -> int:
    return model.objects.using(DB).filter(**kw).count()


def _result_count(**kw) -> int:
    from pipeline.models import FcResult, IfResult, IpResult, WbResult
    return sum(_count(m, **kw) for m in (WbResult, IpResult, IfResult, FcResult))


def plan_for_target(target) -> Plan:
    pk = target.pk
    gene = (target.gene_name or target.protein_name or f"target {pk}").strip()
    return Plan(
        kind="target", pk=pk, label=gene, confirm_with=gene,
        # A nomination is the target's own half — "this site is pursuing it" —
        # and every target created through either door has one, so refusing on
        # them would mean no target could ever be deleted.
        owned=[("site nominations", _count(TargetNomination, target_id=pk)),
               ("classifications", _count(TargetClassification, target_id=pk))],
        blockers=[("antibodies", _count(Antibody, target_id=pk)),
                  ("cell lines", _count(CellLine, target_id=pk)),
                  ("sessions", _count(ExperimentSession, target_id=pk)),
                  ("reports", _count(Report, target_id=pk)),
                  ("site assignments", _count(TargetAssignment, target_id=pk)),
                  ("reagent requests", _count(ReagentRequest, target_id=pk))],
    )


def plan_for_antibody(antibody) -> Plan:
    pk = antibody.pk
    cat = (antibody.catalogue_number or f"antibody {pk}").strip()
    return Plan(
        kind="antibody", pk=pk, confirm_with=cat,
        label=f"{cat}{f' ({antibody.company.name})' if antibody.company_id else ''}",
        owned=[("storage locations", _count(InventoryLocation, antibody_id=pk))],
        # A reading and a published figure are the two things that make an
        # antibody row mean something to somebody else.
        blockers=[("recorded results", _result_count(antibody_id=pk)),
                  ("published figures", _count(PublicationImage, antibody_id=pk))],
    )


def plan_for_cell_line(line) -> Plan:
    pk = line.pk
    name = (line.name or f"cell line {pk}").strip()
    # `gene_of`, not `target.gene_name`: 96 wild types point at a placeholder
    # Target called `NA`, and printing that gives "HAP1 NA WT" in a dialog whose
    # job is to name the row before destroying it.
    gene = target_svc.gene_of(getattr(line, "target", None))
    used_by = (ExperimentSession.objects.using(DB)
               .filter(Q(cell_line_wt_id=pk) | Q(cell_line_ko_id=pk)).count())
    return Plan(
        kind="cell-line", pk=pk, confirm_with=name,
        label=f"{name}{f' {gene} {line.genotype}' if gene else ''}".strip(),
        owned=[("freeze-down batches", _count(CellLineVial, cell_line_id=pk)),
               ("storage locations", _count(InventoryLocation, cell_line_id=pk)),
               ("culture events", _count(CellCultureEvent, cell_line_id=pk)),
               ("samples", _count(Sample, cell_line_id=pk))],
        # `parent_line` and the session links are SET_NULL, which is worse than
        # a cascade here: the knockout survives with no parent and the session
        # survives not knowing what it was run against.
        blockers=[("sessions run on it", used_by),
                  ("knockouts made from it", _count(CellLine, parent_line_id=pk)),
                  ("wild types shipped beside it",
                   _count(CellLine, arrived_with_ko_id=pk))],
    )


def plan_for_session(session) -> Plan:
    pk = session.pk
    gene = target_svc.gene_of(getattr(session, "target", None)) or "?"
    label = f"{gene} {session.procedure_type} on {session.date} (session {pk})"
    readings = _result_count(session_id=pk)
    return Plan(
        kind="session", pk=pk, label=label, confirm_with=str(pk),
        # A session's results are the session. Counted, loudly, in the
        # confirmation rather than used to refuse — otherwise the only sessions
        # anyone could delete would be the empty ones, and a session recorded
        # against the wrong gene is exactly the case this exists for.
        owned=[("result rows", readings),
               ("attachments", _count(FileAttachment, session_id=pk))],
        blockers=[],
    )


_PLANNERS = {
    "target": (Target, plan_for_target),
    "antibody": (Antibody, plan_for_antibody),
    "cell-line": (CellLine, plan_for_cell_line),
    "session": (ExperimentSession, plan_for_session),
}


def whose(kind: str, obj) -> set:
    """The site ids a record belongs to.

    A target has no site of its own — ``Target.site`` is a dead Access-era
    column and no write path sets it (CLAUDE.md), so whose target it is lives on
    its ``TargetNomination`` rows, and a gene two sites are pursuing belongs to
    both. Everything else carries a site directly.
    """
    if kind == "target":
        return {n for n in TargetNomination.objects.using(DB)
                .filter(target_id=obj.pk).values_list("site_id", flat=True) if n}
    return {obj.site_id} if getattr(obj, "site_id", None) else set()


def site_refusal(kind, obj, member, is_superuser, *, action="delete",
                 su_verb="remove") -> str:
    """Why this person may not touch this record, or "".

    **Your own bench's records, or you are a superuser.** Nothing pointing at a
    row makes it safe to remove; it does not make it yours. With five sites and
    a sixth arriving, "who noticed it first" is the wrong answer to "whose record
    is this" — the boards already treat site as half of what makes a row the row
    it is.

    Public because deleting is no longer the only place that asks it:
    ``services/attachments.py`` gates attaching a file to a session and removing
    one on exactly this rule, and a second copy of the wording is how two
    surfaces come to answer one question differently — the failure
    ``services/sites.py`` and ``services/cell_lines.py`` both exist to prevent.
    Named without the underscore rather than reached into.

    ``action``/``su_verb`` name what is being refused. The rule is one rule and
    stays here; only the verb moves, because a sentence reading "nothing is
    yours to delete" under an **Add batch** button describes the wrong act and
    reads as the wrong record having been picked. Defaults keep every existing
    caller's wording byte-for-byte.
    """
    if is_superuser:
        return ""
    mine = getattr(member, "site_id", None)
    if not mine:
        return (f"Your account has no site, so nothing is yours to {action}. "
                f"A superuser can set one on the people board.")
    owners = whose(kind, obj)
    if not owners:
        return (f"No site is recorded against this record, so it is nobody's to "
                f"{action} from here. A superuser can {su_verb} it.")
    if owners - {mine}:
        names = ", ".join(sorted(
            Site.objects.using(DB).filter(pk__in=owners)
            .values_list("name", flat=True)))
        return (f"This record belongs to {names}, not to your site. You can "
                f"{action} your own bench's records; a superuser can {su_verb} any.")
    return ""


def plan(kind: str, pk, *, member=None, is_superuser=False) -> Plan:
    spec = _PLANNERS.get(kind)
    if not spec:
        raise Refused(f"'{kind}' is not something this can delete.")
    model, planner = spec
    obj = (model.objects.using(DB).select_related(*_related(kind))
           .filter(pk=pk).first())
    if obj is None:
        raise Refused("That record is not on file — it may already be deleted.")
    p = planner(obj)
    p.not_yours = site_refusal(kind, obj, member, is_superuser)
    # **Work behind a record is a manifest, not a wall** (owner's decision,
    # 3 Aug): a superuser may delete anything, and a member may delete their own
    # bench's records. So the blockers stop refusing and start describing —
    # what is about to be destroyed, and what is about to be left broken, from
    # Django's own collector. Only computed when it is going to be shown: it
    # walks relations, and a refusal on site grounds needs none of that work.
    if p.blocking or p.not_yours:
        # One walk, three questions. `collect()` reaches every reading and every
        # published figure behind a gene, and this endpoint is one a person
        # waits in front of before a destructive act.
        walk = _walk(obj, companions(kind, obj))
        if not is_superuser and not p.not_yours:
            # **A member's own data is theirs; the cascade decides whether it
            # still is.** A target Leicester nominated can carry McGill's
            # antibodies two levels down, and destroying another lab's readings
            # is not "your own data" by any reading of it. That case stays a
            # superuser's.
            p.not_yours = _cascade_trespass(
                obj, getattr(member, "site_id", None), walk)
        if not p.not_yours:
            p.overriding = True
            p.cascade = cascade(obj, walk)
            p.orphaned = orphans(obj, walk)
    return p


def _cascade_trespass(obj, site_id, collector=None) -> str:
    """Whose records a delete would reach beyond this site's, or "".

    Asked of the collector rather than of the blocker list, for the same reason
    the manifest is: the cascade is transitive, and a target two levels up from
    another site's result rows does not look like it touches them.

    **A nulled row counts as reached.** Setting McGill's session's
    ``cell_line_wt`` to null damages McGill's record just as surely as deleting
    it would — more quietly, in fact, since the row is still there afterwards
    looking complete. Leaving ``field_updates`` out of this check would be the
    same mistake as leaving ``fast_deletes`` out of the manifest.
    """
    collector = collector or _walk(obj)
    direct, via_session = set(), set()

    def _look(instances):
        for row in instances:
            other = getattr(row, "site_id", None)
            if other:
                direct.add(other)
                continue
            # **A reading has no site of its own — its session does.**
            # `WbResult`, the other three result models and `FileAttachment` all
            # carry `session` and no `site`, so an antibody of mine used in
            # McGill's session cascades to McGill's readings and nothing about
            # the row says so. That is the case this whole check exists for,
            # one level of indirection down. Gathered and resolved in one
            # query rather than per row.
            session = getattr(row, "session_id", None)
            if session:
                via_session.add(session)

    for _model, instances in collector.data.items():
        _look(instances)
    for qs in collector.fast_deletes:
        fields = {f.name for f in qs.model._meta.fields}
        if fields & {"site", "session"}:
            _look(qs)
    for (_fld, value), instance_lists in collector.field_updates.items():
        if value is None:
            for objs in instance_lists:
                _look(_rows_of(objs))

    if via_session:
        direct.update(
            s for s in ExperimentSession.objects.using(DB)
            .filter(pk__in=via_session).values_list("site_id", flat=True) if s)

    others = {s for s in direct if s != site_id}
    if not others:
        return ""
    names = ", ".join(sorted(Site.objects.using(DB).filter(pk__in=others)
                             .values_list("name", flat=True)))
    return (f"Deleting this would also change or delete records belonging to "
            f"{names}. You can remove your own bench's work; only a superuser "
            f"can remove another site's.")


def _related(kind):
    return {"target": (), "antibody": ("company", "target"),
            "cell-line": ("target",), "session": ("target",)}.get(kind, ())


def delete(kind: str, pk, typed: str, *, member=None, is_superuser=False,
           agreed=None) -> dict:
    """Delete it, or say why not.

    Every check runs again here, not only in the preview: **a preview is not a
    permission slip**, and the record it described may have gained another
    site's antibody while the dialog sat open.

    ``agreed`` is the total the panel put on the button, and a delete that
    overrides the refusals must carry it. Re-asking whether it is *allowed*
    is not enough on this path — the answer stays yes while the number grows,
    so somebody who agreed to "delete it and 3 other records" would silently
    destroy thirty. Refusing on a changed number costs one re-opened dialog and
    is the only check that is about what the person actually consented to.
    """
    p = plan(kind, pk, member=member, is_superuser=is_superuser)
    if not p.allowed:
        raise Refused(p.why)
    if (typed or "").strip() != p.confirm_with:
        raise Refused(
            f"Type {p.confirm_with} exactly to confirm. Nothing has been "
            f"deleted.")
    if p.overriding:
        now = sum(c for _n, c in p.cascade) + sum(c for _n, c in p.orphaned)
        try:
            was = int(agreed)
        except (TypeError, ValueError):
            raise Refused(
                "This record has work behind it, so the confirmation has to say "
                "how much. Open the delete panel again and read what it lists.")
        if was != now:
            # `record(s)` is the same slip written down in advance, and next to
            # a number is where it costs most (CLAUDE.md).
            noun = "record" if now == 1 else "records"
            raise Refused(
                f"What is behind {p.label} changed while that panel was open — "
                f"it now affects {now:,} other {noun}, not {was:,}. Nothing "
                f"has been deleted. Open it again and read the new list.")

    model, _planner = _PLANNERS[kind]
    with transaction.atomic(using=DB):
        obj = model.objects.using(DB).filter(pk=pk).first()
        if obj is None:
            raise Refused("That record is not on file — it may already be deleted.")
        # Before the record itself, so the rows go rather than being nulled on
        # the way past. Same list the manifest was built from — see
        # `companions()` for why a knockout is part of its gene and a wild type
        # is not.
        for extra in companions(kind, obj):
            extra.delete(using=DB)
        obj.delete(using=DB)
    return {"deleted": True, "kind": kind, "id": pk, "label": p.label,
            "also_removed": [{"noun": n, "count": c} for n, c in p.owned if c]}
