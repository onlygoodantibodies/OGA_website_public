"""The cropper page opens pre-loaded when deep-linked with ?gene= — so a
cropper deep-link lands in a ready-to-crop workspace, not a blank one."""
from __future__ import annotations

from django.template.loader import render_to_string


def test_page_prefilled_and_autoloads_with_gene():
    html = render_to_string("pipeline/cropper.html", {"initial_gene": "SNCA"})
    # The gene field is prefilled...
    assert 'id="gene" value="SNCA"' in html
    # ...and the DB status/antibody autoload is triggered on load.
    assert "checkGene();" in html


def test_page_keeps_default_without_gene():
    """With no deep-linked gene the box is EMPTY, and TREM2 is its placeholder.

    This asserted `value="TREM2"` and had failed on `beta` ever since the
    cropper stopped pre-filling a gene nobody asked for — the app was right and
    the test was pinning the old behaviour. It is the same rule the boards
    carry: *a prompt is not a value, and an example is a prompt*. A prefilled
    `TREM2` is indistinguishable from a gene the reader chose, and it is the one
    that gets cropped against if they do not notice.
    """
    html = render_to_string("pipeline/cropper.html", {"initial_gene": ""})
    assert 'id="gene" value=""' in html
    assert 'placeholder="e.g. TREM2"' in html
    # No auto-trigger when there was no deep-linked gene.
    assert "checkGene();" not in html.split("loadSessionList();")[-1]


def test_view_passes_gene_into_context():
    from django.test import RequestFactory
    from django.contrib.auth.models import User
    from pipeline.views.cropper import cropper

    DB = "pipeline_db"
    req = RequestFactory().get("/pipeline/cropper/?gene=SNCA")
    req.user = User.objects.using(DB).get(username="sara")
    resp = cropper(req)
    assert resp.status_code == 200
    assert b'value="SNCA"' in resp.content
