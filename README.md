# OpenWeather DSS Collector

Collects current weather for five Mumbai DSS sites once a minute, and rolls the
minute data up into 15-minute and 1-hour files. Runs as a single always-on
service so collection no longer depends on anyone's laptop being awake.

## How the data flows

```
OpenWeather current weather API
        |  every minute, 5 sites in parallel
        v
data/1min/<Site>_<date>.csv          one row per minute per site
        |  every 15 min, rebuilt from the minute rows
        v
data/15min/<Site>_15min_<date>.csv   15 minute-rows -> 1 row
data/1hour/<Site>_1hour_<date>.csv   60 minute-rows -> 1 row
```

Aggregates are **rebuilt** from the minute files rather than appended, so a
restart mid-day repairs itself and re-running is always safe.

## What is in each file

**1min** carries the full API response: temperature, feels like, min/max,
pressure (sea and ground level), humidity, visibility, wind speed, direction and
gust, cloud cover, rain and snow, the weather description, sunrise and sunset.

**15min / 1hour** carry mean, min and max for the numeric fields, the most common
weather description, and three columns that keep the summary honest:

| Column | Meaning |
|---|---|
| `rows_used` | how many minute rows went into this bucket |
| `expected_rows` | 15 or 60 |
| `complete` | `yes` when every expected row was present |
| `distinct_readings` | how many *genuinely different* observations were behind it |

`distinct_readings` matters. OpenWeather publishes a new reading roughly every
ten minutes, so a 15-minute bucket built from 15 rows typically contains only
one or two real observations, repeated. The averages are still correct; the
column tells you how much independent data is behind them.

Two aggregation details worth knowing:

- **Wind direction is vector-averaged**, not arithmetic. Averaging compass
  degrees directly breaks at the 0/360 wrap, where 350 and 10 average to due
  south instead of due north.
- **Rainfall takes the peak, not the sum.** `rain_1h` is a rolling one-hour
  total, so adding it across a bucket would count the same rain repeatedly.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /` | status: last collection, file counts, config |
| `GET /health` | health check for Render and uptime pingers |
| `GET /files/<1min\|15min\|1hour>` | list available CSVs |
| `GET /download/<interval>/<name>` | download one CSV |
| `GET /download/day/<YYYY-MM-DD>` | all three intervals for a day, as a zip |
| `POST /aggregate/<YYYY-MM-DD>` | rebuild a day's aggregates on demand |
| `POST /sync` | push today's files to S3 immediately |

## Storage: S3, not the Render disk

Free Render instances have an ephemeral filesystem that is wiped on every restart
and redeploy, and cannot have a persistent disk. S3 is therefore the real store:

- **On boot** the service restores today's files from S3 *before* collecting.
  Without this a restart would leave the local 1-minute file empty, and the
  rebuilt aggregates would silently cover only the time since the restart.
- **Every 5 minutes** it uploads today's files back to S3.

Worst case, a hard crash loses the last five minutes. Everything already synced
survives. At this volume S3 costs roughly a rupee or two a month.

Objects mirror the local layout:

```
s3://<bucket>/openweather-dss/1min/BKC_DSS_2026-09-02.csv
s3://<bucket>/openweather-dss/15min/BKC_DSS_15min_2026-09-02.csv
s3://<bucket>/openweather-dss/1hour/BKC_DSS_1hour_2026-09-02.csv
```

The IAM user needs only `s3:PutObject`, `s3:GetObject` and `s3:ListBucket`,
scoped to that one bucket.

## Keeping a free instance awake

Render spins a free service down after 15 minutes without **inbound** traffic.
Outbound calls to OpenWeather do not count, so the collector cannot keep itself
alive just by working: the process making those calls is what gets stopped.

Two defences, and you want both:

1. **External pinger (primary).** Point cron-job.org or UptimeRobot at
   `https://<your-service>.onrender.com/health` every 10 minutes.
2. **Self-ping (secondary).** The service requests its own public URL every 10
   minutes using `RENDER_EXTERNAL_URL`, which Render sets automatically. This
   only helps while the service is already awake -- a sleeping instance cannot
   ping itself -- so it is a backstop, not a substitute for the external pinger.

Free instances also get 750 hours a month. One service running continuously uses
about 730, so it fits, but a second free service would exceed the allowance.

## Deploying to Render

1. Push this folder to a Git repository.
2. In Render, create a new **Blueprint** from the repo. `render.yaml` defines the
   service, the disk and the start command.
3. Set `OPENWEATHER_API_KEY` in the Render dashboard. It is deliberately marked
   `sync: false` so the key never lives in git.
4. Deploy, then open `/health`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENWEATHER_API_KEY` | *(required)* | API key |
| `DATA_DIR` | `./data` | working directory for CSVs; ephemeral on Render's free plan |
| `S3_BUCKET` | blank | durable store. Blank disables S3 and the service runs local-only |
| `S3_PREFIX` | `openweather-dss` | folder inside the bucket |
| `AWS_REGION` | `ap-south-1` | bucket region |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | *(required for S3)* | IAM user credentials |
| `S3_SYNC_MINUTES` | `5` | how often to upload |
| `KEEPALIVE_MINUTES` | `10` | self-ping interval |
| `COLLECT_START` / `COLLECT_END` | blank | optional IST window, e.g. `06:30` / `18:00`. Blank collects around the clock |
| `RUN_SCHEDULER` | `1` | set to `0` to run the web layer without collecting |

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # add your key
./run_local.sh              # http://localhost:10000
```

## API usage

Five sites once a minute, around the clock, is 7,200 calls a day. OpenWeather's
free tier allows 60 calls a minute (we use 5) and a million a month (we use
about 216,000), so this fits with room to spare.

## Why one worker

The scheduler runs inside the web process. The start command pins gunicorn to
`--workers 1` on purpose: a second worker would mean a second scheduler, and
every minute would be collected twice.
