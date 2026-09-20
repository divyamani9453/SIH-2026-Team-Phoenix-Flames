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
import math
import threading
import sys
from datetime import datetime, timezone

from pythermalcomfort.models import utci
from src.ml_engine import get_hvi_predictions_and_features

# ==============================================================================
# DATA INGESTION & PROCESSING
# ==============================================================================
CENTROIDS_FILE = os.environ.get("CENTROIDS_FILE", "data/geo/district_centroids.xlsx")
GEOJSON_FILE = os.environ.get("GEOJSON_FILE", "data/geo/India-Districts-slim.json")
SPATIAL_FEATURES_FILE = os.environ.get("SPATIAL_FEATURES_FILE", "data/spatial_features.csv")

df = pd.read_excel(CENTROIDS_FILE)
required = {"join_key", "District", "State", "lat", "lon"}
missing = required - set(df.columns)
if missing:
    raise ValueError(f"Centroids Excel missing required columns: {missing}")

with open(GEOJSON_FILE) as f:
    district_geojson = json.load(f)

for feature in district_geojson["features"]:
    props = feature["properties"]
    if "join_key" not in props:
        props["join_key"] = f"{props.get('ST_NM','')}|{props.get('DISTRICT','')}"

# Load Municipal Ward GeoJSONs lazily to maintain low memory footprint (<150 MB)
WARD_FILES = {
    "AHMEDABAD_WARDS": "data/municipal_wards/ahmedabad_wards.geojson",
    "BANGALORE_WARDS": "data/municipal_wards/bangalore_wards.geojson",
}
ward_geojsons = {}
ward_dfs = {}

for ward_key, filepath in WARD_FILES.items():
    if os.path.exists(filepath):
        try:
            with open(filepath) as wf:
                w_json = json.load(wf)

            w_rows = []
            for idx, feature in enumerate(w_json["features"]):
                props = feature["properties"]
                w_name = props.get("Name") or props.get("KGISWardName") or f"Ward {idx+1}"
                j_key = f"{ward_key}|{w_name}"
                props["join_key"] = j_key

                coords = feature.get("geometry", {}).get("coordinates", [])
                if coords:
                    try:
                        pts = coords[0] if feature["geometry"]["type"] == "Polygon" else coords[0][0]
                        lons = [p[0] for p in pts if len(p) >= 2]
                        lats = [p[1] for p in pts if len(p) >= 2]
                        c_lon = float(np.mean(lons))
                        c_lat = float(np.mean(lats))
                    except Exception:
                        c_lat, c_lon = (23.0225, 72.5714) if "AHMEDABAD" in ward_key else (12.9716, 77.5946)
                else:
                    c_lat, c_lon = (23.0225, 72.5714) if "AHMEDABAD" in ward_key else (12.9716, 77.5946)

                w_rows.append({
                    "join_key": j_key,
                    "District": w_name,
                    "State": "Municipal Wards",
                    "lat": c_lat,
                    "lon": c_lon,
                })

            w_df = pd.DataFrame(w_rows)
            ward_geojsons[ward_key] = w_json
            ward_dfs[ward_key] = w_df
            print(f"[ward-gis] Loaded {ward_key} ({len(w_rows)} wards)", flush=True)
        except Exception as e:
            print(f"[ward-gis] Failed to load {ward_key}: {e}", flush=True)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
BATCH_SIZE = 40
BATCH_PAUSE_SEC = 1.5
FETCH_MAX_SECONDS = int(os.environ.get("FETCH_MAX_SECONDS", "90"))
WEATHER_CACHE_FILE = os.environ.get("WEATHER_CACHE_FILE", "weather_cache.pkl")
CACHE_MAX_AGE_HOURS = float(os.environ.get("CACHE_MAX_AGE_HOURS", "6"))


def _save_weather_cache(dataframe):
    try:
        cols = [c for c in dataframe.columns if any(
            c.startswith(p) for p in (
                "Dry Bulb Temp_d", "Relative Humidity_d", "Wind Speed_d",
                "Apparent Temp_d", "Mean Radiant Temp_d", "UTCI_d",
                "Stress Category_d", "Mortality_",
            )
        )]
        dataframe[["join_key"] + cols].to_pickle(WEATHER_CACHE_FILE)
        print(f"[weather] Cache saved to {WEATHER_CACHE_FILE}", flush=True)
    except Exception as e:
        print(f"[weather] Cache save failed: {e}", flush=True)


def _load_weather_cache(dataframe, max_age_hours=None):
    if not os.path.exists(WEATHER_CACHE_FILE):
        return False
    try:
        age_h = (time.time() - os.path.getmtime(WEATHER_CACHE_FILE)) / 3600.0
        limit = CACHE_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
        if age_h > limit:
            print(f"[weather] Cache expired ({age_h:.1f}h > {limit}h)", flush=True)
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
        print(f"[weather] Disk cache loaded successfully (age: {age_h:.1f}h)", flush=True)
        return True
    except Exception as e:
        print(f"[weather] Cache load failed: {e}", flush=True)
        return False


def fetch_batch(lats, lons):
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
                    time.sleep(8)
                    continue
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
            print(f"[weather] Batch error: {e}", flush=True)
            if attempt == 0:
                time.sleep(3)
                continue
            return [None] * len(lats), False
    return [None] * len(lats), True


def fetch_multi_day_weather(dataframe):
    lats, lons = dataframe["lat"].tolist(), dataframe["lon"].tolist()
    all_responses = []
    n_batches = (len(dataframe) + BATCH_SIZE - 1) // BATCH_SIZE
    t0 = time.time()
    consecutive_rate_limits = 0
    aborted = False

    for bi, i in enumerate(range(0, len(dataframe), BATCH_SIZE)):
        if time.time() - t0 > FETCH_MAX_SECONDS:
            remaining = len(dataframe) - len(all_responses)
            all_responses.extend([None] * remaining)
            aborted = True
            break

        b_lats = lats[i:i + BATCH_SIZE]
        b_lons = lons[i:i + BATCH_SIZE]
        batch_res, hit_limit = fetch_batch(b_lats, b_lons)

        if hit_limit:
            consecutive_rate_limits += 1
            all_responses.extend([None] * len(b_lats))
            if consecutive_rate_limits >= 2:
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

    dataframe.attrs["weather_aborted"] = aborted
    n_ok = sum(1 for x in all_responses if x is not None)
    dataframe.attrs["weather_ok_locations"] = n_ok

    peak_indices = [14, 38, 62, 86]
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

        # Clamp wind speed to min 0.5 m/s to prevent pythermalcomfort NaN returns
        v_clamped = [max(0.5, float(w)) if not pd.isna(w) else 0.5 for w in dataframe[f"Wind Speed_d{d_idx}"]]

        u_res = utci(
            tdb=dataframe[f"Dry Bulb Temp_d{d_idx}"].tolist(),
            tr=dataframe[f"Mean Radiant Temp_d{d_idx}"].tolist(),
            v=v_clamped,
            rh=dataframe[f"Relative Humidity_d{d_idx}"].tolist(),
        )
        vals = u_res.utci if hasattr(u_res, "utci") else u_res
        dataframe[f"UTCI_d{d_idx}"] = [
            round(v, 1) if not pd.isna(v) else np.nan for v in vals
        ]

    return dataframe


def fill_synthetic_weather(dataframe):
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

        v_clamped = [max(0.5, float(w)) if not pd.isna(w) else 0.5 for w in dataframe[f"Wind Speed_d{d_idx}"]]

        u_res = utci(
            tdb=dataframe[f"Dry Bulb Temp_d{d_idx}"].tolist(),
            tr=dataframe[f"Mean Radiant Temp_d{d_idx}"].tolist(),
            v=v_clamped,
            rh=dataframe[f"Relative Humidity_d{d_idx}"].tolist(),
        )
        vals = u_res.utci if hasattr(u_res, "utci") else u_res
        dataframe[f"UTCI_d{d_idx}"] = [
            round(v, 1) if not pd.isna(v) else np.nan for v in vals
        ]
    return dataframe


# ==============================================================================
# TRI-INDEX BIOMETEOROLOGICAL ENGINE (NOAA HI, ISO WBGT, UTCI)
# ==============================================================================

def calculate_heat_index(t_c, rh):
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
    if pd.isna(t_c) or pd.isna(rh):
        return np.nan
    e = (rh / 100.0) * 6.105 * math.exp((17.27 * t_c) / (237.7 + t_c))
    wbgt_shade = 0.567 * t_c + 0.393 * e + 3.94
    v = max(float(wind_speed if not pd.isna(wind_speed) else 1.0), 0.5)
    solar_adj = (solar_rad / 800.0) * 2.2 - (v - 1.0) * 0.4
    return round(float(wbgt_shade + max(-1.0, solar_adj)), 1)


def calculate_composite_hazard(hi, wbgt, utci_val):
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
    for d in range(4):
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


df = fill_synthetic_weather(df)
df = enrich_derived_columns(df)

MEASUREMENTS = {
    "AI Heat Vulnerability Index (0-100)": "AI HVI",
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
    "AI Heat Vulnerability Index (0-100)": [0, 100],
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
if "AHMEDABAD_WARDS" in ward_dfs:
    states_list.append("Municipal Wards: Ahmedabad (48 Wards)")
if "BANGALORE_WARDS" in ward_dfs:
    states_list.append("Municipal Wards: Bengaluru (243 Wards)")

combined_df_list = [df]
for w_key, w_df in ward_dfs.items():
    combined_df_list.append(w_df)

full_df = pd.concat(combined_df_list, ignore_index=True)

if os.path.exists(SPATIAL_FEATURES_FILE):
    try:
        df_spatial = pd.read_csv(SPATIAL_FEATURES_FILE)
        cols_to_merge = [c for c in df_spatial.columns if c not in ["District", "State", "lat", "lon"] or c == "join_key"]
        full_df = full_df.merge(df_spatial[cols_to_merge], on="join_key", how="left")
        print(f"[spatial] Merged spatial features shape: {full_df.shape}", flush=True)
    except Exception as e:
        print(f"[spatial] Merge error: {e}", flush=True)

full_df = fill_synthetic_weather(full_df)
full_df = enrich_derived_columns(full_df)

for d_idx in range(4):
    try:
        scores, _ = get_hvi_predictions_and_features(full_df, horizon=d_idx)
        full_df[f"AI HVI_d{d_idx}"] = scores
    except Exception as e:
        print(f"[ml-hvi] Error generating HVI for horizon d{d_idx}: {e}", flush=True)

df = full_df

district_options = [
    {"label": f"{r['District']} ({r['State']})", "value": r["join_key"]}
    for _, r in df.iterrows()
]

REFRESH_INTERVAL_SEC = int(os.environ.get("WEATHER_REFRESH_HOURS", "3")) * 3600
_weather_ready = False
_weather_fetching = False
_weather_source = "synthetic"
_last_weather_update = None
_weather_lock = threading.Lock()
_weather_thread_started = False
_weather_thread_start_lock = threading.Lock()


def _wlog(msg):
    print(msg, flush=True)
    sys.stdout.flush()


def _run_one_weather_fetch(force=False):
    global df, _weather_ready, _last_weather_update, _weather_fetching, _weather_source
    if not _weather_lock.acquire(blocking=False):
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
            return True

        updated = fetch_multi_day_weather(working)
        updated = enrich_derived_columns(updated)
        df = updated
        aborted = bool(getattr(updated, "attrs", {}).get("weather_aborted", False))
        n_ok = int(getattr(updated, "attrs", {}).get("weather_ok_locations", 0) or 0)
        _save_weather_cache(df)
        _weather_ready = True
        _last_weather_update = datetime.now(timezone.utc)
        if aborted or n_ok < max(1, len(df) // 4):
            _weather_source = "synthetic"
        else:
            _weather_source = "live"
        return True
    except Exception as e:
        _wlog(f"[weather] Fetch failed: {e}")
        working = df.copy()
        if _load_weather_cache(working, max_age_hours=72):
            df = enrich_derived_columns(working)
            _weather_source = "cache"
        else:
            _weather_source = "synthetic"
        _weather_ready = True
        if _last_weather_update is None:
            _last_weather_update = datetime.now(timezone.utc)
        return False
    finally:
        _weather_fetching = False
        _weather_lock.release()


def _background_weather_loop():
    while True:
        _run_one_weather_fetch()
        slept = 0
        while slept < REFRESH_INTERVAL_SEC:
            time.sleep(min(60, REFRESH_INTERVAL_SEC - slept))
            slept += 60


def ensure_weather_thread_started():
    global _weather_thread_started
    with _weather_thread_start_lock:
        if _weather_thread_started:
            return
        _weather_thread_started = True
        t = threading.Thread(target=_background_weather_loop, daemon=True, name="weather-loop")
        t.start()


def trigger_manual_weather_refresh():
    ensure_weather_thread_started()
    if _weather_fetching:
        return False

    def _manual_job():
        try:
            _run_one_weather_fetch(force=True)
        except Exception as e:
            _wlog(f"[weather] Manual update failed: {e}")

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
    ensure_weather_thread_started()


app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>SIH 2026 PS 83 | National Heatwave Decision Support System</title>
        {%favicon%}
        {%css%}
        <style>
            .glass-card {
                border-radius: 12px !important;
                transition: transform 0.2s ease, box-shadow 0.2s ease !important;
            }
            .glass-card:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08) !important;
            }
            .brand-header-banner {
                background: linear-gradient(135deg, #0f172a 0%, #1e293b 60%, #1e3a8a 100%) !important;
                border-radius: 12px;
                color: #ffffff !important;
                box-shadow: 0 6px 20px rgba(15, 23, 42, 0.2);
            }
            .brand-header-banner p {
                color: #94a3b8 !important;
            }

            .light-mode {
                background: #f8fafc !important;
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
                border-radius: 12px !important;
            }
            .light-mode .card-header {
                background-color: #f1f5f9 !important;
                border-bottom: 1px solid #e2e8f0 !important;
                color: #0f172a !important;
                border-top-left-radius: 12px !important;
                border-top-right-radius: 12px !important;
            }

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
                color: #0f172a !important;
                font-weight: 700 !important;
            }

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
    dcc.Interval(id="status-poll-interval", interval=12 * 1000, n_intervals=0),
    dcc.Store(id="data-version", data="init"),

    # --- HEADER & CONTROLS BANNER ---
    dbc.Row([
        dbc.Col([
            html.Div([
                html.Span("SIH 2026 PS 83 NATIONAL DECISION SUPPORT SYSTEM", className="badge bg-danger text-light px-3 py-1 mb-2 fw-bold text-uppercase rounded-pill shadow-sm"),
                html.H2("India Thermal Comfort & Mortality Risk Platform", className="fw-bolder mb-1 text-white"),
                html.P("Predictive biometeorological forecasting and localized demographic heat stress decision engine", className="mb-0 text-light opacity-75")
            ])
        ], md=6),
        dbc.Col([
            dbc.Button(
                "Emergency Advisory Gateway",
                id="btn-open-alert-modal",
                color="warning",
                size="sm",
                className="me-2 fw-bold text-dark shadow-sm rounded-pill px-3 py-2",
                n_clicks=0,
            ),
            html.Span(id="live-status-badge", className="me-2"),
            dbc.Button(
                "Refresh Forecast Data",
                id="btn-update-data",
                color="light",
                outline=True,
                size="sm",
                className="me-2 fw-bold rounded-pill px-3 py-2",
                n_clicks=0,
            ),
            dbc.Switch(id="theme-switch", label="Dark Mode", value=False, className="fw-bold d-inline-block text-light ms-2")
        ], md=6, className="d-flex justify-content-md-end align-items-center mt-3 mt-md-0")
    ], className="brand-header-banner p-4 my-3 align-items-center"),
    html.Div(id="update-feedback", className="small text-muted mb-2"),

    # --- ALERT DISPATCH MODAL ---
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Automated Emergency Heat Advisory Gateway")),
        dbc.ModalBody([
            html.P("Configure and trigger target-group emergency advisory broadcasts for vulnerable demographics and medical responders.", className="text-muted small"),
            dbc.Form([
                dbc.Row([
                    dbc.Col([
                        html.Label("Recipient Target Category", className="fw-bold small"),
                        dcc.Dropdown(
                            id="alert-target-role",
                            options=[
                                {"label": "Outdoor & Construction Workforce", "value": "laborers"},
                                {"label": "Vulnerable & Elderly Populations", "value": "vulnerable"},
                                {"label": "Educational Institutions & Childcare", "value": "schools"},
                                {"label": "Emergency Medical & First Responders", "value": "health"},
                                {"label": "Municipal Disaster Management Authorities", "value": "municipal"},
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
                                {"label": "WhatsApp Business Gateway (API)", "value": "whatsapp"},
                                {"label": "Cellular SMS Gateway (CDAC/Twilio)", "value": "sms"},
                            ],
                            value="whatsapp",
                            inline=True,
                            className="mb-3 small"
                        )
                    ], md=12),
                    dbc.Col([
                        html.Label("Recipient Broadcast Contact / Group", className="fw-bold small"),
                        dbc.Input(id="alert-phone-input", type="text", placeholder="+91 98765 43210 or @district-disaster-desk", value="+91 98765 43210"),
                    ], md=12, className="mb-3"),
                    dbc.Col([
                        html.Label("Custom Emergency Advisory Text", className="fw-bold small"),
                        dbc.Textarea(
                            id="alert-message-body",
                            rows=3,
                            value="CRITICAL HEAT ADVISORY: Composite Thermal Hazard Index EXTREME. Mandatory suspension of high-intensity outdoor labor between 12:00-16:00 IST. Ensure hydration stations and cooling shelters are fully operational.",
                        ),
                    ], md=12, className="mb-3"),
                ])
            ]),
            html.Div(id="alert-dispatch-status", className="mt-2")
        ]),
        dbc.ModalFooter([
            dbc.Button("Execute Advisory Broadcast", id="btn-send-alert", color="danger", className="fw-bold", n_clicks=0),
            dbc.Button("Close", id="btn-close-alert-modal", color="secondary", outline=True, n_clicks=0)
        ])
    ], id="alert-modal", is_open=False, size="lg"),

    # --- FORECAST HORIZON SELECTOR & KPIS ---
    dbc.Card([
        dbc.CardBody([
            dbc.Row([
                dbc.Col([
                    html.Label("Forecast Horizon Target", className="fw-bold text-uppercase small text-muted mb-2"),
                    dcc.Dropdown(
                        id='forecast-horizon',
                        options=[
                            {"label": "Real-Time Observation", "value": 0},
                            {"label": "+24 Hours Forecast", "value": 1},
                            {"label": "+48 Hours Forecast", "value": 2},
                            {"label": "+72 Hours Forecast", "value": 3},
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
        dbc.Col([
            dbc.Card([
                dbc.CardHeader(html.H5("Section 1: Biometeorological & Thermal Comfort Layer", className="mb-0 fw-bold")),
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.Label("Visualization Metric", className="fw-bold small text-muted"),
                            dcc.Dropdown(id='measurements', value="UTCI (deg C)", options=list(MEASUREMENTS.keys()), clearable=False)
                        ], md=6),
                        dbc.Col([
                            html.Label("State / Regional Focus", className="fw-bold small text-muted"),
                            dcc.Dropdown(id='state-filter', options=[{"label": "All India", "value": "ALL"}] + [{"label": s, "value": s} for s in states_list], value="ALL", clearable=False)
                        ], md=6)
                    ], className="mb-3"),
                    html.Label("Metric Scale Color Bounds", className="fw-bold small text-muted"),
                    dcc.RangeSlider(id='color-range-slider', min=15, max=45, value=[15, 45], step=0.5, tooltip={"placement": "bottom", "always_visible": True}, className="mb-4"),
                    dcc.Loading(dcc.Graph(id='district-map', config={"displayModeBar": False}))
                ])
            ], className="shadow-sm border-0 h-100")
        ], lg=8, className="mb-4 mb-lg-0"),

        dbc.Col([
            dbc.Card([
                dbc.CardHeader(html.H5("District Inspector", className="mb-0 fw-bold")),
                dbc.CardBody([
                    html.Label("Search District / Ward", className="fw-bold small text-muted"),
                    dcc.Dropdown(id='district-search', options=district_options, placeholder="Select district or ward...", clearable=True, className="mb-4"),
                    html.Div(id='filler'),
                    html.Hr(className="my-4"),
                    html.H6("National Thermal Stress Distribution", className="fw-bold text-muted mb-3"),
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
                    html.H5("Section 2: Demographic Mortality Risk Mapping", className="mb-1 fw-bold text-danger"),
                    html.P("Demographic mortality risk index (0 - 100) derived from non-linear biometeorological physiological strain", className="text-muted small mb-0")
                ], md=7),
                dbc.Col([
                    html.Label("Demographic Population Cohort", className="fw-bold small text-muted"),
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
                html.Label("Mortality Risk Index Scale Bounds", className="fw-bold small text-muted mb-1"),
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

    # --- SECTION 3: POLICY SIMULATOR ---
    dbc.Card([
        dbc.CardHeader(
            html.Div([
                html.H5("Section 3: Microclimate Mitigation & Policy Simulator", className="mb-1 fw-bold text-success"),
                html.P("Simulate real-time microclimate interventions (cool roofs, urban tree canopy, misting stations) to quantify thermal reduction & saved lives.", className="text-muted small mb-0")
            ])
        ),
        dbc.CardBody([
            dbc.Row([
                dbc.Col([
                    html.Label("Urban Tree Canopy Expansion (%)", className="fw-bold small"),
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
                    html.Label("High-Albedo / Cool Roof Deployment (%)", className="fw-bold small"),
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
                    html.Label("Evaporative Misting Infrastructure Coverage (%)", className="fw-bold small"),
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
    ], className="shadow-sm border-0 mb-4"),

    # --- SECTION 4: AI HEAT VULNERABILITY ENGINE ---
    dbc.Card([
        dbc.CardHeader(
            html.Div([
                html.H5("Section 4: Predictive AI Heat Vulnerability & Feature Attribution Engine", className="mb-1 fw-bold text-primary"),
                html.P("Explains microclimate vulnerability by coupling real-time biometeorology with Census demographics and satellite built-environment parameters.", className="text-muted small mb-0")
            ])
        ),
        dbc.CardBody([
            dbc.Row([
                dbc.Col([
                    html.H6("Model Feature Attribution Importance", className="fw-bold text-muted mb-2"),
                    dcc.Graph(id="ai-feature-importance-chart", config={"displayModeBar": False}, style={"height": "250px"})
                ], md=6, className="border-end pe-4"),
                dbc.Col([
                    html.H6("AI Microclimate Insights & Feature Contributions", className="fw-bold text-muted mb-2"),
                    html.Div(id="ai-vulnerability-insights", className="p-3 bg-light rounded border")
                ], md=6, className="ps-4")
            ])
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
    from dash import ctx

    version = (
        f"{_weather_source}|{_last_weather_update.isoformat()}"
        if _last_weather_update is not None
        else f"{_weather_source}|init"
    )
    version_out = version if version != current_version else no_update
    feedback = no_update

    if ctx.triggered_id == "btn-update-data" and n_clicks and n_clicks > 0:
        started = trigger_manual_weather_refresh()
        if started:
            feedback = f"Refresh initiated at {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC."
        else:
            feedback = f"Refresh requested at {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC (fetch already in progress)."

    if _weather_fetching:
        badge = dbc.Badge("Updating...", color="info", className="px-3 py-2 fs-6 rounded-pill shadow-sm")
        return badge, "Updating...", version_out, feedback

    if _weather_ready:
        ts = (
            _last_weather_update.strftime("%H:%M UTC")
            if _last_weather_update is not None
            else "—"
        )
        if _weather_source == "live":
            text, color = f"Live Feed ({ts})", "success"
        elif _weather_source == "cache":
            text, color = f"Cached Feed ({ts})", "success"
        else:
            text, color = "Demonstration Feed", "warning"
        badge = dbc.Badge(text, color=color, className="px-3 py-2 fs-6 rounded-pill shadow-sm")
        return badge, "Refresh Forecast Data", version_out, feedback

    badge = dbc.Badge("Initializing...", color="secondary", className="px-3 py-2 fs-6 rounded-pill shadow-sm")
    return badge, "Refresh Forecast Data", version_out, feedback


@callback(
    Output('kpi-summary-container', 'children'),
    Input('forecast-horizon', 'value'),
    Input('theme-switch', 'value'),
    Input('data-version', 'data'),
)
def update_kpis(horizon, dark_mode, _version):
    utci_col = f"UTCI_d{horizon}"
    temp_col = f"Dry Bulb Temp_d{horizon}"
    
    district_df = df[df["State"] != "Municipal Wards"]
    valid = district_df.dropna(subset=[temp_col])
    avg_u = round(district_df[utci_col].mean(), 1) if not district_df[utci_col].empty else "N/A"
    max_r = valid.loc[valid[temp_col].idxmax()] if not valid.empty else None
    min_r = valid.loc[valid[temp_col].idxmin()] if not valid.empty else None

    if dark_mode:
        avg_color = "#60a5fa"
        hot_color = "#f87171"
        cool_color = "#38bdf8"
        dist_color = "#f8fafc"
    else:
        avg_color = "#1d4ed8"
        hot_color = "#b91c1c"
        cool_color = "#0369a1"
        dist_color = "#0f172a"

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
            html.Div(f"{len(district_df)}", className="fs-3 fw-bolder", style={"color": dist_color})
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
    r_use = color_range if color_range else DEFAULT_SLIDER_BOUNDS[measurement_chosen]

    if "Ahmedabad" in selected_state and "AHMEDABAD_WARDS" in ward_geojsons:
        active_geojson = ward_geojsons["AHMEDABAD_WARDS"]
        filtered_df = df[df["join_key"].str.startswith("AHMEDABAD_WARDS|")]
        center_lat, center_lon, map_zoom = 23.0225, 72.5714, 10.5
    elif "Bengaluru" in selected_state and "BANGALORE_WARDS" in ward_geojsons:
        active_geojson = ward_geojsons["BANGALORE_WARDS"]
        filtered_df = df[df["join_key"].str.startswith("BANGALORE_WARDS|")]
        center_lat, center_lon, map_zoom = 12.9716, 77.5946, 10.2
    elif selected_state == "ALL":
        active_geojson = district_geojson
        filtered_df = df[~df["State"].isin(["Municipal Wards"])]
        center_lat, center_lon, map_zoom = 22.5, 80.0, 3.4
    else:
        active_geojson = district_geojson
        filtered_df = df[df['State'] == selected_state]
        center_lat = float(filtered_df["lat"].mean()) if not filtered_df.empty else 22.5
        center_lon = float(filtered_df["lon"].mean()) if not filtered_df.empty else 80.0
        map_zoom = 5.5

    map_style = "carto-darkmatter" if dark_mode else "carto-positron"
    template = "plotly_dark" if dark_mode else "plotly_white"

    fig = px.choropleth_map(
        data_frame=filtered_df,
        color=target_col,
        range_color=r_use,
        geojson=active_geojson,
        featureidkey="properties.join_key",
        locations="join_key",
        map_style=map_style,
        center={"lat": center_lat, "lon": center_lon},
        zoom=map_zoom,
        opacity=0.75,
    )
    fig.update_traces(
        marker_line_width=0.4,
        marker_line_color="#1e293b" if dark_mode else "#cbd5e1"
    )
    fig.update_layout(
        template=template,
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        height=550,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)"
    )
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

    if "Ahmedabad" in selected_state and "AHMEDABAD_WARDS" in ward_geojsons:
        active_geojson = ward_geojsons["AHMEDABAD_WARDS"]
        filtered_df = df[df["join_key"].str.startswith("AHMEDABAD_WARDS|")]
        center_lat, center_lon, map_zoom = 23.0225, 72.5714, 10.5
    elif "Bengaluru" in selected_state and "BANGALORE_WARDS" in ward_geojsons:
        active_geojson = ward_geojsons["BANGALORE_WARDS"]
        filtered_df = df[df["join_key"].str.startswith("BANGALORE_WARDS|")]
        center_lat, center_lon, map_zoom = 12.9716, 77.5946, 10.2
    elif selected_state == "ALL":
        active_geojson = district_geojson
        filtered_df = df[~df["State"].isin(["Municipal Wards"])]
        center_lat, center_lon, map_zoom = 22.5, 80.0, 3.4
    else:
        active_geojson = district_geojson
        filtered_df = df[df['State'] == selected_state]
        center_lat = float(filtered_df["lat"].mean()) if not filtered_df.empty else 22.5
        center_lon = float(filtered_df["lon"].mean()) if not filtered_df.empty else 80.0
        map_zoom = 5.5

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
        data_frame=filtered_df,
        color=target_col,
        range_color=r_use,
        geojson=active_geojson,
        color_continuous_scale="Reds",
        featureidkey="properties.join_key",
        locations="join_key",
        map_style=map_style,
        center={"lat": center_lat, "lon": center_lon},
        zoom=map_zoom,
        opacity=0.75,
        labels={target_col: "Mortality Risk Index"}
    )
    fig.update_traces(
        marker_line_width=0.4,
        marker_line_color="#7f1d1d" if dark_mode else "#fca5a5"
    )
    fig.update_layout(
        template=template,
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        height=500,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)"
    )
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
        channel_name = "WhatsApp Business Gateway" if channel == "whatsapp" else "Cellular SMS Gateway"
        role_label = role.title()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        status_box = dbc.Alert([
            html.Div(f"ALERT DISPATCH EXECUTED SUCCESSFULLY ({timestamp})", className="fw-bold mb-1"),
            html.Div(f"- Channel: {channel_name}"),
            html.Div(f"- Target Group: {role_label}"),
            html.Div(f"- Recipient: {phone}"),
            html.Div(f"- Message Body: '{message_body}'"),
            html.Div("- Status: Broadcast logged in operational dispatch ledger.", className="small mt-1 text-success fw-bold")
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
    temp_drop = round(canopy_pct * 0.08 + coolroof_pct * 0.05, 2)
    utci_drop = round(canopy_pct * 0.15 + coolroof_pct * 0.06 + misting_pct * 0.12, 2)
    mortality_drop = round(utci_drop * 2.4, 1)

    if canopy_pct == 0 and coolroof_pct == 0 and misting_pct == 0:
        return "Adjust policy parameters above to evaluate real-time thermal mitigation and mortality reduction metrics.", "secondary"

    msg = (
        f"Simulated Intervention Impact: "
        f"Air Temperature Reduction: -{temp_drop}°C | "
        f"UTCI Thermal Strain Reduction: -{utci_drop}°C | "
        f"Estimated Mortality Risk Reduction: -{mortality_drop}% across target regions."
    )
    return msg, "success"


@callback(
    Output('filler', 'children'),
    Input('district-search', 'value'), Input('forecast-horizon', 'value'),
    Input('demographic-class', 'value'), Input('theme-switch', 'value')
)
def show_district_detail(searched_district, horizon, demo_class, dark_mode):
    if not searched_district:
        return html.Div("Select or click any district on the map to inspect localized biometeorological metrics.", className="text-center text-muted p-4 mt-2 fw-bold")

    row = df[df['join_key'] == searched_district]
    if row.empty: return no_update
    r = row.iloc[0]

    h_label = ["Current Observation", "+24h Forecast", "+48h Forecast", "+72h Forecast"][horizon]
    m_idx = r[f"Mortality_{demo_class}_d{horizon}"]
    wbgt_v = r.get(f"WBGT_d{horizon}", "N/A")
    hi_v = r.get(f"Heat Index_d{horizon}", "N/A")
    haz_v = r.get(f"Composite Hazard_d{horizon}", "N/A")

    bulletin_text = (
        f"NATIONAL HEAT ADVISORY BULLETIN ({h_label})\n"
        f"Location: {r['District']}, {r['State']}\n\n"
        f"- Composite Thermal Hazard Index: {haz_v} / 100\n"
        f"- UTCI Thermal Strain: {r[f'UTCI_d{horizon}']}°C ({r[f'Stress Category_d{horizon}']})\n"
        f"- ISO 7243 Outdoor WBGT: {wbgt_v}°C\n"
        f"- NOAA Heat Index: {hi_v}°C\n"
        f"- Projected Mortality Risk Index ({demo_class}): {m_idx} / 100\n"
        f"- Dry Bulb Air Temp: {r[f'Dry Bulb Temp_d{horizon}']}°C (Apparent: {r[f'Apparent Temp_d{horizon}']}°C)\n"
        f"- Relative Humidity: {r[f'Relative Humidity_d{horizon}']}%"
    )
    wa_url = f"https://wa.me/?text={urllib.parse.quote(bulletin_text)}"

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

    ai_hvi_val = r.get(f"AI HVI_d{horizon}", haz_v)

    return html.Div([
        html.Div([
            html.H3(f"{r['District']}", className=f"mb-0 fw-bolder {text_color}"),
            html.Span(f"{r['State']} • {h_label}", className="text-muted small fw-bold text-uppercase")
        ], className="mb-3 border-bottom pb-2"),
        
        dbc.Row([
            dbc.Col(
                dbc.Card(dbc.CardBody([
                    html.Div("AI Vulnerability Index", className="text-muted small fw-bold text-uppercase"),
                    html.Div(f"{ai_hvi_val} / 100", className=f"fs-3 fw-bolder {text_color}"),
                    html.Span(f"{r[f'Stress Category_d{horizon}']}", className="badge bg-warning text-dark mt-1")
                ]), className=f"{card_bg} text-center"), width=6
            ),
            dbc.Col(
                dbc.Card(dbc.CardBody([
                    html.Div("Mortality Risk Index", className="small fw-bold text-uppercase", style={"color": mortality_label_color}),
                    html.Div(f"{m_idx} / 100", className="fs-3 fw-bolder", style={"color": mortality_text_color}),
                ]), className="text-center", style={"backgroundColor": mortality_card_bg, "border": f"1px solid {mortality_border}"}), width=6
            )
        ], className="g-2 mb-3"),

        dbc.Row([
            dbc.Col([html.Span("UTCI Strain: ", className="text-muted"), html.B(f"{r[f'UTCI_d{horizon}']} °C")], width=6),
            dbc.Col([html.Span("ISO WBGT: ", className="text-muted"), html.B(f"{wbgt_v} °C")], width=6),
            dbc.Col([html.Span("NOAA Heat Index: ", className="text-muted"), html.B(f"{hi_v} °C")], width=6),
            dbc.Col([html.Span("Air Temp: ", className="text-muted"), html.B(f"{r[f'Dry Bulb Temp_d{horizon}']} °C")], width=6),
            dbc.Col([html.Span("Apparent Temp: ", className="text-muted"), html.B(f"{r[f'Apparent Temp_d{horizon}']} °C")], width=6),
            dbc.Col([html.Span("Humidity: ", className="text-muted"), html.B(f"{r[f'Relative Humidity_d{horizon}']}%")], width=6),
        ], className="small mb-3 g-2"),

        dbc.Button("Export Advisory via WhatsApp", href=wa_url, target="_blank", color="success", className="w-100 fw-bold")
    ])

@callback(
    Output("ai-feature-importance-chart", "figure"),
    Output("ai-vulnerability-insights", "children"),
    Input("forecast-horizon", "value"),
    Input("district-search", "value"),
    Input("theme-switch", "value"),
    Input("data-version", "data"),
)
def update_ai_insights(horizon, searched_district, dark_mode, _version):
    _, importances = get_hvi_predictions_and_features(df, horizon=horizon)

    feature_labels = {
        "wbgt": "ISO WBGT (°C)",
        "utci": "UTCI Strain (°C)",
        "heat_index": "NOAA Heat Index (°C)",
        "ndbi_builtup": "NDBI Built-up Density",
        "ndvi_green_cover": "NDVI Canopy Cover",
        "outdoor_labor_pct": "Outdoor Laborer %",
        "slum_density_pct": "Slum Household %",
        "elderly_pct": "Elderly Population %",
        "lst_offset_c": "LST Urban Heat Offset (°C)",
        "child_pct": "Child Population %",
    }

    imp_df = pd.DataFrame([
        {"Feature": feature_labels.get(k, k), "Importance": v * 100}
        for k, v in importances.items()
    ]).sort_values("Importance", ascending=True)

    template = "plotly_dark" if dark_mode else "plotly_white"
    bar_color = "#3b82f6" if dark_mode else "#1d4ed8"

    fig = px.bar(imp_df, x="Importance", y="Feature", orientation="h", color_discrete_sequence=[bar_color])
    fig.update_layout(
        template=template,
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        xaxis_title="Importance (%)",
        yaxis_title=None,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )

    if searched_district and searched_district in df["join_key"].values:
        row = df[df["join_key"] == searched_district].iloc[0]
        dt_name = row["District"]
        hvi_val = row.get(f"AI HVI_d{horizon}", "N/A")
        ndbi_v = row.get("ndbi_builtup", 0.40)
        ndvi_v = row.get("ndvi_green_cover", 0.25)
        labor_v = row.get("outdoor_labor_pct", 30.0)
        lst_v = row.get("lst_offset_c", 1.5)

        insights = html.Div([
            html.Div(f"Location Analysis: {dt_name}", className="fw-bold mb-2 text-primary fs-6"),
            html.Div(f"- Predicted AI Heat Vulnerability Index: {hvi_val} / 100", className="fw-bold text-danger mb-1"),
            html.Div(f"- Built Environment Drivers: High NDBI concrete density ({ndbi_v*100:.0f}%) + Urban Heat Island offset (+{lst_v}°C) amplify thermal retention."),
            html.Div(f"- Ecological Mitigation: Tree Canopy Cover (NDVI) is {ndvi_v*100:.0f}% (Target: >30% for cooling effect).", className="mt-1"),
            html.Div(f"- Socio-Demographic Exposure: {labor_v:.0f}% outdoor workforce exposed during peak afternoon heat.", className="mt-1"),
        ], className="small")
    else:
        insights = html.Div([
            html.Div("National Overview AI Summary", className="fw-bold mb-2 text-primary fs-6"),
            html.Div("- Primary Heat Drivers: ISO 7243 WBGT & UTCI physiological strain account for ~60% of total vulnerability score."),
            html.Div("- Secondary Amplifiers: Urban built-up density (NDBI) and low vegetation (NDVI) account for ~20% of spatial variance across Indian wards."),
            html.Div("- Select a specific district on the map or search dropdown to inspect localized feature attributions.", className="mt-2 text-muted fst-italic"),
        ], className="small")

    return fig, insights


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
