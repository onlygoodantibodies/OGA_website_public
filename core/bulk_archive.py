"""One file containing every published figure, built once and served off R2.

The whole media bucket is 473 MB across 5.31k objects (R2 dashboard, 6 Aug
2026), and the published figures are a subset of that. So "download everything"
is an ordinary download — a few hundred megabytes, the size of a single
microscopy stack — and the right shape for it is one prepared object, not a
clever protocol.

Why the archive is built ahead of time and not on request
---------------------------------------------------------
Streaming it live means fetching ~4,300 objects from R2 into the web dyno and
pushing them out again, sequentially, inside gunicorn's worker timeout. Even at
50 ms per object that is over three minutes of wall clock before the last byte,
with no resumability: a dropped connection at 90% starts again from zero. The
same reasoning ``pipeline/management/commands/dataset_snapshot.py`` gives for
running out of a Render Cron Job rather than behind a request applies here with
more force, because this one moves two orders of magnitude more bytes.

Built ahead of time it is a plain object on a bucket Cloudflare already serves:
range requests, resumable, edge-cached, and **zero bytes through Render**.

Why the key is the dataset fingerprint, and there is no pointer
--------------------------------------------------------------
The obvious design is a stable key — ``bulk/oga-figures.zip`` — with something
recording which build is current. Both halves are traps here.

``AWS_S3_FILE_OVERWRITE = False`` is set deliberately (settings.py), so Django's
storage does not overwrite: saving over an existing name silently appends a
suffix and you get ``oga-figures_a8Kd2.zip`` while every reader goes on fetching
the stale original. That is the shape of half the bugs in CLAUDE.md — a write
that succeeds against the wrong object and reports success.

And a pointer — a sidecar object or a database column — is a second source of
truth that can disagree with the bucket. It would need a migration against live
PostgreSQL for the column, or the same overwrite dance for the sidecar.

So the **key is derived from the manifest's own fingerprint**:

    bulk/oga-figures-<version>.zip

which makes three things true by construction rather than by maintenance:

  * the archive can never be out of step with the manifest, because a manifest
    naming a different version names a different key;
  * publishing one figure invalidates it automatically — the new fingerprint
    names a key that does not exist yet, so the endpoint stops offering it
    without anybody remembering to;
  * asking "is the current archive ready?" is one ``exists()`` call, and needs
    no state anywhere.

The cost is that a rebuild is needed after any change, and until it runs there
is no archive for the new version. That is the honest failure — the endpoint
says the archive is being rebuilt and hands over the manifest — rather than the
dishonest one, which is serving yesterday's zip as if it were today's dataset.

Scope
-----
One shared object can only hold one scope, and the scope it holds is the
**public dataset**: every figure on the public antibody pages, which is what
``core/api_manifest.py`` serves an unrestricted consumer. A consumer whose scope
is *narrower* — a manufacturer filtered to their own catalogue, a demo key
limited to named genes — is served by the streaming path instead, which is
affordable precisely because their scope is small.

Never build a per-consumer archive. Thirty consumers is thirty copies of the
same 400 MB, each stale at a different moment.
"""
from __future__ import annotations

import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor

from django.core.cache import cache
from django.core.files import File
from django.core.files.storage import default_storage

PREFIX = 'bulk'
STEM = 'oga-figures'

#: How long to trust an ``exists()``/``size()`` answer. The archive changes at
#: most once a day, and this saves a HEAD to R2 on every download request and
#: every manifest fetch. Short enough that a fresh build is offered within the
#: minute.
_CACHE_SECONDS = 60

#: Images are PNG and JPEG — already compressed. Deflating them again buys about
#: 2% and costs CPU on every byte, on both the build and the reader's unzip.
_COMPRESSION = zipfile.ZIP_STORED

#: How often to report progress. This is a multi-minute job reading a few
#: thousand objects one at a time over the network, and a job that prints
#: nothing for six minutes is indistinguishable from one that has hung — which
#: is exactly how the second real run was read. Frequent enough that the log
#: visibly moves; the *flushing* is the other half, and lives in the command.
PROGRESS_EVERY = 100

#: How many figures to fetch from object storage at once.
#:
#: The first successful run measured **267 ms per figure**, flat across the
#: whole run — that is one HTTPS round trip to R2 each, done one after another,
#: and 4,321 of them is 19 minutes. It is latency, not bandwidth: the figures
#: average around 100 KB, so the connection is idle almost the entire time.
#:
#: Fetching a batch concurrently and writing the batch serially takes it to
#: roughly two minutes. Safe because ``S3Storage`` keeps its boto3 connection in
#: a ``threading.local()`` (django-storages 1.14.4), so each worker gets its
#: own; the zip itself is written from one thread, since ``ZipFile`` is not
#: thread-safe and there is nothing to gain by contending for it.
#:
#: **This is also the memory ceiling**, and the reason it is a batch rather than
#: a queue of 4,321 futures: peak is roughly this many figures held at once, not
#: the size of the archive. At ~100 KB each that is under 2 MB.
READ_CONCURRENCY = 16


def key_for(version: str) -> str:
    """The object key holding the archive for one dataset version."""
    return f'{PREFIX}/{STEM}-{version}.zip'


def url_for(version: str) -> str | None:
    """The public URL of that archive, or ``None`` if it has not been built.

    Answered from the storage backend rather than assembled, so it is correct on
    both sides of the R2 flip — the same reason ``api_manifest._absolute`` does.
    """
    key = key_for(version)
    if not _exists(key):
        return None
    return default_storage.url(key)


def status_for(version: str) -> dict:
    """What the manifest and the download endpoint both report about the archive.

    ``ready`` false is not an error and must not be presented as one: it means a
    figure has been published since the last build and the archive for *this*
    dataset does not exist yet. The manifest is complete and correct throughout.
    """
    key = key_for(version)
    if not _exists(key):
        return {
            'ready': False,
            'note': ('No prepared archive for this version of the dataset yet — '
                     'one is built daily, and a figure has been published since '
                     'the last build. Every file is listed in this manifest and '
                     'can be fetched from its URL in the meantime.'),
        }
    return {
        'ready': True,
        'url': default_storage.url(key),
        'bytes': _size(key),
        'filename': f'{STEM}-{version}.zip',
        'note': ('One zip of every figure in the public dataset, plus '
                 'manifest.csv naming each one. Served from object storage — '
                 'resumable, and safe to fetch with any HTTP client.'),
    }


# ─────────────────────────────────────────────────────────
# Building
# ─────────────────────────────────────────────────────────

#: Refuse to publish an archive that lost more than this share of its figures.
#:
#: The number exists because of what happened on the first real run: the cron
#: job was created without the R2 credentials, so every one of the 4,321 reads
#: hit the local filesystem and failed, and the command cheerfully wrote a
#: 1.5 MB zip holding a manifest, a README and **no images at all** — then
#: exited 0, and Render printed *"Cron job run finished successfully"*.
#:
#: A handful of unreadable objects is a real thing that should not cost the
#: other four thousand, which is why this is a fraction and not zero. But
#: genuine per-file corruption is rare and near-zero; anything above a couple of
#: percent is systematic — a credential, a bucket, a path convention — and an
#: archive built through a systematic failure is worse than no archive, because
#: the API will hand it out as the complete dataset.
MAX_MISSING_FRACTION = 0.02


def storage_refusal():
    """Why an archive must not be built right now, or ``""``.

    The sibling of ``pipeline/services/attachments.py::storage_refusal``, and
    for a sharper reason. That one guards a file somebody uploads; this guards a
    file **nobody is watching**. A Render cron job has no persistent disk, so
    with object storage off the archive is written into a container that is torn
    down when the process exits — built, reported, gone — and the log says
    ``bulk/ now holds 1 archive(s)`` about something that no longer exists.

    This message *does* name the variables, unlike its sibling: that one reaches
    a bench scientist and this one reaches whoever is reading a cron log, who is
    exactly the person who can set them.
    """
    from django.conf import settings

    if getattr(settings, 'USE_R2', False):
        return ''
    if settings.DEBUG:
        return ''                       # dev, where local media is the point
    root = str(getattr(settings, 'MEDIA_ROOT', '') or '')
    persistent = tuple(getattr(settings, 'PERSISTENT_MEDIA_ROOTS', ()) or ())
    if persistent and root.startswith(persistent):
        return ''
    return (
        'USE_R2 is not set, so this would read every figure from the local '
        'filesystem (which does not have them) and write the archive into a '
        'container that is destroyed when this process exits. Nothing would '
        'survive the run.\n'
        'Give this job the same environment group as the web service, or set '
        'USE_R2, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, '
        'R2_ENDPOINT_URL and R2_PUBLIC_DOMAIN on it directly.')


class IncompleteArchive(RuntimeError):
    """Too many figures could not be read, so nothing was published."""


#: Multipart part size. S3 and R2 require every part except the last to be at
#: least 5 MB; 8 MB is boto3's own default and leaves headroom.
PART_SIZE = 8 * 1024 * 1024


class _MultipartSink:
    """A write-only stream that uploads to object storage as it fills.

    The archive is never materialised — not in memory, and not on disk. Bytes go
    into an 8 MB buffer, each full buffer becomes one part, and the object comes
    into existence only when ``commit`` completes the upload. Peak memory is one
    part whatever the dataset does.

    This replaced staging the zip in a ``NamedTemporaryFile``, which was written
    to fix an out-of-memory kill and did not: the run died at
    ``uploading the archive`` with all 4,321 figures read. A container's
    ``/tmp`` is typically **tmpfs**, so "staged on disk" was several hundred
    megabytes of RAM under another name, and boto3's transfer buffers on top of
    it crossed the 512 MB limit. Relocating the file would have meant guessing
    which paths on Render are disk-backed; not having a file cannot be guessed
    wrong.

    ``abort`` is not tidiness. A multipart upload creates no object until it is
    completed, but its uploaded parts are **stored and billed** until the upload
    is aborted — so a build that raises must clean up after itself or leave a
    few hundred megabytes of invisible parts behind on every failure.
    """

    def __init__(self, client, bucket, key, part_size=PART_SIZE):
        self._client = client
        self._bucket = bucket
        self._key = key
        self._part_size = part_size
        self._buffer = bytearray()
        self._parts = []
        self._written = 0
        self._on_part = None
        self._upload_id = client.create_multipart_upload(
            Bucket=bucket, Key=key, ContentType='application/zip')['UploadId']

    def on_part(self, callback):
        """Report each completed part.

        The reads print progress and the upload printed nothing, so a run that
        stalled at ``1008/4321`` gave no way to tell whether it was stuck
        fetching a figure or stuck pushing a part — two different faults with
        the same symptom, which is a blind spot this file created.
        """
        self._on_part = callback

    # -- the file-like surface zipfile needs -------------------------------
    def writable(self):
        return True

    def seekable(self):
        # False, so zipfile writes data descriptors after each entry instead of
        # going back to patch the local headers. Verified: a stored-compression
        # zip written this way opens correctly.
        return False

    def tell(self):
        return self._written

    def flush(self):
        pass

    def write(self, data):
        self._buffer += data
        self._written += len(data)
        while len(self._buffer) >= self._part_size:
            self._upload_part(self._part_size)
        return len(data)

    # -- completion --------------------------------------------------------
    def _upload_part(self, size):
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        number = len(self._parts) + 1
        result = self._client.upload_part(
            Bucket=self._bucket, Key=self._key, PartNumber=number,
            UploadId=self._upload_id, Body=chunk)
        self._parts.append({'ETag': result['ETag'], 'PartNumber': number})
        if self._on_part:
            self._on_part(number, self._written)

    def commit(self):
        # The tail goes up as the final part, which is the one part allowed to
        # be under 5 MB.
        if self._buffer or not self._parts:
            self._upload_part(len(self._buffer))
        self._client.complete_multipart_upload(
            Bucket=self._bucket, Key=self._key, UploadId=self._upload_id,
            MultipartUpload={'Parts': self._parts})

    def abort(self):
        try:
            self._client.abort_multipart_upload(
                Bucket=self._bucket, Key=self._key, UploadId=self._upload_id)
        except Exception:                                    # noqa: BLE001
            # Already failing; a failed cleanup must not replace the real error
            # with a confusing one. R2 expires abandoned parts on its own.
            pass


class _StagedSink:
    """The same interface, backed by a temp file and ``default_storage``.

    For local storage, which has no multipart API. Dev and the test suite run
    here, so the sizes involved are a fixture rather than the dataset.
    """

    def __init__(self, key):
        self._key = key
        self._file = tempfile.NamedTemporaryFile(suffix='.zip')

    def writable(self):
        return True

    def seekable(self):
        return False

    def tell(self):
        return self._file.tell()

    def flush(self):
        self._file.flush()

    def write(self, data):
        return self._file.write(data)

    def on_part(self, callback):
        pass                        # no parts; the whole file goes in one save

    def commit(self):
        self._file.flush()
        self._file.seek(0)
        if default_storage.exists(self._key):
            # AWS_S3_FILE_OVERWRITE = False makes `save` rename rather than
            # overwrite, which would leave the endpoint pointing at whichever
            # copy landed first.
            default_storage.delete(self._key)
        default_storage.save(self._key, File(self._file, name=self._key))
        self._file.close()

    def abort(self):
        self._file.close()


def _s3_client(concurrency):
    """One tuned boto3 client for the whole build, or ``None`` off object storage.

    Three settings, each for something that actually went wrong:

    ``max_pool_connections``
        Botocore's default is **10**, and every worker above that queues. It is
        the concurrency plus headroom for the part uploads, which share this
        client.

    ``connect_timeout`` / ``read_timeout``
        So a stalled connection surfaces as an error on one figure rather than
        as a job that has printed nothing for eleven minutes and cannot be told
        apart from a hang.

    ``retries``
        Bounded, and *reported* by the missing-figures count rather than
        retried for ever underneath a silent log.
    """
    bucket = getattr(default_storage, 'bucket_name', None)
    connection = getattr(default_storage, 'connection', None)
    if not bucket or connection is None:
        return None, None

    from botocore.config import Config

    client = connection.meta.client
    tuned = client.meta.config.merge(Config(
        max_pool_connections=concurrency + 8,
        connect_timeout=15,
        read_timeout=60,
        retries={'max_attempts': 3, 'mode': 'standard'},
    ))
    import boto3
    return boto3.client(
        's3',
        endpoint_url=client.meta.endpoint_url,
        aws_access_key_id=getattr(default_storage, 'access_key', None),
        aws_secret_access_key=getattr(default_storage, 'secret_key', None),
        region_name=getattr(default_storage, 'region_name', None) or 'auto',
        config=tuned,
    ), bucket


def _open_sink(key, client=None, bucket=None):
    """Whichever sink this storage backend supports.

    Streaming multipart wherever object storage is configured — which is every
    environment this command actually runs in — and a temp file for the local
    filesystem, which has no multipart API.
    """
    if client is not None and bucket:
        return _MultipartSink(client, bucket, key)
    return _StagedSink(key)


def _fetch_via_storage(row):
    """``(row, bytes, error)`` for one figure, through Django's storage.

    The local-filesystem path, and the one the tests take. Touches storage only
    — never the ORM — which is what makes it safe to call from a worker thread.
    """
    try:
        with row['_image'].open('rb') as fh:
            return row, fh.read(), None
    except (OSError, ValueError, KeyError) as exc:
        return row, None, exc


def _fetch_via_client(client, bucket):
    """A fetcher that shares one boto3 client across every worker.

    **Not** ``default_storage.open`` in a thread, which is what this replaced.
    ``S3Storage`` keeps its boto3 connection in a ``threading.local()``, so each
    of the 16 workers built its own session and client — sixteen loads of
    botocore's service model, sixteen connection pools, and botocore's default
    pool is ten. The measured result was a 3.2× speedup on concurrency 16, then
    a hard stall.

    One client, one pool sized to the concurrency, shared by all of them.
    botocore *clients* are thread-safe; it is *resources* that are not, and
    ``threading.local()`` in django-storages is guarding the resource.
    """
    def fetch(row):
        try:
            body = client.get_object(Bucket=bucket, Key=row['_image'].name)
            return row, body['Body'].read(), None
        except Exception as exc:                             # noqa: BLE001
            # Deliberately broad: botocore raises ClientError, and the point of
            # this path is that one unreadable object is reported and skipped
            # rather than ending the run.
            return row, None, exc

    return fetch


def build(rows, version, manifest_csv, readme, *, progress=None,
          concurrency=READ_CONCURRENCY):
    """Write the archive for ``version`` and return ``(key, bytes_written)``.

    ``rows`` are manifest rows — each needs ``filename`` and the stored image —
    so the names inside the zip are the manifest's names. A reader who has the
    CSV and the zip can join them on ``filename`` with no further work, which is
    the entire point of shipping the CSV inside.

    **Assembled on disk, not in memory.** It was a ``BytesIO``, on the reasoning
    that the whole bucket is 473 MB and "a Render cron container has more than
    that" — which was asserted and never checked. The job runs on a **Starter**
    instance: 512 MB of RAM, against a few hundred megabytes of zip held whole,
    plus each figure's bytes while it is added, plus Django and boto3. That is
    an out-of-memory kill part way through a six-minute job, which Render
    reports as a failed run with no output explaining it.

    A cron container has no *persistent* disk and plenty of ephemeral disk, and
    the file only has to outlive the upload — so a temp file costs nothing and
    keeps memory flat whatever the dataset does. ``default_storage.save`` reads
    it back in chunks, so the upload never materialises it either.
    """
    written = 0
    missing = []
    done = 0

    key = key_for(version)
    client, bucket = _s3_client(concurrency)
    fetch = _fetch_via_client(client, bucket) if client else _fetch_via_storage
    sink = _open_sink(key, client, bucket)
    if progress:
        sink.on_part(lambda n, total: progress(None, None, part=n, sent=total))

    try:
        with zipfile.ZipFile(sink, 'w', _COMPRESSION) as archive:
            archive.writestr('manifest.csv', manifest_csv)
            archive.writestr('README.txt', readme)

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                # Batched rather than one queue of every row: the batch size is
                # the read-side memory ceiling, and submitting all 4,321 at once
                # would pull the whole dataset into RAM as fast as the network
                # allowed.
                for start in range(0, len(rows), concurrency):
                    batch = rows[start:start + concurrency]
                    for row, blob, error in pool.map(fetch, batch):
                        done += 1
                        if error:
                            # A row whose bytes are gone is a fact worth
                            # reporting, not a reason to abandon 4,000 good
                            # files. The count goes back to the command.
                            missing.append(f'{row["filename"]}: {error}')
                            continue
                        archive.writestr(f'figures/{row["filename"]}', blob)
                        written += 1
                    if progress and done % PROGRESS_EVERY < concurrency:
                        progress(done, len(rows))

        # Checked before the upload is completed, never after. With multipart
        # the object does not exist until `commit`, so raising here leaves
        # nothing for the API to hand out — the same guarantee the staged
        # version got by not calling save().
        expected = len(rows)
        if expected and len(missing) > max(1, expected * MAX_MISSING_FRACTION):
            raise IncompleteArchive(
                f'{len(missing)} of {expected} figures could not be read, which '
                f'is more than {MAX_MISSING_FRACTION:.0%}. Nothing was written: '
                f'an archive built through a systematic failure is worse than '
                f'no archive, because the API hands it out as the whole '
                f'dataset.\nFirst failure: {missing[0]}')

        if progress:
            progress(len(rows), len(rows), uploading=True)
        sink.commit()
    except BaseException:
        # Including KeyboardInterrupt and the SIGTERM Render sends: an aborted
        # run must not leave billed multipart parts behind.
        sink.abort()
        raise

    _forget(key)
    return key, written, missing


#: How many archives to keep, current one included. **This is what stops the
#: bucket growing**: without it every dataset change would leave another few
#: hundred megabytes behind for ever, and the fingerprint key means every change
#: is a new key rather than an overwrite.
#:
#: Two rather than one, and the second is not sentiment. Pruning to exactly the
#: current archive deletes the object somebody may be **downloading right now** —
#: a few hundred megabytes over a slow link takes minutes, and the build runs
#: unattended in the night. One superseded copy costs one archive's worth of a
#: 10 GB allowance and closes that window completely. It is a fixed cost, not a
#: growing one, which is the whole point.
KEEP_ARCHIVES = 2


def _archive_names():
    """Every archive object under the prefix, newest first — or ``None``.

    Matched on the pattern this module writes and nothing else does, so a note,
    a stray upload or a future artefact under ``bulk/`` is never a candidate.

    **``None`` means the listing failed, and is not the same as an empty
    prefix.** It used to return ``[]`` for both, which made a bucket this could
    not read indistinguishable from one holding nothing: ``prune`` would delete
    nothing and report success, ``usage`` would print ``0 archives, 0.0 MB``,
    and the cron log would say that every night while the prefix grew by a few
    hundred megabytes a build. The one number written down to catch drift would
    have been the number that hid it. Same rule as ``requestJson``'s read/write
    argument in ``board.js`` — a failed read is not an empty result.
    """
    try:
        _, names = default_storage.listdir(PREFIX)
    except FileNotFoundError:
        # The prefix does not exist yet, which is emptiness and not failure.
        # The two backends disagree about how to say it: S3 has no directories,
        # so listing a prefix with nothing under it returns empty, while
        # FileSystemStorage raises. Folding this into the failure case above
        # would make every first run — and every dev run — warn that the bucket
        # could not be read when it is simply new.
        return []
    except (OSError, NotImplementedError):
        return None

    keys = [f'{PREFIX}/{n}' for n in names
            if n.startswith(f'{STEM}-') and n.endswith('.zip')]

    def modified(key):
        try:
            return default_storage.get_modified_time(key)
        except (OSError, NotImplementedError, AttributeError):
            # Undated sorts oldest, so an unreadable timestamp makes an object a
            # prune candidate rather than an immortal one. The current archive is
            # protected by name regardless, so this can never drop the live copy.
            return None

    dated = [(modified(k), k) for k in keys]
    undated = [k for stamp, k in dated if stamp is None]
    return ([k for _, k in sorted((d for d in dated if d[0] is not None),
                                  key=lambda pair: pair[0], reverse=True)]
            + undated)


def prune(keep_version, *, keep=KEEP_ARCHIVES, dry_run=False):
    """Delete superseded archives, keeping the current one and ``keep - 1`` more.

    Only ever touches ``bulk/oga-figures-*.zip`` — objects this module created,
    named by a pattern nothing else writes. It never reaches a figure, a session
    attachment or a scientific record, which is why it is not behind the
    ``production-data`` skill's dry-run-by-default rule: the thing it deletes is
    its own previous output.

    The current archive is kept **by name**, never by position, so a clock skew
    or an unreadable timestamp can reorder the list without ever deleting the
    one the API is serving.

    Returns ``None`` when the prefix could not be listed — nothing was deleted
    and nothing is known, which the caller must report rather than print as a
    clean run.
    """
    existing = _archive_names()
    if existing is None:
        return None

    current = key_for(keep_version)
    survivors = {current}
    for key in existing:
        if len(survivors) >= max(1, keep):
            break
        survivors.add(key)

    removed = []
    for key in existing:
        if key in survivors:
            continue
        if not dry_run:
            default_storage.delete(key)
            _forget(key)
        removed.append(key)
    return removed


def usage():
    """``(count, bytes)`` for the archives on file, or ``(None, None)``.

    Reported on every build, successful or not, because a footprint nobody
    prints is one nobody notices growing — and this is the one prefix in the
    bucket that a scheduled job writes to unattended.

    ``(None, None)`` means the prefix could not be read. It must not be printed
    as zero: this figure exists to be watched over months, so the reading that
    means "I could not look" and the reading that means "there is nothing here"
    have to be different on the page.
    """
    keys = _archive_names()
    if keys is None:
        return None, None
    total = 0
    for key in keys:
        size = _size(key)
        if size:
            total += size
    return len(keys), total


README_TEMPLATE = """\
Only Good Antibodies — published figure archive
===============================================

Dataset version : {version}
Built           : {built}
Figures         : {count}

Contents
--------
  figures/      Every published figure in the public dataset, one file each.
  manifest.csv  One row per figure: the filename in here, the gene, the
                catalogue number, the RRID, the supplier, and what the
                characterisation data shows. Join it to the files on the
                `filename` column.

What the characterisation data shows
------------------------------------
{scope}

Three columns say it, and they are one answer in three forms.

`oga_support` is the value to switch on: `supportive`, `limited_support`,
`not_supportive` or `not_tested`. One value per rung.

`oga_display` is the wording used on the OGA site for that value, and it has the
same four rungs:

{rungs}

`oga_qualifier` says what was seen, where the bench judged it -- for example
"detects the target, but is not selective". It is empty where nobody judged
that axis, and it qualifies a supportive result as readily as a negative one.

{limited_note}

`oga_recommendation` is the older enum -- `recommended`, `not_recommended` or
`not_tested`. It is unchanged and will stay that way, so anything switching on
it keeps working. What it cannot say is which of the two negatives a row is:
`limited_support` and `not_supportive` both appear there as `not_recommended`,
and the first is one the bench saw do what the application is for. That is what
`oga_support` is for, and it is the last column rather than the third so that
existing column positions do not move.

Staying up to date
------------------
This archive is rebuilt when the dataset changes. To check whether you hold the
current one, fetch:

    {manifest_url}

and compare `dataset_version` with the value above. The API also serves this
archive's URL directly, under `bulk_download`.

Full documentation: {base_url}/data-access/
"""
# ─────────────────────────────────────────────────────────
# exists()/size() are HEAD requests to R2 — cached, because the download
# endpoint and every manifest fetch both ask.
# ─────────────────────────────────────────────────────────

def _exists(key):
    return _cached(f'oga-bulk-exists:{key}', lambda: default_storage.exists(key))


def _size(key):
    def read():
        try:
            return default_storage.size(key)
        except (OSError, NotImplementedError):
            return None
    return _cached(f'oga-bulk-size:{key}', read)


def _cached(cache_key, produce):
    hit = cache.get(cache_key)
    if hit is not None:
        # False and 0 are real answers, so they are stored wrapped rather than
        # bare — `cache.get` cannot tell a stored False from a miss.
        return hit['value']
    value = produce()
    cache.set(cache_key, {'value': value}, _CACHE_SECONDS)
    return value


def _forget(key):
    cache.delete(f'oga-bulk-exists:{key}')
    cache.delete(f'oga-bulk-size:{key}')
