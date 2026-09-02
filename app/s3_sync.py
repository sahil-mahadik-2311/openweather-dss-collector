"""Mirror the CSV files to an S3 bucket.

Render's disk is ephemeral on the free plan and can be lost on any restart, so
S3 is the durable copy. Two directions matter:

  restore_today()  on boot, pull today's files down before collecting. Without
                   this a restart would leave the local 1-minute file empty and
                   the rebuilt aggregates would cover only the time since reboot.
  sync_today()     every few minutes, push today's files up.

Both are no-ops when S3 is not configured, so the service still runs locally.
"""
import logging
import os
from pathlib import Path

from .config import (AWS_REGION, DATA_DIR, S3_BUCKET, S3_PREFIX, HOUR_DIR,
                     MINUTE_DIR, Q_HOUR_DIR)
from .storage import today_ist

log = logging.getLogger(__name__)

_client = None
DIRS = {"1min": MINUTE_DIR, "15min": Q_HOUR_DIR, "1hour": HOUR_DIR}


def enabled():
    return bool(S3_BUCKET)


def client():
    """Lazily build the S3 client so boto3 is only needed when S3 is configured."""
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("s3", region_name=AWS_REGION)
    return _client


def _key_for(local_path):
    """S3 object key mirroring the local layout: <prefix>/1min/<file>.csv"""
    relative = Path(local_path).relative_to(DATA_DIR).as_posix()
    return f"{S3_PREFIX.strip('/')}/{relative}" if S3_PREFIX else relative


def sync_today(day=None):
    """Upload every CSV for the given day. Small files, so we replace wholesale."""
    if not enabled():
        return {"uploaded": 0, "skipped": "s3 not configured"}
    day = day or today_ist()
    uploaded = 0
    for directory in DIRS.values():
        if not directory.exists():
            continue
        for path in sorted(directory.glob(f"*{day}.csv")):
            try:
                client().upload_file(str(path), S3_BUCKET, _key_for(path),
                                     ExtraArgs={"ContentType": "text/csv"})
                uploaded += 1
            except Exception:
                log.exception("upload failed: %s", path.name)
    log.info("s3 sync %s -> %d files", day, uploaded)
    return {"uploaded": uploaded, "day": day}


def restore_today(day=None):
    """Pull down today's files at boot so collection resumes instead of restarting.

    Only files missing locally are fetched: a local file that already exists is
    the live one being appended to, and must not be overwritten by an older copy.
    """
    if not enabled():
        return {"restored": 0, "skipped": "s3 not configured"}
    day = day or today_ist()
    prefix = f"{S3_PREFIX.strip('/')}/" if S3_PREFIX else ""
    restored = 0
    try:
        paginator = client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(f"{day}.csv"):
                    continue
                relative = key[len(prefix):] if prefix else key
                destination = DATA_DIR / relative
                if destination.exists():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                client().download_file(S3_BUCKET, key, str(destination))
                restored += 1
    except Exception:
        log.exception("restore failed")
    log.info("s3 restore %s -> %d files", day, restored)
    return {"restored": restored, "day": day}
