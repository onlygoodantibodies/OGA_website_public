"""Regression tests for extension packaging.

Written after /extension/download/ returned a 500 in production: the manifest
validator rejected the broad host pattern that had just been added, on purpose,
as an opt-in permission. The distinction it must hold is narrow and easy to get
backwards, so it is pinned here.
"""

import json
import os

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from core.extension_index import (
    NOT_RECOMMENDED, NOT_TESTED, RECOMMENDED, _manifest_problems, _one_to_one,
    build_index,
)
from pipeline.models import Antibody, Company, PublicationImage, Target


def _manifest():
    root = os.path.join(settings.BASE_DIR, 'browser-extension')
    with open(os.path.join(root, 'manifest.json'), encoding='utf-8') as fh:
        return json.load(fh), root


class ManifestValidationTests(SimpleTestCase):

    def test_shipped_manifest_is_packageable(self):
        manifest, root = _manifest()
        self.assertEqual(_manifest_problems(manifest, root), [])

    def test_broad_pattern_is_allowed_when_optional(self):
        """Opt-in broad access is the only way to cover institutional proxies."""
        manifest, root = _manifest()
        manifest['optional_host_permissions'] = ['*://*/*']
        self.assertEqual(_manifest_problems(manifest, root), [])

    def test_broad_pattern_is_rejected_when_granted_at_install(self):
        manifest, root = _manifest()
        manifest['host_permissions'] = ['*://*/*']
        problems = _manifest_problems(manifest, root)
        self.assertTrue(any('host_permissions' in p for p in problems), problems)

    def test_missing_data_collection_declaration_is_caught(self):
        """AMO rejects this outright; it cost a failed submission once already."""
        manifest, root = _manifest()
        del manifest['browser_specific_settings']['gecko']['data_collection_permissions']
        # Not currently checked -- assert the gecko id check still fires so this
        # file fails loudly if the structure is ever renamed.
        manifest['browser_specific_settings']['gecko'].pop('id')
        self.assertTrue(any('gecko' in p for p in _manifest_problems(manifest, root)))


class IdentifierCollapseTests(SimpleTestCase):
    """One identifier must resolve to one antibody, or to none.

    Load-bearing for clone IDs especially: the same clone is routinely sold by
    several suppliers under their own catalogue numbers, so collisions are
    normal rather than exceptional.
    """

    AGREE = {
        'AB_1': {'g': 'TARDBP', 'a': {'WB': 2, 'IP': 0, 'IF': 0, 'FC': 0}},
        'AB_2': {'g': 'TARDBP', 'a': {'WB': 2, 'IP': 0, 'IF': 0, 'FC': 0}},
        'AB_3': {'g': 'TARDBP', 'a': {'WB': 1, 'IP': 0, 'IF': 0, 'FC': 0}},
        'AB_4': {'g': 'APP', 'a': {'WB': 2, 'IP': 0, 'IF': 0, 'FC': 0}},
    }

    def test_a_single_owner_resolves(self):
        resolved, ambiguous = _one_to_one({'gt733': ['AB_1']}, self.AGREE)
        self.assertEqual(resolved, {'gt733': 'AB_1'})
        self.assertEqual(ambiguous, set())

    def test_the_same_product_twice_still_resolves(self):
        """Relabelled by a second supplier, same target and same verdicts."""
        resolved, ambiguous = _one_to_one({'gt733': ['AB_1', 'AB_2']}, self.AGREE)
        self.assertEqual(resolved, {'gt733': 'AB_1'})
        self.assertEqual(ambiguous, set())

    def test_conflicting_verdicts_are_dropped_not_guessed(self):
        resolved, ambiguous = _one_to_one({'gt733': ['AB_1', 'AB_3']}, self.AGREE)
        self.assertEqual(resolved, {})
        self.assertEqual(ambiguous, {'gt733'})

    def test_different_targets_are_dropped(self):
        resolved, ambiguous = _one_to_one({'gt733': ['AB_1', 'AB_4']}, self.AGREE)
        self.assertEqual(resolved, {})
        self.assertEqual(ambiguous, {'gt733'})


class TheSignedBuildAndTheEnvVarCannotDisagreeQuietlyTests(SimpleTestCase):
    """`EXTENSION_XPI_VERSION` must name a file that is in `signed/`.

    The variable is in the Render dashboard and the `.xpi` files are in git, so
    nothing ties them together — the same shape as the Build Command and
    `.python-version`. Both directions fail without a symptom anybody would
    connect to the cause: a version with no file hides the Firefox install button
    and 404s the download under copy still calling it current, and a version left
    behind keeps the team on an older build while the update manifest says they
    are up to date. The twelfth field test reported a version mismatch it could
    not explain and nothing in the repo could answer it.

    A check nobody has seen fail is a check nobody should trust, so each branch
    is driven here as well as the clean case.
    """

    def _run(self):
        from core.apps import _check_signed_firefox_build

        return _check_signed_firefox_build(None)

    def _present(self):
        """The versions actually in `signed/`, oldest first.

        Read off disk rather than typed, because every release adds one: a test
        naming today's newest build asserts nothing the day after it is cut, and
        fails for the one reason that is never a defect.
        """
        import os

        from django.conf import settings
        from core.apps import _version_key

        signed = os.path.join(settings.BASE_DIR, 'browser-extension', 'signed')
        names = [f[len('oga-extension-'):-len('.xpi')]
                 for f in os.listdir(signed) if f.endswith('.xpi')]
        return sorted(names, key=_version_key)

    def test_unset_says_nothing(self):
        """A valid state: no signed build offered, side-load instructions shown."""
        with self.settings(EXTENSION_XPI_VERSION=''):
            self.assertEqual(self._run(), [])

    def test_a_version_with_no_file_is_reported(self):
        with self.settings(EXTENSION_XPI_VERSION='9.9.9'):
            issues = self._run()
        self.assertEqual([i.id for i in issues], ['core.W001'])
        self.assertIn('9.9.9', issues[0].msg)
        # The remedy needs the alternatives, or it names the problem and not the
        # fix — the same rule `services/sites.py` holds for a refused site.
        for version in self._present():
            self.assertIn(version, issues[0].hint)

    def test_a_newer_build_sitting_unserved_is_reported(self):
        present = self._present()
        oldest, newest = present[0], present[-1]
        self.assertNotEqual(oldest, newest, 'needs two builds to have a newer one')

        with self.settings(EXTENSION_XPI_VERSION=oldest):
            issues = self._run()
        self.assertEqual([i.id for i in issues], ['core.W002'])
        self.assertIn(newest, issues[0].msg)

    def test_the_newest_build_being_served_is_silent(self):
        with self.settings(EXTENSION_XPI_VERSION=self._present()[-1]):
            self.assertEqual(self._run(), [])

    def test_versions_sort_numerically_not_as_strings(self):
        """`0.2.10` is above `0.2.9`; a string compare gets that backwards, and
        the whole check is a comparison."""
        from core.apps import _version_key

        self.assertGreater(_version_key('0.2.10'), _version_key('0.2.9'))
        self.assertGreater(_version_key('0.10.0'), _version_key('0.9.0'))


class ThePackagedVersionIsTheOneWeIntendToShipTests(SimpleTestCase):
    """manifest.json is the single source, and package.json must not drift.

    `build_zip_bytes` names the artefact from the manifest, and that zip is what
    goes to the AMO Developer Hub — which only accepts a version above every one
    already uploaded. So the number moving is the deliberate act that makes a new
    signed build possible, and a stale copy of it elsewhere in the tree is the
    kind of thing that gets read as the authority by the next person.
    """

    def test_the_manifest_and_the_dev_harness_agree(self):
        import json
        import os

        from django.conf import settings

        manifest, root = _manifest()
        with open(os.path.join(root, 'package.json'), encoding='utf-8') as fh:
            package = json.load(fh)
        self.assertEqual(manifest['version'], package['version'])

    def test_the_changelog_has_an_entry_for_it(self):
        """A release with no entry is one nobody can tell from the last, and the
        AMO reviewer reads it. This caught a real gap: `collapseIdentifier` went
        in under an MCP-titled commit and was never recorded here."""
        import os

        from django.conf import settings

        manifest, root = _manifest()
        with open(os.path.join(root, 'CHANGELOG.md'), encoding='utf-8') as fh:
            changelog = fh.read()
        self.assertIn(f"\n## {manifest['version']}\n", changelog)


class TheDownloadIsNamedForTheManifestTests(TestCase):
    """The zip's filename and the manifest inside it are one string.

    This is what makes moving the manifest version the whole act of cutting a
    release: `/extension/download/` produces `oga-extension-<version>.zip`, that
    file goes to the AMO Developer Hub, and AMO refuses a version it has already
    seen. If the two could drift, an upload would be rejected for a number the
    page had just handed you.

    Driven through `build_zip_bytes` rather than the view because the view is
    behind `pipeline_member_required`; the naming is the view's only other job.
    """

    databases = {"default", "pipeline_db", "academy_db"}

    def test_the_zip_is_named_for_the_version_inside_it(self):
        import io
        import json
        import zipfile

        from core.extension_index import build_zip_bytes

        payload, version = build_zip_bytes()
        manifest, _root = _manifest()
        self.assertEqual(version, manifest['version'])

        archive = zipfile.ZipFile(io.BytesIO(payload))
        packaged = json.loads(archive.read('manifest.json'))
        self.assertEqual(packaged['version'], version,
                         "the filename and the packaged manifest disagree")

    def test_the_dev_harness_and_the_signed_builds_are_not_shipped(self):
        """A store reviewer should not be handed a devDependency on Playwright to
        explain, nor a signed binary of the previous release."""
        import io
        import zipfile

        from core.extension_index import build_zip_bytes

        payload, _version = build_zip_bytes()
        names = zipfile.ZipFile(io.BytesIO(payload)).namelist()
        self.assertNotIn('package.json', names)
        self.assertFalse([n for n in names if n.startswith('signed/')])


class AnUncuratedGeneIsNotAFailedTestTests(TestCase):
    """The extension badged products as failing tests nobody had run on them.

    ``_verdicts`` took a published figure as proof the application had been
    assessed. Figures go up as sessions are cropped and the recommendations are
    set later, in one pass on ``/pipeline/recommendations/`` — so between those
    two steps every antibody on the gene carried ``NOT_RECOMMENDED``, and the
    extension draws that on a publisher's page beside a named commercial
    product. It is the same defect the portal API had, in the opposite
    direction, and the two disagreed for as long as both existed.

    ``core/verdicts.py`` is the shared reader now. These pin the gate itself,
    not the plumbing: the only difference between the two genes below is
    whether anything on them has been recommended.
    """

    databases = {"default", "pipeline_db", "academy_db"}

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Proteintech")

        # Curated: something on this gene is recommended, so a figure with no
        # flag is a real negative result.
        cls.curated = Target.objects.create(
            protein_name="Alpha-synuclein", gene_name="SNCA")
        cls._antibody(cls.curated, "10842-1-AP", "AB_curated_pass",
                      wb_recommended=True)
        cls._antibody(cls.curated, "sc-12767", "AB_curated_fail")

        # Uncurated: figures published, nobody has been through it yet.
        cls.uncurated = Target.objects.create(
            protein_name="Stathmin-2", gene_name="STMN2")
        cls._antibody(cls.uncurated, "10586-1-AP", "AB_uncurated")

    @classmethod
    def _antibody(cls, target, catalogue, rrid, **flags):
        antibody = Antibody.objects.create(
            target=target, company=cls.company, catalogue_number=catalogue,
            rrid=rrid, **flags)
        PublicationImage.objects.create(
            antibody=antibody, application_type="WB",
            image=f"publication_images/2026/{catalogue}_WB.png")
        return antibody

    def _codes(self):
        index = build_index()
        return {rrid: record["a"]["WB"]
                for rrid, record in index["antibodies"].items()}

    def test_a_figure_on_an_uncurated_gene_is_not_a_failure(self):
        self.assertEqual(self._codes()["AB_uncurated"], NOT_TESTED)

    def test_a_figure_on_a_curated_gene_still_is_one(self):
        """The gate must not swallow real negative results.

        Turning every unflagged antibody into "not tested" would be the same
        error the other way round, and would quietly delete the findings this
        dataset exists to publish.
        """
        codes = self._codes()
        self.assertEqual(codes["AB_curated_fail"], NOT_RECOMMENDED)
        self.assertEqual(codes["AB_curated_pass"], RECOMMENDED)

    def test_one_recommendation_anywhere_curates_the_whole_gene(self):
        """The signal is the gene's, not the antibody's — which is why it is a
        query over every antibody on the target rather than a field."""
        before = self._codes()["AB_uncurated"]
        Antibody.objects.filter(rrid="AB_uncurated").update(ip_recommended=True)
        after = self._codes()["AB_uncurated"]

        self.assertEqual(before, NOT_TESTED)
        self.assertEqual(
            after, NOT_RECOMMENDED,
            "Once anything on the gene is recommended, a published WB figure "
            "with no WB flag is somebody's assessment rather than a gap.")
