"""CSV paths and append/read helpers. One file per site per day per interval."""
import csv
import re
from datetime import datetime
from pathlib import Path

from .config import HOUR_DIR, IST, MINUTE_DIR, Q_HOUR_DIR


def slug(site_name):
    """'BKC DSS' -> 'BKC_DSS', so the site is readable in the filename."""
    return re.sub(r"[^A-Za-z0-9]+", "_", site_name).strip("_")


def today_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")


def minute_path(site_name, day=None):
    return MINUTE_DIR / f"{slug(site_name)}_{day or today_ist()}.csv"


def quarter_path(site_name, day=None):
    return Q_HOUR_DIR / f"{slug(site_name)}_15min_{day or today_ist()}.csv"


def hour_path(site_name, day=None):
    return HOUR_DIR / f"{slug(site_name)}_1hour_{day or today_ist()}.csv"


def append_row(path, columns, row):
    """Append one row, writing the header if the file is new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def write_rows(path, columns, rows):
    """Replace a file with the given rows. Aggregates are rebuilt, not appended."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path):
    if not Path(path).exists():
        return []
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def last_minute_slot(path):
    """Most recent minute already recorded, so a restart cannot duplicate a slot."""
    rows = read_rows(path)
    return rows[-1]["minute_slot_ist"] if rows else None
