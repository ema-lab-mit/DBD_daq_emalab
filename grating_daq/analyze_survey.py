"""Offline survey spectrum (2026-10-30). The wavemeter updates only every ~5 s, so the
live per-event wn is a staircase; here wn(t) is interpolated linearly between the moments
each new reading first appears (shifted back by LAG s for the wavemeter's integration),
dwell comes from the same path (never mix estimators), the gated rate is divided by a slow
running mean (ablation drift), and bins with < MIN_DWELL s are dropped.
Up and down sweeps are kept separate: a real line sits at the same wn in both, a timing-lag
artefact moves in opposite directions.
usage: python grating_daq/analyze_survey.py RUN_DIR [--gate 3.27,4.38] [--bin 0.05] [--lag 0.75]"""
import argparse, csv, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ap = argparse.ArgumentParser()
ap.add_argument("run")
ap.add_argument("--gate", default="3.27,4.38")
ap.add_argument("--bin", type=float, default=0.05)
ap.add_argument("--lag", type=float, default=0.75)   # 10-30: up/down 5P2+5P3 agree at 0.7-0.8 s (0.7 s wavemeter)
ap.add_argument("--min-dwell", type=float, default=2.0)
ap.add_argument("--drift-s", type=float, default=120.0)
a = ap.parse_args()
g0, g1 = (float(x) * 1e-6 for x in a.gate.split(","))

t, wn, tof, sd = [], [], [], []
for r in csv.DictReader(open(os.path.join(a.run, "events.csv"))):
    t.append(float(r["wall_time"])); wn.append(float(r["wn"]))
    tof.append(float(r["tof_s"])); sd.append(int(float(r["sweep_dir"])))
t, wn, tof, sd = map(np.array, (t, wn, tof, sd))
t0 = t[0]; t -= t0
# wavemeter update moments -> knots of the interpolated path
new = np.r_[True, np.diff(wn) != 0]
kt, kw = t[new] - a.lag, wn[new]
gate = (tof >= g0) & (tof < g1)

dt = 0.1
grid = np.arange(max(kt[0], 0), t[-1], dt)
wgrid = np.interp(grid, kt, kw)
dgrid = np.sign(np.gradient(wgrid))
gt = t[gate]
cnt = np.histogram(gt, bins=np.r_[grid, grid[-1] + dt])[0]
# slow drift: running mean of the gated rate
k = int(a.drift_s / dt)
rate = cnt / dt
slow = np.convolve(rate, np.ones(k) / k, mode="same")
slow[slow <= 0] = np.nan
norm = cnt / (slow / np.nanmean(slow))

edges = np.arange(np.floor(wgrid.min() / a.bin) * a.bin, wgrid.max() + a.bin, a.bin)
fig, ax = plt.subplots(2, 1, figsize=(9, 6.5), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
for d, lab, c in ((+1, "up", "#2a78d6"), (-1, "down", "#eb6834"), (0, "both", "k")):
    sel = (dgrid == d) if d else np.isfinite(norm)
    dw = np.histogram(wgrid[sel], edges, weights=np.full(sel.sum(), dt))[0]
    n = np.histogram(wgrid[sel], edges, weights=np.nan_to_num(norm[sel]))[0]
    raw = np.histogram(wgrid[sel], edges, weights=cnt[sel])[0]
    ok = dw >= a.min_dwell
    x = 0.5 * (edges[1:] + edges[:-1])
    y, e = n[ok] / dw[ok], np.sqrt(np.maximum(raw[ok], 1)) / dw[ok]
    ax[0].errorbar(x[ok], y, yerr=e, fmt="o-" if d == 0 else ".-", ms=4 if d == 0 else 3,
                   lw=1.6 if d == 0 else 0.9, color=c, alpha=1 if d == 0 else 0.6, label=lab)
    if d == 0:
        ax[1].bar(x, dw, width=a.bin * 0.9, color="0.6")
        best = np.argmax(np.where(ok, n / np.maximum(dw, 1e-9), -1))
        print(f"peak bin {x[best]:.3f} cm-1: {n[best]/dw[best]:.2f}/s "
              f"(median {np.median(y):.2f}/s, dwell {dw[best]:.1f} s, {raw[best]} counts)")
        print(f"bins with >= {a.min_dwell} s dwell: {ok.sum()}/{len(ok)}; total {dw.sum():.0f} s, "
              f"gated counts {cnt.sum()}")
ax[0].set_ylabel("gated rate, drift-corrected (1/s)")
ax[0].legend(frameon=False, fontsize=9)
ax[1].set_ylabel("dwell (s)"); ax[1].set_xlabel("wavenumber (cm⁻¹)")
for x_ in ax:
    x_.grid(alpha=0.25, lw=0.5)
out = os.path.join(a.run, f"spectrum_lag{a.lag:g}.png")
fig.tight_layout(); fig.savefig(out, dpi=150); print("wrote", out)
