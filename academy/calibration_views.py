"""The western blot calibration study, at /academy/calibrate/.

Anyone with the link signs up with a name, an institutional email and their
western blot experience, reads the criteria (and may comment on them), then
grades the blots one at a time. The rater is remembered in the session, so
there is no account and no password; signing up again with the same email
resumes where they left off. Results are for superusers only.

Under ``/academy/``, which is never edge-cached (``cache_headers.
NEVER_CACHED_PREFIXES``): a cached form page loses its CSRF cookie.
"""
from __future__ import annotations

import csv

from django.contrib.auth.decorators import user_passes_test
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from . import calibration as C
from .models import CalibrationFeedback, CalibrationRater, CalibrationRating

SESSION_KEY = 'calibration_rater'


def _rater(request):
    pk = request.session.get(SESSION_KEY)
    return CalibrationRater.objects.filter(pk=pk).first() if pk else None


def _done_ids(rater):
    return set(rater.ratings.values_list('item_id', flat=True))


@require_http_methods(['GET', 'POST'])
def start(request):
    rater = _rater(request)
    errors, form = {}, {}
    if request.method == 'POST':
        form = {k: (request.POST.get(k) or '').strip() for k in (
            'name', 'email', 'organisation', 'role', 'wb_experience', 'wb_years')}
        form['ko_experience'] = request.POST.get('ko_experience') == 'yes'
        if not form['name']:
            errors['name'] = 'Enter your name.'
        refusal = C.email_refusal(form['email'])
        if refusal:
            errors['email'] = refusal
        if form['role'] not in dict(CalibrationRater.ROLE):
            errors['role'] = 'Choose the role closest to yours.'
        if form['wb_experience'] not in dict(CalibrationRater.EXPERIENCE):
            errors['wb_experience'] = 'Choose your western blot experience.'
        years = None
        if form['wb_years']:
            if form['wb_years'].isdigit() and int(form['wb_years']) <= 60:
                years = int(form['wb_years'])
            else:
                errors['wb_years'] = 'Enter a whole number of years, for example 3.'
        if request.POST.get('consent') != 'yes':
            errors['consent'] = 'Tick to agree, or the study cannot record your answers.'
        if not errors:
            email = form['email'].lower()
            rater, _ = CalibrationRater.objects.update_or_create(
                email=email, defaults={
                    'name': form['name'], 'domain': C.domain_of(email),
                    'organisation': form['organisation'], 'role': form['role'],
                    'wb_experience': form['wb_experience'], 'wb_years': years,
                    'ko_experience': form['ko_experience']})
            request.session[SESSION_KEY] = rater.pk
            return redirect('academy:calibrate_criteria')
    return render(request, 'academy/calibrate/start.html', {
        'rater': rater, 'errors': errors, 'form': form,
        'roles': CalibrationRater.ROLE,
        'experience': CalibrationRater.EXPERIENCE,
        'total': len(C.items()),
    })


@require_http_methods(['GET', 'POST'])
def criteria(request):
    rater = _rater(request)
    if rater is None:
        return redirect('academy:calibrate')
    if request.method == 'POST':
        _save_feedback(request, rater, 'before')
        return redirect('academy:calibrate_rate')
    return render(request, 'academy/calibrate/criteria.html', {
        'rater': rater, 'total': len(C.items()), 'done': len(_done_ids(rater)),
    })


def _save_feedback(request, rater, stage):
    clarity = request.POST.get('clarity') or ''
    comments = (request.POST.get('comments') or '').strip()[:5000]
    if clarity or comments:
        CalibrationFeedback.objects.create(
            rater=rater, stage=stage, comments=comments,
            clarity=int(clarity) if clarity in {'1', '2', '3', '4', '5'} else None)


@require_http_methods(['GET', 'POST'])
def rate(request):
    rater = _rater(request)
    if rater is None:
        return redirect('academy:calibrate')
    error = ''
    if request.method == 'POST':
        item_id = request.POST.get('item_id') or ''
        answer = request.POST.get('answer') or ''
        if C.item(item_id) is None:
            error = 'That blot is not part of the set. Rate the one shown below.'
        elif answer not in C.LABELS:
            error = 'Choose one of the four answers before saving.'
        else:
            seconds = request.POST.get('seconds') or ''
            CalibrationRating.objects.update_or_create(
                rater=rater, item_id=item_id, defaults={
                    'answer': answer,
                    'comment': (request.POST.get('comment') or '').strip()[:500],
                    'seconds': int(seconds) if seconds.isdigit() else None})
            return redirect('academy:calibrate_rate')
    done = _done_ids(rater)
    item_id = C.next_item(rater.pk, done)
    if item_id is None:
        return redirect('academy:calibrate_done')
    return render(request, 'academy/calibrate/rate.html', {
        'rater': rater, 'item_id': item_id,
        'image': f'academy/calibration/items/{item_id}.png',
        'number': len(done) + 1, 'total': len(C.items()),
        'percent': round(100 * len(done) / len(C.items())),
        'answers': list(C.LABELS.items()), 'error': error,
    })


@require_http_methods(['GET', 'POST'])
def done(request):
    rater = _rater(request)
    if rater is None:
        return redirect('academy:calibrate')
    saved = False
    if request.method == 'POST':
        _save_feedback(request, rater, 'after')
        saved = True
    count = len(_done_ids(rater))
    return render(request, 'academy/calibrate/done.html', {
        'rater': rater, 'count': count, 'total': len(C.items()),
        'remaining': len(C.items()) - count, 'saved': saved,
    })


def _superuser(user):
    return user.is_authenticated and user.is_superuser


@user_passes_test(_superuser, login_url='academy:login')
def results(request):
    ratings = list(CalibrationRating.objects.select_related('rater'))
    s = C.summary(ratings)
    raters = list(CalibrationRater.objects.order_by('-created_at'))
    exp_labels = dict(CalibrationRater.EXPERIENCE)
    by_exp = {}
    for r in raters:
        by_exp[r.wb_experience] = by_exp.get(r.wb_experience, 0) + 1
    return render(request, 'academy/calibrate/results.html', {
        's': s, 'raters': raters, 'n_ratings': len(ratings),
        'by_exp': [(exp_labels[k], by_exp.get(k, 0)) for k in exp_labels],
        'kappa_by_exp': [(exp_labels.get(k, k or 'unknown'), v)
                         for k, v in s['kappa_by_experience'].items()],
        'feedback': CalibrationFeedback.objects.select_related('rater')[:500],
        'labels': C.LABELS, 'results_order': C.RESULTS,
    })


@user_passes_test(_superuser, login_url='academy:login')
def results_csv(request):
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = 'attachment; filename="wb_calibration_ratings.csv"'
    w = csv.writer(resp)
    w.writerow(['rater_id', 'domain', 'role', 'wb_experience', 'wb_years',
                'ko_experience', 'item_id', 'kind', 'expected', 'answer',
                'seconds', 'comment', 'params', 'rated_at'])
    for r in CalibrationRating.objects.select_related('rater').order_by('created_at'):
        it = C.item(r.item_id) or {}
        w.writerow([r.rater_id, r.rater.domain, r.rater.role, r.rater.wb_experience,
                    r.rater.wb_years if r.rater.wb_years is not None else '',
                    'yes' if r.rater.ko_experience else 'no', r.item_id,
                    it.get('kind', ''), it.get('expected', ''), r.answer,
                    r.seconds if r.seconds is not None else '', r.comment,
                    it.get('params', ''), r.created_at.isoformat()])
    return resp
