# SIH 2026 Problem Statement 83: National Heatwave Early Warning & Biometeorological Decision Support System

An enterprise-grade, predictive biometeorological decision support platform developed for **Smart India Hackathon (SIH) 2026 Problem Statement 83** (*Early Warning Systems for Heatwaves and Thermal Stress Index*).

This platform integrates real-time satellite biometeorology, multi-index physiological strain modelling, high-resolution Census demographics, urban built-environment remote sensing, Random Forest machine learning vulnerability predictions, interactive urban cooling simulators, and automated multi-channel emergency advisory dispatch gateways.

---

## Technical Architecture Overview

```
                                  [ Open-Meteo Global NWP API ]
                                                │
                                                ▼
  [ Spatial Census & Remote Sensing Data ] ──► [ Data Ingestion & Cache Engine ]
  • 641 Districts                              (Multi-Day Multi-Point NWP)
  • 291 Municipal Wards (Ahmedabad & BLR)      │
  • Census Demographics & Remote Sensing       ▼
                                       [ Biometeorological Engine ]
                                       • Universal Thermal Climate Index (UTCI)
                                       • ISO 7243 Outdoor Wet Bulb Globe Temp (WBGT)
                                       • NOAA Rothfusz Heat Index (HI)
                                       • Composite Thermal Hazard Index
                                                │
                                                ▼
                                  [ Random Forest Machine Learning ]
                                  • Composite Heat Vulnerability Index (HVI)
                                  • Explainable AI (XAI) Feature Importance
                                                │
                                                ▼
                                  [ Dash / Plotly Interactive Portal ]
                                  • MapLibre Spatial Choropleth Engine
                                  • District & Ward Level Inspector
                                  • Microclimate Policy Simulator
                                  • Automated Advisory Dispatch Gateway
```

---

## Core Capabilities & Technical Innovations

### 1. Multi-Index Biometeorological Modeling
Standard heatwave systems rely exclusively on dry-bulb air temperature, failing to account for humidity, wind speed, solar radiation, and human thermoregulation. This platform computes four distinct thermal stress metrics across a 4-day forecast horizon (Real-Time Current, +24h, +48h, +72h):
* **Universal Thermal Climate Index (UTCI):** Physiological strain model calculated via multi-nodal thermoregulation equations based on dry-bulb temperature, mean radiant temperature, relative humidity, and 10m wind speed.
* **ISO 7243 Wet Bulb Globe Temperature (WBGT):** Standardized occupational heat stress metric accounting for solar radiation dissipation and evaporative cooling limits for outdoor workers.
* **NOAA Heat Index (HI):** Rothfusz regression equation expressing apparent human-perceived temperature in shaded environments.
* **Composite Thermal Hazard Index:** Weighted multi-index aggregation ($0.35 \cdot \text{HI} + 0.40 \cdot \text{WBGT} + 0.25 \cdot \text{UTCI}$) normalized on a $0 - 100$ scale.

### 2. High-Resolution Spatial Dataset & Sub-District Drilldowns
* **641 Monitored Districts:** Full coverage of all Indian districts mapped against slim vector boundaries (`data/geo/India-Districts-slim.json`).
* **Municipal Ward Geospatial Layers:** Sub-district drilldown capabilities for **Ahmedabad Municipal Corporation (48 wards)** and **Bengaluru Bruhat Bengaluru Mahanagara Palike (243 wards)**.
* **Integrated Microclimate Features (`data/spatial_features.csv`):**
  * **Census Demographics:** Outdoor labor %, elderly (60+ yrs) %, child (0-5 yrs) %, slum household density %.
  * **Satellite Remote Sensing:** Normalized Difference Vegetation Index (NDVI Canopy Cover), Normalized Difference Built-up Index (NDBI Built-up Density), and Land Surface Temperature (LST Urban Heat Island offset °C).

### 3. Machine Learning Vulnerability Engine & Explainable AI (XAI)
* **Random Forest ML Model (`src/ml_engine.py`):** Trains an ensemble model across biometeorological strain parameters, socio-demographic exposures, and urban satellite indices to predict a composite **AI Heat Vulnerability Index (HVI $0-100$)**.
* **XAI Feature Attribution:** Computes feature importances to quantify whether thermal risk in a specific district or ward is driven primarily by meteorological heat load (WBGT/UTCI) or local vulnerability amplifiers (concrete density, lack of canopy cover, outdoor workforce concentration).

### 4. Demographic Mortality Risk Index
* Computes demographic-specific mortality probability indices ($0 - 100$) derived from non-linear physiological strain functions adjusted by risk coefficients:
  * **Elderly (60+ yrs):** Multiplier $1.8\times$
  * **Children (0-5 yrs):** Multiplier $1.3\times$
  * **Adults (18-59 yrs):** Multiplier $1.0\times$

### 5. Interactive "What-If" Urban Cooling Simulator
Allows urban planners and disaster management authorities to model real-time cooling interventions:
* **Urban Tree Canopy Expansion (%):** Models radiant and air temperature reduction ($-0.08^\circ\text{C}$ per %).
* **High-Albedo / Cool Roof Deployment (%):** Models air temperature reduction ($-0.05^\circ\text{C}$ per %).
* **Evaporative Misting Infrastructure (%):** Models direct UTCI thermal strain reduction ($-0.12^\circ\text{C}$ per %).

### 6. Automated Emergency Heat Advisory Gateway
Simulates targeted emergency advisory dispatches:
* **Multi-Channel Dispatch:** Integrates WhatsApp Business API and Cellular SMS Gateway.
* **Role-Based Targeting:** Tailored alert body templates for outdoor workforce managers, vulnerable elderly households, school administrators, medical first responders, and municipal disaster authorities.

---

## Project Structure

```
.
├── app.py                          # Main Python Dash web application & callback orchestration
├── requirements.txt                # Production dependency specifications
├── LICENSE                         # Open-source license terms
├── README.md                       # Master system documentation
├── assets/                         # Custom CSS, styling assets, and branding
├── data/                           # Spatial datasets and trained ML models
│   ├── geo/                        # Vector GeoJSON boundaries and centroid coordinates
│   │   ├── India-Districts-slim.json
│   │   └── district_centroids.xlsx
│   ├── municipal_wards/            # High-resolution municipal ward GeoJSONs
│   │   ├── ahmedabad_wards.geojson
│   │   └── bangalore_wards.geojson
│   ├── spatial_features.csv        # Integrated Census demographics & satellite remote sensing
│   └── hvi_rf_model.joblib         # Trained Random Forest AI HVI model binary
├── docs/                           # Architecture, deployment, and strategy documentation
│   ├── ARCHITECTURE.md             # System design, data pipelines, and biometeorology equations
│   ├── DEPLOY_RENDER.md            # Production deployment guide for Render / Cloud hosts
│   └── STRATEGY_AND_ROADMAP.md     # Competitive benchmarks and SIH 2026 presentation roadmap
├── src/                            # Machine learning & data processing source code
│   └── ml_engine.py                # Random Forest ML trainer, predictor & XAI module
└── tests/                          # Automated unit and integration test suite
    ├── test_biomet_and_policy.py   # Tests for UTCI, WBGT, HI, policy simulator, memory bounds
    ├── test_ml_engine.py           # Tests for Random Forest model prediction & feature importance
    └── test_spatial_features.py    # Tests for spatial dataset integrity & ward join keys
```

---

## Installation & Local Development

### Prerequisites
* Python 3.10+ (Python 3.12 recommended)
* Git

### Step-by-Step Setup

1. **Clone the Repository:**
   ```bash
   git clone https://github.com/rakeshkar1717-oss/sih26083-heatwave-warning.git
   cd sih26083-heatwave-warning
   ```

2. **Create and Activate a Virtual Environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt pytest
   ```

4. **Run the Application:**
   ```bash
   python app.py
   ```
   Access the interactive dashboard in your browser at `http://localhost:8050`.

5. **Execute Automated Test Suite:**
   ```bash
   PYTHONPATH=. python -m pytest
   ```

---

## Deployment Configuration

The application is engineered to operate strictly within free cloud tier constraints (e.g., Render 512 MB RAM limit):
* **Memory Management:** Lazy loading of sub-district ward GeoJSONs and pre-allocated Pandas structures maintain total memory consumption under **200 MB RAM**.
* **Network & API Optimization:** Open-Meteo NWP forecasts are batched and cached locally (`weather_cache.pkl`) with a 3-hour refresh cycle to avoid rate limits.
* **Gunicorn Server:** Ready for multi-worker WSGI deployment via `gunicorn app:server`.

For step-by-step production setup, refer to `docs/DEPLOY_RENDER.md`.

---

## Competitive Benchmarks & SIH 2026 Evaluation Summary

| Capability / Metric | Competitor Baseline | SIH 2026 Decision Support System |
| :--- | :--- | :--- |
| **Biometeorological Metrics** | Dry Bulb Air Temp only | UTCI, ISO 7243 WBGT, NOAA Heat Index, Composite Hazard Index |
| **Vulnerability Assessment** | Single-variable rule-based alerts | Multi-variable Random Forest ML Heat Vulnerability Index (HVI) |
| **Spatial Granularity** | State / District level | District Level (641 districts) + Ward Level (Ahmedabad & Bengaluru) |
| **Remote Sensing Integration** | None | Satellite NDVI Canopy, NDBI Built-Up, LST Urban Heat Island offset |
| **Decision Support & Policy** | Static report viewing | Interactive Urban Cooling Simulator & WhatsApp/SMS Advisory Gateway |
| **Memory Footprint** | Unoptimized (>800 MB) | Low-footprint (<200 MB RAM, compliant with 512 MB Render limit) |

---

## License

This project is released under the MIT License.
