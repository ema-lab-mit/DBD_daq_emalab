"""2026-11-10 36S 5P2 resonance: every 36S run of the day combined (QMF pole 115 / cmd 34.5 / dM 4.2, u2 2000 V ->
neutrals). Fixed ToF gate 5.0-5.6 us (Jackson 11-10), applied to the raw events regardless of each run's GUI gate.
Dwell per 0.01 bin from each run's spectrum.csv (the engine's wavemeter-polled dwell - the event-based dwell of
plot_34S_1030 fails at ~0.1 events/s); events binned by their wn tag on the same grid. Surveys and parks summed.
Fit: Gaussian + flat, Poisson likelihood on the 0.01 bins; plot rebinned to BIN.
usage: python grating_daq/plot_36S_1110.py [out.png]"""
import csv, json, os, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.optimize import minimize
from scipy.stats import chi2

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
from plot_36S_roadmap_runs import RUNS36 as RUNS, EXCLUDE   # 11-10 + 11-12 36S-gated runs (+ the long scan)
GATE, BIN = (5.0e-6, 5.6e-6), 0.03
LO, HI, FINE = 12624.10, 12624.96, 0.01
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(D, "36S_combined_1110.png")

fe = np.round(np.arange(LO, HI + FINE / 2, FINE), 3)
dw, n = np.zeros(len(fe) - 1), np.zeros(len(fe) - 1)
tof_on, tof_off = [], []
for run in RUNS:
    p = os.path.join(D, run)
    if not os.path.exists(os.path.join(p, "spectrum.csv")):
        print(f"{run}: no spectrum.csv yet, skipped"); continue
    with open(os.path.join(p, "spectrum.csv")) as f:
        rows = [(float(r["wn_center"]), float(r["dwell_s"])) for r in csv.DictReader(f)]
    rd = np.zeros_like(dw)
    for wc, d in rows:
        i = int(np.floor((wc - LO) / FINE + 1e-6))
        if 0 <= i < len(rd):
            rd[i] += 0 if any(a <= wc < b for a, b in EXCLUDE.get(run, [])) else d
    w, tof = [], []
    with open(os.path.join(p, "events.csv"), errors="ignore") as f:
        for r in csv.DictReader(f):
            try:
                w.append(float(r["wn"])); tof.append(float(r["tof_s"]))
            except (TypeError, ValueError):
                pass
    w, tof = np.array(w), np.array(tof)
    fin = np.isfinite(w); w, tof = w[fin], tof[fin]
    idx = np.floor((w - LO) / FINE + 1e-6).astype(int)
    ok = (idx >= 0) & (idx < len(rd))
    ok[ok] &= rd[idx[ok]] > 0                          # only events in bins this run has dwell for
    g = ok & (tof >= GATE[0]) & (tof < GATE[1])
    rn = np.bincount(idx[g], minlength=len(rd))[:len(rd)]
    dw += rd; n += rn
    tof_on += list(tof[ok & (np.abs(w - 12624.585) < 0.08)] * 1e6)
    tof_off += list(tof[ok & (np.abs(w - 12624.585) > 0.25)] * 1e6)
    print(f"{run}: dwell {rd.sum():6.0f} s, {int(rn.sum())} in gate")
print(f"total dwell {dw.sum():.0f} s, {int(n.sum())} in gate")

x = 0.5 * (fe[1:] + fe[:-1]); k = dw > 0


def model(p, xx):
    b, a, x0, s = p
    return b + a * np.exp(-0.5 * ((xx - x0) / s) ** 2)


def nll(p):
    if p[3] <= 0.005 or p[0] < 0 or p[1] < 0:
        return 1e12
    mu = np.clip(model(p, x[k]), 1e-12, None) * dw[k]
    return (mu - n[k] * np.log(mu)).sum()


best = min((minimize(nll, [0.001, 0.02, x0, 0.08], method="Nelder-Mead",
                     options={"xatol": 1e-8, "fatol": 1e-8, "maxiter": 20000})
            for x0 in np.arange(12624.4, 12624.8, 0.02)), key=lambda r: r.fun)
P = best.x
H = np.zeros((4, 4)); h = np.array([2e-5, 2e-4, 2e-4, 2e-4])
for i in range(4):
    for j in range(4):
        ei, ej = np.eye(4)[i] * h[i], np.eye(4)[j] * h[j]
        H[i, j] = (nll(P + ei + ej) - nll(P + ei - ej) - nll(P - ei + ej) + nll(P - ei - ej)) / (4 * h[i] * h[j])
E = np.sqrt(np.diag(np.linalg.inv(H)))
b0 = n[k].sum() / dw[k].sum(); nll0 = (b0 * dw[k] - n[k] * np.log(b0 * dw[k])).sum()
ts = 2 * (nll0 - best.fun)
FW = 2.3548 * P[3]
print(f"b {P[0]*1e3:.2f}({E[0]*1e3:.2f}) mHz, A {P[1]*1e3:.1f}({E[1]*1e3:.1f}) mHz, "
      f"x0 {P[2]:.4f}({E[2]:.4f}), sigma {P[3]:.4f}({E[3]:.4f}) -> FWHM {FW:.3f} cm-1 = {FW*29.9792458:.1f} GHz")
print(f"2dlnL vs flat {ts:.1f} (3 extra params, p = {chi2.sf(ts, 3):.1e})")

# rebin for display
cb = np.round(np.arange(LO, HI + BIN / 2, BIN), 3)
j = np.floor((x - LO) / BIN + 1e-6).astype(int)
Dn = np.bincount(j, weights=n, minlength=len(cb) - 1)[:len(cb) - 1]
Dd = np.bincount(j, weights=dw, minlength=len(cb) - 1)[:len(cb) - 1]
xc = 0.5 * (cb[1:] + cb[:-1]); m = Dd > 150
y = Dn[m] / Dd[m] * 1e3
lo_e = np.where(Dn[m] > 0, Dn[m] - chi2.ppf(0.1587, 2 * Dn[m]) / 2, 0) / Dd[m] * 1e3
hi_e = (chi2.ppf(0.8413, 2 * (Dn[m] + 1)) / 2 - Dn[m]) / Dd[m] * 1e3

plt.rcParams.update({"font.size": 11, "axes.linewidth": 1.0})
FIT, PT = "#b0280f", "black"
fig, a = plt.subplots(figsize=(7.2, 4.6), constrained_layout=True)
xf = np.linspace(LO - 0.05, HI + 0.05, 800)
a.grid(True, color="0.88", lw=0.7, zorder=0)
a.axvline(P[2], color="0.45", ls="--", lw=1.0, zorder=1)
a.errorbar(xc[m], y, yerr=[lo_e, hi_e], fmt="o", color=PT, ms=4.5, elinewidth=1.0, capsize=0, zorder=3)
a.plot(xf, model(P, xf) * 1e3, color=FIT, lw=2.0, zorder=2)
a.set_xlabel("wavenumber $-$ 12624 (cm$^{-1}$)"); a.set_ylabel("gated neutral rate (mHz)")
a.set_xlim(LO - 0.05, HI + 0.05); a.set_ylim(-1, 1.65 * P[1] * 1e3)
a.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(0.1))
a.xaxis.set_minor_locator(matplotlib.ticker.MultipleLocator(0.02))
a.yaxis.set_minor_locator(matplotlib.ticker.AutoMinorLocator(2))
a.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v-12624:.1f}"))
a.tick_params(which="both", direction="in", top=True, right=True)
a.tick_params(which="major", length=6); a.tick_params(which="minor", length=3)
a.text(P[2] - 0.012, 1.55 * P[1] * 1e3,  r"$^{36}$S $6p\,^5P_2$", fontsize=12, va="top", ha="right")
# ToF inset: on vs off resonance, normalized to counts per hour of dwell
b = a.inset_axes([0.665, 0.6, 0.31, 0.37]); b.set_zorder(5)
tb = np.arange(4.6, 6.81, 0.05)
b.axvspan(GATE[0] * 1e6, GATE[1] * 1e6, color="0.9", lw=0)
don = dw[np.abs(x - P[2]) < 0.08].sum(); doff = dw[np.abs(x - P[2]) > 0.25].sum()
b.hist(tof_on, tb, weights=np.full(len(tof_on), 3600 / don), histtype="step", color=FIT, lw=1.3)
b.hist(tof_off, tb, weights=np.full(len(tof_off), 3600 / doff), histtype="step", color="0.45", lw=1.0)
b.set_xlabel("ToF (µs)", fontsize=8, labelpad=1); b.set_ylabel("counts / h", fontsize=8, labelpad=1)
b.tick_params(labelsize=7, direction="in", top=True, right=True)
b.text(0.97, 0.93, "on", transform=b.transAxes, ha="right", va="top", fontsize=8, color=FIT)
b.text(0.97, 0.78, "off", transform=b.transAxes, ha="right", va="top", fontsize=8, color="0.45")
print(f"ToF inset: on dwell {don:.0f} s, off dwell {doff:.0f} s")
fig.savefig(OUT, dpi=200)
print("wrote", OUT)
print(f"on |wn-12624.585|<0.08: {len(tof_on)} events; off >0.25: {len(tof_off)} events (all ToF)")
