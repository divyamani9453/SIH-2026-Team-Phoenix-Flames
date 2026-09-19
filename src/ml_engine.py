import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestRegressor
import joblib
import os

MODEL_PATH = "data/hvi_rf_model.joblib"
FEATURE_NAMES = [
    "utci",
    "wbgt",
    "heat_index",
    "elderly_pct",
    "child_pct",
    "outdoor_labor_pct",
    "slum_density_pct",
    "ndbi_builtup",
    "ndvi_green_cover",
    "lst_offset_c",
]


def train_and_save_model():
    """Train a lightweight Random Forest regressor to predict Heat Vulnerability Index (HVI 0-100)."""
    np.random.seed(42)
    n_samples = 2500

    # Synthetic training grid covering meteorological + demographic + urban spectrum
    utci_v = np.random.uniform(15.0, 48.0, n_samples)
    wbgt_v = np.random.uniform(15.0, 42.0, n_samples)
    hi_v = np.random.uniform(15.0, 52.0, n_samples)
    elderly_v = np.random.uniform(6.0, 20.0, n_samples)
    child_v = np.random.uniform(6.0, 18.0, n_samples)
    labor_v = np.random.uniform(15.0, 70.0, n_samples)
    slum_v = np.random.uniform(2.0, 50.0, n_samples)
    ndbi_v = np.random.uniform(0.10, 0.85, n_samples)
    ndvi_v = np.random.uniform(0.05, 0.70, n_samples)
    lst_v = np.random.uniform(0.0, 5.0, n_samples)

    X = np.column_stack([
        utci_v, wbgt_v, hi_v, elderly_v, child_v, labor_v, slum_v, ndbi_v, ndvi_v, lst_v
    ])

    # Domain formulation for HVI ground truth
    # Meteorological strain (40% weight) + Demographic exposure (35% weight) + Built Environment strain (25% weight)
    met_score = 0.40 * (0.35 * np.clip((utci_v - 20) / 25 * 100, 0, 100) +
                        0.40 * np.clip((wbgt_v - 18) / 20 * 100, 0, 100) +
                        0.25 * np.clip((hi_v - 20) / 28 * 100, 0, 100))

    demo_score = 0.35 * (0.30 * (elderly_v / 20 * 100) +
                         0.20 * (child_v / 18 * 100) +
                         0.30 * (labor_v / 70 * 100) +
                         0.20 * (slum_v / 50 * 100))

    urban_score = 0.25 * (0.45 * (ndbi_v / 0.85 * 100) -
                          0.35 * (ndvi_v / 0.70 * 100) +
                          0.20 * (lst_v / 5.0 * 100))

    y = np.clip(met_score + demo_score + urban_score + np.random.normal(0, 2.0, n_samples), 0, 100)

    rf = RandomForestRegressor(n_estimators=30, max_depth=8, random_state=42)
    rf.fit(X, y)

    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    joblib.dump(rf, MODEL_PATH)
    print(f"[ml-engine] Trained & saved RF HVI model -> {MODEL_PATH}")
    return rf


def load_hvi_model():
    if os.path.exists(MODEL_PATH):
        try:
            return joblib.load(MODEL_PATH)
        except Exception:
            pass
    return train_and_save_model()


# Global model reference
_HVI_MODEL = None


def get_hvi_predictions_and_features(dataframe, horizon=0):
    """Computes AI HVI Score (0-100) and feature importances for a given forecast horizon."""
    global _HVI_MODEL
    if _HVI_MODEL is None:
        _HVI_MODEL = load_hvi_model()

    # Required columns
    col_utci = f"UTCI_d{horizon}"
    col_wbgt = f"WBGT_d{horizon}"
    col_hi = f"Heat Index_d{horizon}"

    # Extract or fallback features
    utci_vals = dataframe[col_utci].fillna(30.0).values
    wbgt_vals = dataframe[col_wbgt].fillna(28.0).values
    hi_vals = dataframe[col_hi].fillna(32.0).values

    elderly = dataframe["elderly_pct"].values if "elderly_pct" in dataframe else np.full(len(dataframe), 10.0)
    child = dataframe["child_pct"].values if "child_pct" in dataframe else np.full(len(dataframe), 10.0)
    labor = dataframe["outdoor_labor_pct"].values if "outdoor_labor_pct" in dataframe else np.full(len(dataframe), 30.0)
    slum = dataframe["slum_density_pct"].values if "slum_density_pct" in dataframe else np.full(len(dataframe), 15.0)
    ndbi = dataframe["ndbi_builtup"].values if "ndbi_builtup" in dataframe else np.full(len(dataframe), 0.40)
    ndvi = dataframe["ndvi_green_cover"].values if "ndvi_green_cover" in dataframe else np.full(len(dataframe), 0.25)
    lst = dataframe["lst_offset_c"].values if "lst_offset_c" in dataframe else np.full(len(dataframe), 1.5)

    X_mat = np.column_stack([
        utci_vals, wbgt_vals, hi_vals, elderly, child, labor, slum, ndbi, ndvi, lst
    ])

    hvi_scores = _HVI_MODEL.predict(X_mat).round(1)
    hvi_scores = np.clip(hvi_scores, 0.0, 100.0)

    feature_importances = dict(zip(FEATURE_NAMES, _HVI_MODEL.feature_importances_.round(3)))

    return hvi_scores, feature_importances


if __name__ == "__main__":
    rf = train_and_save_model()
    print("Feature importances:", dict(zip(FEATURE_NAMES, rf.feature_importances_.round(3))))
