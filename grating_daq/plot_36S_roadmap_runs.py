"""36S grating runs (QMF on mass 36, gate 5.0-5.6 us): 11-10 + 11-12. Add new runs here."""
RUNS36 = ["run_20261110_111518_survey", "run_20261110_114537_survey", "run_20261110_141838_park",
         "run_20261110_141940_park", "run_20261110_145701_park", "run_20261110_145811_park",
         "run_20261110_153007_park", "run_20261110_160115_survey",
         "run_20261112_115733_survey", "run_20261112_115950_survey", "run_20261112_133053_park",
         "run_20261112_133321_survey", "run_20261112_134824_survey", "run_20261112_134911_survey",
         "run_20261112_140220_park", "run_20261112_140257_park", "run_20261112_140418_survey",
         "run_20261112_140503_survey", "run_20261112_142314_park", "run_20261112_142341_survey"]

# (run, wn_lo, wn_hi): bins dropped (dwell AND counts) - 142341: edge bin, 2 counts in 15 s at a reversal
EXCLUDE = {"run_20261112_142341_survey": [(12624.300, 12624.310)]}
