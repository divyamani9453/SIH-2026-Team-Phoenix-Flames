import os
import time
import requests
import numpy as np
import pandas as pd
from pythermalcomfort.models import utci

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
BATCH_SIZE = 40
BATCH_PAUSE_SEC = 1.5


def save_weather_cache(dataframe: pd.DataFrame, cache_filepath: str) -> None:
    """Saves serialized weather and biometeorological data columns to disk pickle cache."""
    try:
        cols = [c for c in dataframe.columns if any(
            c.startswith(p) for p in (
                "Dry Bulb Temp_d", "Relative Humidity_d", "Wind Speed_d",
                "Apparent Temp_d", "Mean Radiant Temp_d", "UTCI_d",
                "Stress Category_d", "Mortality_",
            )
        )]
        dataframe[["join_key"] + cols].to_pickle(cache_filepath)
        print(f"[weather] Cache saved to {cache_filepath}", flush=True)
    except Exception as e:
        print(f"[weather] Cache save failed: {e}", flush=True)


def load_weather_cache(dataframe: pd.DataFrame, cache_filepath: str, max_age_hours: float = 6.0) -> bool:
    """Loads weather data from disk pickle cache if valid and unexpired."""
    if not os.path.exists(cache_filepath):
        return False
    try:
        age_h = (time.time() - os.path.getmtime(cache_filepath)) / 3600.0
        if age_h > max_age_hours:
            print(f"[weather] Cache expired ({age_h:.1f}h > {max_age_hours}h)", flush=True)
            return False
        cache_df = pd.read_pickle(cache_filepath)
        if "join_key" not in cache_df.columns:
            return False
        if not set(dataframe["join_key"]).issubset(set(cache_df["join_key"])):
            print(f"[weather] Cache invalid (missing keys for some locations)", flush=True)
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
    """Executes a single HTTP batch query against the Open-Meteo REST API."""
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


def fetch_multi_day_weather(dataframe: pd.DataFrame, fetch_max_seconds: int = 90) -> pd.DataFrame:
    """Fetches 4-day multi-horizon weather observations for all locations in batches."""
    lats, lons = dataframe["lat"].tolist(), dataframe["lon"].tolist()
    all_responses = []
    t0 = time.time()
    consecutive_rate_limits = 0
    aborted = False

    for bi, i in enumerate(range(0, len(dataframe), BATCH_SIZE)):
        if time.time() - t0 > fetch_max_seconds:
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


def fill_synthetic_weather(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Fills dataframe with realistic synthetic weather measurements for offline mode or testing."""
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
