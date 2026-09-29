"""Email each manufacturer their OGA results. Dry-run by default.

    python manage.py supplier_mailing                          # who would get what
    python manage.py supplier_mailing --write-dir out/         # + the files, to read
    python manage.py supplier_mailing --preview-to me@x.ac.uk  # every email, to me only
    python manage.py supplier_mailing --send                   # send them for real
    python manage.py supplier_mailing --send --updates-only    # the weekly cron

**The first email to an organisation is a one-off summary; after that, an
update only when something is new, and never within a week of the last one**
(owner, 29 Sep 2026). Both rules are read off ``SupplierMailing``, which is
written only after a send succeeds. So a failed send is retried on the next
run, and a week with nothing new sends nothing.

``--updates-only`` is what a cron runs. It never sends a *first* email,
because the first one to a manufacturer is the owner's decision to make, by
hand, after reading a preview. A cron that could start mailing a supplier
the moment their contact row was added is not one anybody signed off.

Recipients: the organisation's active ``SupplierContact`` rows, with
``settings.SUPPLIER_MAILING_CC`` (the owner and Carl) on Cc of every email.
An organisation with no *To* contact, or nothing to report, is skipped by
name.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core.mail import get_connection
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core import supplier_digest as SD
from core import supplier_mail as SM
from core.models import APIConsumer, SupplierMailing

#: Once a week at most. Four hours short of seven days, because a daily cron's
#: start time drifts by minutes: exactly seven days would sometimes land an
#: update on day eight.
MIN_INTERVAL = timedelta(days=7) - timedelta(hours=4)


def configuration_gaps():
    """What this service is missing to send a true email, each in a sentence.

    The cron runs on a service of its own (``academy-db-backup``), whose
    environment is typed in by hand. Every gap here fails *quietly* otherwise:
    no ``ACADEMY_DATABASE_URL`` finds no manufacturers and reports "0 sent", no
    ``PIPELINE_DATABASE_URL`` reports every company as having nothing, and no
    ``USE_R2`` puts figure links in the spreadsheet that go nowhere.
    """
    gaps = []
    for alias, var in (('academy_db', 'ACADEMY_DATABASE_URL'),
                       ('pipeline_db', 'PIPELINE_DATABASE_URL')):
        if 'sqlite' in settings.DATABASES[alias]['ENGINE']:
            gaps.append(f'{var} is not set on this service, so it would read an '
                        'empty local database instead of the live one.')
    if not settings.USE_R2:
        gaps.append('USE_R2 is not True on this service, so the figure links in '
                    'the spreadsheet would point nowhere.')
    elif not getattr(settings, 'R2_PUBLIC_DOMAIN', None):
        gaps.append('R2_PUBLIC_DOMAIN is not set on this service, so the figure '
                    'links in the spreadsheet would point at private storage '
                    'rather than media.onlygoodantibodies.co.uk.')
    if not settings.EMAIL_HOST_PASSWORD:
        gaps.append('EMAIL_HOST_PASSWORD is not set, so no email can be sent.')
    return gaps


def decide(digest, previous, now, updates_only):
    """``(kind or None, reason)``: what this organisation gets today, and why."""
    if digest.is_empty():
        return None, 'nothing to report: no published results and nothing awaiting review'
    if previous is None:
        if updates_only:
            return None, 'has had no first email yet (send that by hand, without --updates-only)'
        return SupplierMailing.LAUNCH, 'first summary'
    if now - previous.sent_at < MIN_INTERVAL:
        nxt = timezone.localtime(previous.sent_at + MIN_INTERVAL)
        return None, f'emailed {previous.sent_at:%d %b}; next update possible {nxt:%d %b}'
    if not digest.has_news():
        return None, f'nothing new since {previous.sent_at:%d %b}'
    return SupplierMailing.UPDATE, 'new data since the last email'


class Command(BaseCommand):
    help = "Email each manufacturer their results (dry run unless --send or --preview-to)."

    def add_arguments(self, parser):
        parser.add_argument('--send', action='store_true', help='Send for real and record it.')
        parser.add_argument('--preview-to', metavar='ADDR',
                            help='Send every email to this address only. Records nothing.')
        parser.add_argument('--updates-only', action='store_true',
                            help='Never send a first email (for the weekly cron).')
        parser.add_argument('--only', action='append', default=[], metavar='NAME',
                            help='Only this organisation (repeatable).')
        parser.add_argument('--write-dir', metavar='DIR',
                            help='Also write each workbook and email body here.')
        parser.add_argument('--review-since', metavar='YYYY-MM-DD',
                            help='First day of the latest review (default: worked out '
                                 'from the release dates).')

    def handle(self, *args, **opts):
        if opts['send'] and opts['preview_to']:
            raise CommandError('Choose --send or --preview-to, not both.')
        sending = opts['send'] or opts['preview_to']
        gaps = configuration_gaps() if sending else []
        if gaps:
            raise CommandError('Nothing sent. ' + ' '.join(gaps)
                               + ' Copy the value from the OGA_website service\'s '
                               'Environment page.')

        since = date.fromisoformat(opts['review_since']) if opts['review_since'] else None
        review = SD.latest_review(since)
        if review[2]:
            self.stdout.write(f"Latest review: {review[0]:%d %b}–{review[1]:%d %b %Y}, "
                              f"{len(review[2])} genes live: {', '.join(review[2])}")
        else:
            self.stdout.write('Latest review: no released genes found.')

        consumers = (APIConsumer.objects.filter(consumer_type='manufacturer', is_active=True,
                                                contacts__is_active=True)
                     .distinct().order_by('name'))
        if opts['only']:
            wanted = {n.lower() for n in opts['only']}
            consumers = [c for c in consumers if c.name.lower() in wanted]
            missing = wanted - {c.name.lower() for c in consumers}
            if missing:
                raise CommandError(f"No active manufacturer with contacts called: {', '.join(sorted(missing))}")

        out_dir = Path(opts['write_dir']) if opts['write_dir'] else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
        now = timezone.now()
        connection = get_connection(timeout=30) if sending else None
        sent = skipped = failed = 0

        for consumer in consumers:
            previous = consumer.mailings.order_by('-sent_at').first()
            digest = SD.build(consumer, previous)
            kind, reason = decide(digest, previous, now, opts['updates_only'])
            to, cc, contacts = SM.recipients(consumer)
            head = (f"{consumer.name}: {digest.antibody_count} antibodies, "
                    f"{len(digest.review)} to review, {len(digest.pending)} awaiting review")
            if kind and not to:
                kind, reason = None, 'no active "to" contact on file'
            if not kind:
                skipped += 1
                self.stdout.write(f"  skip  {head} — {reason}")
                continue

            with_shot = consumer.mailings.count() < SM.SCREENSHOT_EMAILS
            blob = SD.workbook(digest, review)
            name = SD.attachment_name(digest)
            if opts['preview_to']:
                msg = SM.build_message(
                    digest, review, blob, name, [opts['preview_to']], [], contacts,
                    with_shot, subject_prefix=f"[PREVIEW to {', '.join(to)}; cc {', '.join(cc)}] ")
            else:
                msg = SM.build_message(digest, review, blob, name, to, cc, contacts, with_shot)
            line = (f"  {kind:6} {head} — {reason}; to {', '.join(to)}; cc {', '.join(cc)}"
                    + ('; with screenshot' if with_shot and SM.SCREENSHOT.exists() else ''))
            if with_shot and not SM.SCREENSHOT.exists():
                line += f'; SCREENSHOT MISSING ({SM.SCREENSHOT.name}), sent without it'

            if out_dir:
                (out_dir / name).write_bytes(blob)
                (out_dir / (name[:-5] + '.txt')).write_text(
                    f"Subject: {msg.subject}\nTo: {', '.join(msg.to)}\nCc: {', '.join(msg.cc)}\n\n{msg.body}")
                (out_dir / (name[:-5] + '.html')).write_text(msg.alternatives[0][0])

            if not sending:
                self.stdout.write('  would ' + line.strip())
                continue
            msg.connection = connection
            try:
                msg.send(fail_silently=False)
            except Exception as exc:  # the next run retries: nothing was recorded
                failed += 1
                self.stdout.write(self.style.ERROR(f"  FAILED {consumer.name}: {type(exc).__name__}"))
                continue
            sent += 1
            self.stdout.write('  sent ' + line.strip())
            if opts['send']:
                SupplierMailing.objects.create(
                    consumer=consumer, kind=kind, sent_at=timezone.now(),
                    subject=msg.subject, recipients=f"to: {', '.join(to)}; cc: {', '.join(cc)}",
                    attachment=name, antibodies=digest.antibody_count,
                    not_supportive=len(digest.review), changes=digest.change_count,
                    fingerprint=digest.fingerprint)

        mode = ('sent' if opts['send'] else f"previewed to {opts['preview_to']}"
                if opts['preview_to'] else 'dry run, nothing sent')
        self.stdout.write(f"{sent} {mode}; {skipped} skipped; {failed} failed.")
        if failed:
            raise CommandError(f'{failed} email(s) failed to send; they will be retried next run.')
