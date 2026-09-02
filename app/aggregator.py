"""Roll the 1-minute files up into 15-minute and 1-hour files.

Buckets are clock-aligned and built from the minute grid: a 15-minute row is
made from the fifteen 1-minute rows of that quarter hour, an hourly row from
sixty. Because the collector writes a row every minute whether or not
OpenWeather has published a new reading, most buckets contain repeated values;
`distinct_readings` records how many genuinely different observations were
behind the averages, and `complete` says whether the full set of rows was there.
"""
import logging
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta

from .config import (HOUR_DIR, IST, Q_HOUR_DIR, ROWS_PER_15MIN, ROWS_PER_HOUR,
                     SITES)
from .storage import (hour_path, minute_path, quarter_path, read_rows,
                      today_ist, write_rows)

log = logging.getLogger(__name__)

AGG_COLUMNS = [
    "site", "bucket_start_ist", "bucket_end_ist",
    "rows_used", "expected_rows", "complete", "distinct_readings",
    "temp_mean", "temp_min", "temp_max",
    "feels_like_mean", "feels_like_max",
    "humidity_mean", "humidity_min", "humidity_max",
    "pressure_mean", "sea_level_mean", "grnd_level_mean",
    "visibility_mean",
    "wind_speed_mean", "wind_speed_max", "wind_dir_mean", "wind_gust_max",
    "clouds_mean",
    "rain_1h_max", "rain_3h_max",
    "weather_main", "weather_description",
    "first_observation_ist", "last_observation_ist",
]


def _floats(values):
    """Numeric values only. Blank cells mean 'not reported', not zero."""
    out = []
    for value in values:
        if value in ("", None):
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            pass
    return out


def _mean(values, digits=2):
    nums = _floats(values)
    return round(statistics.mean(nums), digits) if nums else ""


def _min(values, digits=2):
    nums = _floats(values)
    return round(min(nums), digits) if nums else ""


def _max(values, digits=2):
    nums = _floats(values)
    return round(max(nums), digits) if nums else ""


def _mode(values):
    present = [v for v in values if v]
    return Counter(present).most_common(1)[0][0] if present else ""


def _mean_direction(values):
    """Average wind direction as unit vectors.

    Averaging compass degrees arithmetically breaks at the 0/360 wrap: 350 and 10
    are 20 degrees apart but average to 180, due south, instead of due north.
    """
    degrees = _floats(values)
    if not degrees:
        return ""
    x = sum(math.cos(math.radians(d)) for d in degrees)
    y = sum(math.sin(math.radians(d)) for d in degrees)
    if abs(x) < 1e-12 and abs(y) < 1e-12:
        return ""                      # opposing directions cancelled out
    return round((math.degrees(math.atan2(y, x)) + 360) % 360)


def _bucket_start(minute_slot, minutes):
    stamp = datetime.strptime(minute_slot, "%Y-%m-%d %H:%M")
    if minutes == 60:
        return stamp.replace(minute=0)
    return stamp.replace(minute=(stamp.minute // minutes) * minutes)


def _summarise(site_name, start, rows, minutes, expected):
    column = lambda key: [r.get(key, "") for r in rows]
    observations = [r["observation_time_ist"] for r in rows if r.get("observation_time_ist")]
    return {
        "site": site_name,
        "bucket_start_ist": start.strftime("%Y-%m-%d %H:%M"),
        "bucket_end_ist": (start + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M"),
        "rows_used": len(rows),
        "expected_rows": expected,
        "complete": "yes" if len(rows) == expected else "no",
        "distinct_readings": len({r["dt"] for r in rows if r.get("dt")}),
        "temp_mean": _mean(column("temp")),
        "temp_min": _min(column("temp")),
        "temp_max": _max(column("temp")),
        "feels_like_mean": _mean(column("feels_like")),
        "feels_like_max": _max(column("feels_like")),
        "humidity_mean": _mean(column("humidity"), 1),
        "humidity_min": _min(column("humidity"), 0),
        "humidity_max": _max(column("humidity"), 0),
        "pressure_mean": _mean(column("pressure"), 1),
        "sea_level_mean": _mean(column("sea_level"), 1),
        "grnd_level_mean": _mean(column("grnd_level"), 1),
        "visibility_mean": _mean(column("visibility"), 0),
        "wind_speed_mean": _mean(column("wind_speed")),
        "wind_speed_max": _max(column("wind_speed")),
        "wind_dir_mean": _mean_direction(column("wind_deg")),
        "wind_gust_max": _max(column("wind_gust")),
        "clouds_mean": _mean(column("clouds_all"), 1),
        # rain_1h is a rolling one-hour total, so adding it across a bucket would
        # count the same rainfall several times over. Take the peak instead.
        "rain_1h_max": _max(column("rain_1h")),
        "rain_3h_max": _max(column("rain_3h")),
        "weather_main": _mode(column("weather_main")),
        "weather_description": _mode(column("weather_description")),
        "first_observation_ist": min(observations) if observations else "",
        "last_observation_ist": max(observations) if observations else "",
    }


def aggregate_site(site_name, minutes, expected, dest_path, day):
    rows = read_rows(minute_path(site_name, day))
    if not rows:
        return 0
    buckets = {}
    for row in rows:
        slot = row.get("minute_slot_ist")
        if not slot:
            continue
        buckets.setdefault(_bucket_start(slot, minutes), []).append(row)

    summaries = [_summarise(site_name, start, buckets[start], minutes, expected)
                 for start in sorted(buckets)]
    write_rows(dest_path, AGG_COLUMNS, summaries)
    return len(summaries)


def rebuild(day=None):
    """Rebuild both aggregate levels for a day. Idempotent: safe to run anytime."""
    day = day or today_ist()
    totals = {"15min": 0, "1hour": 0}
    for site in SITES:
        name = site["site"]
        totals["15min"] += aggregate_site(name, 15, ROWS_PER_15MIN,
                                          quarter_path(name, day), day)
        totals["1hour"] += aggregate_site(name, 60, ROWS_PER_HOUR,
                                          hour_path(name, day), day)
    log.info("aggregate %s -> %d 15-min rows, %d hourly rows",
             day, totals["15min"], totals["1hour"])
    return totals
