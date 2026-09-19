import pandas as pd
import numpy as np
from src.ml_engine import get_hvi_predictions_and_features, load_hvi_model

def test_ml_hvi_engine():
    df_spatial = pd.read_csv("data/spatial_features.csv")
    df_spatial["UTCI_d0"] = 35.0
    df_spatial["WBGT_d0"] = 32.0
    df_spatial["Heat Index_d0"] = 38.0

    scores, importances = get_hvi_predictions_and_features(df_spatial, horizon=0)

    assert len(scores) == len(df_spatial)
    assert scores.min() >= 0.0
    assert scores.max() <= 100.0
    assert "wbgt" in importances
    assert "utci" in importances
    assert sum(importances.values()) > 0.95
