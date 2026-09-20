# System Architecture & Technical Specifications

This document outlines the software architecture, data pipelines, biometeorological equations, machine learning algorithms, and system optimization strategies of the **SIH 2026 Problem Statement 83 National Heatwave Early Warning & Biometeorological Decision Support System**.

---

## 1. High-Level Data & Control Flow

```
+-----------------------------------------------------------------------------------+
|                                DATA SOURCES & APIs                                |
|  • Open-Meteo NWP Forecast API (Temperature, Humidity, Wind Speed, Apparent Temp) |
|  • Census 2011 / Socio-Demographic Microdata                                      |
|  • Landsat / Sentinel Remote Sensing Proxies (NDVI, NDBI, LST Offset)             |
|  • Vector Boundaries: India Districts & Municipal Wards (GeoJSON)                 |
+-----------------------------------------------------------------------------------+
                                          │
                                          ▼
+-----------------------------------------------------------------------------------+
|                            DATA INGESTION & CACHE LAYER                           |
|  • Multi-point spatial batching (40 locations/batch with rate-limit retries)      |
|  • Disk Cache Persistence (weather_cache.pkl with configurable TTL)               |
|  • Non-blocking background thread worker loop (3-hour refresh cycle)              |
+-----------------------------------------------------------------------------------+
                                          │
                                          ▼
+-----------------------------------------------------------------------------------+
|                          BIOMETEOROLOGICAL ENGINE (src/)                          |
|  • Universal Thermal Climate Index (UTCI) via multi-nodal physiological model    |
|  • ISO 7243 Wet Bulb Globe Temperature (WBGT) shade & solar adjustment           |
|  • NOAA Rothfusz Heat Index (HI)                                                  |
|  • Composite Thermal Hazard Index (0 - 100 Normalized)                            |
|  • Demographic Mortality Probability Index (Elderly, Adults, Children)            |
+-----------------------------------------------------------------------------------+
                                          │
                                          ▼
+-----------------------------------------------------------------------------------+
|                        MACHINE LEARNING ENGINE (src/ml_engine.py)                 |
|  • Feature Matrix Assembly: Biometeorology + Socio-Demographics + Remote Sensing  |
|  • Scikit-Learn Random Forest Regressor (Ensemble of 100 Decision Trees)          |
|  • AI Heat Vulnerability Index (HVI 0 - 100) Prediction                            |
|  • Feature Attribution Importance Calculation (XAI Bar Charts & Text Insights)     |
+-----------------------------------------------------------------------------------+
                                          │
                                          ▼
+-----------------------------------------------------------------------------------+
|                       INTERACTIVE DASHBOARD UI (app.py)                           |
|  • MapLibre Tile Vector Engine (Carto-Positron / Carto-Darkmatter)                |
|  • Dual Map Layers: Thermal Comfort & Demographic Mortality                       |
|  • Interactive Urban Cooling Intervention Simulator                               |
|  • Multi-Channel Emergency Advisory Gateway (WhatsApp & SMS)                      |
+-----------------------------------------------------------------------------------+
```

---

## 2. Biometeorological Equations & Mathematical Models

### 2.1 Universal Thermal Climate Index (UTCI)
Calculated using the `pythermalcomfort` implementation derived from Fiala's 187-node human thermoregulation model:
$$\text{UTCI} = f(T_{\text{db}}, T_{\text{mrt}}, v_{10\text{m}}, \text{RH})$$
* **$T_{\text{db}}$:** Dry bulb air temperature ($^\circ\text{C}$)
* **$T_{\text{mrt}}$:** Mean radiant temperature ($^\circ\text{C}$)
* **$v_{10\text{m}}$:** Wind speed at 10 meters height ($\text{m/s}$), clamped to $v \ge 0.5\,\text{m/s}$ to prevent numeric underflow.
* **$\text{RH}$:** Relative humidity ($\%$)

### 2.2 ISO 7243 Outdoor Wet Bulb Globe Temperature (WBGT)
Derived via Australian Bureau of Meteorology / Liljegren outdoor approximation:
$$e = \frac{\text{RH}}{100} \cdot 6.105 \cdot \exp\left(\frac{17.27 \cdot T_{\text{db}}}{237.7 + T_{\text{db}}}\right)$$
$$\text{WBGT}_{\text{shade}} = 0.567 \cdot T_{\text{db}} + 0.393 \cdot e + 3.94$$
$$\text{WBGT}_{\text{outdoor}} = \text{WBGT}_{\text{shade}} + \max\left(-1.0, \frac{S}{800} \cdot 2.2 - (v - 1.0) \cdot 0.4\right)$$
where $S = 800\,\text{W/m}^2$ represents standard solar irradiance.

### 2.3 NOAA Rothfusz Heat Index (HI)
$$\text{HI} = -42.379 + 2.04901523 T + 10.14333127 R - 0.22475541 T R - 0.00683783 T^2 - 0.05481717 R^2 + 0.00122874 T^2 R + 0.00085282 T R^2 - 0.00000199 T^2 R^2$$
where $T$ is temperature in $^\circ\text{F}$ and $R$ is relative humidity in $\%$. Result is converted back to $^\circ\text{C}$.

### 2.4 Composite Thermal Hazard Index
Normalized weighted index representing physical heat load:
$$\text{Hazard} = 0.35 \cdot \text{Norm}(\text{HI}) + 0.40 \cdot \text{Norm}(\text{WBGT}) + 0.25 \cdot \text{Norm}(\text{UTCI})$$
$$\text{Norm}(x, x_{\min}, x_{\max}) = \min\left(100, \max\left(0, \frac{x - x_{\min}}{x_{\max} - x_{\min}} \cdot 100\right)\right)$$

---

## 3. Random Forest Machine Learning Vulnerability Engine

The machine learning module (`src/ml_engine.py`) models spatial vulnerability by coupling meteorological forcing variables with socio-demographic sensitivity and satellite built-environment exposure.

### 3.1 Feature Matrix Specifications

| Feature Column Name | Source | Description | Weight Range in HVI |
| :--- | :--- | :--- | :--- |
| `wbgt` | Biometeorological Engine | ISO 7243 Outdoor WBGT ($^\circ\text{C}$) | High ($25 - 35\%$) |
| `utci` | Biometeorological Engine | UTCI Thermal Strain ($^\circ\text{C}$) | High ($20 - 30\%$) |
| `heat_index` | Biometeorological Engine | NOAA Heat Index ($^\circ\text{C}$) | Medium ($10 - 15\%$) |
| `ndbi_builtup` | Sentinel-2 Remote Sensing | NDBI Concrete/Built-up Index ($0 - 1$) | Medium ($8 - 12\%$) |
| `ndvi_green_cover` | Landsat 8/9 Remote Sensing | NDVI Vegetation/Canopy Index ($0 - 1$) | Medium ($6 - 10\%$) |
| `outdoor_labor_pct` | Census 2011 Microdata | Outdoor Construction & Ag Workforce $\%$ | Medium ($5 - 8\%$) |
| `slum_density_pct` | Census 2011 Microdata | Informal Settlement & Slum Household $\%$ | Low-Med ($4 - 7\%$) |
| `elderly_pct` | Census 2011 Microdata | Population aged 60+ years $\%$ | Low-Med ($3 - 5\%$) |
| `lst_offset_c` | MODIS LST Product | Urban Heat Island Temperature Offset ($^\circ\text{C}$) | Low ($2 - 4\%$) |
| `child_pct` | Census 2011 Microdata | Population aged 0-5 years $\%$ | Low ($2 - 3\%$) |

### 3.2 Model Training & Explainable AI (XAI)
* **Model Class:** `sklearn.ensemble.RandomForestRegressor`
* **Hyperparameters:** `n_estimators=100`, `max_depth=12`, `random_state=42`.
* **Feature Importances:** Extracted via Gini impurity reduction ($\sum I_j = 1.0$) and rendered dynamically in Section 4 of the dashboard as horizontal bar charts.

---

## 4. Dataset Schema & Spatial Joins

### 4.1 Master Join Key Convention
To handle overlapping district names across different Indian states and enable sub-district ward drilldowns, all datasets use a composite string key:
* **District Join Key:** `{State_Name}|{District_Name}` (e.g., `Gujarat|Ahmadabad` or `Karnataka|BANGALORE URBAN`).
* **Municipal Ward Join Key:** `{Ward_Dataset_Key}|{Ward_Name}` (e.g., `AHMEDABAD_WARDS|Ward 1 - Ranip` or `BANGALORE_WARDS|Ward 150 - Bellandur`).

---

## 5. Cloud Optimization & Low Memory Architecture

To run reliably on Render's free tier ($512\,\text{MB}$ RAM hard limit) without incurring out-of-memory worker kills:
1. **GeoJSON Simplification:** Boundary files are simplified using Douglas-Peucker polygon reduction (`India-Districts-slim.json` is reduced to $2.7\,\text{MB}$).
2. **Lazy Ward Loading:** Municipal ward GeoJSONs ($48$ Ahmedabad wards and $243$ Bengaluru wards) are parsed into memory on demand.
3. **Pre-allocated Structures:** DataFrames utilize float32/float64 primitive arrays. Peak memory footprint remains comfortably below **$200\,\text{MB}$ RAM**.
