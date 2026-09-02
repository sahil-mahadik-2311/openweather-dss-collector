"""Configuration. Everything tunable lives here or in environment variables."""
import os
from datetime import timedelta, timezone
from pathlib import Path

# --- credentials -------------------------------------------------------------
API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
API_URL = "https://api.openweathermap.org/data/2.5/weather"
UNITS = "metric"
REQUEST_TIMEOUT = 20          # seconds per HTTP call
RETRIES = 3                   # attempts per site before giving up for this minute

# --- time --------------------------------------------------------------------
# All filenames, bucket boundaries and timestamps are in IST, so a "day" in the
# data means an Indian calendar day regardless of where the server runs.
IST = timezone(timedelta(hours=5, minutes=30))

# --- storage -----------------------------------------------------------------
# On Render, point DATA_DIR at the mount path of a persistent disk. Without one,
# the filesystem is wiped on every deploy and restart.
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
MINUTE_DIR = DATA_DIR / "1min"
Q_HOUR_DIR = DATA_DIR / "15min"
HOUR_DIR = DATA_DIR / "1hour"

# --- S3 (durable copy) -------------------------------------------------------
# Leave S3_BUCKET blank to disable. When set, files are restored at boot and
# uploaded every few minutes, so an ephemeral Render disk stops being a risk.
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = os.environ.get("S3_PREFIX", "openweather-dss")
AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
S3_SYNC_MINUTES = int(os.environ.get("S3_SYNC_MINUTES", "5"))

# --- keepalive ---------------------------------------------------------------
# Render free instances sleep after 15 minutes without INBOUND traffic. Outbound
# calls to OpenWeather do not count. Render sets RENDER_EXTERNAL_URL itself, so
# the service can request its own public URL to generate that inbound traffic.
# This is a second line of defence: use an external pinger as the primary one,
# because once the instance is asleep it cannot ping itself awake.
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
KEEPALIVE_MINUTES = int(os.environ.get("KEEPALIVE_MINUTES", "10"))

# --- schedule ----------------------------------------------------------------
# Collection runs around the clock by default. Set COLLECT_START/COLLECT_END
# (24h IST, e.g. "06:30" and "18:00") to restrict it to a window.
COLLECT_START = os.environ.get("COLLECT_START", "")
COLLECT_END = os.environ.get("COLLECT_END", "")

ROWS_PER_15MIN = 15           # one 1-minute row per minute
ROWS_PER_HOUR = 60

# --- sites -------------------------------------------------------------------
SITES = [
    {"site": "BKC DSS",       "lat": 19.0688, "lon": 72.8703},
    {"site": "Backbay DSS",   "lat": 18.9350, "lon": 72.8102},
    {"site": "Dahisar DSS",   "lat": 19.2518, "lon": 72.8587},
    {"site": "Vrindavan DSS", "lat": 19.0385, "lon": 72.9232},
    {"site": "Mindspace DSS", "lat": 19.1136, "lon": 72.8697},
]

# --- 1-minute CSV schema -----------------------------------------------------
MINUTE_COLUMNS = [
    "site", "req_lat", "req_lon",
    "minute_slot_ist", "fetched_at_ist", "observation_time_ist", "dt",
    "coord_lat", "coord_lon",
    "weather_id", "weather_main", "weather_description", "weather_icon",
    "temp", "feels_like", "temp_min", "temp_max",
    "pressure", "humidity", "sea_level", "grnd_level",
    "visibility",
    "wind_speed", "wind_deg", "wind_gust",
    "clouds_all",
    "rain_1h", "rain_3h", "snow_1h", "snow_3h",
    "sunrise_ist", "sunset_ist",
    "city_name", "timezone_offset_sec",
]
