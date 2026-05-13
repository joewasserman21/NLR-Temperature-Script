# 8760 Average Temperature

Small Flask UI for pulling hourly air temperature from NREL's NSRDB and
averaging each hour-of-year across N historical years.

## Local setup

```bash
pip install -r requirements.txt
cp .env.example .env       # then edit .env and fill in your NREL key + email
python app.py
```

Open <http://localhost:5000>.

Get a free NREL API key at <https://developer.nlr.gov/signup/>.

## Deploy to Railway

1. Push this repo to GitHub (public or private — doesn't matter).
2. Go to <https://railway.app> → **New Project** → **Deploy from GitHub repo** → pick your repo.
3. Railway auto-detects Python, installs `requirements.txt`, and reads `Procfile` for the start command.
4. In the Railway dashboard → **Variables** → add:
   - `NREL_API_KEY` — your key
   - `NREL_EMAIL` — your email (NREL requires one with every request)
   - `NREL_NAME` *(optional)*
   - `NREL_AFFIL` *(optional)*
5. Settings → **Networking** → **Generate Domain**. Visit the URL it gives you.

That's it. Each push to the linked branch triggers an auto-redeploy.

## Notes

- Uses NREL's **GOES Aggregated v4** endpoint
  (`nsrdb-GOES-aggregated-v4-0-0-download.csv`). NREL migrated to
  `developer.nlr.gov` on April 30, 2026 — the old `developer.nrel.gov` host
  returns 410 Gone.
- Location input accepts a **city name** (`Indianapolis, IN`) or coordinates —
  decimal (`39.613744, -85.659600`) or DMS (`39°36'49"N 85°39'34"W`).
- City names are geocoded with OpenStreetMap Nominatim (free, no key).
- Default range: 10 most recent years (currently 2015–2024).
- Output CSV: 8760 rows · `hour_of_year, month, day, hour, avg_temp_C, avg_temp_F` · integers.
- NREL rate-limits CSV pulls to 1/sec, so 10 years ≈ 15 seconds.
- Generated CSVs are written to `downloads/`. On Railway this directory is
  ephemeral (wiped on redeploy), which is fine for this use case since users
  download files immediately after generation.

## Files

```
app.py              Flask backend (parsing, geocoding, NREL fetch)
templates/
  index.html        React UI
static/
  logo.png, logo2.png
Procfile            Railway start command (gunicorn)
requirements.txt    Pinned deps incl. gunicorn
.python-version     Pinned Python (3.12)
.env.example        Template for local .env
.gitignore
```

## Gunicorn config (Procfile)

```
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 300
```

`--workers 1` is intentional: the app uses a background thread + an in-process
progress dict. Multiple workers wouldn't share that state, so progress polling
could land on a worker that hasn't run your job. One worker with 4 threads
handles concurrent users fine for this kind of light tool.
