"""What a public page may do to the site while it waits for UniProt.

On 2 Sep 2026 the selection tool's gene box asked UniProt once per pause in
somebody's typing — `T`, `TP`, `TP53`, `P5`, `P51`, `ga`, `n` — with no cache
and a 10-second timeout. The site runs one gunicorn instance with four threads,
so the lookups took the lot: Render's `/healthz` probes queued for ~14 seconds
twice over, timed out, and the instance was restarted at 15:53. Nothing raised,
nothing was logged as an error, and the only outward sign was a Render alert.

These pin the three properties that keep it from happening again, all of them
in `uniprot.lookup_interactive`. They are unit tests over a stub, deliberately:
the thing to prove is that a *slow* answer cannot occupy the whole process, and
that is a claim about threads, not about UniProt.
"""
import threading
import time

import pytest
from django.core.cache import cache

from pipeline.services import uniprot

#: What the site actually runs on, and the number that makes the cap mean
#: something: `gunicorn OGA_website.wsgi:application --threads 4`, one instance.
#: The start command lives on the Render service and nothing in this repo can
#: change it, which is exactly why the number is written down here — a cap that
#: is not *below* it leaves nothing to answer `/healthz` with.
SITE_THREADS = 4


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def _answer(**over):
    out = {"found": True, "unavailable": False, "gene_name": "SNCA",
           "protein_name": "Alpha-synuclein", "uniprot_id": "P37840"}
    out.update(over)
    return out


def test_a_repeated_query_costs_one_call(monkeypatch):
    """One string, one call — however many times the box is asked about it."""
    calls = []
    monkeypatch.setattr(uniprot, "lookup",
                        lambda text, timeout=None: calls.append(text) or _answer())

    first = uniprot.lookup_interactive("SNCA")
    again = uniprot.lookup_interactive("  snca ")   # same query, typed loosely

    assert calls == ["SNCA"]
    assert again == first


def test_an_outage_is_cached_only_briefly(monkeypatch):
    """A failure is held, so an outage is not re-asked per visitor — but for
    two minutes, not a day: a transient outage must clear without a deploy."""
    stored = {}
    monkeypatch.setattr(cache, "set",
                        lambda key, value, ttl=None: stored.update({key: ttl}))
    monkeypatch.setattr(uniprot, "lookup",
                        lambda text, timeout=None: _answer(found=False, unavailable=True))

    uniprot.lookup_interactive("SNCA")
    assert list(stored.values()) == [uniprot._UNAVAILABLE_CACHE_SECONDS]


def test_the_deadline_is_shorter_than_the_health_check(monkeypatch):
    """The whole point of the shorter timeout: a call must not outlast the
    5-second probe that decides whether this instance is alive."""
    seen = {}
    monkeypatch.setattr(uniprot, "lookup",
                        lambda text, timeout=None: seen.update(timeout=timeout) or _answer())

    uniprot.lookup_interactive("SNCA")
    assert seen["timeout"] == uniprot.INTERACTIVE_TIMEOUT_SECONDS
    assert uniprot.INTERACTIVE_TIMEOUT_SECONDS < 5


def test_the_cap_leaves_threads_over():
    """A cap at or above the thread count is not a cap.

    Pinned apart from the storm below, because the storm derives its arithmetic
    from `MAX_INTERACTIVE_CALLS` and so cannot notice the constant being raised
    — a test that moves with the value it is checking.
    """
    assert uniprot.MAX_INTERACTIVE_CALLS < SITE_THREADS


def test_a_storm_of_lookups_leaves_threads_for_the_rest_of_the_site(monkeypatch):
    """The half that actually keeps the site up.

    Eight visitors ask at once and UniProt answers none of them. At most
    `MAX_INTERACTIVE_CALLS` may be *inside* a call at any moment; the rest are
    told `unavailable` straight away rather than queueing, so `/healthz` and
    every page that needs no network still have a thread to run on.
    """
    release = threading.Event()
    inside = threading.Semaphore(0)
    peak = {"n": 0}
    live = {"n": 0}
    lock = threading.Lock()

    def slow(text, timeout=None):
        with lock:
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
        inside.release()
        release.wait(5)
        with lock:
            live["n"] -= 1
        return _answer()

    monkeypatch.setattr(uniprot, "lookup", slow)

    answers = [None] * 8
    def ask(i):
        answers[i] = uniprot.lookup_interactive(f"GENE{i}")

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    # Once the cap is in flight, every other caller must have come back already
    # rather than waiting behind them.
    for _ in range(uniprot.MAX_INTERACTIVE_CALLS):
        assert inside.acquire(timeout=5)
    deadline = time.monotonic() + 5
    while sum(a is not None for a in answers) < 8 - uniprot.MAX_INTERACTIVE_CALLS:
        assert time.monotonic() < deadline, "callers over the cap were made to wait"
        time.sleep(0.01)

    release.set()
    for t in threads:
        t.join(timeout=5)

    assert peak["n"] <= uniprot.MAX_INTERACTIVE_CALLS
    turned_away = [a for a in answers if a.get("resolved_from") == "busy"]
    assert len(turned_away) == 8 - uniprot.MAX_INTERACTIVE_CALLS
    # Refused for being busy says "ask again", never "no such gene" — and is not
    # remembered, because it is not a fact about the gene.
    assert all(a["unavailable"] and not a["found"] for a in turned_away)
    # ...and it is not remembered: "we were busy" is not a fact about the gene,
    # so the next visitor to ask must get a real lookup rather than this.
    for i, a in enumerate(answers):
        if a.get("resolved_from") == "busy":
            assert cache.get(uniprot._cache_key(f"GENE{i}")) is None
