"""Manufacturer emails — who at each company is sent their results, and resending.

Superusers only, like Impact and People & access: it emails named people at
partner companies, and the list is a picture of OGA's commercial contacts.
Every write is in ``core/manufacturer_contacts.py``; this turns a form into a
call.

**Each press redirects** so a reload cannot send the email twice, and the
receipt is carried in the session and drawn inside the organisation it is
about, which the redirect scrolls to — the global flash area is at the top of
a long page, and a receipt nobody sees reads as a press that did nothing.
"""
from django.conf import settings
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from core import manufacturer_contacts as MC
from pipeline.decorators import pipeline_superuser_required

__all__ = ['manufacturer_emails', 'manufacturer_emails_action']

_RECEIPT = 'manufacturer_emails_receipt'


@pipeline_superuser_required
@require_GET
def manufacturer_emails(request):
    receipt = request.session.pop(_RECEIPT, None)
    return render(request, 'pipeline/manufacturer_emails.html', {
        'organisations': MC.manufacturers(),
        'receipt': receipt,
        'oga_cc': ', '.join(getattr(settings, 'SUPPLIER_MAILING_CC', [])),
    })


@pipeline_superuser_required
@require_POST
def manufacturer_emails_action(request):
    post = request.POST
    action = post.get('action', '')
    org = post.get('consumer', '')
    try:
        if action == 'add':
            text = MC.add_contact(org, post.get('name'), post.get('email'), post.get('role'))
        elif action == 'role':
            text = MC.update_contact(org, post.get('contact'), role=post.get('role'))
        elif action == 'name':
            text = MC.update_contact(org, post.get('contact'), name=post.get('name', ''))
        elif action in ('stop', 'start'):
            text = MC.update_contact(org, post.get('contact'), active=(action == 'start'))
        elif action == 'remove':
            text = MC.remove_contact(org, post.get('contact'))
        elif action == 'domains':
            text = MC.set_domains(org, post.get('email_domains'))
        elif action == 'resend':
            text = MC.resend(org, post.getlist('contacts'),
                             post.get('consented', '').split(','))
        else:
            raise MC.Refused("That button is not one this page knows. Reload the page.")
        ok = True
    except MC.Refused as exc:
        text, ok = str(exc), False
    request.session[_RECEIPT] = {'consumer': str(org), 'text': text, 'ok': ok}
    return redirect(reverse('pipeline:manufacturer_emails') + f'#org-{org}')
