"""2026-10-30 34S resolving-QMF surveys (QMF cmd 32.5, pole 115, dM 4.3, u2 2000 V -> neutrals),
all data combined: run_20261030_155702_survey (15 min) + run_20261030_161248_survey (~5 min, ended by
the PC crash). Same estimator as analyze_survey.py per run (wn(t) between wavemeter updates shifted
by LAG, dwell from the same path), then counts and dwell summed over runs. Gaussian + flat fit per line.
usage: python grating_daq/plot_34S_1030.py"""
import csv, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RUNS = ["run_20261030_155702_survey", "run_20261030_161248_survey"]
GATE, LAG, BIN, DT, MIN_DWELL = (4.05e-6, 5.01e-6), 0.75, 0.04, 0.1, 2.0
SET34 = {"⁵P₁": 12623.6030, "⁵P₂": 12624.8510, "⁵P₃": 12626.4583}
POS32 = [12625.195, 12626.811]                       # resolved 32S today (lag 0.75)

edges = np.arange(12624.4, 12627.7 + BIN, BIN)
dw, n = np.zeros(len(edges) - 1), np.zeros(len(edges) - 1)
tot = 0.0
for run in RUNS:
    t, w, tof = [], [], []
    with open(os.path.join(D, run, "events.csv"), errors="ignore") as f:
        for r in csv.DictReader(f):
            try:
                t.append(float(r["wall_time"])); w.append(float(r["wn"])); tof.append(float(r["tof_s"]))
            except (TypeError, ValueError):
                pass                                   # torn last line (crash)
    t, w, tof = np.array(t), np.array(w), np.array(tof)
    new = np.r_[True, np.diff(w) != 0]
    kt, kw = t[new] - LAG, w[new]
    grid = np.arange(max(kt[0], t[0]), t[-1], DT)
    dw += np.histogram(np.interp(grid, kt, kw), edges)[0] * DT
    g = (tof >= GATE[0]) & (tof < GATE[1])
    n += np.histogram(np.interp(t[g], kt, kw), edges)[0]
    tot += t[-1] - t[0]
    print(f"{run}: {t[-1]-t[0]:.0f} s, {g.sum()} gated")

x = 0.5 * (edges[1:] + edges[:-1])
ok = dw >= MIN_DWELL
y, e = n[ok] / dw[ok], np.sqrt(np.maximum(n[ok], 1)) / dw[ok]
xo = x[ok]

fig, ax = plt.subplots(figsize=(8, 4))
ax.errorbar(xo, y, yerr=np.sqrt(n[ok]) / dw[ok], fmt="o", ms=3.5, color="#eb6834",
            ecolor="#eb683466", lw=1, label="data")
gauss = lambda X, a, m, s, c: c + a * np.exp(-0.5 * ((X - m) / s) ** 2)
fits = {}
for name in ("⁵P₂", "⁵P₃"):
    m0 = SET34[name] + 0.05
    sel = np.abs(xo - m0) < 0.45
    p, cov = curve_fit(gauss, xo[sel], y[sel], p0=[1.0, m0, 0.08, 0.05], sigma=e[sel],
                       absolute_sigma=True, bounds=([0, m0 - 0.3, 0.02, 0], [20, m0 + 0.3, 0.4, 5]))
    fits[name] = (p, np.sqrt(cov[1, 1]))
    xf = np.linspace(xo[sel].min(), xo[sel].max(), 300)
    ax.plot(xf, gauss(xf, *p), color="k", lw=1.3, label="Gaussian fit" if name == "⁵P₂" else None)
    ax.annotate(f"{name}\n{p[1]:.3f}", (p[1], p[3] + p[0]), xytext=(p[1] + 0.32, 1.25),
                ha="center", va="bottom", fontsize=9,
                arrowprops=dict(arrowstyle="-", color="0.4", lw=0.8))
    print(f"34S {name}: centre {p[1]:.3f}({np.sqrt(cov[1,1])*1e3:.0f}) cm-1, FWHM {2.355*p[2]:.3f}, "
          f"peak {p[0]:.2f}/s over {p[3]:.3f}/s")
ax.set_ylim(-0.05, 1.75)
ax.set_xlabel("wavenumber (cm⁻¹)"); ax.set_ylabel("³⁴S rate (1/s)")
ax.legend(frameon=False, fontsize=8, loc="upper right")
ax.grid(alpha=0.25, lw=0.5)
ax.ticklabel_format(axis="x", useOffset=False)
fig.tight_layout()
out = os.path.join(D, "34S_resolved_20261030.png")
fig.savefig(out, dpi=180); print("total", round(tot), "s, gated", int(n.sum()), "->", out)
