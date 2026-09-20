import math
import numpy as np
import pandas as pd
from pythermalcomfort.models import utci

DEMO_WEIGHTS = {
    "Elderly (60+ yrs)": 1.8,
    "Adults (18-59 yrs)": 1.0,
    "Children (0-5 yrs)": 1.3,
}

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


def calculate_heat_index(t_c: float, rh: float) -> float:
    """Calculates NOAA Heat Index in degrees Celsius using Steadman / Rothfusz regression equation."""
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


def calculate_wbgt(t_c: float, rh: float, wind_speed: float, solar_rad: float = 800.0) -> float:
    """Calculates ISO 7243 Wet Bulb Globe Temperature (WBGT) estimate in degrees Celsius."""
    if pd.isna(t_c) or pd.isna(rh):
        return np.nan
    e = (rh / 100.0) * 6.105 * math.exp((17.27 * t_c) / (237.7 + t_c))
    wbgt_shade = 0.567 * t_c + 0.393 * e + 3.94
    v = max(float(wind_speed if not pd.isna(wind_speed) else 1.0), 0.5)
    solar_adj = (solar_rad / 800.0) * 2.2 - (v - 1.0) * 0.4
    return round(float(wbgt_shade + max(-1.0, solar_adj)), 1)


def calculate_composite_hazard(hi: float, wbgt: float, utci_val: float) -> float:
    """Computes composite thermal hazard score (0 - 100) combining NOAA HI, ISO WBGT, and UTCI strain."""
    if pd.isna(hi) or pd.isna(wbgt) or pd.isna(utci_val):
        return np.nan
    norm_hi = min(100.0, max(0.0, (hi - 20.0) / 30.0 * 100.0))
    norm_wbgt = min(100.0, max(0.0, (wbgt - 18.0) / 22.0 * 100.0))
    norm_utci = min(100.0, max(0.0, (utci_val - 20.0) / 30.0 * 100.0))
    hazard = 0.35 * norm_hi + 0.40 * norm_wbgt + 0.25 * norm_utci
    return round(float(hazard), 1)


def utci_stress_category(value: float) -> str:
    """Categorizes UTCI thermal strain level according to Universal Thermal Climate Index standards."""
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


def calculate_f_utci(val: float) -> float:
    """Non-linear physiological strain response factor derived from UTCI value."""
    if pd.isna(val) or val <= 26:
        return 0.05
    if val <= 32:
        return 0.05 + 0.02 * (val - 26)
    if val <= 38:
        return 0.17 + 0.04 * (val - 32)
    if val <= 46:
        return 0.41 + 0.06 * (val - 38)
    return 0.89 + 0.08 * (val - 46)


def enrich_derived_columns(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Enriches input dataframe with multi-horizon biometeorological indices and demographic mortality scores."""
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
