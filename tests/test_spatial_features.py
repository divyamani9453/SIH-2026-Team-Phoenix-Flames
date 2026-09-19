import pandas as pd

def test_spatial_features_dataset():
    df_spatial = pd.read_csv("data/spatial_features.csv")
    assert len(df_spatial) == 932
    assert df_spatial.isna().sum().sum() == 0
    assert "join_key" in df_spatial.columns
    assert "elderly_pct" in df_spatial.columns
    assert "ndbi_builtup" in df_spatial.columns
