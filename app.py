"""
NREL 8760 Average Temperature Tool
==================================
Pulls hourly air temperature from NREL NSRDB (GOES Aggregated v4 endpoint),
averages each hour-of-year across the previous N years, rounds to int,
and serves the result as a downloadable CSV.

Run:
    pip install -r requirements.txt
    python app.py
    # Open http://localhost:5000
"""
from flask import Flask, request, jsonify, send_file, send_from_directory
import requests
import pandas as pd
import threading
import re
import os
from io import StringIO

app = Flask(__name__)

# ─── Defaults (overridable via UI or env vars) ────────────────────────────
# Set these in Railway → Variables, or in a local .env file.
DEFAULT_API_KEY = os.environ.get("NREL_API_KEY", "")
DEFAULT_EMAIL   = os.environ.get("NREL_EMAIL",   "")
DEFAULT_NAME    = os.environ.get("NREL_NAME",    "Joe Wasserman")
DEFAULT_AFFIL   = os.environ.get("NREL_AFFIL",   "DESRI")

# NREL migrated from developer.nrel.gov to developer.nlr.gov on April 30, 2026.
# The old host now returns 410 Gone.
NSRDB_URL = "https://developer.nlr.gov/api/nsrdb/v2/solar/nsrdb-GOES-aggregated-v4-0-0-download.csv"

# Most recent year currently available in NSRDB v4 (data lags ~1 year)
DEFAULT_END_YEAR = 2024
DEFAULT_N_YEARS  = 10

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ─── Shared progress state ────────────────────────────────────────────────
_progress = {
    "current": 0, "total": 0,
    "status": "idle", "message": "",
    "filename": None, "error": None,
    "head": None, "tail": None,
    "lat": None, "lon": None, "years": None,
}
_lock = threading.Lock()


# ─── Coordinate parser ────────────────────────────────────────────────────
def parse_coordinates(text):
    """
    Parse a coordinate string in decimal or DMS form.
    Returns (lat, lon) tuple of floats.

    Accepts:
      "39.613744, -85.659600"
      "39.613744 -85.659600"
      "39°36'49.5\"N, 85°39'34.6\"W"
      "39 36 49.5 N 85 39 34.6 W"
      "N 39° 36.825', W 85° 39.577'"     (degrees decimal minutes)
      "39.613744° N, 85.659600° W"
    """
    if not text or not text.strip():
        raise ValueError("Empty coordinate string")
    text = text.strip()

    # Fast path: two decimal numbers separated by comma or whitespace
    m = re.match(
        r'^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$',
        text,
    )
    if m:
        return float(m.group(1)), float(m.group(2))

    # DMS / mixed path: split on comma or "and", otherwise split at hemisphere boundary
    parts = re.split(r'[,;]|\s+and\s+', text, maxsplit=1)
    if len(parts) != 2:
        # try splitting after first hemisphere letter
        m2 = re.match(r'(.+?[NnSs])\s+(.+)', text)
        if m2:
            parts = [m2.group(1), m2.group(2)]
        else:
            # try splitting at the boundary where the second number's sign appears
            m3 = re.match(r'(\S.+?)\s+(-?\d.+)$', text)
            if m3:
                parts = [m3.group(1), m3.group(2)]
            else:
                raise ValueError(f"Cannot split coordinate string: {text!r}")

    lat = _parse_dms(parts[0])
    lon = _parse_dms(parts[1])
    return lat, lon


def _parse_dms(s):
    """Parse a single DMS / DDM / decimal coordinate component."""
    s = s.strip()

    # Pick off hemisphere letter (N/S/E/W)
    hemi = None
    hm = re.search(r'([NnSsEeWw])', s)
    if hm:
        hemi = hm.group(1).upper()
        s = re.sub(r'[NnSsEeWw]', '', s)

    # Pick off leading sign
    sign = 1
    s = s.strip()
    if s.startswith('-'):
        sign = -1
        s = s[1:]
    elif s.startswith('+'):
        s = s[1:]

    # Find numeric components (deg, min, sec)
    nums = re.findall(r'\d+(?:\.\d+)?', s)
    if not nums:
        raise ValueError(f"No numeric value found in {s!r}")

    deg = float(nums[0])
    minutes = float(nums[1]) if len(nums) > 1 else 0.0
    seconds = float(nums[2]) if len(nums) > 2 else 0.0

    val = deg + minutes / 60.0 + seconds / 3600.0

    if hemi:
        # hemisphere wins over a stray minus sign
        val = abs(val)
        if hemi in ("S", "W"):
            val = -val
    else:
        val *= sign

    return val


# ─── Geocoder (OpenStreetMap Nominatim) ───────────────────────────────────
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim requires a real, identifying User-Agent per their usage policy.
NOMINATIM_UA = "8760-temp-tool/1.0 (joe.wasserman@desri.com)"

# Tiny in-process cache so the as-you-type field doesn't re-query Nominatim
# for the same string repeatedly.
_geocode_cache = {}


def geocode_place(query):
    """
    Resolve a city/place name to (lat, lon, display_name) via Nominatim.
    Raises ValueError on no match or HTTP failure.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("Empty location")
    if q in _geocode_cache:
        return _geocode_cache[q]

    r = requests.get(
        NOMINATIM_URL,
        params={"q": q, "format": "json", "limit": 1, "addressdetails": 0},
        headers={"User-Agent": NOMINATIM_UA, "Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        raise ValueError(f"No location found for {q!r}")
    top = results[0]
    out = (float(top["lat"]), float(top["lon"]), top.get("display_name", q))
    _geocode_cache[q] = out
    return out


def resolve_location(text):
    """
    Accepts either coordinates (decimal or DMS) or a city/place name.
    Returns (lat, lon, display_name_or_None).
    Tries coordinate parsing first; falls back to geocoding if that fails.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty location")
    try:
        lat, lon = parse_coordinates(text)
        return lat, lon, None
    except Exception:
        # Not coordinates — try geocoding
        return geocode_place(text)


# ─── Background fetch job ─────────────────────────────────────────────────
def fetch_job(lat, lon, years, api_key, email, full_name, affiliation):
    """Pull one year at a time, then average across years per hour-of-year."""
    global _progress
    try:
        frames = []
        for i, year in enumerate(years):
            with _lock:
                _progress["current"] = i
                _progress["status"] = "running"
                _progress["message"] = f"Fetching {year} ({i+1}/{len(years)})…"

            params = {
                "api_key": api_key,
                "wkt": f"POINT({lon} {lat})",
                "names": year,
                "interval": 60,
                "attributes": "air_temperature",
                "utc": "false",
                "leap_day": "false",
                "email": email,
                "full_name": full_name,
                "affiliation": affiliation,
                "reason": "research",
                "mailing_list": "false",
            }
            r = requests.get(NSRDB_URL, params=params, timeout=180)

            # NREL sometimes returns JSON errors with 200/422. Sniff it.
            ct = r.headers.get("Content-Type", "")
            if "json" in ct.lower() or r.text.lstrip().startswith("{"):
                try:
                    j = r.json()
                    errs = j.get("errors") or [j.get("outputs", {}).get("message", "Unknown error")]
                    raise RuntimeError(f"NREL API ({year}): {'; '.join(map(str, errs))}")
                except ValueError:
                    pass

            r.raise_for_status()

            df = pd.read_csv(StringIO(r.text), skiprows=2)
            if len(df) < 8000:
                raise RuntimeError(f"Year {year} returned only {len(df)} rows (expected ~8760)")
            df = df.head(8760).copy()  # guard against any extra rows
            df["hour_of_year"] = range(len(df))
            df["year"] = year
            frames.append(df[["year", "hour_of_year", "Month", "Day", "Hour", "Temperature"]])

        with _lock:
            _progress["current"] = len(years)
            _progress["message"] = "Averaging across years…"

        all_yrs = pd.concat(frames, ignore_index=True)

        avg = (
            all_yrs.groupby("hour_of_year")
                   .agg(
                       month=("Month", "first"),
                       day=("Day", "first"),
                       hour=("Hour", "first"),
                       avg_temp_C=("Temperature", "mean"),
                   )
                   .reset_index()
        )

        # Round to nearest integer
        avg["avg_temp_C"] = avg["avg_temp_C"].round().astype(int)
        avg["avg_temp_F"] = (avg["avg_temp_C"] * 9 / 5 + 32).round().astype(int)

        y1, y2 = min(years), max(years)
        filename = f"avg_8760_lat{lat:.4f}_lon{lon:.4f}_{y1}-{y2}.csv"
        path = os.path.join(OUTPUT_DIR, filename)
        avg.to_csv(path, index=False)

        head = avg.head(5).to_dict(orient="records")
        tail = avg.tail(5).to_dict(orient="records")

        with _lock:
            _progress["status"] = "done"
            _progress["filename"] = filename
            _progress["message"] = f"Done · {len(years)} years averaged"
            _progress["head"] = head
            _progress["tail"] = tail
            _progress["years"] = list(years)
            _progress["lat"] = lat
            _progress["lon"] = lon

    except Exception as e:
        with _lock:
            _progress["status"] = "error"
            _progress["error"] = str(e)
            _progress["message"] = f"Error: {e}"


# ─── Routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    # Serve raw HTML — bypass Jinja so React's {{...}} isn't interpreted as templating
    return send_from_directory(os.path.join(app.root_path, "templates"), "index.html")


@app.route("/logo.png")
def logo():
    return send_from_directory(os.path.join(app.root_path, "static"), "logo.png")


@app.route("/logo2.png")
def logo2():
    return send_from_directory(os.path.join(app.root_path, "static"), "logo2.png")


@app.route("/api/parse_coords", methods=["POST"])
def api_parse_coords():
    data = request.get_json(force=True, silent=True) or {}
    try:
        lat, lon, display_name = resolve_location(data.get("text", ""))
        return jsonify({"lat": lat, "lon": lon, "display_name": display_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/run", methods=["POST"])
def api_run():
    global _progress
    data = request.get_json(force=True, silent=True) or {}

    try:
        lat, lon, _display = resolve_location(data.get("coords", ""))
    except Exception as e:
        return jsonify({"error": f"Location lookup failed: {e}"}), 400

    try:
        end_year = int(data.get("end_year", DEFAULT_END_YEAR))
        n_years  = int(data.get("n_years",  DEFAULT_N_YEARS))
    except (TypeError, ValueError):
        return jsonify({"error": "end_year and n_years must be integers"}), 400

    if n_years < 1 or n_years > 27:
        return jsonify({"error": "n_years must be between 1 and 27"}), 400
    if end_year < 1998 or end_year > 2024:
        return jsonify({"error": "end_year must be between 1998 and 2024"}), 400

    years = list(range(end_year - n_years + 1, end_year + 1))

    api_key = data.get("api_key") or DEFAULT_API_KEY
    email   = data.get("email")   or DEFAULT_EMAIL
    full_name   = data.get("full_name")   or DEFAULT_NAME
    affiliation = data.get("affiliation") or DEFAULT_AFFIL

    if not api_key:
        return jsonify({"error": "NREL_API_KEY is not set. In Railway, add it under Variables. Locally, set it in your shell or a .env file."}), 400
    if not email:
        return jsonify({"error": "NREL_EMAIL is not set. NREL requires an email with every request."}), 400

    with _lock:
        if _progress["status"] == "running":
            return jsonify({"error": "A job is already running"}), 409
        _progress.update({
            "current": 0, "total": len(years),
            "status": "running",
            "message": f"Starting · {len(years)} years ({years[0]}–{years[-1]})",
            "filename": None, "error": None,
            "head": None, "tail": None,
            "lat": lat, "lon": lon, "years": list(years),
        })

    t = threading.Thread(
        target=fetch_job,
        args=(lat, lon, years, api_key, email, full_name, affiliation),
        daemon=True,
    )
    t.start()

    return jsonify({"ok": True, "lat": lat, "lon": lon, "years": years})


@app.route("/api/progress")
def api_progress():
    with _lock:
        return jsonify(dict(_progress))


@app.route("/api/download/<path:filename>")
def api_download(filename):
    # basic safety: no path traversal
    safe = os.path.basename(filename)
    path = os.path.join(OUTPUT_DIR, safe)
    if not os.path.isfile(path):
        return jsonify({"error": "file not found"}), 404
    return send_file(path, as_attachment=True, download_name=safe)


if __name__ == "__main__":
    # Local dev only — Railway/production uses gunicorn via the Procfile.
    port = int(os.environ.get("PORT", 5000))
    print("─" * 56)
    print(" NREL 8760 Average Temperature Tool")
    print(f" Open: http://localhost:{port}")
    print("─" * 56)
    app.run(host="0.0.0.0", port=port, debug=True)
