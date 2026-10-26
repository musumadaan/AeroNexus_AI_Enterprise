---
doc_type: policy
module: pdm
revision: 1
effective: 2024-01-01
title: Predictive Maintenance Policy
---
# Inspection trigger
Open an inspection work order when the 90 percent lower bound of predicted remaining useful life is 30 cycles or less, or when the engine health score exceeds the anomaly threshold.

# Decision authority
Predictions are decision support. A certifying technician decides whether an engine is removed from service.

# Evidence
Each work order must reference the model version, the prediction interval and the sensor trend that triggered it.
