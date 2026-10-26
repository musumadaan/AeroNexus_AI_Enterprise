---
doc_type: glossary
module: evals
title: Evaluation Glossary
---
# Precision
Precision is the share of extracted values that are correct. High precision means few wrong values reach the ERP.

# Recall
Recall is the share of true values on the drawing that the system found. Low recall means more fields are left for a person to fill in.

# F1
F1 is the harmonic mean of precision and recall, a single number that is high only when both are high.

# Auto-accept precision
Auto-accept precision is the accuracy of only those fields the system accepted without a human. It is the most important safety number because those values are not double checked.

# Calibration error
Expected calibration error compares the confidence the system reports with how often it is actually right. A well calibrated system that says 90 percent confident is right about 90 percent of the time.

# Cohen's kappa
Cohen's kappa measures agreement between two raters after removing agreement expected by chance. Values above 0.6 are usually read as substantial agreement and above 0.8 as strong agreement.

# Groundedness
Groundedness is the share of factual sentences in an answer that are supported by the cited source passage.

# Golden dataset
A golden dataset is a frozen, carefully labelled set of examples used only for testing, never for training, so that scores stay comparable across versions.
