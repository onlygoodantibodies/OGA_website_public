#!/usr/bin/env python3
"""Exercise every OGA Data API endpoint with a real key and say what worked.

Run it from your own machine — a Claude Code web session cannot reach
onlygoodantibodies.co.uk (the agent proxy refuses the tunnel), so the only way
to test the live API against real data is from somewhere that can.

    python3 bin/check_api.py --key YOUR_KEY

Standard library only, so there is nothing to install. It reads, and never
writes: `/mark-reviewed/`, `/portal-config/` and `/report-issue/` change state
on our side and are deliberately not called. `/antibodies/` is called with
`preview=true` for the same reason — without it, that endpoint advances your
review cursor.

Pass the key with `--key`, or set `OGA_API_KEY` in the environment. Do not
paste a real key into a chat window: it goes into the transcript, and a key in
a URL ends up in server logs and browser history for the same reason.

Exit code is 0 when every check passed, 1 otherwise, so it is usable from cron
or CI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = 'https://onlygoodantibodies.co.uk/api/v1'
TIMEOUT = 30

PASS, FAIL, SKIP = 'PASS', 'FAIL', 'skip'


class Result:
    def __init__(self):
        self.rows = []

    def add(self, status, name, detail=''):
        self.rows.append((status, name, detail))
        mark = {PASS: '  ok  ', FAIL: ' FAIL ', SKIP: ' skip '}[status]
        print(f'[{mark}] {name}' + (f' — {detail}' if detail else ''))

    @property
    def failed(self):
        return [r for r in self.rows if r[0] == FAIL]


def _opaque(tag):
    """An entity-tag without its weak marker — what RFC 9110 compares on."""
    tag = (tag or '').strip()
    return tag[2:] if tag[:2].upper() == 'W/' else tag


class Headers:
    """Case-insensitive response headers.

    Not decoration. `dict(response.headers)` keeps whatever case the wire used,
    and **HTTP/2 lowercases every header name** — so `ETag` arrives as `etag`
    behind Cloudflare and a `.get('ETag')` finds nothing. Against a local
    Django dev server, which is HTTP/1.1 and sends `ETag`, the same lookup
    works. That is how this script reported the live API as sending no ETag on
    7 Aug 2026 when it sends one: a bug that could only appear in production.
    """

    def __init__(self, raw):
        self._items = {} if raw is None else {k.lower(): v for k, v in raw.items()}

    def get(self, name, default=None):
        return self._items.get(name.lower(), default)

    def __bool__(self):
        return bool(self._items)


def call(base, path, key, *, params=None, method='GET', headers=None):
    """One request. Returns (status, headers, parsed-or-bytes, seconds)."""
    url = f'{base.rstrip("/")}/{path.lstrip("/")}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, method=method)
    request.add_header('X-API-Key', key)
    # Some CDNs refuse the default urllib agent, and the error looks like a
    # permissions problem when it is not.
    request.add_header('User-Agent', 'oga-api-check/1.0')
    for name, value in (headers or {}).items():
        request.add_header(name, value)

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            elapsed = time.monotonic() - started
            status, resp_headers = response.status, Headers(response.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        elapsed = time.monotonic() - started
        status, resp_headers = exc.code, Headers(exc.headers)
    except Exception as exc:                       # network, DNS, TLS, timeout
        return None, Headers(None), exc, time.monotonic() - started

    if 'json' in resp_headers.get('Content-Type', ''):
        try:
            return status, resp_headers, json.loads(body), elapsed
        except ValueError:
            return status, resp_headers, body, elapsed
    return status, resp_headers, body, elapsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', default=os.environ.get('OGA_API_KEY', ''))
    parser.add_argument('--base', default=DEFAULT_BASE)
    parser.add_argument('--download', action='store_true',
                        help='also fetch /download/ (the whole archive — large)')
    args = parser.parse_args()

    if not args.key:
        parser.error('No key. Pass --key or set OGA_API_KEY.')

    out = Result()
    print(f'Checking {args.base}\n')

    # --- the key itself -----------------------------------------------------
    status, _, body, secs = call(args.base, 'status/', args.key)
    if status is None:
        out.add(FAIL, 'status/', f'could not connect: {body}')
        print('\nNothing else can be checked without a connection.')
        return 1
    if status == 200 and isinstance(body, dict):
        who = body.get('consumer', '?')
        out.add(PASS, 'status/', f'{who}, {secs:.2f}s')
    elif status in (401, 403):
        out.add(FAIL, 'status/', f'HTTP {status} — the key was refused')
        print('\nEvery other endpoint uses the same key; stopping.')
        return 1
    else:
        out.add(FAIL, 'status/', f'HTTP {status}')

    # --- the read endpoints -------------------------------------------------
    for path, params, check, label in [
        ('genes/', None, lambda b: isinstance(b, dict), 'genes/'),
        ('antibodies/', {'preview': 'true'}, lambda b: isinstance(b, dict),
         'antibodies/?preview=true'),
        ('manifest/', None, lambda b: 'files' in b, 'manifest/'),
    ]:
        status, _, body, secs = call(args.base, path, args.key, params=params)
        if status == 200 and check(body):
            n = len(body.get('files') or body.get('antibodies')
                    or body.get('genes') or [])
            out.add(PASS, label, f'{n} item(s), {secs:.2f}s')
        else:
            out.add(FAIL, label, f'HTTP {status}')

    # --- the manifest in its other two formats ------------------------------
    for fmt, sniff in (('csv', b'url,filename,'), ('urls', b'http')):
        status, _, body, secs = call(args.base, 'manifest/', args.key,
                                     params={'format': fmt})
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        if status == 200 and raw.startswith(sniff):
            out.add(PASS, f'manifest/?format={fmt}', f'{len(raw)} bytes')
        else:
            out.add(FAIL, f'manifest/?format={fmt}',
                    f'HTTP {status}, starts {raw[:40]!r}')

    # --- the documented CSV header is the one that arrives ------------------
    status, _, body, _ = call(args.base, 'manifest/', args.key,
                              params={'format': 'csv'})
    if status == 200 and isinstance(body, bytes):
        header = body.split(b'\n')[0].decode().strip()
        expected = ('url,filename,gene,catalogue_number,rrid,supplier,'
                    'application,application_display,oga_recommendation,'
                    'product_link,discontinued,gene_page_url,image_id,added_at')
        if header == expected:
            out.add(PASS, 'CSV header matches the reference')
        else:
            out.add(FAIL, 'CSV header matches the reference', f'got {header}')

    # --- conditional requests: the thing a sync client depends on -----------
    # The header and the body carry the same value on purpose, so a mismatch
    # between them is worth naming rather than silently preferring one.
    status, headers, body, _ = call(args.base, 'manifest/', args.key)
    header_etag = headers.get('ETag')
    body_etag = (body or {}).get('sync', {}).get('etag') if isinstance(body, dict) else None
    etag = header_etag or body_etag

    if not etag:
        out.add(FAIL, 'manifest/ sends an ETag',
                'neither the header nor sync.etag — every sync refetches everything')
    elif not header_etag:
        # A proxy stripping the header is a real failure even though the body
        # still has the value: `If-None-Match` is answered from the header.
        out.add(FAIL, 'manifest/ sends an ETag header',
                'body has sync.etag but no ETag header arrived — something '
                'between us and the app is dropping it')
    else:
        weakened = header_etag.strip()[:2].upper() == 'W/'
        out.add(PASS, 'manifest/ sends an ETag',
                header_etag[:24] + '…' + (' (weakened in transit)' if weakened else ''))
        # Compare opaque tags, not raw strings. A proxy that compresses the
        # body MUST weaken the tag it forwards, so `W/"x"` beside a body
        # carrying `"x"` is correct behaviour, not a disagreement — reporting
        # it as one is how this script cried wolf on 7 Aug 2026.
        if body_etag and _opaque(body_etag) != _opaque(header_etag):
            out.add(FAIL, 'header ETag and sync.etag agree',
                    f'header {header_etag[:20]}… vs body {body_etag[:20]}…')

    if etag:
        status, _, _, secs = call(args.base, 'manifest/', args.key,
                                  headers={'If-None-Match': etag})
        if status == 304:
            out.add(PASS, 'If-None-Match answers 304', f'{secs:.2f}s')
        else:
            out.add(FAIL, 'If-None-Match answers 304', f'got HTTP {status}')

    # --- the spec, which needs no key --------------------------------------
    status, _, body, _ = call(args.base, 'openapi.json', '')
    if status == 200 and isinstance(body, dict) and 'paths' in body:
        out.add(PASS, 'openapi.json', f'{len(body["paths"])} paths, no key needed')
    else:
        out.add(FAIL, 'openapi.json', f'HTTP {status}')

    # --- the bulk archive ---------------------------------------------------
    if args.download:
        status, headers, body, secs = call(args.base, 'download/', args.key)
        if status == 200 and isinstance(body, bytes) and body[:2] == b'PK':
            out.add(PASS, 'download/', f'{len(body) / 1e6:.1f} MB, {secs:.1f}s')
        else:
            out.add(FAIL, 'download/', f'HTTP {status}')
    else:
        out.add(SKIP, 'download/', 'pass --download to fetch the whole archive')

    print()
    if out.failed:
        print(f'{len(out.failed)} check(s) failed.')
        return 1
    print('Everything answered.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
