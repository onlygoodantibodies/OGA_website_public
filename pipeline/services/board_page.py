"""One page of a board, and the shape every rows endpoint answers in.

Every board built **every matching row** and handed the browser the lot. The
antibodies board is 3,225 rows of fifteen cells, and the owner's review got
*"page unresponsive"* on it — a browser dialog, on the app's largest and most
used table. The cost is on both sides of the wire: `row_for` runs per row here,
and `board.js` then builds one string of every `<tr>` and paints it.

So a rows endpoint returns a slice and says which:

    {"ok": True, "rows": [...], "count": 3225, "page": 1, "pages": 65,
     "per_page": 50}

Three things this holds, and each of them is a way it could go quietly wrong:

* **`count` is the whole filtered set, not the page.** It is what the board
  prints beside the filters — "3,225 antibodies" — and a count that silently
  became "50" would read as the filters having matched fifty things.
* **The slice happens before `row_for`.** Paginating the built rows would still
  build all of them, which is most of the cost and all of the queries.
* **An export is not paginated.** Downloads read the filtered queryset whole,
  which is why `page` is not a filter: see the note in `board.js::create`. A
  page number that leaked into `formQuery` would scope a download to fifty rows
  and say nothing about it.
"""
from __future__ import annotations

DEFAULT_PER_PAGE = 50

# Enough to let somebody who wants the whole gene on one screen have it, low
# enough that the answer is still a page rather than the dataset.
MAX_PER_PAGE = 500


def read_params(request) -> tuple[int, int]:
    """`(page, per_page)` from a request, both sane whatever was in the URL.

    A typed or stale `?page=` is clamped rather than refused: the worst outcome
    of nonsense here is an empty grid over filters that match plenty, which
    reads as data loss.
    """
    def _int(name, default):
        try:
            return int(request.GET.get(name) or default)
        except (TypeError, ValueError):
            return default

    per_page = max(1, min(MAX_PER_PAGE, _int("per_page", DEFAULT_PER_PAGE)))
    return max(1, _int("page", 1)), per_page


def locate_param(request):
    """`?locate=<id>` — the row the caller wants to land on, or None."""
    return (request.GET.get("locate") or "").strip() or None


def page_of(qs, pk, per_page: int = DEFAULT_PER_PAGE):
    """Which page a given row is on, or None if it is not in this queryset.

    **Pagination breaks every link that points at a row.** The sessions board is
    opened by id from two places — `?open=<id>`, which is where the retired
    `pipeline:session_detail` redirects a bookmark, and "Record its results
    here" after a session is created — and both looked the row up in the drawn
    grid. Drawing fifty rows instead of all of them makes that fail for anything
    further down, and the fallback message said *"That session is outside the
    current filters — clear them to see it."*: false, because the row matches
    the filters perfectly, and actively unhelpful, because clearing the filters
    makes the list longer and pushes the row further away.

    So the *server* answers where the row is, and the board goes there. Asked
    only when a caller passes an id, and one query returning integers — the
    ordering is the queryset's own, so this cannot disagree with the page it is
    paging.
    """
    if not pk:
        return None
    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return None
    ids = list(qs.values_list("pk", flat=True))
    if pk not in ids:
        return None
    return ids.index(pk) // per_page + 1


def slice_rows(qs, row_for, *, page: int = 1, per_page: int = DEFAULT_PER_PAGE,
               locate=None) -> dict:
    """One page of `qs`, built through `row_for`, plus what page it is.

    `page` past the end comes back as the last page rather than empty — a board
    whose filters have just narrowed is the common way to get there, and an
    empty grid is the one answer that is never true.

    `locate` is a row id to land on: it wins over `page`, and `located` says
    whether it was found, so the caller can tell "it is on page 3" from "it is
    genuinely not in this filtered set" — two different messages.
    """
    count = qs.count()
    pages = max(1, -(-count // per_page))     # ceil
    located = None
    if locate:
        located = page_of(qs, locate, per_page)
        if located:
            page = located
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    return {
        "rows": [row_for(obj) for obj in qs[start:start + per_page]],
        "count": count,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        # None when nothing was asked for; False when the id is not in this
        # filtered set at all.
        "located": (bool(located) if locate else None),
    }
