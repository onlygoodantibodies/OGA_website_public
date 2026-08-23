from django import template
from django.utils.safestring import mark_safe

register = template.Library()

@register.simple_tag
def sortable_header(field_name, display_name, current_sort):
    if current_sort == field_name:
        arrow = ' ▲'
        next_sort = f'-{field_name}'
    elif current_sort == f'-{field_name}':
        arrow = ' ▼'
        next_sort = field_name
    else:
        arrow = ''
        next_sort = field_name
    return mark_safe(
        f'<a href="?sort={next_sort}" class="hover:text-ycharos-600">'
        f'{display_name}{arrow}</a>'
    )

STATUS_COLORS = {
    'published': 'bg-green-100 text-green-800',
    'report_ready': 'bg-blue-100 text-blue-800',
    'in_progress': 'bg-yellow-100 text-yellow-800',
    'wb_complete': 'bg-yellow-100 text-yellow-800',
    'ip_complete': 'bg-yellow-100 text-yellow-800',
    'if_complete': 'bg-yellow-100 text-yellow-800',
    'fc_complete': 'bg-yellow-100 text-yellow-800',
    'cancelled': 'bg-red-100 text-red-800',
    'on_hold': 'bg-gray-100 text-gray-800',
    'not_started': 'bg-gray-100 text-gray-600',
}

STATUS_LABELS = {
    'published': 'Published',
    'report_ready': 'Report Ready',
    'in_progress': 'In Progress',
    'wb_complete': 'WB Complete',
    'ip_complete': 'IP Complete',
    'if_complete': 'IF Complete',
    'fc_complete': 'FC Complete',
    'cancelled': 'Cancelled',
    'on_hold': 'On Hold',
    'not_started': 'Not Started',
}

@register.simple_tag
def status_badge(status, display=None):
    if display is None:
        display = STATUS_LABELS.get(status, status)
    css = STATUS_COLORS.get(status, 'bg-gray-100 text-gray-800')
    return mark_safe(
        f'<span class="inline-flex px-2 py-1 text-xs font-semibold rounded-full {css}">'
        f'{display}</span>'
    )

@register.simple_tag
def procedure_indicator(count):
    if count and count > 0:
        return mark_safe('<span class="inline-flex w-5 h-5 items-center justify-center rounded-full bg-green-100 text-green-600 text-xs">✓</span>')
    return mark_safe('<span class="inline-flex w-5 h-5 items-center justify-center rounded-full bg-gray-100 text-gray-400 text-xs">—</span>')

@register.simple_tag(takes_context=True)
def query_string(context, **kwargs):
    request = context['request']
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value is None or value == '':
            params.pop(key, None)
        else:
            params[key] = value
    qs = params.urlencode()
    return f'?{qs}' if qs else ''

@register.simple_tag
def supplier_app_icons(antibody):
    apps = []
    if antibody.supplier_validated_wb:
        apps.append('WB')
    if antibody.supplier_validated_ip:
        apps.append('IP')
    if antibody.supplier_validated_if:
        apps.append('IF')
    if antibody.supplier_validated_fc:
        apps.append('FC')
    if antibody.supplier_validated_ihc:
        apps.append('IHC')
    if antibody.supplier_validated_elisa:
        apps.append('ELISA')
    if not apps:
        return mark_safe('<span class="text-gray-400">—</span>')
    badges = ' '.join(
        f'<span class="inline-flex px-1.5 py-0.5 text-xs rounded bg-gray-100 text-gray-600">{a}</span>'
        for a in apps
    )
    return mark_safe(badges)

@register.filter
def dict_get(d, key):
    """Access a dictionary value by key in a template."""
    if isinstance(d, dict):
        return d.get(key, '')
    return ''

@register.filter
def get_attr(obj, attr_name):
    """Access a model attribute by name in a template."""
    try:
        return getattr(obj, attr_name, '')
    except Exception:
        return ''

@register.filter
def ip_volume(concentration):
    """
    Calculate IP antibody volume: 2 µg ÷ concentration (µg/mL) × 1000 = µL.
    Concentration is stored in µg/mL. Formula: volume_µL = (2 / conc_µg_per_mL) * 1000
    Returns formatted string like '4.0' or '—' if not calculable.
    """
    try:
        conc = float(concentration)
        if conc <= 0:
            return '—'
        vol = (2.0 / conc) * 1000  # result in µL
        if vol >= 10:
            return f'{vol:.0f}'
        return f'{vol:.1f}'
    except (TypeError, ValueError, ZeroDivisionError):
        return '—'


@register.filter
def doi_link(value):
    """The href for a stored DOI, or '' when the value cannot be one.

    `Report.zenodo_doi` is a URLField that validates nothing on save, so rows on
    file can hold text no browser can follow — Carl's workbook accepts a bare
    `10.…` and stores it unchanged, and the board's cell accepted anything at all
    until services/doi.py was put in front of it. Rendering that as an href is
    the harmful half: it resolves against this site, so the reader gets a Not
    Found page that reads as the record having gone missing.
    """
    from pipeline.services import doi
    return doi.link(value)


@register.filter
def doi_text(value):
    """What a DOI reads as on screen — `10.5281/zenodo.1`, not the doi.org URL."""
    from pipeline.services import doi
    return doi.display(value)
