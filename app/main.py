"""Web service + in-process scheduler.

Render needs a process that binds a port, so the scheduler lives inside a small
Flask app. The HTTP side exists to keep the service reachable, to expose health
for an uptime pinger, and to let you download the CSVs without shell access.
"""
import io
import logging
import os
import csv
import fcntl
import zipfile
from datetime import datetime, time as dtime
from pathlib import Path

import requests

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, jsonify, send_file

from .aggregator import rebuild
from .collector import collect_once
from . import s3_sync
from .config import (API_KEY, COLLECT_END, COLLECT_START, DATA_DIR, HOUR_DIR,
                     IST, KEEPALIVE_MINUTES, MINUTE_DIR, Q_HOUR_DIR, S3_BUCKET,
                     S3_SYNC_MINUTES, SELF_URL, SITES)
from .storage import today_ist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("weather")

DIRS = {"1min": MINUTE_DIR, "15min": Q_HOUR_DIR, "1hour": HOUR_DIR}

# Status is derived from the files on disk, not from counters in memory. The
# scheduler and the HTTP handler can run in different processes on Render, and a
# restart clears memory anyway -- the files are the only thing both sides agree on.
SYNC_MARKER = DATA_DIR / ".last_s3_sync"


def _newest_minute_slot():
    """Latest minute recorded across today's 1-minute files, and the row count."""
    latest, rows = None, 0
    if not MINUTE_DIR.exists():
        return None, 0
    for path in MINUTE_DIR.glob(f"*{today_ist()}.csv"):
        try:
            with path.open(newline="", encoding="utf-8") as f:
                site_rows = list(csv.DictReader(f))
        except OSError:
            continue
        rows += len(site_rows)
        if site_rows:
            slot = site_rows[-1].get("minute_slot_ist")
            if slot and (latest is None or slot > latest):
                latest = slot
    return latest, rows


def _mtime_ist(path):
    if not Path(path).exists():
        return None
    return datetime.fromtimestamp(Path(path).stat().st_mtime, IST).strftime("%Y-%m-%d %H:%M:%S")


def _newest_file_time(directory):
    if not directory.exists():
        return None
    files = list(directory.glob(f"*{today_ist()}.csv"))
    return _mtime_ist(max(files, key=lambda p: p.stat().st_mtime)) if files else None


def _parse_time(value):
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return dtime(hour, minute)
    except (ValueError, AttributeError):
        return None


def within_window():
    """True when collection should run. No window configured means always."""
    start, end = _parse_time(COLLECT_START), _parse_time(COLLECT_END)
    if not start or not end:
        return True
    now = datetime.now(IST).time()
    return start <= now <= end if start <= end else (now >= start or now <= end)


def job_collect():
    if not within_window():
        return
    try:
        collect_once()
    except Exception:                       # never let one bad minute kill the scheduler
        log.exception("collection failed")


def job_aggregate():
    try:
        rebuild()
    except Exception:
        log.exception("aggregation failed")


def job_s3_sync():
    try:
        result = s3_sync.sync_today()
        if result.get("uploaded"):
            # Touch a marker so any process can report when the last sync happened.
            SYNC_MARKER.parent.mkdir(parents=True, exist_ok=True)
            SYNC_MARKER.write_text(datetime.now(IST).isoformat())
    except Exception:
        log.exception("s3 sync failed")


def job_keepalive():
    """Request our own public URL so Render sees inbound traffic and stays awake."""
    try:
        requests.get(f"{SELF_URL.rstrip('/')}/health", timeout=15)
    except Exception as exc:
        log.warning("keepalive ping failed: %s", exc)


def create_app():
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify(status="ok", time=datetime.now(IST).isoformat())

    @app.get("/")
    def status():
        latest_slot, row_count = _newest_minute_slot()
        counts = {}
        for label, directory in DIRS.items():
            files = sorted(p.name for p in directory.glob("*.csv")) if directory.exists() else []
            counts[label] = {"files": len(files), "names": files[-5:]}
        return jsonify(
            service="openweather-dss-collector",
            now_ist=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            sites=[s["site"] for s in SITES],
            window=f"{COLLECT_START or '00:00'}-{COLLECT_END or '23:59'} IST",
            api_key_configured=bool(API_KEY),
            data_dir=str(DATA_DIR),
            last_collect_slot=latest_slot,
            minute_rows_today=row_count,
            last_aggregate=_newest_file_time(Q_HOUR_DIR),
            s3_bucket=S3_BUCKET or None,
            keepalive_url=SELF_URL or None,
            last_s3_sync=_mtime_ist(SYNC_MARKER),
            data=counts,
        )

    @app.get("/files/<interval>")
    def files(interval):
        directory = DIRS.get(interval) or abort(404, "unknown interval")
        if not directory.exists():
            return jsonify(files=[])
        return jsonify(files=sorted(p.name for p in directory.glob("*.csv")))

    @app.get("/download/<interval>/<path:name>")
    def download(interval, name):
        directory = DIRS.get(interval) or abort(404, "unknown interval")
        target = (directory / name).resolve()
        # Reject anything that escapes the interval directory.
        if directory.resolve() not in target.parents or not target.exists():
            abort(404, "file not found")
        return send_file(target, as_attachment=True, download_name=target.name)

    @app.get("/download/day/<day>")
    def download_day(day):
        """Every CSV for one IST date, across all three intervals, as one zip."""
        buffer = io.BytesIO()
        found = 0
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for label, directory in DIRS.items():
                if not directory.exists():
                    continue
                for path in sorted(directory.glob(f"*{day}.csv")):
                    archive.write(path, f"{label}/{path.name}")
                    found += 1
        if not found:
            abort(404, f"no files for {day}")
        buffer.seek(0)
        return send_file(buffer, mimetype="application/zip", as_attachment=True,
                         download_name=f"weather_{day}.zip")

    @app.post("/sync")
    def sync_now():
        return jsonify(s3_sync.sync_today())

    @app.post("/aggregate/<day>")
    def aggregate_day(day):
        return jsonify(day=day, **rebuild(day))

    return app


_LOCK_HANDLE = None


def _claim_scheduler_lock():
    """Take an exclusive lock so exactly one process ever runs the scheduler.

    Gunicorn is pinned to one worker, but a module can still be imported more than
    once (master plus worker, or a reload). Two schedulers would mean two
    collections a minute. The lock is held for the process lifetime and released
    by the OS when the process exits, so a crashed process does not wedge it.
    """
    global _LOCK_HANDLE
    lock_path = DATA_DIR / ".scheduler.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    handle.write(str(os.getpid()))
    handle.flush()
    _LOCK_HANDLE = handle          # keep a reference; closing would drop the lock
    return True


def start_scheduler():
    """Start the background jobs in whichever process wins the lock."""
    if not _claim_scheduler_lock():
        log.info("scheduler already running in another process (pid %s) -- skipping",
                 os.getpid())
        return None
    if not API_KEY:
        log.error("OPENWEATHER_API_KEY is not set -- collection will fail")

    # Recover today's files before the first collection, so a restart continues
    # the day instead of starting it over with an empty 1-minute file.
    if s3_sync.enabled():
        s3_sync.restore_today()

    scheduler = BackgroundScheduler(timezone=IST)
    # coalesce + max_instances=1: if the service was briefly busy or asleep, run
    # once on wake rather than replaying every missed minute at the same moment.
    scheduler.add_job(job_collect, "cron", minute="*", id="collect",
                      coalesce=True, max_instances=1, misfire_grace_time=30)
    # Two minutes past each quarter, so the bucket it summarises is closed.
    scheduler.add_job(job_aggregate, "cron", minute="2,17,32,47", id="aggregate",
                      coalesce=True, max_instances=1, misfire_grace_time=120)
    if s3_sync.enabled():
        scheduler.add_job(job_s3_sync, "cron", minute=f"*/{S3_SYNC_MINUTES}",
                          id="s3_sync", coalesce=True, max_instances=1,
                          misfire_grace_time=120)

    if SELF_URL:
        scheduler.add_job(job_keepalive, "cron", minute=f"*/{KEEPALIVE_MINUTES}",
                          id="keepalive", coalesce=True, max_instances=1)

    scheduler.start()
    log.info("scheduler started | sites=%d | window=%s | data=%s",
             len(SITES), f"{COLLECT_START or '00:00'}-{COLLECT_END or '23:59'}", DATA_DIR)
    return scheduler


app = create_app()

# Gunicorn imports this module in each worker. The service is pinned to one
# worker in the start command so the scheduler cannot run in parallel copies.
if os.environ.get("RUN_SCHEDULER", "1") == "1":
    start_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
