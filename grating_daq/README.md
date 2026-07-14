# Grating Ti:sa DAQ (survey + park)

A dedicated GUI for the grating laser, separate from the main DBD DAQ, with two
modes matched to what the sticky HydraPollux drive can actually do.

**Launch** (beamline machine, repo root, laser-lab servers already up):

```powershell
$env:EPICS_CA_ADDR_LIST = "10.54.6.1"
python grating_daq/main.py
```

Prerequisites on the laser-lab machine: `grating_controller.py` server (:8000)
and the wavemeter IOC (`LaserLab:wavenumber_3`). Do NOT run the main DBD DAQ at
the same time (both would own the tagger).

## Survey mode — bird's-eye spectrum

Continuous back-and-forth glide between *wn low* and *wn high*. The stage never
stops mid-range, so stick-slip never bites. Every event is stamped with the
measured wn; the spectrum is **gated counts / dwell seconds** per bin, so
nonuniform sweep speed, lurches, and the drive's direction asymmetry all
normalize out. Down-glides use a conservative short leg (`reverse_leg_scale`).

## Park mode — lifetime measurements

Deadband hold at *park target wn*: inside the deadband nothing is commanded
(an uncommanded stage cannot stick-slip); outside it, one small proportional
nudge (halved when moving against the smooth direction), then a quiet period.
Use **Update park target** to retune during a run, **Log NRI marker** to record
the current NRI delay so lifetime analysis can segment the run. Watch *gated
rate vs time* + the ToF histogram.

## ToF gating

The ToF plot shows ALL events; drag the shaded region to set the gate. The gate
selects what feeds the live spectrum / rate displays only — **everything is
saved ungated**, so gates can be re-chosen in analysis.

## Data

Each run gets `grating_daq/data/run_<stamp>_<mode>/`:

- `events.csv` — every event: wall_time, channel, tof_s, wn, sweep_dir, bunch_id
- `spectrum.csv` — wn bin centers, dwell_s, counts, gated counts, gated rate
- `markers.csv` — NRI-delay / park-target markers with timestamps
- `meta.json` — mode, parameters, totals, final gate

Hardware constants (`wn_per_unit`, wavemeter channel, preferred direction) are
read from the repo's `settings.json` `control_settings.grating`, so the two
apps share one source of truth.
