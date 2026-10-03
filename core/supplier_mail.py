"""The email a manufacturer is sent: who it goes to, what it says, what is attached.

``core/supplier_digest.py`` builds the facts; this writes the message; the
command ``supplier_mailing`` decides when. Kept apart so the words can be read
and tested without a database, and so the command stays about cadence.

Three decisions.

**The key is never in the email.** It goes to several people and to OGA's own
copies, and it is forwarded. The email links to ``/data-access/key/``, which
sends the organisation's one shared key to whoever asks from its own domain.

**The competitor-view screenshot rides in the first few emails only**
(``SCREENSHOT_EMAILS``) as a reminder of what the portal is. By the fourth
email a reader either uses the portal or does not, and an image they have
already seen three times is just noise in their inbox.

**Every number in the body is the attachment's own**, read off the same
``Digest``, so the sentence and the spreadsheet cannot disagree.
"""
from __future__ import annotations

from email.mime.image import MIMEImage
from html import escape
from pathlib import Path

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from . import supplier_digest as SD

#: How many emails to one organisation carry the screenshot.
SCREENSHOT_EMAILS = 3

#: The screenshot of the portal's *Compare suppliers* tab.
SCREENSHOT = Path(__file__).resolve().parent / 'static' / 'core' / 'email' / 'portal_compare_suppliers.png'
SCREENSHOT_CID = 'oga-portal-compare'

PORTAL_URL = f'{SD.BASE_URL}/portal/'
KEY_URL = f'{SD.BASE_URL}/data-access/key/'


def recipients(consumer):
    """``(to, cc)`` — the contacts on file, then OGA's own copies on Cc."""
    contacts = list(consumer.contacts.filter(is_active=True))
    to = [c.email for c in contacts if c.role == c.TO]
    cc = [c.email for c in contacts if c.role == c.CC]
    for addr in getattr(settings, 'SUPPLIER_MAILING_CC', []):
        if addr.lower() not in {a.lower() for a in to + cc}:
            cc.append(addr)
    return to, cc, contacts


def subject(digest):
    name = digest.consumer.name
    if digest.since is None:
        return f'{name}: your antibody results from Only Good Antibodies'
    return f'{name}: new Only Good Antibodies results since {digest.since.sent_at:%d %B}'


def _greeting(contacts, any_role=False):
    first = [c.name.split()[0] for c in contacts
             if (any_role or c.role == c.TO) and c.name.strip()]
    if not first:
        return 'Hello,'
    if len(first) == 1:
        return f'Dear {first[0]},'
    return 'Dear ' + ', '.join(first[:-1]) + f' and {first[-1]},'


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def paragraphs(digest, review, with_screenshot):
    """The body as a list of ``(kind, text)`` — one writer for both HTML and plain text.

    ``kind`` is ``p`` (paragraph), ``li`` (bullet), ``button`` (text|url),
    ``img`` or ``small``.
    """
    name = digest.consumer.name
    out = []
    first_day, last_day, review_genes = review or (None, None, [])
    theirs = sorted(set(review_genes) & set(digest.genes))

    if digest.since is None:
        if review_genes:
            line = (f"All the genes from our latest review are now live on the "
                    f"site: {', '.join(review_genes)}.")
            if theirs:
                line += (f" {_plural(len(theirs), 'of them includes', 'of them include')} "
                         f"{name} antibodies: {', '.join(theirs)}.")
            out.append(('p', line))
        out.append(('p', (
            "Genes still in our pipeline appear in the portal's Pre-release tab "
            "as soon as their reports are completed, before the review. That "
            "gives you the chance to tell us about a wrong catalogue number, a "
            "withdrawn lot or an RRID before anything goes public.")))
        out.append(('p', (
            f"Attached is a summary of all our results for {name} so far: "
            f"{_plural(digest.antibody_count, 'antibody', 'antibodies')} with "
            f"published results across {_plural(len(digest.genes), 'gene')}.")))
    else:
        c = digest.changes
        bits = []
        if c.get('new'):
            bits.append(_plural(len(c['new']), 'new result'))
        if c.get('changed'):
            bits.append(_plural(len(c['changed']), 'changed result'))
        if c.get('new_pending'):
            bits.append(_plural(len(c['new_pending']), 'new figure') + ' awaiting review')
        out.append(('p', (
            f"New for {name} since our last email on "
            f"{digest.since.sent_at:%d %B}: {'; '.join(bits) or 'nothing'}. "
            "The attached spreadsheet has everything so far, and its "
            "\"What's new\" sheet lists what changed.")))
        new_genes = sorted({r['gene'] for r in digest.results
                            if f"{r['antibody_id']}:{r['application']}" in set(c.get('new', []))})
        if new_genes:
            out.append(('p', f"Genes with new results: {', '.join(new_genes)}."))

    counts = digest.counts()
    if digest.results:
        out.append(('p', 'By application:'))
    for app in SD.R.APPLICATIONS:
        row = counts[app]
        total = sum(row.values())
        if total:
            out.append(('li', (
                f"{app}: {row[SD.R.SUPPORTIVE]} supportive, "
                f"{row[SD.R.LIMITED_SUPPORT]} limited support, "
                f"{row[SD.R.NOT_SUPPORTIVE]} not supportive")))

    if digest.review:
        out.append(('p', (
            f"Please review: {_plural(len(digest.review), 'antibody', 'antibodies')} "
            "had no application come back supportive. They are listed on the "
            "\"Please review\" sheet, each with a link to its figures, so your "
            "product team can decide what to do. The portal's Not supportive "
            "tab shows the same list with the figures side by side.")))
    if digest.pending:
        one = len(digest.pending) == 1
        out.append(('p', (
            f"{_plural(len(digest.pending), 'figure')} of your antibodies "
            f"({', '.join(digest.pending_genes)}) "
            f"{'is' if one else 'are'} awaiting our review. "
            f"{'It is' if one else 'They are'} not published yet, and you can "
            f"see {'it' if one else 'them'} in the portal's Pre-release tab.")))

    out.append(('p', (
        f"Everything is in the data portal: your results, the figures to "
        f"download, embed cards for your product pages, and the per-gene "
        f"comparison with other suppliers. {name} has one portal key, shared by "
        f"everyone there. Anyone at {name} can have it emailed to them "
        f"straight away from the button below, using their work address.")))
    out.append(('button', f"Open the data portal|{PORTAL_URL}"))
    out.append(('button', f"Request your company's key|{KEY_URL}"))

    if with_screenshot:
        out.append(('p', (
            "As a reminder, this is the portal's Compare suppliers view: every "
            "supplier's antibodies against one gene, yours among them.")))
        out.append(('img', SCREENSHOT_CID))

    out.append(('p', (
        "We will send an update when new results are added, at most once a "
        "week. If someone else at your company should get these, or you "
        "would rather not, just reply to this email.")))
    out.append(('small', SD.R.SCOPE_NOTE))
    # Temporary notices (`recommendations.INTERIM_NOTES`) — a manufacturer's
    # results are where a western blot verdict reaches them.
    out.extend(('small', note) for note in SD.R.INTERIM_NOTES.values())
    return out


def build_message(digest, review, attachment, attachment_name, to, cc, contacts,
                  with_screenshot, subject_prefix='', greet_everyone=False):
    parts = paragraphs(digest, review, with_screenshot and SCREENSHOT.exists())
    greeting = _greeting(contacts, any_role=greet_everyone)
    sign = ['Best wishes,', 'The Only Good Antibodies team', SD.BASE_URL]

    text = [greeting, '']
    for kind, value in parts:
        if kind == 'li':
            text.append(f'  - {value}')
            continue
        if text and text[-1].startswith('  - '):
            text.append('')
        if kind == 'button':
            label, url = value.split('|', 1)
            text += [f'{label}: {url}', '']
        elif kind == 'img':
            continue
        else:
            text += [value, '']
    text += sign

    html = [f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            f'line-height:1.55;color:#1f2d3d;max-width:640px">',
            f'<p>{escape(greeting)}</p>']
    in_list = False
    for kind, value in parts:
        if kind == 'li' and not in_list:
            html.append('<ul>')
            in_list = True
        if kind != 'li' and in_list:
            html.append('</ul>')
            in_list = False
        if kind == 'p':
            html.append(f'<p>{escape(value)}</p>')
        elif kind == 'li':
            html.append(f'<li>{escape(value)}</li>')
        elif kind == 'small':
            html.append(f'<p style="font-size:12px;color:#5b6b80">{escape(value)}</p>')
        elif kind == 'button':
            label, url = value.split('|', 1)
            html.append(
                f'<p><a href="{escape(url)}" style="display:inline-block;'
                f'background:#10428a;color:#ffffff;text-decoration:none;'
                f'padding:10px 18px;border-radius:6px;font-weight:bold">'
                f'{escape(label)}</a></p>')
        elif kind == 'img':
            html.append(f'<p><img src="cid:{value}" alt="The data portal\'s Compare '
                        f'suppliers view" style="max-width:100%;border:1px solid '
                        f'#d6e1f2;border-radius:6px"></p>')
    if in_list:
        html.append('</ul>')
    html.append('<p>' + '<br>'.join(escape(s) for s in sign) + '</p></div>')

    msg = EmailMultiAlternatives(
        subject=subject_prefix + subject(digest),
        body='\n'.join(text),
        from_email=f'Only Good Antibodies <{settings.EMAIL_HOST_USER}>',
        to=to, cc=cc,
        reply_to=[settings.EMAIL_HOST_USER],
    )
    msg.attach_alternative(''.join(html), 'text/html')
    if with_screenshot and SCREENSHOT.exists():
        image = MIMEImage(SCREENSHOT.read_bytes(), 'png')
        image.add_header('Content-ID', f'<{SCREENSHOT_CID}>')
        image.add_header('Content-Disposition', 'inline',
                         filename='portal_compare_suppliers.png')
        msg.attach(image)
    msg.attach(attachment_name, attachment,
               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    return msg
