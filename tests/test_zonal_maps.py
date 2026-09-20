import pytest
import pandas as pd
import json
import app

def test_all_states_map_generation_synthetic():
    """Verify that every state and municipal ward zonal view renders non-empty map traces."""
    for state in app.states_list:
        fig_thermal = app.update_thermal_map('UTCI (deg C)', state, [15, 45], 0, False, 'init')
        fig_mort = app.update_mortality_map('Elderly (60+ yrs)', state, 0, [0, 100], False, 'init')

        assert len(fig_thermal.data) > 0, f"No data trace in thermal map for state: {state}"
        assert len(fig_mort.data) > 0, f"No data trace in mortality map for state: {state}"

        n_locs_thermal = len(fig_thermal.data[0].locations)
        n_locs_mort = len(fig_mort.data[0].locations)

        assert n_locs_thermal > 0, f"Zero locations mapped in thermal map for state: {state}"
        assert n_locs_mort > 0, f"Zero locations mapped in mortality map for state: {state}"

def test_municipal_wards_geojson_matching():
    """Verify municipal ward GeoJSON feature join keys match dataframe join keys exactly."""
    for ward_key in ["AHMEDABAD_WARDS", "BANGALORE_WARDS"]:
        assert ward_key in app.ward_geojsons, f"Missing ward GeoJSON for {ward_key}"
        w_json = app.ward_geojsons[ward_key]
        w_df = app.df[app.df["join_key"].str.startswith(f"{ward_key}|")]

        assert len(w_df) == len(w_json["features"]), f"Mismatch in ward count for {ward_key}"

        df_keys = set(w_df["join_key"])
        geo_keys = set(f["properties"]["join_key"] for f in w_json["features"])

        assert df_keys == geo_keys, f"Join key mismatch in {ward_key}"

def test_real_data_enrichment_map_generation():
    """Verify map figures render correctly when real weather data is populated."""
    working = app.df.copy()
    working = app.enrich_derived_columns(working)

    for state in ['ALL', 'Gujarat', 'Municipal Wards: Ahmedabad (48 Wards)']:
        fig_thermal = app.update_thermal_map('UTCI (deg C)', state, [15, 45], 0, False, 'init')
        fig_mort = app.update_mortality_map('Elderly (60+ yrs)', state, 0, [0, 100], False, 'init')

        assert len(fig_thermal.data[0].locations) > 0
        assert len(fig_mort.data[0].locations) > 0
