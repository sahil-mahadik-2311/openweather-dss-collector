"""Web service + in-process scheduler.

Render needs a process that binds a port, so the scheduler lives inside a small
Flask app. The HTTP side exists to keep the service reachable, to expose health
for an uptime pinger, and to let you download the CSVs without shell access.
"""
import io
import logging
import os
import zipfile
from datetime import datetime, time as dtime

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

STATE = {"last_collect": None, "last_aggregate": None, "last_s3_sync": None,
         "collects": 0, "errors": 0}


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
        result = collect_once()
        STATE["last_collect"] = result["minute_slot"]
        STATE["collects"] += 1
        if result["failed"]:
            STATE["errors"] += result["failed"]
    except Exception:                       # never let one bad minute kill the scheduler
        STATE["errors"] += 1
        log.exception("collection failed")


def job_aggregate():
    try:
        rebuild()
        STATE["last_aggregate"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        log.exception("aggregation failed")


def job_s3_sync():
    try:
        result = s3_sync.sync_today()
        if result.get("uploaded"):
            STATE["last_s3_sync"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
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
            last_collect_slot=STATE["last_collect"],
            last_aggregate=STATE["last_aggregate"],
            s3_bucket=S3_BUCKET or None,
            keepalive_url=SELF_URL or None,
            last_s3_sync=STATE["last_s3_sync"],
            collections_this_boot=STATE["collects"],
            errors_this_boot=STATE["errors"],
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


def start_scheduler():
    """Start the background jobs. Called once, from the single worker process."""
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
