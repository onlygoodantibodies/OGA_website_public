"""
Upload everything under MEDIA_ROOT to the Cloudflare R2 bucket, preserving each
file's relative path as the object key (e.g. MEDIA_ROOT/experiments/2648_WB.png
-> key 'experiments/2648_WB.png'). Because the database stores those same
relative keys, no DB changes are needed.

Run this in the Render shell BEFORE flipping USE_R2=True — the media files are
already on the Render filesystem there, so it pushes straight to R2.

Reads credentials from env: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
R2_BUCKET_NAME, R2_ENDPOINT_URL.

Usage:
    python manage.py upload_media_to_r2 --dry-run     # preview, uploads nothing
    python manage.py upload_media_to_r2               # upload, skips existing keys
    python manage.py upload_media_to_r2 --overwrite   # re-upload everything
"""
import mimetypes
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

REQUIRED_ENV = ('R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY',
                'R2_BUCKET_NAME', 'R2_ENDPOINT_URL')


class Command(BaseCommand):
    help = "Upload MEDIA_ROOT files to the Cloudflare R2 bucket (keys = relative paths)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help="List what would upload; change nothing.")
        parser.add_argument('--overwrite', action='store_true',
                            help="Re-upload files even if the key already exists in the bucket.")

    def handle(self, *args, **opts):
        try:
            import boto3
        except ImportError:
            raise CommandError("boto3 is not installed — add it to requirements.txt and redeploy.")

        missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
        if missing:
            raise CommandError("Missing env var(s): " + ", ".join(missing))

        media_root = str(settings.MEDIA_ROOT)
        if not os.path.isdir(media_root):
            raise CommandError(f"MEDIA_ROOT does not exist: {media_root}")

        mimetypes.add_type('image/svg+xml', '.svg')  # be explicit for SVGs
        bucket = os.environ['R2_BUCKET_NAME']
        dry, overwrite = opts['dry_run'], opts['overwrite']

        s3 = boto3.client(
            's3',
            endpoint_url=os.environ['R2_ENDPOINT_URL'],
            aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
            aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
            region_name='auto',
        )

        existing = set()
        if not overwrite:
            for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket):
                existing.update(o['Key'] for o in page.get('Contents', []))

        uploaded = skipped = 0
        total_bytes = 0
        for root, _dirs, files in os.walk(media_root):
            for name in files:
                path = os.path.join(root, name)
                key = os.path.relpath(path, media_root).replace(os.sep, '/')
                if not overwrite and key in existing:
                    skipped += 1
                    continue
                if dry:
                    self.stdout.write(f"would upload: {key}")
                    uploaded += 1
                    continue
                ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
                s3.upload_file(path, bucket, key, ExtraArgs={'ContentType': ctype})
                uploaded += 1
                total_bytes += os.path.getsize(path)
                if uploaded % 200 == 0:
                    self.stdout.write(f"  uploaded {uploaded}...")

        verb = "would upload" if dry else "uploaded"
        self.stdout.write(self.style.SUCCESS(
            f"Done. {verb}: {uploaded}; skipped (already present): {skipped}; "
            f"{total_bytes / 1e6:.1f} MB."
        ))

        if not dry:
            count = 0
            for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket):
                count += len(page.get('Contents', []))
            self.stdout.write(self.style.SUCCESS(f"Bucket '{bucket}' now holds {count} objects."))
