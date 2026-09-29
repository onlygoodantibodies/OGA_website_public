"""Europe PMC annotations: what would be *silently* wrong.

The platform answers by email an hour after upload, so nothing here can be
found by running it. Four silent failures are pinned:

* a printed identifier the extension would mark that this does not (the
  identifier rules drifting from ``matcher.js``), or a tag carrying more than
  the antibody's name (Europe PMC's schema has nowhere for it);
* a span attributed to a reagent the snapshot did not say the paper used — the
  page must never widen what CiteAb asserted;
* a row the platform would refuse, written anyway;
* a paper left out with nothing saying so.
"""
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from core import europepmc_annotations as E


JATS = """<?xml version="1.0"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>
<title-group><article-title>TDP-43 in <italic>C9orf72</italic> models</article-title></title-group>
<abstract><p>We studied TDP-43. Loss of nuclear TDP-43 was seen.</p></abstract></article-meta></front>
<body>
<sec sec-type="intro"><title>Introduction</title><p>Background here.</p></sec>
<sec><title>Materials and methods</title><sec><title>Antibodies</title>
<p>Primary antibodies were: anti-TDP-43 (Bio-Techne, cat. no. MAB7778, RRID:AB_10679421) at 1:1000;
anti-C9orf72 (Proteintech, 66 140-1-Ig) at 1:500; anti-GAPDH (Abcam, ab9485) at 1:5000. Blots were imaged.</p>
</sec></sec>
<sec sec-type="results"><title>Results and discussion</title><p>Fig. 1 shows MAB7778 staining.</p>
<fig id="f1"><label>Figure 1</label><caption><p>TDP-43 (MAB7778) in HAP1 cells.</p></caption></fig>
<table-wrap><table><tr><td>MAB7778</td><td>Bio-Techne</td></tr><tr><td>66140-1-Ig</td></tr></table></table-wrap>
</sec></body>
<back><ref-list><ref><mixed-citation>Someone used MAB7778 in 2019.</mixed-citation></ref></ref-list></back>
</article>"""

INDEX = {
    "source": "https://onlygoodantibodies.co.uk",
    "antibodies": {
        "AB_10679421": {"n": "MAB7778", "g": "TARDBP", "s": "Bio-Techne", "cl": "671834",
                        "a": {"WB": 1, "IP": 2, "IF": 2, "FC": 0}},
        "AB_2881555": {"n": "66140-1-Ig", "g": "C9orf72", "s": "Proteintech",
                       "a": {"WB": 2, "IP": 1, "IF": 0, "FC": 0}, "q": {"IP": "sd"}},
        # In the index, printed in the text, and NOT attributed to the paper by
        # the snapshot: must draw nothing.
        "AB_307275": {"n": "ab9485", "g": "GAPDH", "s": "Abcam",
                      "a": {"WB": 2, "IP": 0, "IF": 0, "FC": 0}},
    },
}

PAPER = {
    "AB_10679421": {"applications": ["WB", "IF"], "ambiguous": False, "untested_terms": []},
    "AB_2881555": {"applications": ["WB"], "ambiguous": True, "untested_terms": []},
}


def _reagents():
    return E.reagents_for(PAPER, INDEX)


class IdentifierRulesMirrorTheExtensionTests(SimpleTestCase):
    """The same printed strings the extension resolves must resolve here."""

    def test_typesetting_variants_key_the_same(self):
        for printed in ("14 060–1-AP", "14,060-1-AP", "14​060-1-AP"):
            self.assertEqual(E.identifier_key(printed), "14060-1-ap", printed)
        self.assertEqual(E.collapse_identifier("14060-1AP"), "140601ap")
        self.assertEqual(E.collapse_identifier("ab12"), "", "short keys are never compared")
        self.assertEqual(E.rrid_key("RRID: AB–10679421"), "AB_10679421")

    def test_a_bare_short_clone_is_not_a_key(self):
        self.assertFalse(E.is_distinctive_clone("200"))
        self.assertFalse(E.is_distinctive_clone("5"))
        self.assertTrue(E.is_distinctive_clone("671834"))
        self.assertTrue(E.is_distinctive_clone("D11A10"))

    def test_runs_without_django(self):
        """The runner and the owner's laptop have python3 and nothing else."""
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys, core.europepmc_annotations; "
             "sys.exit(1 if {'django', 'minio'} & set(sys.modules) else 0)"],
            cwd=Path(E.__file__).resolve().parent.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class TheArticleIsReadInOrderTests(SimpleTestCase):

    def test_sections_and_order(self):
        blocks = E.sections_from_jats(JATS)
        self.assertEqual(blocks[0], ("Title", "TDP-43 in C9orf72 models"))
        self.assertEqual(blocks[1][0], "Abstract")
        sections = {s for s, _ in blocks}
        self.assertIn("Methods", sections)
        self.assertIn("Results", sections)
        self.assertIn("Figure", sections)
        self.assertIn("Table", sections)
        # A nested <sec> with no cue of its own is still in its parent's section.
        antibodies_para = next(s for s, t in blocks if t.startswith("Primary antibodies"))
        self.assertEqual(antibodies_para, "Methods")
        # References are not read: a catalogue number there is another paper's.
        self.assertFalse(any("2019" in t for _, t in blocks))
        # A table row is one block with a space between cells.
        self.assertIn(("Table", "MAB7778 Bio-Techne"), blocks)

    def test_a_pubmed_record_is_its_title_and_abstract_without_markup(self):
        record = {"title": "TDP-43  in models.", "abstract": "<h4>Methods</h4>We used MAB7778 at 1:1000."}
        self.assertEqual(E.abstract_blocks(record),
                         [("Title", "TDP-43 in models."), ("Abstract", "Methods We used MAB7778 at 1:1000.")])
        self.assertEqual(E.abstract_blocks({"title": "", "abstract": ""}), [])

    def test_sentences_do_not_split_on_a_catalogue_label(self):
        parts = E.sentences("anti-TDP-43 (Bio-Techne, cat. no. MAB7778) at 1:1000. "
                            "Blots were imaged. Fig. 2 shows it.")
        self.assertEqual(parts, ["anti-TDP-43 (Bio-Techne, cat. no. MAB7778) at 1:1000.",
                                 "Blots were imaged.", "Fig. 2 shows it."])


class AnnotationsQuoteThePageAndNeverWidenTheSnapshotTests(SimpleTestCase):

    def setUp(self):
        self.anns, self.dropped = E.annotate(
            E.sections_from_jats(JATS), _reagents(), "https://onlygoodantibodies.co.uk")

    def test_exact_is_what_the_article_printed(self):
        exacts = [a["exact"] for a in self.anns]
        self.assertIn("66 140-1-Ig", exacts, "the typeset spelling, not the stored one")
        self.assertIn("RRID:AB_10679421", exacts)
        self.assertIn("MAB7778", exacts)

    def test_a_reagent_the_snapshot_did_not_attribute_draws_nothing(self):
        self.assertFalse(any(a["exact"] == "ab9485" for a in self.anns))
        self.assertFalse(any("ab9485" in t["name"] for a in self.anns for t in a["tags"]))

    def test_position_prefix_postfix_and_section(self):
        first = next(a for a in self.anns if a["exact"] == "MAB7778")
        self.assertRegex(first["position"], r"^\d+\.1$")
        self.assertTrue(first["prefix"].endswith("cat. no. "))
        self.assertTrue(first["postfix"].startswith(", RRID:AB_10679421"))
        self.assertEqual(first["section"], "Methods")
        sentence = first["position"].split(".")[0]
        chunks = [a["position"] for a in self.anns if a["position"].startswith(sentence + ".")]
        self.assertEqual(chunks, [f"{sentence}.{i}" for i in range(1, len(chunks) + 1)])
        self.assertEqual(next(a["section"] for a in self.anns if a["prefix"] == "TDP-43 ("), "Figure")

    def test_an_identifier_alone_in_its_sentence_is_dropped_and_counted(self):
        # The one-cell table row `66140-1-Ig` has no text either side.
        self.assertEqual(self.dropped, 1)
        for ann in self.anns:
            self.assertTrue(ann["prefix"] or ann["postfix"])

    def test_every_annotation_is_a_named_entity(self):
        """Europe PMC's review: named-entity annotations only, never a sentence."""
        for ann in self.anns:
            self.assertIn("position", ann)
            self.assertTrue(ann["exact"])
            self.assertLess(len(ann["exact"]), 40, "a span is an identifier, not a sentence")

    def test_the_tag_is_the_canonical_name_and_nothing_else(self):
        """Their review: the name field holds the antibody's canonical name; every
        other fact goes on the linked page. The RRID citation form is that name."""
        tag = next(a for a in self.anns if a["exact"] == "MAB7778")["tags"][0]
        self.assertEqual(tag["name"], "Bio-Techne Cat# MAB7778, RRID:AB_10679421")
        self.assertEqual(tag["uri"], "https://onlygoodantibodies.co.uk/antibodies/TARDBP/?ab=MAB7778")
        for word in ("supportive", "tested", "used in this paper", "WB", "·"):
            self.assertNotIn(word, tag["name"])
        clone = next(a for a in self.anns if a["exact"] == "RRID:AB_10679421")["tags"][0]
        self.assertEqual(clone["name"], tag["name"], "every way of printing one antibody gets one name")

    def test_canonical_name_without_a_supplier_or_catalogue(self):
        base = {"rrid": "AB_1", "catalogue": "", "supplier": "", "gene": "X", "clone": "",
                "codes": {}, "qualifiers": {}, "discontinued": False, "used_for": [],
                "untested_terms": [], "ambiguous": False}
        self.assertEqual(E.canonical_name(base), "RRID:AB_1")
        self.assertEqual(E.canonical_name({**base, "catalogue": "ab1"}), "Cat# ab1, RRID:AB_1")
        self.assertEqual(E.canonical_name({**base, "supplier": "Abcam"}), "Abcam, RRID:AB_1")

    def test_every_row_is_submittable(self):
        row = E.row_for("PMC", "PMC1234567", "oga", self.anns)
        self.assertEqual(E.validate_row(row), [])

    def test_every_annotation_carries_the_registered_type(self):
        """Required by the platform's schema, and the word we register with
        them: the first sample had no ``type`` at all and would have failed
        every row."""
        self.assertTrue(self.anns)
        self.assertEqual({a["type"] for a in self.anns}, {E.ANNOTATION_TYPE})


class AShortCatalogueNumberNeedsItsSentenceToSaySoTests(SimpleTestCase):
    """Cell Signaling numbers are four digits: below the collapsed minimum, and a
    number that appears in dilutions, years and figure counts. The first live
    run registered an EMPTY collapsed key for one and matched every short word
    in the paper -- 2,519 annotations on one article, 'and' 1,823 times."""

    CST = {"source": "https://x", "antibodies": {
        "AB_2299553": {"n": "3872", "g": "PLCG2", "s": "Cell Signaling Technology",
                       "a": {"WB": 2, "IP": 0, "IF": 0, "FC": 0}}}}
    USE = {"AB_2299553": {"applications": [], "ambiguous": True, "untested_terms": []}}

    def _spans(self, sentence):
        reagents = E.reagents_for(self.USE, self.CST)
        return [sentence[s:e] for s, e, _, _ in E.spans_in(sentence, E.lookup_keys(reagents))]

    def test_no_empty_key_is_ever_registered(self):
        keys = E.lookup_keys(E.reagents_for(self.USE, self.CST))
        self.assertNotIn(("collapsed", ""), keys)
        self.assertFalse(any(not k[-1] for k in keys))

    def test_ordinary_words_do_not_match(self):
        self.assertEqual(self._spans("Cells were washed and the membrane was blocked with milk."), [])

    def test_the_number_matches_only_beside_antibody_context(self):
        self.assertEqual(self._spans("Blots were probed with anti-PLCγ2 (Cell Signaling, #3872) overnight."), ["3872"])
        self.assertEqual(self._spans("PLCγ2 Cell Signaling 3872 rabbit"), ["3872"])
        self.assertEqual(self._spans("A total of 3872 cells were counted per field."), [])

    def test_a_dilution_or_a_unit_is_not_a_catalogue_number(self):
        self.assertEqual(self._spans("The antibody was diluted 1:3872 in blocking buffer."), [])
        self.assertEqual(self._spans("Antibody incubations ran for 3872 min in cat. buffer."), [])

    def test_a_sentence_ending_full_stop_is_not_part_of_the_span(self):
        spans = E.spans_in("Cells were stained with 66 140-1-Ig.", E.lookup_keys(_reagents()))
        self.assertEqual([("Cells were stained with 66 140-1-Ig."[s:e]) for s, e, _, _ in spans],
                         ["66 140-1-Ig"])

    def test_a_long_identifier_needs_no_context(self):
        spans = E.spans_in("Lysates were run and 66 140-1-Ig was applied.", E.lookup_keys(_reagents()))
        self.assertEqual([(s, e, kind) for s, e, kind, _ in spans], [(21, 32, "catalogue")])


class ValidationRefusesWhatThePlatformWouldTests(SimpleTestCase):

    def test_problems_are_named(self):
        bad = {"src": "PPR", "id": "", "provider": "oga",
               "anns": [{"position": "x", "exact": "", "prefix": "", "postfix": "",
                         "section": "Bibliography", "tags": [{"name": "", "uri": "/relative"}]}]}
        problems = E.validate_row(bad)
        for fragment in ("src", "id is empty", "exact is empty", "type is empty",
                         "neither prefix nor postfix", "position", "section",
                         "name is empty", "uri is not absolute"):
            self.assertTrue(any(fragment in p for p in problems), (fragment, problems))
        self.assertEqual(E.validate_row({"src": "MED", "id": "1", "provider": "x", "anns": []}),
                         ["anns is empty"])


GOOD_ANN = {"position": "3.1", "prefix": "anti-TDP-43 (", "exact": "12892-1-AP",
            "postfix": ", Proteintech)", "section": "Methods", "type": E.ANNOTATION_TYPE,
            "tags": [{"name": "Proteintech Cat# 12892-1-AP, RRID:AB_2200505",
                      "uri": "https://onlygoodantibodies.co.uk/antibodies/TARDBP/?ab=12892-1-AP"}]}


class TheOfficialSchemaIsMirroredTests(SimpleTestCase):
    """Europe PMC's validator is a program whose schema is vendored beside this
    module; the required keys are read from that file, not retyped, so a
    schema change that lands here fails a test rather than a submission."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = Path(E.__file__).parent / "data" / "europepmc_ne_annotation_schema.json"
        cls.schema = json.loads(path.read_text())

    def good_row(self):
        return E.row_for("PMC", "PMC1234567", "oga", [dict(GOOD_ANN)])

    def test_a_good_row_passes(self):
        self.assertEqual(E.validate_row(self.good_row()), [])

    def test_a_row_is_exactly_the_four_keys_the_schema_names(self):
        self.assertEqual(set(self.schema["required"]), set(E.SUBMISSION_KEYS))
        self.assertEqual(self.schema["maxProperties"], len(E.SUBMISSION_KEYS))
        row = self.good_row()
        row["note"] = "helpful"
        self.assertTrue(any("unexpected key" in p and "note" in p for p in E.validate_row(row)))

    def test_every_required_key_is_refused_when_missing(self):
        ann_schema = self.schema["properties"]["anns"]["items"]
        tag_schema = ann_schema["properties"]["tags"]["items"]
        for key in self.schema["required"]:
            row = self.good_row()
            del row[key]
            self.assertTrue(E.validate_row(row), key)
        for key in ann_schema["required"]:
            row = self.good_row()
            del row["anns"][0][key]
            self.assertTrue(any(key in p for p in E.validate_row(row)), key)
        for key in tag_schema["required"]:
            row = self.good_row()
            row["anns"][0]["tags"] = [dict(GOOD_ANN["tags"][0])]
            del row["anns"][0]["tags"][0][key]
            self.assertTrue(any(key in p for p in E.validate_row(row)), key)

    def test_our_sources_are_on_the_schema_s_list_and_preprints_are_not(self):
        pattern = re.compile(self.schema["properties"]["src"]["pattern"])
        for src in E.SOURCES:
            self.assertTrue(pattern.fullmatch(src), src)
        self.assertIsNone(pattern.fullmatch("PPR"))

    def test_the_validator_s_own_rule_a_prefix_or_a_postfix(self):
        row = self.good_row()
        row["anns"][0]["prefix"] = row["anns"][0]["postfix"] = ""
        self.assertTrue(any("neither prefix nor postfix" in p for p in E.validate_row(row)))


class TheBuildNamesWhatItLeavesOutTests(SimpleTestCase):

    def _artefact(self):
        rrids = ["AB_10679421", "AB_2881555", "AB_000000"]
        # `citations.unpack`'s encoding: <rrid index hex>:<flags hex>
        return {
            "rrids": rrids, "tokens": [],
            "papers": ["0:5,1:11", "0:1", "0:1", "0:1", "2:1", "0:1"],
            "by_pmid": {"111": 0, "PPR999": 1, "222": 2, "333": 3, "444": 4, "555": 5},
        }

    def test_rows_and_report(self):
        calls = {"search": 0, "fetch": []}

        def search(pmids, email):
            calls["search"] += 1
            return {
                # Open access, identifiers in the full text, none in the abstract.
                "111": {"pmcid": "PMC111", "open_access": True, "title": "Paper one", "abstract": ""},
                # Abstract only, and the abstract prints the catalogue number.
                "222": {"pmcid": "", "open_access": False, "title": "Paper two",
                        "abstract": "<h4>Methods</h4>Blots used MAB7778 from Bio-Techne."},
                # Open access, and nothing prints an identifier anywhere.
                "333": {"pmcid": "PMC333", "open_access": True, "title": "Paper three", "abstract": "Nothing."},
            }
            # 555 is not returned at all.

        def fetch(pmcid, email):
            calls["fetch"].append(pmcid)
            if pmcid == "PMC111":
                return JATS
            return "<article><body><p>No reagent named here.</p></body></article>"

        with tempfile.TemporaryDirectory() as tmp:
            cache = E.Cache(tmp)
            rows, report = E.build(self._artefact(), INDEX, "oga", cache, search=search,
                                   fetch=fetch, sleep=lambda s: None)
            self.assertEqual([(r["src"], r["id"]) for r in rows], [("PMC", "PMC111"), ("MED", "222")])
            self.assertEqual(rows[1]["anns"][0]["exact"], "MAB7778")
            self.assertEqual(rows[1]["anns"][0]["section"], "Abstract")
            self.assertEqual(rows[1]["anns"][0]["position"], "2.1", "the title is sentence 1")
            self.assertEqual(rows[1]["provider"], "oga")
            self.assertEqual(report["papers_annotated"], 2)
            self.assertEqual(report["rows"], 2)
            self.assertEqual(report["where"]["abstract"], ["222"])
            self.assertEqual(report["where"]["full_text"], ["111"])
            self.assertEqual(report["where"]["open_access_not_printed"], ["333"])
            self.assertEqual(report["where"]["no_open_access_full_text"], ["222"])
            self.assertEqual(report["left_out"]["identifier_not_printed"], ["333"])
            self.assertEqual(report["left_out"]["preprint"], ["PPR999"])
            self.assertEqual(report["left_out"]["no_public_record"], ["444"])
            self.assertEqual(report["left_out"]["not_in_europe_pmc"], ["555"])
            self.assertEqual(report["contextless_spans"], 1)
            self.assertEqual(report["candidates"], 4)
            self.assertEqual([E.validate_row(r) for r in rows], [[], []])

            # A second run asks nothing again: records and text are cached.
            rows2, _ = E.build(self._artefact(), INDEX, "oga", E.Cache(tmp), search=search,
                               fetch=fetch, sleep=lambda s: None)
            self.assertEqual(calls["search"], 1)
            self.assertEqual(sorted(calls["fetch"]), ["PMC111", "PMC333"])
            self.assertEqual(rows2, rows)

            lines = []
            E.print_report(report, say=lambda *a, **k: lines.append(" ".join(map(str, a))))
            text = "\n".join(lines)
            self.assertIn("PPR999", text)
            self.assertIn("333", text)
            self.assertIn("no identifier is printed", text)

    def test_a_record_cached_without_an_abstract_is_asked_again(self):
        asked = []

        def search(pmids, email):
            asked.extend(pmids)
            return {p: {"pmcid": "", "open_access": False, "title": "T", "abstract": ""} for p in pmids}

        with tempfile.TemporaryDirectory() as tmp:
            cache = E.Cache(tmp)
            cache.records["111"] = {"pmcid": "", "open_access": False, "title": "old shape"}
            cache.save_records()
            E.build(self._artefact(), INDEX, "oga", E.Cache(tmp), search=search,
                    fetch=lambda p, e: "", sleep=lambda s: None)
            self.assertIn("111", asked)

    def test_files_split_under_the_platforms_limit(self):
        row = E.row_for("PMC", "PMC1", "oga", [{"position": "1.1", "prefix": "x", "exact": "y",
                                                 "postfix": "", "section": "Methods",
                                                 "tags": [{"name": "n", "uri": "http://u"}]}])
        with tempfile.TemporaryDirectory() as tmp:
            single = E.write_files([row] * 3, tmp, "one")
            self.assertEqual([p.name for p in single], ["one.jsonl"])
            self.assertEqual(len(single[0].read_text().splitlines()), 3)
            self.assertEqual(json.loads(single[0].read_text().splitlines()[0])["id"], "PMC1")

            many = E.write_files([row] * (E.MAX_ROWS_PER_FILE + 1), tmp, "many")
            names = [p.name for p in many]
            self.assertEqual(names, ["many.part001.jsonl", "many.part002.jsonl", "many.tar.gz"])
            self.assertEqual(len(many[0].read_text().splitlines()), E.MAX_ROWS_PER_FILE)
            with tarfile.open(many[-1]) as tar:
                self.assertEqual(sorted(tar.getnames()), names[:2])


class _FakeStorage:
    """The four calls the submission half makes, over a dict."""

    def __init__(self, objects=None):
        self.objects = objects or {}
        self.puts = []

    def fput_object(self, bucket, name, path, content_type=None):
        self.puts.append((bucket, name, path, content_type))
        self.objects[(bucket, name)] = Path(path).read_bytes()

    def stat_object(self, bucket, name):
        return type("Stat", (), {"size": len(self.objects[(bucket, name)])})()

    def list_objects(self, bucket):
        return [type("Obj", (), {"object_name": n})() for (b, n) in self.objects if b == bucket]

    def get_object(self, bucket, name):
        data = self.objects[(bucket, name)]  # KeyError when absent, as S3Error would be
        return type("Resp", (), {"read": lambda self: data, "close": lambda self: None})()


class SubmissionIsExplicitAndReadBackTests(SimpleTestCase):

    def test_the_one_file_is_the_archive_when_there_is_one(self):
        parts = [Path("a.part001.jsonl"), Path("a.part002.jsonl"), Path("a.tar.gz")]
        self.assertEqual(E.submission_file(parts), Path("a.tar.gz"))
        self.assertEqual(E.submission_file([Path("a.jsonl")]), Path("a.jsonl"))
        self.assertIsNone(E.submission_file([]))

    def test_no_credentials_is_a_refusal_naming_both_variables(self):
        with self.assertRaises(E.SubmissionRefused) as caught:
            E.storage_client(environ={})
        for var in E.CREDENTIAL_VARS:
            self.assertIn(var, str(caught.exception))
        self.assertIn("Nothing was sent", str(caught.exception))

    def test_no_driver_is_a_refusal_naming_the_install(self):
        environ = {E.CREDENTIAL_VARS[0]: "u", E.CREDENTIAL_VARS[1]: "p"}
        import builtins
        real_import = builtins.__import__

        def no_minio(name, *args, **kwargs):
            if name == "minio":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        builtins.__import__ = no_minio
        try:
            with self.assertRaises(E.SubmissionRefused) as caught:
                E.storage_client(environ=environ)
        finally:
            builtins.__import__ = real_import
        self.assertIn("pip install minio", str(caught.exception))

    def test_client_is_built_from_the_environment_and_never_the_command_line(self):
        seen = {}

        def driver(endpoint, access_key, secret_key, secure):
            seen.update(endpoint=endpoint, user=access_key, password=secret_key, secure=secure)
            return "client"

        environ = {E.CREDENTIAL_VARS[0]: "user", E.CREDENTIAL_VARS[1]: "secret"}
        self.assertEqual(E.storage_client(environ=environ, driver=driver), "client")
        self.assertEqual(seen, {"endpoint": E.STORAGE_ENDPOINT, "user": "user",
                                "password": "secret", "secure": True})

    def test_submit_puts_into_submissions_and_reads_the_size_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oga_annotations.2026_09_18_1200.jsonl"
            path.write_text('{"src": "MED"}\n')
            storage = _FakeStorage()
            lines = []
            name = E.submit(path, storage, say=lambda *a, **k: lines.append(" ".join(map(str, a))))
            self.assertEqual(name, path.name)
            self.assertEqual(storage.puts, [(E.SUBMISSIONS_BUCKET, path.name, str(path),
                                             "application/octet-stream")])
            self.assertTrue(any("read back" in l for l in lines))
            self.assertTrue(any(f"--results {path.name}" in l for l in lines))

    def test_a_short_read_back_is_refused_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.jsonl"
            path.write_text("abc")
            storage = _FakeStorage()
            storage.stat_object = lambda bucket, name: type("Stat", (), {"size": 1})()
            with self.assertRaises(E.SubmissionRefused) as caught:
                E.submit(path, storage, say=lambda *a, **k: None)
            self.assertIn("send it again", str(caught.exception))

    def test_a_missing_file_sends_nothing(self):
        storage = _FakeStorage()
        with self.assertRaises(E.SubmissionRefused):
            E.submit("/no/such/file.jsonl", storage, say=lambda *a, **k: None)
        self.assertEqual(storage.puts, [])

    def test_results_fetches_both_files_or_says_they_are_not_there_yet(self):
        storage = _FakeStorage({
            (E.RESULTS_BUCKET, "Result_x.jsonl.txt"): b"Annotations Loading performed successfully",
            (E.SUBMISSIONS_BUCKET, "Log_x.jsonl.txt"): b"Loading starting",
            (E.RESULTS_BUCKET, "Result_older.txt"): b"failed",
        })
        lines = []
        say = lambda *a, **k: lines.append(" ".join(map(str, a)))
        self.assertTrue(E.results(storage, "x.jsonl", say=say))
        text = "\n".join(lines)
        self.assertIn("performed successfully", text)
        self.assertIn("Loading starting", text)
        lines.clear()
        self.assertFalse(E.results(storage, "never-sent.jsonl", say=say))
        self.assertIn("not there yet", "\n".join(lines))
        lines.clear()
        self.assertTrue(E.results(storage, say=say))
        self.assertIn("Result_older.txt", "\n".join(lines))
        self.assertIn("Result_x.jsonl.txt", "\n".join(lines))
