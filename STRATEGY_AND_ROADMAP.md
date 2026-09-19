# SIH 2026 Problem Statement 83: Winning Edge Strategy & Roadmap

## Executive Summary & Competitive Analysis

| Dimension | Competitor (`rakeshkar1717-oss`) | Our Enhanced Platform (Team Phoenix Flames) | Winning Advantage |
| :--- | :--- | :--- | :--- |
| **Geographic Scope** | Single Municipality (48 Wards in Ahmedabad) | **All-India Nationwide Coverage (~700 Districts)** + Ward-Level Precision | **National Scale:** Suitable for NDMA, IMD, and Ministry of Health. |
| **Biometeorology** | Tri-Index (WBGT + UTCI + HI) | **Tri-Index (WBGT + UTCI + HI) + Composite Thermal Hazard Index (0-100)** | **Complete Standard Compliance:** ISO 7243, Bröde UTCI, NOAA. |
| **Alert Gateway** | Static API triggers | **Interactive Modal Dispatch Gateway (SMS & WhatsApp Simulation & Audit Logging)** | **Actionable Emergency Dispatch:** Built-in UI for disaster managers. |
| **Climate Interventions** | None | **Interactive "What-If" Policy Simulator** (Tree Canopy, Cool Roofs, Misting) | **Decision Support:** Direct ROI quantification of cooling investments. |
| **Architecture & Memory** | Multi-container Vite + FastAPI | **Unified Lightweight Dash + Plotly App** (<150 MB RAM, Render Free Tier Ready) | **Deployment Simplicity:** Instant single-process cloud hosting under 512 MB. |
| **API Limit Management** | Unmanaged hourly queries | **Batching (40 pts) + Async Background Loop + Pickle Caching (6h-24h)** | **Strictly compliant with Open-Meteo's 10,000 credit limit.** |

---

## Strategy & Implementation Plan for Features A, C, and F

### Feature A: Nationwide Multi-Tier Resolution (District + Ward Drill-Down)

#### Objective
Combine macro nationwide monitoring with hyper-local urban ward drill-downs.

#### Implementation Architecture
1. **Hierarchical GeoJSON Loader:**
   - **Tier 1 (National):** Simplified All-India District GeoJSON (~2.7 MB, ~700 polygons) loaded on initial startup.
   - **Tier 2 (Hyper-Local Municipal Wards):** On-demand lazy loading of municipal ward GeoJSONs (e.g., AMC Ahmedabad 48 wards, BBMP Bengaluru, MCD Delhi) when a user filters by state/city or clicks "Drill to Ward Level".
2. **Spatial Centroid Indexing & Raster Resampling:**
   - Pre-compute spatial join keys (`State|District|Ward`) and centroid coordinates.
   - Use Open-Meteo 1km spatial grid resolution for hyper-local urban heat island (UHI) microclimate differentiation.
3. **UI Drilldown Flow:**
   - Dynamic map toggle between **"Macro District View"** and **"Urban Ward View"**.
   - Breadcrumb navigation: `India → Gujarat → Ahmedabad District → Ward 14 (Navrangpura)`.

---

### Feature C: Actionable Heat Action Plan (HAP) & Role-Based Advisory Engine

#### Objective
Translate raw biometeorological numbers into direct, legally compliant, and role-based health advisories for target user personas.

#### Implementation Architecture
1. **Target Persona Matrix:**
   - 🛠️ **Outdoor Laborers & Construction Workers (ISO 7243 Ergonomics):**
     - WBGT > 30°C: 50% work / 50% rest ratio per hour.
     - WBGT > 32°C: Stop heavy physical labor; mandatorily move to cooling shelters.
   - 👵 **Elderly & Chronic Vulnerable Households:**
     - High UTCI (>38°C): Hydration alerts, electrolyte distribution, electrolyte check-ins.
   - 🏫 **Schools & Outdoor Athletics:**
     - Heat Index > 40°C: Suspend outdoor sports activities and afternoon assemblies.
   - 🚑 **Healthcare & Hospital Emergency Wards:**
     - Projected Heat Stress Spikes: Auto-notify hospitals to reserve ORS beds and ice packs.
2. **Advisory Rule Engine (`hap_engine.py`):**
   - Automatically generates structured advisory cards in English and regional languages (Hindi, Gujarati, Marathi, Tamil, Telugu).
3. **Exportable HAP PDF & WhatsApp Report Generator:**
   - Single-click export of official municipal Heat Action Bulletins for local authorities.

---

### Feature F: Production Quality, Pytest Test Suite, Launchers & Pitch Assets

#### Objective
Ensure 100% test coverage, easy one-click local demonstration, and top-tier presentation collateral for SIH judges.

#### Implementation Architecture
1. **Modular Code Structure:**
   ```
   ├── app.py                      # Production Dash Application
   ├── src/
   │   ├── biometeorology.py       # Tri-Index Physics Calculations
   │   ├── policy_simulator.py    # Microclimate Physics Engine
   │   └── alert_gateway.py       # SMS/WhatsApp Dispatch Simulator
   ├── tests/
   │   └── test_biomet_and_policy.py # Comprehensive Pytest Suite
   ├── scripts/
   │   ├── start_demo.sh           # Unix/Linux One-Click Launcher
   │   └── start_demo.bat          # Windows One-Click Launcher
   ├── docs/
   │   ├── PPT_CONTENT.md          # 8-Slide SIH Presentation Structure
   │   └── DEMO_SCRIPT.md          # 5-Minute Rehearsed Presentation Script
   └── Dockerfile                  # Containerized deployment spec
   ```
2. **Comprehensive Test Suite (`pytest`):**
   - Verification of NOAA HI, ISO WBGT, UTCI, and Composite Hazard formulas against known test vectors.
   - Verification of policy simulator outputs and memory constraints (<150 MB RAM).
3. **One-Click Launchers:**
   - Cross-platform shell/batch scripts that auto-install virtual environments and spin up the dashboard on `http://localhost:8050`.

---

## Resource & Deployment Compliance Guarantee

- **Open-Meteo 10,000 Credit Limit:**
  - With 700 districts batched in groups of 40 (18 API calls per national fetch) and cached for 6 hours, total daily API calls = **72 requests/day** (<0.8% of daily quota).
- **Render 512 MB RAM Limit:**
  - Slim GeoJSON (~2.7 MB) + Pandas + Plotly Dash memory footprint is **~120 MB RAM**, well within Render's free tier budget.
