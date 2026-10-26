"""PySpark feature pipeline must reproduce the pandas features (runs in CI's spark job; skipped without pyspark/Java)."""
import numpy as np
import pandas as pd
import pytest

pyspark = pytest.importorskip("pyspark")


def test_spark_window_features_match_pandas():
    from aeronexus.pdm.cmapss import window_features as pd_features
    from aeronexus.pipelines.spark_features import _spark, window_features

    rng = np.random.default_rng(3)
    rows = []
    for unit, n in ((1, 45), (2, 12), (3, 2)):  # long, short and degenerate engines
        for c in range(1, n + 1):
            rows.append({"unit": unit, "cycle": c, "s2": rng.normal() + 0.02 * c, "s11": rng.normal() - 0.01 * c})
    z = pd.DataFrame(rows)
    sensors = ["s2", "s11"]
    expected = pd_features(z, sensors).sort_values(["unit", "cycle"]).reset_index(drop=True)
    spark = _spark()
    got = window_features(spark.createDataFrame(z), sensors).toPandas().sort_values(["unit", "cycle"]).reset_index(drop=True)
    for col in [c for c in expected.columns if c != "unit"]:
        np.testing.assert_allclose(got[col].to_numpy(float), expected[col].to_numpy(float), rtol=1e-6, atol=1e-9, err_msg=col)
