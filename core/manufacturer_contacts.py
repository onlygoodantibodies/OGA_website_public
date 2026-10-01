"""Who at each manufacturer is emailed their results, managed from the pipeline.

The owner forwards two kinds of reply to the results email and both were
jobs for Django admin or a CSV and a command: *please add my colleague* and
*I never got it, can you resend?* (30 Sep 2026 — Cell Signaling asked for a
colleague to be added; a MilliporeSigma contact was on the 29 Sep send and had
nothing in their inbox). This module is every write the pipeline's
*Manufacturer emails* page makes; ``pipeline/views/manufacturer_emails.py``
only turns a form into a call.

Four decisions.

**A contact is stopped, not deleted, unless it was a typo.** ``is_active``
is how the weekly cron is told to leave somebody alone, and the row keeps
the name. *Remove* exists for an address that was mistyped and never
meant anybody.

**A resend is the full summary, to the people ticked and nobody else** at
the company, with OGA's own copies on Cc as on every email. It is recorded
as a ``SupplierMailing`` of kind ``resend`` so the record of who was sent what
is whole, and it is **never** the cursor (``SupplierMailing.cursor_for``):
the organisation's weekly update is still measured from its last real
email, so a resend neither delays the next update nor swallows what it
would have reported.

**An address outside the organisation's key domains is allowed and said.**
Companies use more than one domain (ABclonal is ``.us`` and ``.com``), so a
contact on another is not wrong; but the key-request page only answers the
listed domains, so that person could not ask for the key and the page says
so beside the row.

**Every refusal names the field and what would work**, and a send that
cannot happen on this service says which setting is missing rather than
failing later in the SMTP handshake.
"""
from __future__ import annotations


from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import get_connection
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from credentials.institutional import email_domain

from . import supplier_digest as SD
from . import supplier_mail as SM
from .models import APIConsumer, SupplierContact, SupplierMailing

#: The weekly limit and the send checks are the command's own, so the page's
#: "next update possible" and "cannot send" are the command's answers.
from .management.commands.supplier_mailing import MIN_INTERVAL, configuration_gaps

DB = 'academy_db'


class Refused(Exception):
    """A sentence for the page. Nothing was written or sent."""


def manufacturers():
    """Every organisation the results email can go to, with what the page draws."""
    rows = []
    consumers = (APIConsumer.objects.filter(consumer_type='manufacturer', is_internal=False)
                 .order_by('-is_active', 'name'))
    for consumer in consumers:
        contacts = list(consumer.contacts.order_by('-is_active', 'role', 'email'))
        domains = consumer.get_email_domains()
        cursor = SupplierMailing.cursor_for(consumer)
        mailings = list(consumer.mailings.order_by('-sent_at')[:5])
        active_to = [c for c in contacts if c.is_active and c.role == c.TO]
        for c in contacts:
            c.outside_domains = bool(domains) and not _on_domains(c.email, domains)
        notes = []
        if not consumer.is_active:
            notes.append("Their key is switched off, so the weekly update skips them.")
        if consumer.is_active and not active_to:
            notes.append('Nobody is on the "To" line, so the weekly update skips them. '
                         'Add a contact, or switch an existing one to "To".')
        if cursor is None:
            notes.append("They have not had their first summary yet. That one is "
                         "sent by hand (the weekly update never starts a company).")
        rows.append({
            'consumer': consumer,
            'contacts': contacts,
            'domains': domains,
            'cursor': cursor,
            'next_update': (timezone.localtime(cursor.sent_at + MIN_INTERVAL)
                            if cursor else None),
            'mailings': mailings,
            'notes': notes,
            'active_count': sum(1 for c in contacts if c.is_active),
        })
    return rows


def _on_domains(email, domains):
    domain = email_domain(email)
    return any(domain == d or domain.endswith('.' + d) for d in domains)


def _consumer(pk):
    try:
        return APIConsumer.objects.get(pk=pk, consumer_type='manufacturer')
    except (APIConsumer.DoesNotExist, ValueError, TypeError):
        raise Refused("That organisation is not on file any more. Reload the page.")


def _contact(consumer, pk):
    try:
        return consumer.contacts.get(pk=pk)
    except (SupplierContact.DoesNotExist, ValueError, TypeError):
        raise Refused(f"That contact is no longer on {consumer.name}'s list. Reload the page.")


def _role(value):
    value = (value or '').strip().lower()
    if value not in (SupplierContact.TO, SupplierContact.CC):
        raise Refused('Choose "To" or "Cc" for the contact.')
    return value


def add_contact(consumer_pk, name, email, role):
    """Add somebody to an organisation's list. Returns the receipt sentence."""
    consumer = _consumer(consumer_pk)
    email = (email or '').strip()
    name = (name or '').strip()
    role = _role(role)
    try:
        validate_email(email)
    except ValidationError:
        raise Refused(f'"{email or "(blank)"}" is not an email address. '
                      'Type it in full, for example firstname.lastname@cellsignal.com.')
    existing = consumer.contacts.filter(email__iexact=email).first()
    if existing:
        state = 'and is being emailed' if existing.is_active else 'but is stopped'
        raise Refused(f"{existing.email} is already on {consumer.name}'s list {state}. "
                      + ('' if existing.is_active else 'Press "Start emailing" on that row.'))
    try:
        with transaction.atomic(using=DB):
            contact = SupplierContact.objects.create(
                consumer=consumer, name=name, email=email, role=role)
    except IntegrityError:
        raise Refused(f"{email} is already on {consumer.name}'s list.")
    line = (f"Added {contact.name or contact.email} to {consumer.name} "
            f"on the {'To' if role == SupplierContact.TO else 'Cc'} line. "
            "They will be on every email from now on, starting with the next weekly update.")
    domains = consumer.get_email_domains()
    if domains and not _on_domains(email, domains):
        line += (f" Their address is not on {consumer.name}'s key domains "
                 f"({', '.join(domains)}), so they cannot request the portal key "
                 "themselves until that domain is added below.")
    return line + " Nothing has been sent to them yet: use Resend to send them the summary now."


def update_contact(consumer_pk, contact_pk, *, name=None, role=None, active=None):
    consumer = _consumer(consumer_pk)
    contact = _contact(consumer, contact_pk)
    changed = []
    if name is not None and name.strip() != contact.name:
        contact.name = name.strip()
        changed.append('name')
    if role is not None:
        role = _role(role)
        if role != contact.role:
            contact.role = role
            changed.append('role')
    if active is not None and bool(active) != contact.is_active:
        contact.is_active = bool(active)
        changed.append('is_active')
    if not changed:
        return f"Nothing changed for {contact.email}."
    contact.save(update_fields=changed)
    who = contact.name or contact.email
    if 'is_active' in changed:
        return (f"{who} will be emailed again from the next update."
                if contact.is_active else
                f"Stopped emailing {who}. The row is kept; press Start emailing to undo.")
    if 'role' in changed:
        return f"{who} is now on the {'To' if contact.role == contact.TO else 'Cc'} line."
    return f"Saved {who}'s name."


def remove_contact(consumer_pk, contact_pk):
    consumer = _consumer(consumer_pk)
    contact = _contact(consumer, contact_pk)
    email = contact.email
    contact.delete()
    return (f"Removed {email} from {consumer.name}. Emails already sent to them "
            "are still listed under Sent.")


def set_domains(consumer_pk, text):
    consumer = _consumer(consumer_pk)
    cleaned = ','.join(d.strip().lower().lstrip('@')
                       for d in (text or '').replace(';', ',').split(',')
                       if d.strip().lstrip('@'))
    bad = [d for d in cleaned.split(',') if d and ('.' not in d or '@' in d or ' ' in d)]
    if bad:
        raise Refused(f"{', '.join(bad)} is not a domain. Type just the part after the @, "
                      "for example cellsignal.com, separated by commas.")
    before = consumer.get_email_domains()
    consumer.email_domains = cleaned
    try:
        consumer.clean()
    except ValidationError as exc:
        raise Refused(' '.join(exc.message_dict.get('email_domains', exc.messages)))
    consumer.save(update_fields=['email_domains'])
    after = consumer.get_email_domains()
    if before == after:
        return f"{consumer.name}'s key domains are unchanged."
    if not after:
        return (f"{consumer.name} now has no key domains, so nobody there can "
                "request the portal key from the website.")
    return (f"Anyone at {', '.join(after)} can now have {consumer.name}'s portal key "
            "emailed to them from the website.")


def resend(consumer_pk, contact_pks, consented):
    """Send the full summary to the ticked contacts now. Returns the receipt.

    ``consented`` is the recipient list the button showed; a different list
    is refused, so what was pressed is what is sent.
    """
    consumer = _consumer(consumer_pk)
    chosen = list(consumer.contacts.filter(pk__in=[p for p in contact_pks if str(p).isdigit()])
                  .order_by('role', 'email'))
    if not chosen:
        raise Refused("Tick at least one person to send the summary to.")
    stopped = [c.email for c in chosen if not c.is_active]
    if stopped:
        raise Refused(f"{', '.join(stopped)} is stopped. Press Start emailing on "
                      "that row first, or untick them.")
    to = [c.email for c in chosen]
    if sorted(a.lower() for a in to) != sorted(a.strip().lower() for a in consented if a.strip()):
        raise Refused("The people ticked changed after the button was drawn. "
                      "Nothing was sent; check the ticks and press again.")
    gaps = configuration_gaps()
    if gaps:
        raise Refused("Nothing sent: this server cannot send the email yet. " + ' '.join(gaps))

    digest = SD.build(consumer, None)
    if digest.is_empty():
        raise Refused(f"Nothing sent: {consumer.name} has no published results and "
                      "nothing awaiting review, so the summary would be empty.")
    review = SD.latest_review(None)
    blob = SD.workbook(digest, review)
    name = SD.attachment_name(digest)
    cc = [a for a in getattr(settings, 'SUPPLIER_MAILING_CC', [])
          if a.lower() not in {t.lower() for t in to}]
    msg = SM.build_message(digest, review, blob, name, to, cc, chosen,
                           with_screenshot=True, greet_everyone=True)
    msg.connection = get_connection(timeout=30)
    try:
        msg.send(fail_silently=False)
    except Exception as exc:  # the page says it failed; nothing is recorded
        import logging
        logging.getLogger(__name__).exception('Resend to %s failed', consumer.name)
        raise Refused(f"The email could not be sent ({type(exc).__name__}); nothing "
                      "was recorded. Try again in a few minutes.")
    SupplierMailing.objects.create(
        consumer=consumer, kind=SupplierMailing.RESEND, sent_at=timezone.now(),
        subject=msg.subject, recipients=f"to: {', '.join(to)}; cc: {', '.join(cc)}",
        attachment=name, antibodies=digest.antibody_count,
        not_supportive=len(digest.review), changes=0, fingerprint=digest.fingerprint)
    return (f"Sent {consumer.name}'s full summary ({digest.antibody_count} antibodies, "
            f"spreadsheet attached) to {', '.join(to)}, with {', '.join(cc) or 'nobody'} "
            "on Cc. Their weekly updates carry on from the last regular email. "
            "If it does not arrive, their company's filter may be holding it: ask them to "
            f"check quarantine and allow {settings.EMAIL_HOST_USER}.")
