# Databricks notebook source
# MAGIC %md
# MAGIC # AeroNexus · C-MAPSS feature pipeline (PySpark → Delta)
# MAGIC Runs on **Databricks Free Edition** (serverless, free for non-commercial use).
# MAGIC 1. Upload `train_FD00x.txt` / `test_FD00x.txt` (from `backend/data/public/cmapss`) to a Unity Catalog volume,
# MAGIC    e.g. `/Volumes/workspace/default/cmapss/`.
# MAGIC 2. Import this file (Workspace › Import), attach serverless compute, run all.
# MAGIC 3. Optional: create a Job from it with a `subset` parameter; AeroNexus can trigger it via `DATABRICKS_JOB_ID`.
# MAGIC
# MAGIC The feature logic is the same code as `backend/aeronexus/pipelines/spark_features.py` (inlined so the notebook is self-contained).

# COMMAND ----------

dbutils.widgets.text("subset", "FD001")  # noqa: F821 (Databricks globals)
dbutils.widgets.text("volume", "/Volumes/workspace/default/cmapss")  # noqa: F821
subset = dbutils.widgets.get("subset")  # noqa: F821
volume = dbutils.widgets.get("volume")  # noqa: F821

# COMMAND ----------

from pyspark.sql import Window
from pyspark.sql import functions as F

NAMES = ["unit", "cycle", "os1", "os2", "os3"] + [f"s{i}" for i in range(1, 22)]
SENSORS = [f"s{i}" for i in range(1, 22)]


def load(path):
    parts = F.split(F.trim(F.col("value")), r"\s+")
    return (spark.read.text(path)  # noqa: F821
            .select(*[parts.getItem(i).cast("double").alias(c) for i, c in enumerate(NAMES)])
            .withColumn("unit", F.col("unit").cast("int")).withColumn("cycle", F.col("cycle").cast("int")))


def regime(df):
    return df.withColumn("regime", F.concat_ws("_", F.round("os1", 0).cast("int").cast("string"),
                                               F.format_number(F.round("os2", 2), 2), F.round("os3", 0).cast("int").cast("string")))


train, test = regime(load(f"{volume}/train_{subset}.txt")), regime(load(f"{volume}/test_{subset}.txt"))
spread = train.groupBy("regime").agg(*[F.stddev_samp(s).alias(s) for s in SENSORS]).agg(*[F.avg(s).alias(s) for s in SENSORS]).first()
sensors = [s for s in SENSORS if (spread[s] or 0) > 1e-4]
stats = train.groupBy("regime").agg(*[F.avg(s).alias(f"{s}_mu") for s in sensors], *[F.stddev_samp(s).alias(f"{s}_sd") for s in sensors])
z = test.join(stats, "regime", "left")
for s in sensors:
    z = z.withColumn(s, (F.col(s) - F.col(f"{s}_mu")) / F.when(F.col(f"{s}_sd") > 0, F.col(f"{s}_sd")).otherwise(F.lit(1.0)))

order = Window.partitionBy("unit").orderBy("cycle")
w30 = order.rowsBetween(-29, 0)
z = z.withColumn("_t", (F.row_number().over(order) - 1).cast("double"))
n, st, stt = F.count(F.lit(1)).over(w30), F.sum("_t").over(w30), F.sum(F.col("_t") ** 2).over(w30)
cols = [F.col("unit"), F.col("cycle")]
for s in sensors:
    cols += [F.col(s).alias(f"{s}_last")] + [F.avg(s).over(order.rowsBetween(-(w - 1), 0)).alias(f"{s}_m{w}") for w in (5, 15, 30)]
    cols.append(F.coalesce(F.stddev_samp(s).over(w30), F.lit(0.0)).alias(f"{s}_sd30"))
    den = n * stt - st * st
    cols.append(F.when((n >= 3) & (den != 0), (n * F.sum(F.col("_t") * F.col(s)).over(w30) - st * F.sum(s).over(w30)) / den)
                .otherwise(F.lit(0.0)).alias(f"{s}_slope30"))
features = z.select(*cols)

# COMMAND ----------

table = f"workspace.default.cmapss_features_{subset.lower()}"
features.write.mode("overwrite").format("delta").saveAsTable(table)
display(spark.table(table).limit(20))  # noqa: F821
print(f"{features.count()} rows × {len(features.columns)} columns → {table}")
