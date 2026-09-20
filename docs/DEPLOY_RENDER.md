# Production Cloud Deployment Guide (Render & WSGI)

This document provides step-by-step instructions for deploying the **SIH 2026 Problem Statement 83 Decision Support System** on Render or any WSGI-compliant cloud host within 512 MB RAM constraints.

---

## 1. Directory Structure Requirements

Ensure the following mature file layout is committed to your repository:

```
app.py
requirements.txt
src/
  └── ml_engine.py
data/
  ├── spatial_features.csv
  ├── hvi_rf_model.joblib
  ├── geo/
  │   ├── district_centroids.xlsx
  │   └── India-Districts-slim.json
  └── municipal_wards/
      ├── ahmedabad_wards.geojson
      └── bangalore_wards.geojson
```

Do **not** upload unsimplified vector GeoJSON files (>25 MB).

---

## 2. Render Cloud Configuration

1. Log in to [Render Dashboard](https://dashboard.render.com).
2. Click **New +** → **Web Service**.
3. Connect your GitHub repository.
4. Configure service settings:

| Setting | Value |
| :--- | :--- |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn app:server --workers 1 --threads 2 --timeout 120 --bind 0.0.0.0:$PORT` |
| **Instance Type** | Free or Starter (512 MB RAM) |

---

## 3. Critical Start Command Specification

Copy this exact command to ensure proper health check port binding:

```bash
gunicorn app:server --workers 1 --threads 2 --timeout 120 --bind 0.0.0.0:$PORT
```

### Key Parameters:
* `--workers 1`: Keeps memory consumption within 200 MB (additional workers multiply memory usage).
* `--threads 2`: Enables multi-threaded request processing without duplicating process memory.
* `--timeout 120`: Prevents timeout kills during initial weather cache warming.
* `--bind 0.0.0.0:$PORT`: Required for Render's automatic HTTP health checks.

---

## 4. Environment Variables (Optional Defaults)

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `CENTROIDS_FILE` | `data/geo/district_centroids.xlsx` | Path to district centroids Excel |
| `GEOJSON_FILE` | `data/geo/India-Districts-slim.json` | Path to slim vector GeoJSON |
| `SPATIAL_FEATURES_FILE` | `data/spatial_features.csv` | Path to static Census & satellite CSV |
| `WEATHER_REFRESH_HOURS` | `3` | Hours between Open-Meteo background refreshes |
| `FETCH_MAX_SECONDS` | `90` | Hard wall-clock cap for live API fetches |

---

## 5. Memory Footprint Breakdown

| Subsystem Component | Peak Memory Usage |
| :--- | :--- |
| Python Runtime + Dependencies (Pandas, Scikit-Learn, Dash) | ~140 - 180 MB |
| Slim GeoJSON Vector Boundaries in Memory | ~8 MB |
| DataFrames (Centroids, Weather, Spatial Features) | ~4 MB |
| Dash / Plotly Graphing Engine Runtime | ~40 - 60 MB |
| **Total Peak Memory Consumption** | **~192 - 252 MB RAM** |

*Comfortably compliant with the 512 MB Render limit.*
