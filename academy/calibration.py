"""The western blot calibration study — the one reader for its items and scores.

Raters grade a fixed set of blots against the criteria. The set is a committed
manifest (``academy/data/calibration_items.json``, built by
``bin/calibration/build_items.py``): synthetic blots with a known answer and the
parameters that drew them, and published OGA blots with the antibody name
cropped off and a *proposed* answer. The manifest never reaches a browser; a
page gets an opaque item id and an image URL.

The answers are the three results in the criteria plus ``unsure``. Agreement is
Fleiss' kappa over the three results, with ``unsure`` left out (an honest
"cannot tell" is data about the blot, not a vote for a category).
"""
from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / 'data' / 'calibration_items.json'

#: The three results, in the order the criteria give them.
RESULTS = ('main', 'other', 'none')
LABELS = {
    'main': 'Detects the target as the main signal',
    'other': 'Detects the target, with strong or numerous other bands',
    'none': 'No clear target-specific signal',
    'unsure': 'Unsure',
}

#: Consumer mailboxes. An institutional address is anything else; a company
#: address counts, since manufacturers' scientists are raters too.
FREE_MAIL = {
    'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'live.com',
    'msn.com', 'yahoo.com', 'ymail.com', 'icloud.com', 'me.com', 'mac.com',
    'aol.com', 'proton.me', 'protonmail.com', 'pm.me', 'gmx.com', 'gmx.net',
    'gmx.de', 'mail.com', 'yandex.com', 'yandex.ru', 'qq.com', '163.com',
    '126.com', 'zoho.com', 'fastmail.com', 'tutanota.com', 'hey.com',
    'web.de', 'mail.ru', 'naver.com', 'sina.com', 'rediffmail.com',
}
_FREE_PREFIXES = ('hotmail.', 'yahoo.', 'outlook.', 'live.', 'btinternet.')
_EMAIL = re.compile(r'^[^@\s]+@([A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)$')


def email_refusal(email):
    """Why ``email`` is not an institutional address, or '' if it is."""
    m = _EMAIL.match((email or '').strip())
    if not m:
        return 'Enter an email address, for example j.smith@le.ac.uk.'
    domain = m.group(1).lower()
    if domain in FREE_MAIL or domain.startswith(_FREE_PREFIXES):
        return ('Use your university, institute or company address rather than '
                f'a personal one ({domain}), for example j.smith@le.ac.uk.')
    return ''


def domain_of(email):
    return email.strip().rsplit('@', 1)[-1].lower()


@lru_cache(maxsize=1)
def items():
    """Every item in the set, in manifest order."""
    return json.loads(MANIFEST.read_text())['items']


def item(item_id):
    return next((i for i in items() if i['id'] == item_id), None)


def order_for(rater_id):
    """This rater's order: the whole set, shuffled once per rater."""
    ids = [i['id'] for i in items()]
    random.Random(f'calibration-{rater_id}').shuffle(ids)
    return ids


def next_item(rater_id, done):
    """The first item in this rater's order they have not rated, or None."""
    return next((i for i in order_for(rater_id) if i not in done), None)


# ── scoring ──────────────────────────────────────────────────────────────

def fleiss_kappa(table):
    """Fleiss' kappa for ``{item: Counter(answer → n)}``, items with ≥ 2 raters.

    Unequal rater counts per item are allowed (each item's agreement is
    computed over its own raters). Returns None when there is nothing to score.
    """
    rows = [c for c in table.values() if sum(c.values()) >= 2]
    if not rows:
        return None
    totals = Counter()
    p_items = []
    for c in rows:
        n = sum(c.values())
        totals.update(c)
        p_items.append((sum(v * v for v in c.values()) - n) / (n * (n - 1)))
    grand = sum(totals.values())
    p_bar = sum(p_items) / len(p_items)
    p_e = sum((v / grand) ** 2 for v in totals.values())
    if p_e >= 1:
        return None
    return (p_bar - p_e) / (1 - p_e)


def summary(ratings):
    """Everything the results page draws, from ``CalibrationRating`` rows.

    ``ratings`` carry ``rater_id``, ``item_id`` and ``answer``; experience is
    read off ``rater.wb_experience`` when present, for the split by experience.
    """
    by_item = defaultdict(Counter)
    by_item_exp = defaultdict(lambda: defaultdict(Counter))
    for r in ratings:
        by_item[r.item_id][r.answer] += 1
        exp = getattr(getattr(r, 'rater', None), 'wb_experience', '')
        by_item_exp[exp][r.item_id][r.answer] += 1

    def scored(table, kind=None):
        keep = {k: Counter({a: n for a, n in c.items() if a in RESULTS})
                for k, c in table.items()
                if kind is None or (item(k) or {}).get('kind') == kind}
        return fleiss_kappa(keep)

    rows = []
    for it in items():
        c = by_item.get(it['id'], Counter())
        votes = {a: c.get(a, 0) for a in RESULTS + ('unsure',)}
        decided = {a: votes[a] for a in RESULTS}
        n = sum(decided.values())
        top = max(decided, key=decided.get) if n else None
        expected = it['expected']
        rows.append({
            'id': it['id'], 'kind': it['kind'], 'expected': expected,
            'params': it['params'], 'votes': votes, 'n': n + votes['unsure'],
            'majority': top,
            'majority_share': round(decided[top] / n, 2) if n else None,
            'matches_expected': (top == expected) if (n and expected in RESULTS) else None,
        })

    # Where the line falls: share answering "main" against the strongest
    # off-target band, over the synthetic blots that record one.
    curve = []
    for row in rows:
        ratio = row['params'].get('max_ratio')
        if row['kind'] == 'synthetic' and ratio is not None and row['n']:
            decided = sum(row['votes'][a] for a in RESULTS)
            if decided:
                curve.append({'ratio': ratio, 'main_share':
                              round(row['votes']['main'] / decided, 2),
                              'n': decided, 'id': row['id']})
    curve.sort(key=lambda p: p['ratio'])

    return {
        'rows': rows,
        'kappa_all': scored(by_item),
        'kappa_synthetic': scored(by_item, 'synthetic'),
        'kappa_real': scored(by_item, 'real'),
        'kappa_by_experience': {exp: scored(t) for exp, t in by_item_exp.items()},
        'curve': curve,
    }
