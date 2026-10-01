"""What changed in this API, as data a program can read.

**A consumer should be able to ask the API what changed, not only read about it
in API.md** (owner, 29 Sep 2026). 269 of the API's 357 all-time requests were
keyless and are deliberately never identified, so there is nobody to email; the
only channel that reaches every caller is the API itself. So this list is the
one source:

* ``API_VERSION`` (`core/api_schema.py`) is ``CURRENT``, so ``openapi.json``'s
  ``info.version`` cannot drift from the newest entry here;
* ``/api/v1/`` names the version and links ``/api/v1/changelog/``;
* ``/api/v1/changelog/`` serves these entries, newest first, and
  ``?since=2.2.0`` answers only what is newer than the version a client last
  saw;
* every ``/api/`` reply carries ``OGA-API-Version``, so a client notices a new
  version on a request it was making anyway (`OGA_website/api_version.py`).

API.md §12 is the prose; each entry here is the same release, one line per
change, and a test holds the two to the same version numbers.

``kind`` is one of ``added``, ``changed``, ``removed``, ``fixed`` — ``changed``
and ``removed`` are the two a client may have to act on, and ``action`` says
what to do when there is something to do.
"""
from __future__ import annotations

CHANGELOG = [
    {
        "version": "2.5.0",
        "date": "2026-10-01",
        "summary": "The manifest carries the methods paragraph for each "
                   "supportive figure, and the paragraph names only the "
                   "secondary antibody matching the primary's host.",
        "changes": [
            {"kind": "added", "where": "/manifest/ (JSON and CSV), bulk archive manifest.csv",
             "what": "oga_methods_text: the Copy methods paragraph for that "
                     "figure, the same text as oga_methods[application].text. "
                     "Empty where the result is not supportive; the last CSV "
                     "column, so positional readers keep their indices. "
                     "manifest_version is 6, so a held ETag is told once."},
            {"kind": "changed", "where": "oga_methods.text, oga_methods_text",
             "what": "Where a report lists several secondary antibodies, the "
                     "paragraph names the one raised against the primary's "
                     "host species (and its concentration, where the report "
                     "gives one per species); figure references and the "
                     "extraction's notes are no longer printed.",
             "action": "None: the field is the same text, written better."},
        ],
    },
    {
        "version": "2.4.0",
        "date": "2026-09-30",
        "summary": "oga_methods: how each supportive published figure was "
                   "made, per application, with the paragraph the website's "
                   "Copy methods button copies.",
        "changes": [
            {"kind": "added", "where": "/antibodies/, /gene-detail/",
             "what": "oga_methods, keyed by application like oga_support: "
                     "text (the methods paragraph, citing the report DOI), "
                     "amount (this antibody's dilution or amount), "
                     "amount_basis (report_named, report_protocol, "
                     "report_general or lab_record), source (the DOI) and "
                     "conditions (the run's conditions as the report states "
                     "them). Present only where the result is supportive and "
                     "there is a methods record, so a key appears when a "
                     "result is regraded to supportive. An absent key says "
                     "nothing about the antibody."},
        ],
    },
    {
        "version": "2.3.0",
        "date": "2026-09-29",
        "summary": "?application= refuses a value it does not know, and accepts "
                   "the everyday names for the five applications.",
        "changes": [
            {"kind": "changed", "where": "/antibodies/?application=",
             "what": "An unrecognised value is refused with 400 and the list of "
                     "accepted values. It was silently ignored, so "
                     "?application=IF returned every antibody unfiltered, "
                     "indistinguishable from a real answer.",
             "action": "If you send ?application=, check for a 400. Use WB, IP, "
                       "ICC-IF, FC or IHC, or one of the aliases below."},
            {"kind": "added", "where": "/antibodies/?application=",
             "what": "Everyday names are accepted and mapped to the five codes: "
                     "IF, ICC, immunofluorescence and immunocytochemistry → "
                     "ICC-IF; western blot → WB; immunoprecipitation → IP; flow, "
                     "flow cytometry and FACS → FC; immunohistochemistry → IHC. Case, "
                     "spaces and punctuation are ignored."},
            {"kind": "added", "where": "/antibodies/",
             "what": "application_filter: the code the reply was filtered on, "
                     "or null — so a client can see what its parameter was read "
                     "as."},
            {"kind": "added", "where": "/changelog/",
             "what": "This changelog, as JSON. ?since=<version> answers only "
                     "what is newer. No key needed."},
            {"kind": "added", "where": "every /api/ reply",
             "what": "An OGA-API-Version header, so a client notices a new "
                     "version on a request it was making anyway."},
            {"kind": "fixed", "where": "every endpoint taking X-API-Key",
             "what": "A key that is not a UUID is refused with 403, like any "
                     "other wrong key. It was a 500."},
            {"kind": "fixed", "where": "/changelog/?since=",
             "what": "A version newer than the current one is refused with 400. "
                     "It answered up_to_date: true."},
            {"kind": "changed", "where": "supplier-scoped keys",
             "what": "A manufacturer's key matches its supplier by name "
                     "ignoring case and punctuation, and every record filed "
                     "under that supplier's display name, so a vial filed "
                     "under a second spelling of the supplier is no longer "
                     "missing from its feed."},
        ],
    },
    {
        "version": "2.2.0",
        "date": "2026-09-26",
        "summary": "IHC (on cell pellets, not tissue) joins as a fifth "
                   "application.",
        "changes": [
            {"kind": "added", "where": "/antibodies/",
             "what": "An IHC key in oga_support, oga_display, "
                     "oga_recommendations, oga_qualifiers and recommendations."},
            {"kind": "changed", "where": "experiment_type, manifest application, "
                                         "?application=, /not-supportive/, "
                                         "/gene-progress/",
             "what": "IHC is a new enum value.",
             "action": "Regenerate a client built strictly from openapi.json, "
                       "and decide what to do with IHC where you switch on the "
                       "application."},
            {"kind": "changed", "where": "/manifest/",
             "what": "Body version 5: rows include IHC figures. No column "
                     "changed."},
        ],
    },
    {
        "version": "2.1.0",
        "date": "2026-09-14",
        "summary": "oga_support: the four-rung result as a controlled value.",
        "changes": [
            {"kind": "added", "where": "/antibodies/, /manifest/, /not-supportive/",
             "what": "oga_support: supportive, limited_support, not_supportive, "
                     "not_tested. The CSV carries it as the last column."},
            {"kind": "added", "where": "/antibodies/",
             "what": "oga_display and oga_qualifiers documented (shipped "
                     "since 12 Sep)."},
            {"kind": "removed", "where": "/antibodies/ oga_display.verdict",
             "what": "The legacy value, duplicated from oga_recommendations.",
             "action": "Read oga_support (or oga_recommendations)."},
        ],
    },
    {
        "version": "2.0.0",
        "date": "2026-08-07",
        "summary": "verdicts removed; ?gene= on /manifest/; conditional "
                   "requests fixed.",
        "changes": [
            {"kind": "removed", "where": "/antibodies/ verdicts",
             "what": "A deprecated alias of oga_recommendations.",
             "action": "Read oga_recommendations or oga_support."},
            {"kind": "added", "where": "/manifest/?gene=",
             "what": "Mirror part of the dataset incrementally."},
            {"kind": "fixed", "where": "If-None-Match",
             "what": "Weak comparison, so a reconnect is answered 304."},
        ],
    },
]

CURRENT = CHANGELOG[0]["version"]


def _as_tuple(version: str):
    try:
        return tuple(int(p) for p in version.strip().split("."))
    except ValueError:
        return None


def since(version: str):
    """The entries newer than ``version``, or None if it is not a version."""
    wanted = _as_tuple(version)
    if wanted is None:
        return None
    return [e for e in CHANGELOG if _as_tuple(e["version"]) > wanted]
