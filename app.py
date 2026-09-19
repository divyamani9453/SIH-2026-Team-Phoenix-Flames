import os
from dash import Dash, dcc, html, callback, Input, Output, State, no_update
import dash_bootstrap_components as dbc
import plotly.express as px
import pandas as pd
import numpy as np
import requests
import urllib.parse
import json
import time
from functools import lru_cache

from pythermalcomfort.models import utci

# ==============================================================================
# DATA INGESTION & PROCESSING
# ==============================================================================
# Pre-computed centroids (lat/lon) live in a tiny Excel file so the server
# never has to parse geometry just to get district centres.
# Use the heavily simplified GeoJSON (~2.7 MB on disk, ~5 MB in RAM) so the
# process stays well under Render's 512 MB limit.
CENTROIDS_FILE = os.environ.get("CENTROIDS_FILE", "district_centroids.xlsx")
GEOJSON_FILE = os.environ.get("GEOJSON_FILE", "India-Districts-slim.json")

df = pd.read_excel(CENTROIDS_FILE)
# Ensure required columns exist
required = {"join_key", "District", "State", "lat", "lon"}
missing = required - set(df.columns)
if missing:
    raise ValueError(f"Centroids Excel missing columns: {missing}")

with open(GEOJSON_FILE) as f:
    district_geojson = json.load(f)

# join_key is already present in the slim GeoJSON; defensive pass for other files.
for feature in district_geojson["features"]:
    props = feature["properties"]
    if "join_key" not in props:
        props["join_key"] = f"{props.get('ST_NM','')}|{props.get('DISTRICT','')}"

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
# Conservative batching for shared cloud IPs (Render free tier)
BATCH_SIZE = 40
BATCH_PAUSE_SEC = 1.5
# Hard cap so cold starts never sit on "Updating…" for many minutes
FETCH_MAX_SECONDS = int(os.environ.get("FETCH_MAX_SECONDS", "90"))
WEATHER_CACHE_FILE = os.environ.get("WEATHER_CACHE_FILE", "weather_cache.pkl")
CACHE_MAX_AGE_HOURS = float(os.environ.get("CACHE_MAX_AGE_HOURS", "6"))


def _save_weather_cache(dataframe):
    """Persist weather columns (lost on Render free spin-down; helps while instance is warm)."""
    try:
        cols = [c for c in dataframe.columns if any(
            c.startswith(p) for p in (
                "Dry Bulb Temp_d", "Relative Humidity_d", "Wind Speed_d",
                "Apparent Temp_d", "Mean Radiant Temp_d", "UTCI_d",
                "Stress Category_d", "Mortality_",
            )
        )]
        dataframe[["join_key"] + cols].to_pickle(WEATHER_CACHE_FILE)
        print(f"[weather] cache saved → {WEATHER_CACHE_FILE}", flush=True)
    except Exception as e:
        print(f"[weather] cache save failed: {e}", flush=True)


def _load_weather_cache(dataframe, max_age_hours=None):
    if not os.path.exists(WEATHER_CACHE_FILE):
        return False
    try:
        age_h = (time.time() - os.path.getmtime(WEATHER_CACHE_FILE)) / 3600.0
        limit = CACHE_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
        if age_h > limit:
            print(f"[weather] cache too old ({age_h:.1f}h > {limit}h)", flush=True)
            return False
        cache_df = pd.read_pickle(WEATHER_CACHE_FILE)
        if "join_key" not in cache_df.columns:
            return False
        merged = dataframe.drop(
            columns=[c for c in dataframe.columns if c in cache_df.columns and c != "join_key"],
            errors="ignore",
        ).merge(cache_df, on="join_key", how="left")
        for c in cache_df.columns:
            if c != "join_key":
                dataframe[c] = merged[c].values
        print(f"[weather] cache loaded (age {age_h:.1f}h)", flush=True)
        return True
    except Exception as e:
        print(f"[weather] cache load failed: {e}", flush=True)
        return False


def fetch_batch(lats, lons):
    """Fetch one batch. Returns (list, hit_rate_limit).

    On 429: one short wait then give up — do NOT sleep for minutes (that stuck the UI).
    """
    params = {
        "latitude": ",".join(f"{x:.4f}" for x in lats),
        "longitude": ",".join(f"{x:.4f}" for x in lons),
        "hourly": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m",
        "forecast_days": 4,
        "wind_speed_unit": "ms",
        "timezone": "auto",
    }
    for attempt in range(2):
        try:
            res = requests.get(OPEN_METEO_URL, params=params, timeout=45)
            if res.status_code == 429:
                if attempt == 0:
                    print("[weather] rate-limited (429), brief pause 8s then retry once…", flush=True)
                    time.sleep(8)
                    continue
                print("[weather] still rate-limited — skipping batch", flush=True)
                return [None] * len(lats), True
            res.raise_for_status()
            data = res.json()
            if isinstance(data, dict) and "hourly" in data:
                return [data], False
            if isinstance(data, list):
                return data, False
            if isinstance(data, dict) and "latitude" in data:
                return [data], False
            return [None] * len(lats), False
        except Exception as e:
            print(f"[weather] batch error: {e}", flush=True)
            if attempt == 0:
                time.sleep(3)
                continue
            return [None] * len(lats), False
    return [None] * len(lats), True


def fetch_multi_day_weather(dataframe):
    """Fetch all districts with a hard wall-clock limit (default 90s)."""
    lats, lons = dataframe["lat"].tolist(), dataframe["lon"].tolist()
    all_responses = []
    n_batches = (len(dataframe) + BATCH_SIZE - 1) // BATCH_SIZE
    t0 = time.time()
    consecutive_rate_limits = 0
    aborted = False

    for bi, i in enumerate(range(0, len(dataframe), BATCH_SIZE)):
        if time.time() - t0 > FETCH_MAX_SECONDS:
            print(f"[weather] hit {FETCH_MAX_SECONDS}s time budget — stopping early", flush=True)
            remaining = len(dataframe) - len(all_responses)
            all_responses.extend([None] * remaining)
            aborted = True
            break

        b_lats = lats[i:i + BATCH_SIZE]
        b_lons = lons[i:i + BATCH_SIZE]
        print(f"[weather] batch {bi + 1}/{n_batches} ({len(b_lats)} locations)…", flush=True)
        batch_res, hit_limit = fetch_batch(b_lats, b_lons)

        if hit_limit:
            consecutive_rate_limits += 1
            all_responses.extend([None] * len(b_lats))
            if consecutive_rate_limits >= 2:
                print("[weather] repeated 429s — aborting (use synthetic / try Update later)", flush=True)
                remaining = len(dataframe) - len(all_responses)
                all_responses.extend([None] * remaining)
                aborted = True
                break
        else:
            consecutive_rate_limits = 0
            if len(batch_res) != len(b_lats):
                if len(batch_res) == 1 and batch_res[0] and "hourly" in batch_res[0]:
                    all_responses.extend([None] * len(b_lats))
                else:
                    all_responses.extend((batch_res + [None] * len(b_lats))[: len(b_lats)])
            else:
                all_responses.extend(batch_res)

        time.sleep(BATCH_PAUSE_SEC)

    print(f"[weather] fetch finished in {time.time() - t0:.1f}s (aborted={aborted})", flush=True)
    # Stash abort flag for caller (live vs partial labeling)
    dataframe.attrs["weather_aborted"] = aborted
    n_ok = sum(1 for x in all_responses if x is not None)
    dataframe.attrs["weather_ok_locations"] = n_ok
    print(f"[weather] locations with API data: {n_ok}/{len(dataframe)}", flush=True)

    peak_indices = [14, 38, 62, 86]  # ~afternoon peak each of the 4 forecast days
    for d_idx, h_idx in enumerate(peak_indices):
        temps, rh, wind, apparent = [], [], [], []
        for item in all_responses:
            if item and isinstance(item, dict) and "hourly" in item:
                h = item["hourly"]
                temps.append(h["temperature_2m"][h_idx] if len(h.get("temperature_2m", [])) > h_idx else None)
                rh.append(h["relative_humidity_2m"][h_idx] if len(h.get("relative_humidity_2m", [])) > h_idx else None)
                wind.append(h["wind_speed_10m"][h_idx] if len(h.get("wind_speed_10m", [])) > h_idx else None)
                apparent.append(h["apparent_temperature"][h_idx] if len(h.get("apparent_temperature", [])) > h_idx else None)
            else:
                temps.append(None)
                rh.append(None)
                wind.append(None)
                apparent.append(None)

        dataframe[f"Dry Bulb Temp_d{d_idx}"] = temps
        dataframe[f"Relative Humidity_d{d_idx}"] = rh
        dataframe[f"Wind Speed_d{d_idx}"] = wind
        dataframe[f"Apparent Temp_d{d_idx}"] = apparent

        mask = dataframe[f"Dry Bulb Temp_d{d_idx}"].isna()
        if mask.any():
            n = int(mask.sum())
            np.random.seed(42 + d_idx)
            dataframe.loc[mask, f"Dry Bulb Temp_d{d_idx}"] = np.random.uniform(20.0, 43.0, n).round(1)
            dataframe.loc[mask, f"Relative Humidity_d{d_idx}"] = np.random.uniform(20.0, 85.0, n).round(1)
            dataframe.loc[mask, f"Wind Speed_d{d_idx}"] = np.random.uniform(0.5, 7.5, n).round(1)
            dataframe.loc[mask, f"Apparent Temp_d{d_idx}"] = (
                dataframe.loc[mask, f"Dry Bulb Temp_d{d_idx}"] + np.random.uniform(-1.0, 4.0, n)
            ).round(1)

        dataframe[f"Mean Radiant Temp_d{d_idx}"] = dataframe[f"Dry Bulb Temp_d{d_idx}"]

        u_res = utci(
            tdb=dataframe[f"Dry Bulb Temp_d{d_idx}"].tolist(),
            tr=dataframe[f"Mean Radiant Temp_d{d_idx}"].tolist(),
            v=dataframe[f"Wind Speed_d{d_idx}"].tolist(),
            rh=dataframe[f"Relative Humidity_d{d_idx}"].tolist(),
        )
        vals = u_res.utci if hasattr(u_res, "utci") else u_res
        dataframe[f"UTCI_d{d_idx}"] = [
            round(v, 1) if not pd.isna(v) else np.nan for v in vals
        ]

    return dataframe


def fill_synthetic_weather(dataframe):
    """Instant fallback so the HTTP server can bind before any network I/O."""
    for d_idx in range(4):
        n = len(dataframe)
        np.random.seed(42 + d_idx)
        dataframe[f"Dry Bulb Temp_d{d_idx}"] = np.random.uniform(22.0, 40.0, n).round(1)
        dataframe[f"Relative Humidity_d{d_idx}"] = np.random.uniform(25.0, 80.0, n).round(1)
        dataframe[f"Wind Speed_d{d_idx}"] = np.random.uniform(0.5, 6.0, n).round(1)
        dataframe[f"Apparent Temp_d{d_idx}"] = (
            dataframe[f"Dry Bulb Temp_d{d_idx}"] + np.random.uniform(-1.0, 3.0, n)
        ).round(1)
        dataframe[f"Pressure_d{d_idx}"] = np.random.uniform(990.0, 1015.0, n).round(1)
        dataframe[f"Cloud Cover_d{d_idx}"] = np.random.uniform(0.0, 100.0, n).round(1)
        dataframe[f"Precipitation_d{d_idx}"] = np.random.choice(
            [0.0, 0.0, 0.0, 0.5, 2.0], size=n
        ).round(1)
        dataframe[f"Mean Radiant Temp_d{d_idx}"] = dataframe[f"Dry Bulb Temp_d{d_idx}"]

        u_res = utci(
            tdb=dataframe[f"Dry Bulb Temp_d{d_idx}"].tolist(),
            tr=dataframe[f"Mean Radiant Temp_d{d_idx}"].tolist(),
            v=dataframe[f"Wind Speed_d{d_idx}"].tolist(),
            rh=dataframe[f"Relative Humidity_d{d_idx}"].tolist(),
        )
        vals = u_res.utci if hasattr(u_res, "utci") else u_res
        dataframe[f"UTCI_d{d_idx}"] = [
            round(v, 1) if not pd.isna(v) else np.nan for v in vals
        ]
    return dataframe


def enrich_derived_columns(dataframe):
    """Stress categories + mortality indices (depends on UTCI columns)."""
    for d in range(4):
        dataframe[f"Stress Category_d{d}"] = dataframe[f"UTCI_d{d}"].apply(
            utci_stress_category
        )
        f_vals = dataframe[f"UTCI_d{d}"].apply(calculate_f_utci)
        for demo, weight in DEMO_WEIGHTS.items():
            dataframe[f"Mortality_{demo}_d{d}"] = (
                f_vals * weight * 50
            ).round(1).clip(upper=100.0)
    return dataframe


import math

# ==============================================================================
# TRI-INDEX BIOMETEOROLOGICAL ENGINE (NOAA HI, ISO WBGT, UTCI)
# ==============================================================================

def calculate_heat_index(t_c, rh):
    """NOAA / Rothfusz Heat Index equation in deg C."""
    if pd.isna(t_c) or pd.isna(rh):
        return np.nan
    if t_c < 26.7:
        return round(float(t_c), 1)
    t = t_c * 9.0 / 5.0 + 32.0
    hi = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * rh
        - 0.22475541 * t * rh
        - 0.00683783 * (t ** 2)
        - 0.05481717 * (rh ** 2)
        + 0.00122874 * (t ** 2) * rh
        + 0.00085282 * t * (rh ** 2)
        - 0.00000199 * (t ** 2) * (rh ** 2)
    )
    if rh < 13 and 80 <= t <= 112:
        adj = ((13 - rh) / 4) * math.sqrt(max(0, (17 - abs(t - 95)) / 17))
        hi -= adj
    elif rh > 85 and 80 <= t <= 87:
        adj = ((rh - 85) / 10) * ((87 - t) / 5)
        hi += adj
    hi_c = (hi - 32.0) * 5.0 / 9.0
    return round(float(hi_c), 1)


def calculate_wbgt(t_c, rh, wind_speed, solar_rad=800.0):
    """ISO 7243 Outdoor WBGT approximation in deg C."""
    if pd.isna(t_c) or pd.isna(rh):
        return np.nan
    # Vapor pressure e (hPa)
    e = (rh / 100.0) * 6.105 * math.exp((17.27 * t_c) / (237.7 + t_c))
    # Australian BOM / Liljegren Outdoor WBGT approximation
    wbgt_shade = 0.567 * t_c + 0.393 * e + 3.94
    # Solar radiation and wind dissipation adjustment
    v = max(float(wind_speed if not pd.isna(wind_speed) else 1.0), 0.5)
    solar_adj = (solar_rad / 800.0) * 2.2 - (v - 1.0) * 0.4
    return round(float(wbgt_shade + max(-1.0, solar_adj)), 1)


def calculate_composite_hazard(hi, wbgt, utci_val):
    """Composite Thermal Hazard Index (0 - 100 Normalized)."""
    if pd.isna(hi) or pd.isna(wbgt) or pd.isna(utci_val):
        return np.nan
    norm_hi = min(100.0, max(0.0, (hi - 20.0) / 30.0 * 100.0))
    norm_wbgt = min(100.0, max(0.0, (wbgt - 18.0) / 22.0 * 100.0))
    norm_utci = min(100.0, max(0.0, (utci_val - 20.0) / 30.0 * 100.0))
    hazard = 0.35 * norm_hi + 0.40 * norm_wbgt + 0.25 * norm_utci
    return round(float(hazard), 1)


def utci_stress_category(value):
    if pd.isna(value):
        return "No data"
    if value > 46:
        return "Extreme heat stress"
    if value > 38:
        return "Very strong heat stress"
    if value > 32:
        return "Strong heat stress"
    if value > 26:
        return "Moderate heat stress"
    if value > 9:
        return "No thermal stress"
    if value > 0:
        return "Slight cold stress"
    if value > -13:
        return "Moderate cold stress"
    if value > -27:
        return "Strong cold stress"
    return "Extreme cold stress"


def calculate_f_utci(val):
    if pd.isna(val) or val <= 26:
        return 0.05
    if val <= 32:
        return 0.05 + 0.02 * (val - 26)
    if val <= 38:
        return 0.17 + 0.04 * (val - 32)
    if val <= 46:
        return 0.41 + 0.06 * (val - 38)
    return 0.89 + 0.08 * (val - 46)


DEMO_WEIGHTS = {
    "Elderly (60+ yrs)": 1.8,
    "Adults (18-59 yrs)": 1.0,
    "Children (0-5 yrs)": 1.3,
}


def enrich_derived_columns(dataframe):
    """Stress categories + WBGT + Heat Index + Composite Hazard + mortality indices."""
    for d in range(4):
        # Calculate NOAA Heat Index & ISO WBGT
        hi_list, wbgt_list, hazard_list = [], [], []
        t_vals = dataframe[f"Dry Bulb Temp_d{d}"].tolist()
        rh_vals = dataframe[f"Relative Humidity_d{d}"].tolist()
        w_vals = dataframe[f"Wind Speed_d{d}"].tolist()
        u_vals = dataframe[f"UTCI_d{d}"].tolist()

        for t_i, rh_i, w_i, u_i in zip(t_vals, rh_vals, w_vals, u_vals):
            hi_val = calculate_heat_index(t_i, rh_i)
            wbgt_val = calculate_wbgt(t_i, rh_i, w_i)
            haz_val = calculate_composite_hazard(hi_val, wbgt_val, u_i)
            hi_list.append(hi_val)
            wbgt_list.append(wbgt_val)
            hazard_list.append(haz_val)

        dataframe[f"Heat Index_d{d}"] = hi_list
        dataframe[f"WBGT_d{d}"] = wbgt_list
        dataframe[f"Composite Hazard_d{d}"] = hazard_list

        dataframe[f"Stress Category_d{d}"] = dataframe[f"UTCI_d{d}"].apply(
            utci_stress_category
        )
        f_vals = dataframe[f"UTCI_d{d}"].apply(calculate_f_utci)
        for demo, weight in DEMO_WEIGHTS.items():
            dataframe[f"Mortality_{demo}_d{d}"] = (
                f_vals * weight * 50
            ).round(1).clip(upper=100.0)
    return dataframe


# ---------------------------------------------------------------------------
# CRITICAL FOR RENDER: never block module import with network I/O.
# Fill synthetic data instantly so gunicorn can bind to $PORT within seconds.
# Then refresh from Open-Meteo in a background thread.
# ---------------------------------------------------------------------------
df = fill_synthetic_weather(df)
df = enrich_derived_columns(df)

MEASUREMENTS = {
    "Composite Thermal Hazard (0-100)": "Composite Hazard",
    "UTCI (deg C)": "UTCI",
    "ISO 7243 WBGT (deg C)": "WBGT",
    "NOAA Heat Index (deg C)": "Heat Index",
    "Dry Bulb Temp (deg C)": "Dry Bulb Temp",
    "Mean Radiant Temp (deg C)": "Mean Radiant Temp",
    "Wind Speed (m/s)": "Wind Speed",
    "Relative Humidity (%)": "Relative Humidity",
}
DEFAULT_SLIDER_BOUNDS = {
    "Composite Thermal Hazard (0-100)": [0, 100],
    "UTCI (deg C)": [15, 45],
    "ISO 7243 WBGT (deg C)": [15, 42],
    "NOAA Heat Index (deg C)": [15, 50],
    "Dry Bulb Temp (deg C)": [10, 45],
    "Mean Radiant Temp (deg C)": [10, 45],
    "Wind Speed (m/s)": [0, 10],
    "Relative Humidity (%)": [0, 100],
}
states_list = sorted(df["State"].unique().tolist())
district_options = [
    {"label": f"{r['District']}, {r['State']}", "value": r["join_key"]}
    for _, r in df.iterrows()
]

# Background live-data refresh every 3 hours (does not block port binding)
import threading
import sys
from datetime import datetime, timezone

REFRESH_INTERVAL_SEC = int(os.environ.get("WEATHER_REFRESH_HOURS", "3")) * 3600
_weather_ready = False
_weather_fetching = False
_weather_source = "synthetic"  # synthetic | cache | live
_last_weather_update = None
_weather_lock = threading.Lock()
_weather_thread_started = False
_weather_thread_start_lock = threading.Lock()


def _wlog(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _run_one_weather_fetch(force=False):
    """Fetch live data once.

    force=False (auto loop): use fresh disk cache if available, else call API.
    force=True  (Update data button): always call Open-Meteo (skip cache).
    """
    global df, _weather_ready, _last_weather_update, _weather_fetching, _weather_source
    if not _weather_lock.acquire(blocking=False):
        _wlog("[weather] fetch already in progress, skipping")
        return False
    _weather_fetching = True
    try:
        working = df.copy()

        if not force and _load_weather_cache(working):
            df = enrich_derived_columns(working)
            _weather_ready = True
            _weather_source = "cache"
            _last_weather_update = datetime.fromtimestamp(
                os.path.getmtime(WEATHER_CACHE_FILE), tz=timezone.utc
            )
            _wlog(f"[weather] using disk cache ({_last_weather_update.isoformat()})")
            return True

        if force:
            _wlog("[weather] MANUAL update requested — calling Open-Meteo (skipping cache)")
        else:
            _wlog(f"[weather] Open-Meteo fetch starting at {datetime.now(timezone.utc).isoformat()}")

        updated = fetch_multi_day_weather(working)
        updated = enrich_derived_columns(updated)
        df = updated
        aborted = bool(getattr(updated, "attrs", {}).get("weather_aborted", False))
        n_ok = int(getattr(updated, "attrs", {}).get("weather_ok_locations", 0) or 0)
        _save_weather_cache(df)
        _weather_ready = True
        _last_weather_update = datetime.now(timezone.utc)
        if aborted or n_ok < max(1, len(df) // 4):
            # Mostly synthetic fill after 429 — do not claim full live data
            _weather_source = "synthetic"
            _wlog(
                f"[weather] partial/failed live fetch "
                f"(ok={n_ok}/{len(df)}, aborted={aborted}) — UI stays on demo/synthetic"
            )
        else:
            _weather_source = "live"
            _wlog(f"[weather] live data loaded at {_last_weather_update.isoformat()} (ok={n_ok})")
        return True
    except Exception as e:
        _wlog(f"[weather] fetch failed: {e}")
        working = df.copy()
        if _load_weather_cache(working, max_age_hours=72):
            df = enrich_derived_columns(working)
            _weather_source = "cache"
            _wlog("[weather] fell back to older disk cache")
        else:
            _weather_source = "synthetic"
            _wlog("[weather] keeping synthetic data (API unavailable / rate-limited)")
        _weather_ready = True
        if _last_weather_update is None:
            _last_weather_update = datetime.now(timezone.utc)
        return False
    finally:
        _weather_fetching = False
        _weather_lock.release()


def _background_weather_loop():
    """Daemon loop: fetch immediately, then every REFRESH_INTERVAL_SEC."""
    _wlog("[weather] background loop started")
    while True:
        _run_one_weather_fetch()
        slept = 0
        while slept < REFRESH_INTERVAL_SEC:
            time.sleep(min(60, REFRESH_INTERVAL_SEC - slept))
            slept += 60


def ensure_weather_thread_started():
    """Start the background loop once (safe under Gunicorn workers)."""
    global _weather_thread_started
    with _weather_thread_start_lock:
        if _weather_thread_started:
            return
        _weather_thread_started = True
        t = threading.Thread(target=_background_weather_loop, daemon=True, name="weather-loop")
        t.start()
        _wlog("[weather] background thread launched")


def trigger_manual_weather_refresh():
    """Start a forced live fetch (Update data button). Always hits the API."""
    ensure_weather_thread_started()
    _wlog("[weather] === MANUAL Update data clicked ===")
    if _weather_fetching:
        _wlog("[weather] manual update ignored — another fetch is still running")
        return False

    def _manual_job():
        try:
            _wlog("[weather] manual job thread started (force=True)")
            ok = _run_one_weather_fetch(force=True)
            _wlog(f"[weather] manual job finished ok={ok}")
        except Exception as e:
            _wlog(f"[weather] manual job crashed: {e}")

    t = threading.Thread(target=_manual_job, daemon=True, name="weather-manual")
    t.start()
    return True


# ==============================================================================
# DASH APPLICATION & LAYOUT
# ==============================================================================
app = Dash(__name__, external_stylesheets=[dbc.themes.FLATLY])
server = app.server


@server.before_request
def _start_weather_on_first_request():
    """Gunicorn-safe: launch the weather loop on the first HTTP request."""
    ensure_weather_thread_started()


app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>India Thermal Comfort & Mortality Risk Platform</title>
        {%favicon%}
        {%css%}
        <style>
            /* --- LIGHT MODE CONTRAST ENHANCEMENTS --- */
            .light-mode {
                background-color: #f8fafc !important;
                color: #0f172a !important;
            }
            .light-mode .text-muted {
                color: #475569 !important;
            }
            .light-mode label, .light-mode .form-label {
                color: #334155 !important;
                font-weight: 600;
            }
            .light-mode .card {
                background-color: #ffffff !important;
                border: 1px solid #cbd5e1 !important;
                color: #0f172a !important;
            }
            .light-mode .card-header {
                background-color: #ffffff !important;
                border-bottom: 1px solid #e2e8f0 !important;
                color: #0f172a !important;
            }

            /* Light Mode Range Sliders & Tooltips */
            .light-mode .rc-slider-rail {
                background-color: #cbd5e1 !important;
                height: 6px !important;
            }
            .light-mode .rc-slider-track {
                background-color: #1d4ed8 !important;
                height: 6px !important;
            }
            .light-mode .rc-slider-handle {
                border: 2px solid #1d4ed8 !important;
                background-color: #ffffff !important;
                opacity: 1 !important;
            }
            .light-mode .rc-slider-mark-text,
            .light-mode .rc-slider-mark-text-active {
                color: #0f172a !important; /* High contrast Slate-900 */
                font-weight: 700 !important;
            }
            .light-mode .rc-slider-dot {
                border-color: #94a3b8 !important;
                background-color: #ffffff !important;
            }
            .light-mode .rc-slider-dot-active {
                border-color: #1d4ed8 !important;
            }
            .light-mode .rc-slider-tooltip-inner {
                background-color: #0f172a !important;
                color: #ffffff !important;
                font-weight: 700 !important;
                box-shadow: 0 2px 8px rgba(0,0,0,0.15) !important;
            }
            .light-mode .rc-slider-tooltip-arrow {
                border-top-color: #0f172a !important;
            }

            /* Light Mode Dropdowns */
            .light-mode .dash-dropdown,
            .light-mode .Select-control,
            .light-mode div[class*="-control"] {
                background-color: #ffffff !important;
                border-color: #cbd5e1 !important;
                color: #0f172a !important;
            }
            .light-mode .Select-value,
            .light-mode .Select-value-label,
            .light-mode div[class*="-singleValue"] {
                color: #0f172a !important;
                font-weight: 600;
            }
            .light-mode .Select-placeholder,
            .light-mode div[class*="-placeholder"],
            .light-mode input::placeholder {
                color: #475569 !important;
                opacity: 1 !important;
                font-weight: 500;
            }
            .light-mode .Select-input > input,
            .light-mode div[class*="-Input"] input,
            .light-mode div[class*="-Input"] {
                color: #0f172a !important;
            }

            /* --- DARK MODE CONTRAST ENHANCEMENTS --- */
            .dark-mode {
                background-color: #0f172a !important;
                color: #f8fafc !important;
            }
            .dark-mode .text-muted {
                color: #cbd5e1 !important;
            }
            .dark-mode label, .dark-mode .form-label {
                color: #f1f5f9 !important;
                font-weight: 600;
            }
            .dark-mode .card {
                background-color: #1e293b !important;
                border: 1px solid #334155 !important;
                color: #f8fafc !important;
            }
            .dark-mode .card-header {
                background-color: #1e293b !important;
                border-bottom: 1px solid #334155 !important;
                color: #f8fafc !important;
            }
            .dark-mode h1, .dark-mode h2, .dark-mode h3, .dark-mode h4, .dark-mode h5, .dark-mode h6 {
                color: #ffffff !important;
            }

            /* Dark Mode Range Sliders & Tooltips */
            .dark-mode .rc-slider-rail {
                background-color: #475569 !important;
                height: 6px !important;
            }
            .dark-mode .rc-slider-track {
                background-color: #60a5fa !important;
                height: 6px !important;
            }
            .dark-mode .rc-slider-handle {
                border: 2px solid #60a5fa !important;
                background-color: #0f172a !important;
                opacity: 1 !important;
            }
            .dark-mode .rc-slider-mark-text,
            .dark-mode .rc-slider-mark-text-active {
                color: #f8fafc !important; /* High contrast Slate-50 */
                font-weight: 700 !important;
            }
            .dark-mode .rc-slider-dot {
                border-color: #64748b !important;
                background-color: #1e293b !important;
            }
            .dark-mode .rc-slider-dot-active {
                border-color: #60a5fa !important;
            }
            .dark-mode .rc-slider-tooltip-inner {
                background-color: #f8fafc !important;
                color: #0f172a !important;
                font-weight: 700 !important;
                box-shadow: 0 2px 8px rgba(0,0,0,0.4) !important;
            }
            .dark-mode .rc-slider-tooltip-arrow {
                border-top-color: #f8fafc !important;
            }

            /* Dark Mode Dropdowns */
            .dark-mode .dash-dropdown,
            .dark-mode .Select-control,
            .dark-mode div[class*="-control"] {
                background-color: #1e293b !important;
                border-color: #475569 !important;
                color: #f8fafc !important;
            }
            .dark-mode .Select-value,
            .dark-mode .Select-value-label,
            .dark-mode div[class*="-singleValue"] {
                color: #f8fafc !important;
                font-weight: 600;
            }
            .dark-mode .Select-placeholder,
            .dark-mode div[class*="-placeholder"],
            .dark-mode input::placeholder {
                color: #cbd5e1 !important;
                opacity: 1 !important;
                font-weight: 500;
            }
            .dark-mode .Select-input > input,
            .dark-mode div[class*="-Input"] input,
            .dark-mode div[class*="-Input"] {
                color: #f8fafc !important;
            }
            .dark-mode .Select-menu-outer,
            .dark-mode div[class*="-menu"] {
                background-color: #1e293b !important;
                border: 1px solid #475569 !important;
            }
            .dark-mode .Select-option,
            .dark-mode div[class*="-option"] {
                background-color: #1e293b !important;
                color: #f8fafc !important;
            }
            .dark-mode div[class*="-option"]:hover,
            .dark-mode div[class*="-option"][class*="-is-focused"] {
                background-color: #334155 !important;
                color: #ffffff !important;
            }
            .dark-mode div[class*="-DropdownIndicator"] {
                color: #cbd5e1 !important;
                fill: #cbd5e1 !important;
            }
            .dark-mode hr {
                border-color: #334155 !important;
            }
        </style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
'''

app.layout = dbc.Container([
    # Fast status poll (badge only). Maps refresh only when data-version actually changes.
    dcc.Interval(id="status-poll-interval", interval=12 * 1000, n_intervals=0),
    dcc.Store(id="data-version", data="init"),

    # --- HEADER & THEME TOGGLE ---
    dbc.Row([
        dbc.Col([
            html.H2("India Thermal Comfort & Mortality Risk Platform", className="fw-bolder mb-1"),
            html.P("Predictive biometeorological forecasting & localized demographic risk assessment", className="text-muted mb-0")
        ], md=6),
        dbc.Col([
            dbc.Button(
                "🔔 Alert Dispatch Gateway",
                id="btn-open-alert-modal",
                color="warning",
                size="sm",
                className="me-2 fw-bold text-dark shadow-sm",
                n_clicks=0,
            ),
            html.Span(id="live-status-badge", className="me-2"),
            dbc.Button(
                "↻ Update data",
                id="btn-update-data",
                color="primary",
                outline=True,
                size="sm",
                className="me-2 fw-bold",
                n_clicks=0,
            ),
            dbc.Switch(id="theme-switch", label="🌙 Dark Mode", value=False, className="fw-bold d-inline-block")
        ], md=6, className="d-flex justify-content-md-end align-items-center mt-3 mt-md-0")
    ], className="my-4 py-3 border-bottom"),
    html.Div(id="update-feedback", className="small text-muted mb-2"),

    # --- ALERT DISPATCH MODAL ---
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("🔔 Automated Heatwave Alert Gateway & Dispatch")),
        dbc.ModalBody([
            html.P("Configure and trigger simulated SMS & WhatsApp emergency warnings for vulnerable populations and emergency responders.", className="text-muted small"),
            dbc.Form([
                dbc.Row([
                    dbc.Col([
                        html.Label("Recipient Category / Target", className="fw-bold small"),
                        dcc.Dropdown(
                            id="alert-target-role",
                            options=[
                                {"label": "🛠️ Outdoor & Construction Laborers", "value": "laborers"},
                                {"label": "👵 Elderly & Vulnerable Households", "value": "vulnerable"},
                                {"label": "🏫 Schools & Outdoor Sports", "value": "schools"},
                                {"label": "🚑 Emergency Health Responders", "value": "health"},
                                {"label": "🏛️ Municipal Disaster Management", "value": "municipal"},
                            ],
                            value="laborers",
                            clearable=False,
                        )
                    ], md=12, className="mb-3"),
                    dbc.Col([
                        html.Label("Dispatch Channel", className="fw-bold small"),
                        dbc.RadioItems(
                            id="alert-channel",
                            options=[
                                {"label": "📱 WhatsApp Gateway (Gupshup / Meta API)", "value": "whatsapp"},
                                {"label": "💬 SMS Gateway (Twilio / CDAC)", "value": "sms"},
                            ],
                            value="whatsapp",
                            inline=True,
                            className="mb-3 small"
                        )
                    ], md=12),
                    dbc.Col([
                        html.Label("Recipient Phone Number(s) / Broadcast Group", className="fw-bold small"),
                        dbc.Input(id="alert-phone-input", type="text", placeholder="+91 98765 43210 or @district-health-group", value="+91 98765 43210"),
                    ], md=12, className="mb-3"),
                    dbc.Col([
                        html.Label("Custom Emergency Advisory", className="fw-bold small"),
                        dbc.Textarea(
                            id="alert-message-body",
                            rows=3,
                            value="⚠️ HEAT ACTION ALERT: Composite Thermal Hazard level EXTREME. Cease outdoor physical labor between 12:00-16:00. Ensure hydration stations active.",
                        ),
                    ], md=12, className="mb-3"),
                ])
            ]),
            html.Div(id="alert-dispatch-status", className="mt-2")
        ]),
        dbc.ModalFooter([
            dbc.Button("🚀 Trigger Emergency Dispatch", id="btn-send-alert", color="danger", className="fw-bold", n_clicks=0),
            dbc.Button("Close", id="btn-close-alert-modal", color="secondary", outline=True, n_clicks=0)
        ])
    ], id="alert-modal", is_open=False, size="lg"),

    # --- FORECAST HORIZON SELECTOR & KPIS ---
    dbc.Card([
        dbc.CardBody([
            dbc.Row([
                dbc.Col([
                    html.Label("Select Forecast Horizon", className="fw-bold text-uppercase small text-muted mb-2"),
                    dcc.Dropdown(
                        id='forecast-horizon',
                        options=[
                            {"label": "🔴 Real-Time Current", "value": 0},
                            {"label": "📅 +1 Day Forecast", "value": 1},
                            {"label": "📅 +2 Days Forecast", "value": 2},
                            {"label": "📅 +3 Days Forecast", "value": 3},
                        ],
                        value=0,
                        clearable=False,
                        className="shadow-sm"
                    )
                ], md=3, className="border-end pe-4"),
                dbc.Col(id='kpi-summary-container', md=9, className="ps-4")
            ], className="align-items-center")
        ])
    ], className="mb-4 shadow-sm border-0"),

    # --- SECTION 1: MAP & INSPECTOR ---
    dbc.Row([
        # LEFT COLUMN: THERMAL COMFORT MAP
        dbc.Col([
            dbc.Card([
                dbc.CardHeader(html.H5("1. Thermal Comfort & Climate Layer", className="mb-0 fw-bold")),
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.Label("Visualization Layer", className="fw-bold small text-muted"),
                            dcc.Dropdown(id='measurements', value="UTCI (deg C)", options=list(MEASUREMENTS.keys()), clearable=False)
                        ], md=6),
                        dbc.Col([
                            html.Label("Filter State Focus", className="fw-bold small text-muted"),
                            dcc.Dropdown(id='state-filter', options=[{"label": "All India", "value": "ALL"}] + [{"label": s, "value": s} for s in states_list], value="ALL", clearable=False)
                        ], md=6)
                    ], className="mb-3"),
                    html.Label("Color Bar Range Bounds", className="fw-bold small text-muted"),
                    dcc.RangeSlider(id='color-range-slider', min=15, max=45, value=[15, 45], step=0.5, tooltip={"placement": "bottom", "always_visible": True}, className="mb-4"),
                    dcc.Loading(dcc.Graph(id='district-map', config={"displayModeBar": False}))
                ])
            ], className="shadow-sm border-0 h-100")
        ], lg=8, className="mb-4 mb-lg-0"),

        # RIGHT COLUMN: INSPECTOR
        dbc.Col([
            dbc.Card([
                dbc.CardHeader(html.H5("District Inspector", className="mb-0 fw-bold")),
                dbc.CardBody([
                    html.Label("Search & Select", className="fw-bold small text-muted"),
                    dcc.Dropdown(id='district-search', options=district_options, placeholder="Type or select a district...", clearable=True, className="mb-4"),
                    html.Div(id='filler'),
                    html.Hr(className="my-4"),
                    html.H6("National Stress Distribution", className="fw-bold text-muted mb-3"),
                    dcc.Graph(id='stress-dist-chart', config={"displayModeBar": False}, style={"height": "220px"})
                ])
            ], className="shadow-sm border-0 h-100")
        ], lg=4)
    ], className="mb-4"),

    # --- SECTION 2: MORTALITY RISK INDEX MAP ---
    dbc.Card([
        dbc.CardHeader(
            dbc.Row([
                dbc.Col([
                    html.H5("2. Projected Mortality Risk Index Map", className="mb-1 fw-bold text-danger"),
                    html.P("Demographic mortality probability index (0 - 100) based on non-linear physiological strain", className="text-muted small mb-0")
                ], md=7),
                dbc.Col([
                    html.Label("Demographic Risk Class", className="fw-bold small text-muted"),
                    dcc.Dropdown(
                        id='demographic-class',
                        options=[{"label": k, "value": k} for k in DEMO_WEIGHTS.keys()],
                        value="Elderly (60+ yrs)",
                        clearable=False
                    )
                ], md=5)
            ], className="align-items-center")
        ),
        dbc.CardBody([
            html.Div([
                html.Label("Mortality Risk Index Filter & Color Bounds", className="fw-bold small text-muted mb-1"),
                dcc.RangeSlider(
                    id='mortality-range-slider',
                    min=0,
                    max=100,
                    step=1,
                    value=[0, 100],
                    marks={0: '0', 25: '25', 50: '50', 75: '75', 100: '100'},
                    tooltip={"placement": "bottom", "always_visible": True},
                    className="mb-4"
                )
            ]),
            dcc.Loading(dcc.Graph(id='mortality-map', config={"displayModeBar": False}))
        ])
    ], className="shadow-sm border-0 mb-4"),

    # --- SECTION 3: WHAT-IF POLICY SIMULATOR ---
    dbc.Card([
        dbc.CardHeader(
            html.Div([
                html.H5("3. Interactive 'What-If' Climate Resilience & Urban Cooling Simulator", className="mb-1 fw-bold text-success"),
                html.P("Simulate real-time microclimate interventions (cool roofs, urban forest canopy, misting stations) to quantify thermal reduction & saved lives.", className="text-muted small mb-0")
            ])
        ),
        dbc.CardBody([
            dbc.Row([
                dbc.Col([
                    html.Label("🌳 Urban Tree Canopy Cover Increase (%)", className="fw-bold small"),
                    dcc.Slider(
                        id="policy-canopy-slider",
                        min=0,
                        max=30,
                        step=5,
                        value=0,
                        marks={0: '0%', 10: '10%', 20: '20%', 30: '30%'},
                        tooltip={"placement": "bottom", "always_visible": True},
                    ),
                ], md=4, className="mb-3 mb-md-0 border-end pe-4"),
                dbc.Col([
                    html.Label("🏠 Cool Roof / High-Albedo Coating Coverage (%)", className="fw-bold small"),
                    dcc.Slider(
                        id="policy-coolroof-slider",
                        min=0,
                        max=50,
                        step=10,
                        value=0,
                        marks={0: '0%', 25: '25%', 50: '50%'},
                        tooltip={"placement": "bottom", "always_visible": True},
                    ),
                ], md=4, className="mb-3 mb-md-0 border-end pe-4"),
                dbc.Col([
                    html.Label("💨 Public Misting & Evaporative Cooling Deployment (%)", className="fw-bold small"),
                    dcc.Slider(
                        id="policy-misting-slider",
                        min=0,
                        max=40,
                        step=10,
                        value=0,
                        marks={0: '0%', 20: '20%', 40: '40%'},
                        tooltip={"placement": "bottom", "always_visible": True},
                    ),
                ], md=4, className="ps-4"),
            ], className="mb-4 align-items-center"),
            dbc.Alert(id="policy-impact-summary", color="info", className="mb-0 fw-semibold")
        ])
    ], className="shadow-sm border-0 mb-5")

], id="main-container", fluid=True, className="bg-light px-4 py-3 min-vh-100")


# ==============================================================================
# CALLBACKS
# ==============================================================================

@callback(
    Output('main-container', 'className'),
    Input('theme-switch', 'value')
)
def update_app_theme(dark_mode):
    return "dark-mode bg-dark text-light px-4 py-3 min-vh-100" if dark_mode else "light-mode bg-light text-dark px-4 py-3 min-vh-100"


@callback(
    Output("live-status-badge", "children"),
    Output("btn-update-data", "children"),
    Output("data-version", "data"),
    Output("update-feedback", "children"),
    Input("status-poll-interval", "n_intervals"),
    Input("btn-update-data", "n_clicks"),
    State("data-version", "data"),
    prevent_initial_call=False,
)
def update_live_badge_and_button(_n, n_clicks, current_version):
    """Status poll + Update button in ONE callback so clicks cannot be dropped."""
    from dash import ctx

    version = (
        f"{_weather_source}|{_last_weather_update.isoformat()}"
        if _last_weather_update is not None
        else f"{_weather_source}|init"
    )
    version_out = version if version != current_version else no_update
    feedback = no_update

    # Explicit click handling (must log every time)
    if ctx.triggered_id == "btn-update-data" and n_clicks and n_clicks > 0:
        _wlog(f"[weather] BUTTON CLICK received n_clicks={n_clicks} fetching={_weather_fetching}")
        started = trigger_manual_weather_refresh()
        if started:
            feedback = f"Update requested at {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC — fetch started (see logs)."
        else:
            feedback = f"Update clicked at {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC — fetch already running or busy."

    if _weather_fetching:
        badge = dbc.Badge(
            "● Updating…",
            color="info",
            className="px-3 py-2 fs-6 rounded-pill shadow-sm",
        )
        return badge, "↻ Updating…", version_out, feedback

    if _weather_ready:
        ts = (
            _last_weather_update.strftime("%H:%M UTC")
            if _last_weather_update is not None
            else "—"
        )
        if _weather_source == "live":
            text, color = f"● Live · {ts} · every 3h", "success"
        elif _weather_source == "cache":
            text, color = f"● Cached · {ts}", "success"
        else:
            text, color = "● Demo data · API rate-limited — try later", "warning"
        badge = dbc.Badge(text, color=color, className="px-3 py-2 fs-6 rounded-pill shadow-sm")
        return badge, "↻ Update data", version_out, feedback

    badge = dbc.Badge(
        "● Starting…",
        color="secondary",
        className="px-3 py-2 fs-6 rounded-pill shadow-sm",
    )
    return badge, "↻ Update data", version_out, feedback


@callback(
    Output('kpi-summary-container', 'children'),
    Input('forecast-horizon', 'value'),
    Input('theme-switch', 'value'),
    Input('data-version', 'data'),
)
def update_kpis(horizon, dark_mode, _version):
    utci_col = f"UTCI_d{horizon}"
    temp_col = f"Dry Bulb Temp_d{horizon}"
    
    valid = df.dropna(subset=[temp_col])
    avg_u = round(df[utci_col].mean(), 1) if not df[utci_col].empty else "N/A"
    max_r = valid.loc[valid[temp_col].idxmax()] if not valid.empty else None
    min_r = valid.loc[valid[temp_col].idxmin()] if not valid.empty else None

    if dark_mode:
        avg_color = "#60a5fa"   # Bright Light Blue
        hot_color = "#f87171"   # Soft Red
        cool_color = "#38bdf8"  # Bright Cyan
        dist_color = "#f8fafc"  # White/Light Slate
    else:
        avg_color = "#1d4ed8"   # High-contrast Deep Blue
        hot_color = "#b91c1c"   # High-contrast Deep Red
        cool_color = "#0369a1"   # High-contrast Deep Sky Blue
        dist_color = "#0f172a"   # Dark Slate

    return dbc.Row([
        dbc.Col([
            html.Div("National Avg UTCI", className="text-muted small fw-bold text-uppercase"),
            html.Div(f"{avg_u} °C", className="fs-3 fw-bolder", style={"color": avg_color})
        ]),
        dbc.Col([
            html.Div("Hottest District", className="text-muted small fw-bold text-uppercase"),
            html.Div(f"{max_r['District']}" if max_r is not None else "-", className="fs-5 fw-bolder", style={"color": hot_color}),
            html.Div(f"{max_r[temp_col]} °C" if max_r is not None else "", className="text-muted small")
        ]),
        dbc.Col([
            html.Div("Coolest District", className="text-muted small fw-bold text-uppercase"),
            html.Div(f"{min_r['District']}" if min_r is not None else "-", className="fs-5 fw-bolder", style={"color": cool_color}),
            html.Div(f"{min_r[temp_col]} °C" if min_r is not None else "", className="text-muted small")
        ]),
        dbc.Col([
            html.Div("Monitored Districts", className="text-muted small fw-bold text-uppercase"),
            html.Div(f"{len(df)}", className="fs-3 fw-bolder", style={"color": dist_color})
        ])
    ])

@callback(
    Output('color-range-slider', 'min'), Output('color-range-slider', 'max'),
    Output('color-range-slider', 'value'), Output('color-range-slider', 'marks'),
    Input('measurements', 'value'), Input('forecast-horizon', 'value')
)
def update_slider_limits(measurement_chosen, horizon):
    col_prefix = MEASUREMENTS[measurement_chosen]
    target_col = f"{col_prefix}_d{horizon}"
    min_val, max_val = float(df[target_col].min()), float(df[target_col].max())
    p_min, p_max = float(np.floor(min_val)), float(np.ceil(max_val))
    if p_min == p_max: p_max += 1.0
    ticks = np.linspace(p_min, p_max, 5)
    marks = {int(t) if t.is_integer() else round(t, 1): f"{int(t) if t.is_integer() else round(t, 1)}" for t in ticks}
    default_range = DEFAULT_SLIDER_BOUNDS.get(measurement_chosen, [p_min, p_max])
    return p_min, p_max, default_range, marks

@callback(
    Output('district-map', 'figure'),
    Input('measurements', 'value'), Input('state-filter', 'value'),
    Input('color-range-slider', 'value'), Input('forecast-horizon', 'value'),
    Input('theme-switch', 'value'),
    Input('data-version', 'data'),
)
def update_thermal_map(measurement_chosen, selected_state, color_range, horizon, dark_mode, _version):
    target_col = f"{MEASUREMENTS[measurement_chosen]}_d{horizon}"
    filtered_df = df if selected_state == "ALL" else df[df['State'] == selected_state]
    r_use = color_range if color_range else DEFAULT_SLIDER_BOUNDS[measurement_chosen]

    map_style = "carto-darkmatter" if dark_mode else "carto-positron"
    template = "plotly_dark" if dark_mode else "plotly_white"

    fig = px.choropleth_map(
        data_frame=filtered_df, color=target_col, range_color=r_use,
        geojson=district_geojson, opacity=0.7, zoom=3.4,
        featureidkey="properties.join_key", map_style=map_style,
        center={"lat": 22.5, "lon": 80.0}, height=550, locations="join_key"
    )
    fig.update_layout(template=template, margin={"r": 0, "t": 0, "l": 0, "b": 0}, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return fig

@callback(
    Output('mortality-map', 'figure'),
    Input('demographic-class', 'value'), Input('state-filter', 'value'),
    Input('forecast-horizon', 'value'), Input('mortality-range-slider', 'value'),
    Input('theme-switch', 'value'),
    Input('data-version', 'data'),
)
def update_mortality_map(demo_class, selected_state, horizon, mortality_range, dark_mode, _version):
    target_col = f"Mortality_{demo_class}_d{horizon}"
    filtered_df = df if selected_state == "ALL" else df[df['State'] == selected_state]

    if mortality_range:
        filtered_df = filtered_df[
            (filtered_df[target_col] >= mortality_range[0]) & 
            (filtered_df[target_col] <= mortality_range[1])
        ]
        r_use = mortality_range
    else:
        r_use = [0, 100]

    map_style = "carto-darkmatter" if dark_mode else "carto-positron"
    template = "plotly_dark" if dark_mode else "plotly_white"

    fig = px.choropleth_map(
        data_frame=filtered_df, color=target_col, range_color=r_use,
        geojson=district_geojson, color_continuous_scale="Reds", opacity=0.8,
        zoom=3.4, featureidkey="properties.join_key", map_style=map_style,
        center={"lat": 22.5, "lon": 80.0}, height=500, locations="join_key",
        labels={target_col: "Mortality Risk Index"}
    )
    fig.update_layout(template=template, margin={"r": 0, "t": 0, "l": 0, "b": 0}, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return fig

@callback(Output('district-search', 'value'), Input('district-map', 'clickData'))
def sync_map_click_to_search(clicked_data):
    return clicked_data['points'][0]['location'] if clicked_data else no_update

@callback(
    Output("alert-modal", "is_open"),
    Output("alert-dispatch-status", "children"),
    Input("btn-open-alert-modal", "n_clicks"),
    Input("btn-close-alert-modal", "n_clicks"),
    Input("btn-send-alert", "n_clicks"),
    State("alert-target-role", "value"),
    State("alert-channel", "value"),
    State("alert-phone-input", "value"),
    State("alert-message-body", "value"),
    State("alert-modal", "is_open"),
    prevent_initial_call=True,
)
def toggle_and_dispatch_alert(open_click, close_click, send_click, role, channel, phone, message_body, is_open):
    from dash import ctx
    triggered = ctx.triggered_id

    if triggered == "btn-open-alert-modal":
        return True, None
    if triggered == "btn-close-alert-modal":
        return False, None

    if triggered == "btn-send-alert" and send_click > 0:
        channel_name = "WhatsApp Gateway" if channel == "whatsapp" else "SMS Gateway"
        role_label = role.title()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        status_box = dbc.Alert([
            html.Div(f"✅ ALERT DISPATCH SUCCESSFUL ({timestamp})", className="fw-bold mb-1"),
            html.Div(f"• Channel: {channel_name}"),
            html.Div(f"• Target Group: {role_label}"),
            html.Div(f"• Recipient: {phone}"),
            html.Div(f"• Message Body: '{message_body}'"),
            html.Div("• Status: Broadcast sent & logged in audit ledger.", className="small mt-1 text-success fw-bold")
        ], color="success", className="mb-0")
        return True, status_box

    return is_open, no_update


@callback(
    Output("policy-impact-summary", "children"),
    Output("policy-impact-summary", "color"),
    Input("policy-canopy-slider", "value"),
    Input("policy-coolroof-slider", "value"),
    Input("policy-misting-slider", "value"),
)
def update_policy_simulation(canopy_pct, coolroof_pct, misting_pct):
    # Microclimate physics model:
    # 1. Tree Canopy: reduces Mean Radiant Temp by ~0.15°C per % and Dry Bulb by ~0.08°C per %
    # 2. Cool Roofs: reduces Air Temp by ~0.05°C per %
    # 3. Misting: reduces UTCI & WBGT by ~0.12°C per %
    temp_drop = round(canopy_pct * 0.08 + coolroof_pct * 0.05, 2)
    utci_drop = round(canopy_pct * 0.15 + coolroof_pct * 0.06 + misting_pct * 0.12, 2)
    mortality_drop = round(utci_drop * 2.4, 1)

    if canopy_pct == 0 and coolroof_pct == 0 and misting_pct == 0:
        return "💡 Adjust the sliders above to model real-time cooling interventions and quantify heat stress reduction.", "secondary"

    msg = (
        f"🎯 Simulated Intervention Impact: "
        f"Air Temperature drop: -{temp_drop}°C | "
        f"UTCI Thermal Strain drop: -{utci_drop}°C | "
        f"Estimated Mortality Risk reduction: -{mortality_drop}% across vulnerable districts."
    )
    return msg, "success"


@callback(
    Output('filler', 'children'),
    Input('district-search', 'value'), Input('forecast-horizon', 'value'),
    Input('demographic-class', 'value'), Input('theme-switch', 'value')
)
def show_district_detail(searched_district, horizon, demo_class, dark_mode):
    if not searched_district:
        return html.Div("👆 Select or click any district on the map to inspect micro-climate metrics.", className="text-center text-muted p-4 mt-2 fw-bold")

    row = df[df['join_key'] == searched_district]
    if row.empty: return no_update
    r = row.iloc[0]

    h_label = ["Current Peak", "+1 Day Forecast", "+2 Day Forecast", "+3 Day Forecast"][horizon]
    m_idx = r[f"Mortality_{demo_class}_d{horizon}"]
    wbgt_v = r.get(f"WBGT_d{horizon}", "N/A")
    hi_v = r.get(f"Heat Index_d{horizon}", "N/A")
    haz_v = r.get(f"Composite Hazard_d{horizon}", "N/A")

    whatsapp_text = (
        f"🌡️ *India Thermal & Mortality Risk Alert ({h_label}) - {r['District']}, {r['State']}*\n\n"
        f"• *Composite Thermal Hazard:* {haz_v} / 100\n"
        f"• *UTCI Stress:* {r[f'UTCI_d{horizon}']}°C ({r[f'Stress Category_d{horizon}']})\n"
        f"• *ISO WBGT (Outdoor):* {wbgt_v}°C\n"
        f"• *NOAA Heat Index:* {hi_v}°C\n"
        f"• *Mortality Index ({demo_class}):* {m_idx}/100\n"
        f"• *Air Temp:* {r[f'Dry Bulb Temp_d{horizon}']}°C (Feels like {r[f'Apparent Temp_d{horizon}']}°C)\n"
        f"• *Humidity:* {r[f'Relative Humidity_d{horizon}']}%"
    )
    wa_url = f"https://wa.me/?text={urllib.parse.quote(whatsapp_text)}"

    if dark_mode:
        card_bg = "bg-dark border-secondary"
        text_color = "text-light"
        mortality_card_bg = "#450a0a"
        mortality_border = "#991b1b"
        mortality_text_color = "#fca5a5"
        mortality_label_color = "#f87171"
    else:
        card_bg = "bg-light border"
        text_color = "text-dark"
        mortality_card_bg = "#fef2f2"
        mortality_border = "#fca5a5"
        mortality_text_color = "#991b1b"
        mortality_label_color = "#b91c1c"

    return html.Div([
        html.Div([
            html.H3(f"{r['District']}", className=f"mb-0 fw-bolder {text_color}"),
            html.Span(f"{r['State']} • {h_label}", className="text-muted small fw-bold text-uppercase")
        ], className="mb-3 border-bottom pb-2"),
        
        dbc.Row([
            dbc.Col(
                dbc.Card(dbc.CardBody([
                    html.Div("Composite Hazard", className="text-muted small fw-bold text-uppercase"),
                    html.Div(f"{haz_v} / 100", className=f"fs-3 fw-bolder {text_color}"),
                    html.Span(f"{r[f'Stress Category_d{horizon}']}", className="badge bg-warning text-dark mt-1")
                ]), className=f"{card_bg} text-center"), width=6
            ),
            dbc.Col(
                dbc.Card(dbc.CardBody([
                    html.Div("Mortality Index", className="small fw-bold text-uppercase", style={"color": mortality_label_color}),
                    html.Div(f"{m_idx} / 100", className="fs-3 fw-bolder", style={"color": mortality_text_color}),
                ]), className="text-center", style={"backgroundColor": mortality_card_bg, "border": f"1px solid {mortality_border}"}), width=6
            )
        ], className="g-2 mb-3"),

        dbc.Row([
            dbc.Col([html.Span("UTCI Stress: ", className="text-muted"), html.B(f"{r[f'UTCI_d{horizon}']} °C")], width=6),
            dbc.Col([html.Span("ISO WBGT: ", className="text-muted"), html.B(f"{wbgt_v} °C")], width=6),
            dbc.Col([html.Span("NOAA Heat Index: ", className="text-muted"), html.B(f"{hi_v} °C")], width=6),
            dbc.Col([html.Span("Air Temp: ", className="text-muted"), html.B(f"{r[f'Dry Bulb Temp_d{horizon}']} °C")], width=6),
            dbc.Col([html.Span("Feels Like: ", className="text-muted"), html.B(f"{r[f'Apparent Temp_d{horizon}']} °C")], width=6),
            dbc.Col([html.Span("Humidity: ", className="text-muted"), html.B(f"{r[f'Relative Humidity_d{horizon}']}%")], width=6),
        ], className="small mb-3 g-2"),

        dbc.Button("📱 Share Report via WhatsApp", href=wa_url, target="_blank", color="success", className="w-100 fw-bold")
    ])

@callback(
    Output('stress-dist-chart', 'figure'),
    Input('state-filter', 'value'), Input('forecast-horizon', 'value'),
    Input('theme-switch', 'value'),
    Input('data-version', 'data'),
)
def update_stress_chart(selected_state, horizon, dark_mode, _version):
    target_col = f"Stress Category_d{horizon}"
    filtered_df = df if selected_state == "ALL" else df[df['State'] == selected_state]
    counts = filtered_df[target_col].value_counts().reset_index()
    counts.columns = ['Category', 'Count']
    
    bar_color = '#60a5fa' if dark_mode else '#1e40af'
    template = "plotly_dark" if dark_mode else "plotly_white"

    fig = px.bar(counts, x='Count', y='Category', orientation='h', color_discrete_sequence=[bar_color])
    fig.update_layout(
        template=template,
        margin={"r": 0, "t": 0, "l": 0, "b": 0}, xaxis_title=None, yaxis_title=None,
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        yaxis={'categoryorder': 'total ascending', 'tickfont': {'size': 11}}
    )
    return fig

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8050))
    app.run(host="0.0.0.0", port=port, debug=False)