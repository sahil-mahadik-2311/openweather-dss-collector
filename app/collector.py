"""Fetch current conditions for every site and append one row per minute."""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from .config import (API_KEY, API_URL, IST, MINUTE_COLUMNS, REQUEST_TIMEOUT,
                     RETRIES, SITES, UNITS)
from .storage import append_row, last_minute_slot, minute_path

log = logging.getLogger(__name__)


class FetchError(Exception):
    pass


def fetch_site(lat, lon):
    """One site's current conditions, retrying transient failures."""
    params = {"lat": lat, "lon": lon, "appid": API_KEY, "units": UNITS}
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            # 401/404 are configuration faults, not blips -- do not burn retries.
            if resp.status_code in (401, 404):
                raise FetchError(f"HTTP {resp.status_code}: {resp.text[:160]}")
            last = f"HTTP {resp.status_code}"
        except FetchError:
            raise
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < RETRIES:
            continue
    raise FetchError(f"{last} after {RETRIES} attempts")


def _ist(epoch):
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, IST).strftime("%Y-%m-%d %H:%M:%S")


def to_row(site, data, minute_slot, fetched_at):
    """Flatten one API response into the 1-minute schema.

    Absent keys stay empty: OpenWeather omits a phenomenon that is not happening,
    so a blank rain column means no rain, not a failed read.
    """
    weather = (data.get("weather") or [{}])[0]
    main = data.get("main", {})
    wind = data.get("wind", {})
    rain = data.get("rain", {})
    snow = data.get("snow", {})
    sys_ = data.get("sys", {})
    return {
        "site": site["site"],
        "req_lat": site["lat"],
        "req_lon": site["lon"],
        "minute_slot_ist": minute_slot,
        "fetched_at_ist": fetched_at,
        "observation_time_ist": _ist(data.get("dt")),
        "dt": data.get("dt"),
        "coord_lat": data.get("coord", {}).get("lat"),
        "coord_lon": data.get("coord", {}).get("lon"),
        "weather_id": weather.get("id"),
        "weather_main": weather.get("main"),
        "weather_description": weather.get("description"),
        "weather_icon": weather.get("icon"),
        "temp": main.get("temp"),
        "feels_like": main.get("feels_like"),
        "temp_min": main.get("temp_min"),
        "temp_max": main.get("temp_max"),
        "pressure": main.get("pressure"),
        "humidity": main.get("humidity"),
        "sea_level": main.get("sea_level"),
        "grnd_level": main.get("grnd_level"),
        "visibility": data.get("visibility"),
        "wind_speed": wind.get("speed"),
        "wind_deg": wind.get("deg"),
        "wind_gust": wind.get("gust"),
        "clouds_all": data.get("clouds", {}).get("all"),
        "rain_1h": rain.get("1h"),
        "rain_3h": rain.get("3h"),
        "snow_1h": snow.get("1h"),
        "snow_3h": snow.get("3h"),
        "sunrise_ist": _ist(sys_.get("sunrise")),
        "sunset_ist": _ist(sys_.get("sunset")),
        "city_name": data.get("name"),
        "timezone_offset_sec": data.get("timezone"),
    }


def collect_once():
    """Fetch all sites in parallel and write one row each into today's 1-min file.

    Parallel fetching keeps every site inside the same second, so a given minute
    slot is directly comparable across sites. Writes happen on this thread only;
    each site owns its own file, so no lock is needed.
    """
    now = datetime.now(IST)
    minute_slot = now.strftime("%Y-%m-%d %H:%M")
    fetched_at = now.strftime("%Y-%m-%d %H:%M:%S")

    payloads = {}
    with ThreadPoolExecutor(max_workers=len(SITES)) as pool:
        futures = {pool.submit(fetch_site, s["lat"], s["lon"]): s for s in SITES}
        for future in as_completed(futures):
            site = futures[future]
            try:
                payloads[site["site"]] = future.result()
            except FetchError as exc:
                log.warning("%s: %s", site["site"], exc)

    written = skipped = 0
    for site in SITES:
        data = payloads.get(site["site"])
        if data is None:
            continue
        path = minute_path(site["site"])
        # A restart inside the same minute must not produce two rows for one slot.
        if last_minute_slot(path) == minute_slot:
            skipped += 1
            continue
        append_row(path, MINUTE_COLUMNS, to_row(site, data, minute_slot, fetched_at))
        written += 1

    failed = len(SITES) - len(payloads)
    log.info("collect %s -> written=%d skipped=%d failed=%d",
             minute_slot, written, skipped, failed)
    return {"minute_slot": minute_slot, "written": written,
            "skipped": skipped, "failed": failed}
