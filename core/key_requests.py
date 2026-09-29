"""Send a person their organisation's key, if their address says whose they are.

``/data-access/key/`` used to be a ``mailto:`` — a day's round trip for
something a domain already answers. Now the answer is automatic in the one case
it can be: an address on a domain an organisation has on file
(``APIConsumer.email_domains``) is **emailed that organisation's key**, scoped
to that organisation's products because the key already is.

Four rules, each a way this could hand the wrong key to the wrong person.

**One key per organisation, shared** (owner, 29 Sep 2026). A request never
creates a key. It sends the one the organisation has, so every person there
shares one supplier scope and one review cursor. An organisation with no key
on file is an *unmatched* request, and a person at OGA sets one up.

**The key goes to the mailbox, never the page.** Typing an address proves
nothing; receiving mail at it does. So the page says the key was sent and
never prints it, and says the same words whether or not the domain matched
(the inbox tells the person which happened), so the page cannot be used to
find out which domains hold a key.

**A domain matches exactly, or as a subdomain, and a personal provider never
matches.** ``abcam.com`` matches ``uk.abcam.com`` and not ``notabcam.com``.
``gmail.com`` is refused before any lookup, whatever an organisation's row
says.

**Two organisations claiming one domain is a question, not a coin toss.** This
is the cell-line rule applied to keys: nothing is sent, and the request goes
to OGA with both names, because the wrong key reveals one manufacturer's
unpublished figures to another.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.urls import reverse

from credentials.institutional import email_domain, is_free_provider

from .models import APIConsumer, KeyRequest

logger = logging.getLogger(__name__)

SITE = 'https://onlygoodantibodies.co.uk'


def consumers_for(email):
    """Active consumers whose ``email_domains`` cover this address."""
    domain = email_domain(email)
    if not domain:
        return []
    found = []
    for consumer in APIConsumer.objects.filter(is_active=True).exclude(email_domains=''):
        for owned in consumer.get_email_domains():
            if domain == owned or domain.endswith('.' + owned):
                found.append(consumer)
                break
    return found


def handle(email, name='', organisation=''):
    """Decide, send, record. Returns ``(outcome, consumer_or_None)``.

    Never raises on a mail failure. The request is recorded either way, and
    the outcome says what happened so the page does not claim a send that failed.
    """
    email = (email or '').strip()
    if is_free_provider(email):
        KeyRequest.objects.create(email=email, name=name, organisation=organisation,
                                  outcome=KeyRequest.REFUSED)
        return KeyRequest.REFUSED, None

    matches = consumers_for(email)
    if len(matches) == 1:
        consumer = matches[0]
        sent = _send_key(consumer, email, name)
        outcome = KeyRequest.SENT if sent else KeyRequest.FAILED
        KeyRequest.objects.create(email=email, name=name, organisation=organisation,
                                  consumer=consumer, outcome=outcome)
        if not sent:
            _tell_oga(email, name, organisation,
                      f"Matched {consumer.name}, but the key email failed to send.")
        return outcome, consumer

    note = ('No organisation on file for this domain.' if not matches else
            'More than one organisation claims this domain: '
            + ', '.join(c.name for c in matches) + '. Nothing was sent.')
    KeyRequest.objects.create(email=email, name=name, organisation=organisation,
                              outcome=KeyRequest.UNMATCHED)
    _tell_oga(email, name, organisation, note)
    return KeyRequest.UNMATCHED, None


def _send_key(consumer, email, name):
    greeting = f"Dear {name}," if name else "Hello,"
    body = (
        f"{greeting}\n\n"
        f"Here is the Only Good Antibodies data portal key for {consumer.name}:\n\n"
        f"    {consumer.api_key}\n\n"
        f"Open the portal at {SITE}{reverse('portal')} and paste the key in.\n\n"
        f"This key belongs to {consumer.name}, not to you personally. Everyone "
        f"at {consumer.name} uses the same one. It shows your own products' "
        "results and figures, the per-gene comparison with other suppliers, "
        "and your figures that are awaiting OGA's review. Please keep it "
        "inside the company.\n\n"
        "If you did not ask for this, you can ignore this email. The key was "
        "only sent to your address.\n\n"
        "Best wishes,\nThe Only Good Antibodies team\n" + SITE + "\n")
    try:
        EmailMessage(
            subject=f"Your Only Good Antibodies portal key ({consumer.name})",
            body=body,
            from_email=_from(),
            to=[email],
            reply_to=[settings.EMAIL_HOST_USER],
            connection=get_connection(timeout=10),
        ).send(fail_silently=False)
        return True
    except Exception:
        logger.exception('Key email to %s failed', email)
        return False


def _tell_oga(email, name, organisation, note):
    try:
        EmailMessage(
            subject=f"[Key request] {organisation or email}",
            body=(f"{note}\n\nFrom: {name or '-'} <{email}>\n"
                  f"Organisation (as typed): {organisation or '-'}\n\n"
                  "If they should have a key, add their domain to the "
                  "organisation's API consumer (Email domains) in Django admin, "
                  "and ask them to request it again. Or create a consumer if "
                  "the organisation has none."),
            from_email=_from(),
            to=[settings.EMAIL_HOST_USER],
            reply_to=[email],
            connection=get_connection(timeout=10),
        ).send(fail_silently=False)
    except Exception:
        logger.exception('Key request notification failed')


def _from():
    return f"Only Good Antibodies <{settings.EMAIL_HOST_USER}>"
