"""C-MAPSS feature engineering in PySpark: the same features as aeronexus.pdm.cmapss.window_features, at cluster scale.

Runs anywhere Spark runs, and needs no change to the existing pandas pipeline:
  * locally:      pip install -r requirements-spark.txt && python -m aeronexus.pipelines.spark_features --subset FD001
  * Databricks:   notebooks/databricks/cmapss_feature_pipeline.py (Free Edition, serverless) writes a Delta table
  * CI:           tests/test_spark_features.py checks parity with the pandas features (skipped without pyspark/Java)

Per engine, ordered by cycle, using only past rows (no look-ahead):
  last value, rolling means over 5/15/30 cycles, rolling sample std over 30 (0 when undefined),
  OLS slope over the last 30 cycles (0 with fewer than 3 points), cycle count.
Regime normalisation (FD002/FD004 run in six operating conditions) z-scores each sensor per regime with training stats.
"""
from __future__ import annotations

import argparse
from typing import Any

WINDOWS = (5, 15, 30)


def _spark() -> Any:
    from pyspark.sql import SparkSession

    return (SparkSession.builder.appName("aeronexus-cmapss-features").master("local[*]")
            .config("spark.sql.shuffle.partitions", "8").config("spark.ui.enabled", "false").getOrCreate())


def regime_col(F: Any) -> Any:
    """Operating regime key: os1 rounded to 0 dp, os2 to 2 dp, os3 to 0 dp (matches the pandas Normalizer)."""
    return F.concat_ws("_", F.round("os1", 0).cast("int").cast("string"), F.format_number(F.round("os2", 2), 2),
                       F.round("os3", 0).cast("int").cast("string"))


def normalise(train: Any, df: Any, sensors: list[str]) -> Any:
    """Z-score each sensor per regime using training statistics; unseen regimes fall back to the mean of regime stats."""
    from pyspark.sql import functions as F

    tr = train.withColumn("regime", regime_col(F))
    stats = tr.groupBy("regime").agg(*[F.avg(s).alias(f"{s}_mu") for s in sensors],
                                     *[F.stddev_samp(s).alias(f"{s}_sd") for s in sensors])
    glob = stats.agg(*[F.avg(f"{s}_mu").alias(f"{s}_gmu") for s in sensors], *[F.avg(f"{s}_sd").alias(f"{s}_gsd") for s in sensors])
    d = df.withColumn("regime", regime_col(F)).join(stats, "regime", "left").crossJoin(glob)
    for s in sensors:
        mu = F.coalesce(F.col(f"{s}_mu"), F.col(f"{s}_gmu"))
        sd = F.coalesce(F.when(F.col(f"{s}_sd") > 0, F.col(f"{s}_sd")), F.col(f"{s}_gsd"), F.lit(1.0))
        d = d.withColumn(s, (F.col(s) - mu) / sd)
    return d.select("unit", "cycle", *sensors)


def window_features(z: Any, sensors: list[str]) -> Any:
    """Per-engine rolling features with Spark window functions (rowsBetween = trailing rows only)."""
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    order = Window.partitionBy("unit").orderBy("cycle")
    d = z.withColumn("_t", (F.row_number().over(order) - 1).cast("double"))
    cols = [F.col("unit"), F.col("cycle").cast("double").alias("cycle")]
    w30 = order.rowsBetween(-29, 0)
    n = F.count(F.lit(1)).over(w30)
    st, stt = F.sum("_t").over(w30), F.sum(F.col("_t") * F.col("_t")).over(w30)
    for s in sensors:
        cols.append(F.col(s).alias(f"{s}_last"))
        for w in WINDOWS:
            cols.append(F.avg(s).over(order.rowsBetween(-(w - 1), 0)).alias(f"{s}_m{w}"))
        cols.append(F.coalesce(F.stddev_samp(s).over(w30), F.lit(0.0)).alias(f"{s}_sd30"))
        sy, sty = F.sum(s).over(w30), F.sum(F.col("_t") * F.col(s)).over(w30)
        den = n * stt - st * st
        cols.append(F.when((n >= 3) & (den != 0), (n * sty - st * sy) / den).otherwise(F.lit(0.0)).alias(f"{s}_slope30"))
    return d.select(*cols)


def load_cmapss(spark: Any, path: str) -> Any:
    """Read a raw C-MAPSS text file (space separated, 26 columns)."""
    from pyspark.sql import functions as F

    names = ["unit", "cycle", "os1", "os2", "os3"] + [f"s{i}" for i in range(1, 22)]
    raw = spark.read.text(path)
    parts = F.split(F.trim(F.col("value")), r"\s+")
    return raw.select(*[parts.getItem(i).cast("double").alias(c) for i, c in enumerate(names)]) \
        .withColumn("unit", F.col("unit").cast("int")).withColumn("cycle", F.col("cycle").cast("int"))


def run(subset: str, data_dir: str, out: str | None = None) -> dict[str, Any]:
    """Local run against backend/data/public/cmapss; writes Parquet (Delta on Databricks)."""
    from ..pdm.cmapss import SENSORS

    spark = _spark()
    train = load_cmapss(spark, f"{data_dir}/train_{subset}.txt")
    test = load_cmapss(spark, f"{data_dir}/test_{subset}.txt")
    from pyspark.sql import functions as F

    spread = train.withColumn("regime", regime_col(F)).groupBy("regime").agg(*[F.stddev_samp(s).alias(s) for s in SENSORS])
    means = spread.agg(*[F.avg(s).alias(s) for s in SENSORS]).first().asDict()
    sensors = [s for s in SENSORS if (means.get(s) or 0) > 1e-4]  # drop flat sensors, as the pandas Normalizer does
    feats = window_features(normalise(train, test, sensors), sensors)
    rows = feats.count()
    if out:
        feats.write.mode("overwrite").parquet(out)
    return {"subset": subset, "sensors": sensors, "rows": rows, "columns": len(feats.columns), "output": out}


if __name__ == "__main__":
    from ..public_data import REGISTRY

    ap = argparse.ArgumentParser()
    ap.add_argument("--subset", default="FD001")
    ap.add_argument("--out", default=None, help="output folder for Parquet features")
    a = ap.parse_args()
    print(run(a.subset, str(REGISTRY["cmapss"].folder), a.out))
