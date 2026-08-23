"""
DepMap expression data service for feasibility lookups.

Replaces the previous API-based approach with local database lookups
from cached DepMap data (imported via import_depmap_expression command).

Scoping doc §5.1 / Vision doc Phase 1 / Nature Protocols Fig. 3-4:
  Check DepMap TPM values — threshold ≥2.5 log2(TPM+1) for adequate expression.
  Show expression across multiple cell line backgrounds, not just HAP1.
"""

import logging
from pipeline.models import DepMapExpression

logger = logging.getLogger(__name__)

DB = 'pipeline_db'

# YCharOS expression threshold from Nature Protocols paper
EXPRESSION_THRESHOLD = 2.5

# Clean up cell line names for display.
# RPE1 has 6 subclones in DepMap — keep only ss6 (highest expressing) as "RPE1".
# LNCaP clone FGC is the standard LNCaP — rename for clarity.
CELL_LINE_RENAME = {
    'RPE1-ss6': 'RPE1',
    'LNCaP clone FGC': 'LNCaP',
    'HCT 116': 'HCT116',
    'U-2 OS': 'U2OS',
    'U-251 MG': 'U251MG',
    'U-87 MG': 'U87MG',
    'MDA-MB-231': 'MDAMB231',
    'SH-SY5Y': 'SHSY5Y',
    'K-562': 'K562',
    'THP-1': 'THP1',
}

# Subclones to exclude — these clutter the display
CELL_LINE_EXCLUDE = {
    'RPE1-ss48',
    'RPE1-ss77',
    'RPE1-ss119',
    'RPE1-ss111',
    'RPE1-ss51',
}


def get_expression(gene_name: str) -> dict:
    """
    Look up cached DepMap expression data for a gene.

    Returns dict with keys:
        found (bool),
        hap1_tpm (float|None) — HAP1 value for headline display,
        above_threshold (bool) — whether HAP1 ≥ 2.5,
        cell_lines (list of {name, tpm, above_threshold}) — all cached lines,
        best_line (dict|None) — highest-expressing line,
        expressing_count (int) — lines above threshold,
        total_lines (int) — total lines with data,
        error (str|None)
    """
    result = {
        'found': False,
        'hap1_tpm': None,
        'above_threshold': False,
        'cell_lines': [],
        'best_line': None,
        'expressing_count': 0,
        'total_lines': 0,
        'error': None,
    }

    if not gene_name or not gene_name.strip():
        result['error'] = 'No gene name provided'
        return result

    gene_name = gene_name.strip().upper()

    try:
        # Get all cached expression data for this gene
        expressions = (
            DepMapExpression.objects.using(DB)
            .filter(gene_name__iexact=gene_name)
            .order_by('-tpm_log2')
        )

        if not expressions.exists():
            # Try without exact match — maybe gene name format differs
            expressions = (
                DepMapExpression.objects.using(DB)
                .filter(gene_name__icontains=gene_name)
                .order_by('-tpm_log2')
            )

        if not expressions.exists():
            result['error'] = (
                f"No DepMap expression data cached for '{gene_name}'. "
                f"The gene may not be in the dataset or the cache may need updating."
            )
            return result

        result['found'] = True

        # Build cell line list with cleanup
        cell_line_data = []
        seen_names = set()

        for expr in expressions:
            raw_name = expr.cell_line

            # Skip excluded subclones
            if raw_name in CELL_LINE_EXCLUDE:
                continue

            # Rename for clean display
            display_name = CELL_LINE_RENAME.get(raw_name, raw_name)

            # Skip if we already have this name (handles any remaining dupes)
            if display_name in seen_names:
                continue
            seen_names.add(display_name)

            above = expr.tpm_log2 >= EXPRESSION_THRESHOLD
            entry = {
                'name': display_name,
                'tpm': round(expr.tpm_log2, 2),
                'above_threshold': above,
                'depmap_id': expr.depmap_id,
            }
            cell_line_data.append(entry)

            if above:
                result['expressing_count'] += 1

        result['cell_lines'] = cell_line_data
        result['total_lines'] = len(cell_line_data)

        # HAP1 specifically (headline number for feasibility)
        hap1 = expressions.filter(cell_line__iexact='HAP1').first()
        if hap1:
            result['hap1_tpm'] = round(hap1.tpm_log2, 2)
            result['above_threshold'] = hap1.tpm_log2 >= EXPRESSION_THRESHOLD

        # Best expressing line (from cleaned list)
        if cell_line_data:
            result['best_line'] = cell_line_data[0]  # Already sorted desc

        return result

    except Exception as e:
        logger.error("DepMap lookup failed for '%s': %s", gene_name, e)
        result['error'] = f"DepMap lookup error: {str(e)}"
        return result


def get_expression_batch(gene_names: list) -> dict:
    """
    Batch lookup for multiple genes. Used by batch feasibility.

    Returns dict mapping gene_name -> expression dict (same format as get_expression).
    """
    results = {}
    for gene in gene_names:
        results[gene.strip().upper()] = get_expression(gene)
    return results


def get_expressing_lines(gene_name: str, min_tpm: float = EXPRESSION_THRESHOLD) -> list:
    """
    Get cell lines expressing a gene above threshold.
    Useful for cell line selection (Nature Protocols Fig. 3-4 workflow).

    Returns list of {name, tpm, depmap_id} sorted by expression descending.
    """
    gene_name = gene_name.strip().upper()

    expressions = (
        DepMapExpression.objects.using(DB)
        .filter(gene_name__iexact=gene_name, tpm_log2__gte=min_tpm)
        .order_by('-tpm_log2')
    )

    return [
        {
            'name': CELL_LINE_RENAME.get(expr.cell_line, expr.cell_line),
            'tpm': round(expr.tpm_log2, 2),
            'depmap_id': expr.depmap_id,
        }
        for expr in expressions
        if expr.cell_line not in CELL_LINE_EXCLUDE
    ]


def is_cache_populated() -> dict:
    """
    Check if the DepMap cache has data. Useful for UI status display.

    Returns dict with: populated (bool), gene_count, cell_line_count, release.
    """
    try:
        total = DepMapExpression.objects.using(DB).count()
        if total == 0:
            return {'populated': False, 'gene_count': 0,
                    'cell_line_count': 0, 'release': ''}

        genes = (DepMapExpression.objects.using(DB)
                 .values('gene_name').distinct().count())
        lines = (DepMapExpression.objects.using(DB)
                 .values('cell_line').distinct().count())
        release = (DepMapExpression.objects.using(DB)
                   .values_list('depmap_release', flat=True)
                   .first() or '')

        return {
            'populated': True,
            'gene_count': genes,
            'cell_line_count': lines,
            'release': release,
        }
    except Exception:
        return {'populated': False, 'gene_count': 0,
                'cell_line_count': 0, 'release': ''}