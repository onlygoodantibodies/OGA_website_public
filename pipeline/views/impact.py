"""The impact page — what the site has recorded people doing.

The numbers were all in Django admin, one model at a time across four apps, so
they existed and could not be quoted. `services/impact.py` does the arithmetic;
this hands it to a template.

**Superuser-only**, and that is a decision rather than caution by default. Two
of the five sections are commercially sensitive in the way `/gene-detail/` is —
which organisations hold API keys and how hard they are pulling is a picture of
who OGA's partners are and how engaged each one is — and the other three
aggregate people who are not pipeline members at all: academy learners,
workshop attendees, scientists who built a validation plan. A bench member
needs none of it to do bench work. If the team ever does need it for grant
writing, `pipeline_member_required` is the one-word change, and the page draws
no personal data either way.
"""
from django.http import Http404
from django.shortcuts import render

from pipeline.decorators import pipeline_superuser_required
from pipeline.services import consumer_activity, impact


@pipeline_superuser_required
def impact_dashboard(request):
    return render(request, 'pipeline/impact.html', impact.everything())


@pipeline_superuser_required
def api_consumer(request, pk):
    """One organisation's side of the API, in sentences.

    Reached by clicking a name in either of the impact page's two organisation
    tables, and superuser-only for the same reason that page is: how hard a
    named partner is pulling, and what they have looked at, is a picture of
    OGA's commercial relationships rather than anything a bench member needs.

    `get_object_or_404` is not used, because `APIConsumer` lives in `academy_db`
    and the shortcut's model lookup is the plain manager either way — this spells
    the 404 out so the reason a pk misses is legible next to the router note.
    """
    from core.models import APIConsumer

    try:
        consumer = APIConsumer.objects.get(pk=pk)
    except APIConsumer.DoesNotExist:
        raise Http404('No API consumer with that id.')
    return render(request, 'pipeline/api_consumer.html',
                  consumer_activity.for_consumer(consumer))
