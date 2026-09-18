from functools import wraps
from django.shortcuts import redirect
from django.http import HttpResponseForbidden, JsonResponse


def is_pipeline_member(request):
    """Whether this request's user holds active pipeline access.

    The same two lookups ``pipeline_member_required`` makes, as a question a
    template can be answered with rather than a gate. It exists for the public
    ``/extension/`` page, which draws a team-only build link — the endpoint
    behind it is still gated by the decorator, so this only decides whether the
    control is *drawn*.

    **Anonymous returns False without touching a database.** This is called from
    a public page, so the common visitor is a crawler or a stranger and must not
    cost two queries; the same reasoning as ``templates/404.html`` never asking
    whether a visitor is a member.
    """
    if not request.user.is_authenticated:
        return False
    from pipeline.models import Member
    from django.contrib.auth.models import User
    try:
        pipe_user = User.objects.using('pipeline_db').get(
            username=request.user.username
        )
        Member.objects.using('pipeline_db').get(
            user_id=pipe_user.pk, is_active=True
        )
    except (User.DoesNotExist, Member.DoesNotExist):
        return False
    return True


def pipeline_member_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('/accounts/login/?next=/pipeline/start/')
        from pipeline.models import Member
        from django.contrib.auth.models import User
        try:
            pipe_user = User.objects.using('pipeline_db').get(
                username=request.user.username
            )
            Member.objects.using('pipeline_db').get(
                user_id=pipe_user.pk, is_active=True
            )
        except (User.DoesNotExist, Member.DoesNotExist):
            return HttpResponseForbidden(
                '<h1>Access Denied</h1>'
                '<p>You do not have access to the YCharOS Pipeline. '
                'Contact your site administrator.</p>'
            )
        return view_func(request, *args, **kwargs)
    return wrapper


def pipeline_superuser_required(view_func):
    """Everything ``pipeline_member_required`` asks, plus a Django superuser.

    For the surfaces that hand out access rather than record science: creating a
    login, changing somebody's site or role, and the ``is_staff`` flag, which
    opens raw table access to the whole site — the public pages and the academy
    included, not just the pipeline.

    Two deliberate details. The check is ``request.user.is_superuser`` on the
    **academy_db** account, because that is the row ``authenticate()`` returned
    and therefore the only one whose flags were actually verified this request;
    the pipeline_db copy is a mirror kept in step by ``services/members.py``, not
    an authority. And a refused **fetch** gets JSON rather than an HTML page,
    because the boards' `requestJson` reads a body: an HTML 403 arrives as
    "unexpected token <" and the grid says the rows could not be loaded without
    ever saying why.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        gated = pipeline_member_required(view_func)
        if not request.user.is_authenticated:
            return redirect('/accounts/login/?next=' + request.path)
        if not request.user.is_superuser:
            wants_json = (request.headers.get('X-Requested-With') == 'fetch'
                          or request.method == 'POST')
            if wants_json:
                return JsonResponse(
                    {'ok': False,
                     'error': 'This page manages who can use the pipeline, so it '
                              'is restricted to superusers. Ask one of them.'},
                    status=403)
            return HttpResponseForbidden(
                '<h1>Superusers only</h1>'
                '<p>This page manages who can use the pipeline — who has a login, '
                'what site they belong to, and what they may do. It is restricted '
                'to superusers. Ask one of them to make the change.</p>')
        return gated(request, *args, **kwargs)
    return wrapper
