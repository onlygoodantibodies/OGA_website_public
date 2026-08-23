"""
Figure-cropper session persistence (spec §7).

Kept in a separate module (imported at the end of pipeline/models.py) to avoid
editing models.py while the dedup session works there. These models are pure
working state for the cropper — they do NOT hold published data; the commit step
writes Target / Antibody / PublicationImage separately.

  CropperSession  — one in-progress gene (shared gene/legend settings + antibody
                    list). Owned by a pipeline user (by username, avoiding the
                    two-user-pool FK).
  CropperImage    — one uploaded composite figure, staged so it survives a reload,
                    with its application, grid geometry, and cell→antibody mapping.

All rows live in pipeline_db (routed by app_label).
"""
from django.db import models


class CropperSession(models.Model):
    owner_username = models.CharField(max_length=150, db_index=True)
    gene = models.CharField(max_length=255, blank=True)
    # shared gene / legend settings (spec §8)
    cell_line = models.CharField(max_length=255, blank=True)
    genotype = models.CharField(max_length=4, default="KO")   # KO | KD
    fc_secondary = models.BooleanField(default=False)
    uniprot_id = models.CharField(max_length=50, blank=True)
    protein_name = models.CharField(max_length=255, blank=True)
    antibody_list = models.TextField(blank=True)              # pasted catalogues, one per line
    # per-catalogue metadata from the pasted table (spec §4):
    # {catalogue: {company, rrid, clonality, is_recombinant, clone_id, host, supplier_url, discontinued}}
    metadata = models.JSONField(default=dict, blank=True)
    # human-set "recommended" flags, per antibody per application (spec §10):
    # {catalogue: {"WB": true, "ICC-IF": true, ...}}
    recommended = models.JSONField(default=dict, blank=True)
    overwrite_ack = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "pipeline"
        ordering = ["-updated_at"]
        verbose_name = "Cropper session"

    def __str__(self):
        return f"Cropper: {self.gene or '(no gene)'} · {self.owner_username}"


class CropperImage(models.Model):
    session = models.ForeignKey(
        CropperSession, on_delete=models.CASCADE, related_name="images",
        null=True, blank=True,          # staged before its session is first saved
    )
    order = models.IntegerField(default=0)
    application_type = models.CharField(max_length=10, default="WB")   # WB/IP/ICC-IF/FC
    name = models.CharField(max_length=255, blank=True)
    # staged composite figure — separate prefix from published publication_images/
    image = models.FileField(upload_to="cropper_staging/%Y/%m/")
    nat_w = models.IntegerField(default=0)
    nat_h = models.IntegerField(default=0)
    # grid geometry: {bounds:{top,bottom}, hLines:[], vLines:[[..]], bandLeft:[], bandRight:[]}
    grid = models.JSONField(default=dict, blank=True)
    # cell mapping: {"b_c": {assigned, ab, manual, ocr:{...}}}
    mapping = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "pipeline"
        ordering = ["order", "id"]
        verbose_name = "Cropper image"

    def __str__(self):
        return f"{self.application_type} · {self.name or self.image.name}"
