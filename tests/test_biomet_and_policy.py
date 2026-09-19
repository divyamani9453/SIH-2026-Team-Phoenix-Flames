import os
import pytest
import numpy as np
import pandas as pd

from app import (
    calculate_heat_index,
    calculate_wbgt,
    calculate_composite_hazard,
    utci_stress_category,
    calculate_f_utci,
    enrich_derived_columns,
    df,
)


def test_heat_index_calculations():
    # Below threshold (26.7 C) returns dry bulb temp
    assert calculate_heat_index(25.0, 50.0) == 25.0
    # High heat & humidity test
    hi = calculate_heat_index(35.0, 60.0)
    assert 44.0 <= hi <= 47.0


def test_wbgt_calculations():
    wbgt = calculate_wbgt(35.0, 50.0, 2.0, solar_rad=800.0)
    assert 28.0 <= wbgt <= 38.0


def test_composite_hazard_index():
    # Moderate inputs
    haz_mod = calculate_composite_hazard(hi=30.0, wbgt=25.0, utci_val=30.0)
    assert 20.0 <= haz_mod <= 60.0

    # Extreme inputs
    haz_ext = calculate_composite_hazard(hi=45.0, wbgt=35.0, utci_val=42.0)
    assert haz_ext > 70.0


def test_utci_stress_categories():
    assert utci_stress_category(48.0) == "Extreme heat stress"
    assert utci_stress_category(40.0) == "Very strong heat stress"
    assert utci_stress_category(35.0) == "Strong heat stress"
    assert utci_stress_category(28.0) == "Moderate heat stress"
    assert utci_stress_category(15.0) == "No thermal stress"


def test_df_enrichment_columns():
    test_df = pd.DataFrame({
        "join_key": ["State|Dist1"],
        "District": ["Dist1"],
        "State": ["State"],
        "lat": [20.0],
        "lon": [78.0],
        "Dry Bulb Temp_d0": [38.0],
        "Relative Humidity_d0": [50.0],
        "Wind Speed_d0": [2.0],
        "Apparent Temp_d0": [40.0],
        "Mean Radiant Temp_d0": [38.0],
        "UTCI_d0": [39.0],
        "Dry Bulb Temp_d1": [35.0],
        "Relative Humidity_d1": [40.0],
        "Wind Speed_d1": [1.5],
        "Apparent Temp_d1": [36.0],
        "Mean Radiant Temp_d1": [35.0],
        "UTCI_d1": [36.0],
        "Dry Bulb Temp_d2": [32.0],
        "Relative Humidity_d2": [30.0],
        "Wind Speed_d2": [2.0],
        "Apparent Temp_d2": [32.0],
        "Mean Radiant Temp_d2": [32.0],
        "UTCI_d2": [31.0],
        "Dry Bulb Temp_d3": [28.0],
        "Relative Humidity_d3": [30.0],
        "Wind Speed_d3": [2.5],
        "Apparent Temp_d3": [28.0],
        "Mean Radiant Temp_d3": [28.0],
        "UTCI_d3": [27.0],
    })

    enriched = enrich_derived_columns(test_df)
    for d in range(4):
        assert f"Heat Index_d{d}" in enriched.columns
        assert f"WBGT_d{d}" in enriched.columns
        assert f"Composite Hazard_d{d}" in enriched.columns
        assert f"Stress Category_d{d}" in enriched.columns
        assert f"Mortality_Elderly (60+ yrs)_d{d}" in enriched.columns


def test_memory_and_dataset_size():
    # Memory check to verify staying under Render 512 MB limit
    import psutil
    process = psutil.Process(os.getpid())
    mem_mb = process.memory_info().rss / (1024 * 1024)
    print(f"Current process memory: {mem_mb:.2f} MB")
    assert mem_mb < 350.0  # Well below 512 MB
