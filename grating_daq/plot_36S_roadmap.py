"""36S 6p 5P2 roadmap (2026-11-12): where to point the injection-seeded (IS) laser for 36S.

Inputs
  * IS (CREMA-Analysis library, v2 tail model): 32S rest frequency nu0 = 756363.33 GHz (doubled-laser
    units, as in the library), 34S-32S shift 580.3(4.9)(0.9) MHz, beam KE 10109 eV (32S, +190.8 V cal).
  * 36S-32S shift predicted by mass-scaling the measured 34S-32S shift (GRASP field-shift term F = -0.048
    GHz/fm^2 x literature radii is ~10 MHz; its sign is not checked, so it is carried as an uncertainty).
  * Grating Ti:sa 5P2 centers (lab frame, grating wavemeter ch3): 32S, 34S, 36S, fitted here from the
    survey data (Gaussian + flat, Poisson likelihood, dwell from spectrum.csv).
Pre-October 36S data (e.g. the March IS 36S file) is deliberately NOT used.

Lab collinear wavenumber of isotope A at beam KE: wn = nu_rest(A) * D_A(KE) / (2c), D = sqrt((1+b)/(1-b)).
The grating wavemeter offset vs the HeNe-calibrated IS scale is taken from the 32S/34S grating centers.
usage: python grating_daq/plot_36S_roadmap.py [out.png]"""
import csv, json, os, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.optimize import minimize
from plot_36S_roadmap_runs import RUNS36, EXCLUDE

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(D, "36S_roadmap.png")
C = 29.9792458                     # GHz per cm^-1
U = 931494.10242e3                 # eV per u
ME = 548.58e-6
MASS = {32: 31.9720711744, 34: 33.967867004, 36: 35.96708071}
NU0, KE_IS = 756363.33, 10109.0    # GHz (doubled units), eV
D34, D34_U = 0.5803, np.hypot(0.0049, 0.0009)

GATE = {32: (3.49e-6, 4.39e-6), 34: (4.30e-6, 5.00e-6), 36: (5.0e-6, 5.6e-6)}
WIN = {32: (12624.90, 12625.50), 34: (12624.60, 12625.20), 36: (12624.10, 12624.96)}
RUNS = {
    32: ["run_20261110_094012_survey"],
    34: ["run_20261110_101531_survey", "run_20261112_094338_survey"],
    36: RUNS36,
}
FINE = 0.01


def doppler(A, ke):
    m = (MASS[A] - ME) * U
    g = 1 + ke / m
    b = np.sqrt(1 - 1 / g ** 2)
    return np.sqrt((1 + b) / (1 - b))


def mu(A):
    return (MASS[A] - MASS[32]) / (MASS[A] * MASS[32])


def load(A):
    lo, hi = WIN[A]
    fe = np.round(np.arange(lo, hi + FINE / 2, FINE), 3)
    dw, n = np.zeros(len(fe) - 1), np.zeros(len(fe) - 1)
    for run in RUNS[A]:
        p = os.path.join(D, run)
        if not os.path.exists(os.path.join(p, "spectrum.csv")):
            print(f"  {run}: no spectrum.csv yet, skipped"); continue
        rd = np.zeros_like(dw)
        with open(os.path.join(p, "spectrum.csv")) as f:
            for r in csv.DictReader(f):
                i = int(np.floor((float(r["wn_center"]) - lo) / FINE + 1e-6))
                if 0 <= i < len(rd):
                    rd[i] += 0 if any(a <= float(r["wn_center"]) < b for a, b in EXCLUDE.get(run, [])) else float(r["dwell_s"])
        w, t = [], []
        with open(os.path.join(p, "events.csv"), errors="ignore") as f:
            for r in csv.DictReader(f):
                try:
                    w.append(float(r["wn"])); t.append(float(r["tof_s"]))
                except (TypeError, ValueError):
                    pass
        w, t = np.array(w), np.array(t)
        fin = np.isfinite(w); w, t = w[fin], t[fin]
        idx = np.floor((w - lo) / FINE + 1e-6).astype(int)
        ok = (idx >= 0) & (idx < len(rd))
        ok[ok] &= rd[idx[ok]] > 0
        g = ok & (t >= GATE[A][0]) & (t < GATE[A][1])
        rn = np.bincount(idx[g], minlength=len(rd))[:len(rd)]
        dw += rd; n += rn
        print(f"  {run}: dwell {rd.sum():6.0f} s, {int(rn.sum())} gated")
    return 0.5 * (fe[1:] + fe[:-1]), dw, n


def fit(x, dw, n, x0s):
    k = dw > 0

    def model(p, xx):
        return p[0] + p[1] * np.exp(-0.5 * ((xx - p[2]) / p[3]) ** 2)

    def nll(p):
        if p[3] <= 0.005 or p[0] < 0 or p[1] < 0:
            return 1e12
        m = np.clip(model(p, x[k]), 1e-12, None) * dw[k]
        return (m - n[k] * np.log(m)).sum()
    a0 = max(n[k].sum() / dw[k].sum(), 1e-3)
    best = min((minimize(nll, [0.2 * a0, 3 * a0, x0, 0.09], method="Nelder-Mead",
                         options={"xatol": 1e-9, "fatol": 1e-9, "maxiter": 40000}) for x0 in x0s),
               key=lambda r: r.fun)
    P = best.x
    h = np.array([abs(P[0]) * 1e-3 + 1e-6, abs(P[1]) * 1e-3 + 1e-6, 2e-4, 2e-4])
    H = np.zeros((4, 4))
    for i in range(4):
        for j in range(4):
            ei, ej = np.eye(4)[i] * h[i], np.eye(4)[j] * h[j]
            H[i, j] = (nll(P + ei + ej) - nll(P + ei - ej) - nll(P - ei + ej) + nll(P - ei - ej)) / (4 * h[i] * h[j])
    E = np.sqrt(np.abs(np.diag(np.linalg.inv(H))))
    return P, E, model


res = {}
for A in (32, 34, 36):
    print(f"{A}S:")
    # 32S/34S: fit each run on its own and average the centers (summing runs with different peak
    # rates and wn coverage distorts the line shape); 36S is too sparse per run -> one combined fit
    groups = [RUNS[A]] if A == 36 else [[r] for r in RUNS[A]]
    allruns, fits = RUNS[A], []
    for grp in groups:
        RUNS[A] = grp
        x, dw, n = load(A)
        P, E, model = fit(x, dw, n, np.arange(WIN[A][0] + 0.15, WIN[A][1] - 0.15, 0.02))
        fits.append(dict(x=x, dw=dw, n=n, P=P, E=E, model=model))
        print(f"  center {P[2]:.4f}({E[2]:.4f}) cm-1, FWHM {2.3548*P[3]*2*C:.1f} GHz (doubled), "
              f"peak {P[1]*1e3:.1f} mHz, bg {P[0]*1e3:.2f} mHz")
    RUNS[A] = allruns
    c = np.array([f["P"][2] for f in fits]); e = np.array([f["E"][2] for f in fits])
    wgt = 1 / e ** 2
    cen = (c * wgt).sum() / wgt.sum()
    cen_u = max(1 / np.sqrt(wgt.sum()), c.std(ddof=1) / np.sqrt(len(c)) if len(c) > 1 else 0)
    res[A] = dict(fits=fits, c=cen, u=cen_u)
    print(f"  -> {A}S center {cen:.4f}({cen_u:.4f})")

# IS-scale lab positions at the IS beam energy, and the 36S prediction
k = mu(36) / mu(34)
d36 = D34 * k
d36_u = np.hypot(D34_U * k, 0.012)          # 34S shift error scaled + field-shift sign (~12 MHz)
nu_rest = {32: NU0, 34: NU0 + D34, 36: NU0 + d36}
wn_is = {A: nu_rest[A] * doppler(A, KE_IS) / (2 * C) for A in nu_rest}
off = np.mean([res[A]["c"] - wn_is[A] for A in (32, 34)])      # equal weight: systematics dominate
off_u = abs(res[34]["c"] - wn_is[34] - (res[32]["c"] - wn_is[32])) / 2
gr36 = res[36]["c"] - off
nu36_gr = 2 * C * gr36 / doppler(36, KE_IS)
d36_gr = nu36_gr - NU0
d36_gr_u = 2 * C * np.hypot(res[36]["u"], off_u) / doppler(36, KE_IS)
# 34S on the grating, referenced to the grating 32S (no IS input)
d34_gr = 2 * C * (res[34]["c"] - res[32]["c"]) / doppler(34, KE_IS) +     NU0 * (doppler(32, KE_IS) / doppler(34, KE_IS) - 1)
d34_gr_u = 2 * C * np.hypot(res[34]["u"], res[32]["u"]) / doppler(34, KE_IS)
print(f"grating offset vs IS scale: {off:+.4f}({off_u:.4f}) cm-1 (32S {res[32]['c']-wn_is[32]:+.4f}, "
      f"34S {res[34]['c']-wn_is[34]:+.4f})")
print(f"36S-32S predicted {d36*1e3:.0f}({d36_u*1e3:.0f}) MHz; grating {d36_gr:.2f}({d36_gr_u:.2f}) GHz")
print(f"IS lab wn @ {KE_IS:.0f} eV (HeNe-calibrated scale): " +
      ", ".join(f"{A}S {wn_is[A]:.4f}" for A in wn_is))
dv = 2 * C * 1e3 * (wn_is[36] - wn_is[34])
slope = (doppler(36, KE_IS + 1) - doppler(36, KE_IS)) * nu_rest[36] / (2 * C)
print(f"36S IS window: {wn_is[36]:.4f} +/- 0.03 cm-1; beam-energy slope {slope*1e4:.2f}e-4 cm-1/eV")

# ---------------------------------------------------------------- IS 32S/34S spectra (March 2026)
ISDIR = r"C:\Users\EMALAB\Documents\Jackson\Archived\data\S data for analysis"
IS_FILES = {32: ["32S_3-23-26.csv", "32S_3-24-26.csv", "32S_3-27-26.csv"],
            34: ["34S_3-23-26.csv", "34S_3-24-26.csv", "34S_3-27-26.csv"]}
IS_GATE = {32: (4.25e-6, 5.5e-6), 34: (5.1e-6, 5.8e-6)}     # library per-isotope ToF gates
HENE_REF = 1e7 / 632.992                                     # vacuum wn, pipeline convention
IS_BIN = 0.0015
import pandas as pd
from scipy.optimize import curve_fit


def gm(xx, b0, a0, x0, s):
    return b0 + a0 * np.exp(-0.5 * ((xx - x0) / s) ** 2)


isd = {}
for A, files in IS_FILES.items():
    wn_b, hit_b = [], []
    for fn in files:
        d = pd.read_csv(os.path.join(ISDIR, fn),
                        usecols=["channel", "tof", "wavemeter_wn1", "wavemeter_wn4", "bunch_id"])
        d["wnc"] = d["wavemeter_wn1"] * HENE_REF / d["wavemeter_wn4"]     # HeNe scale correction
        g = (d["channel"] == 2) & (d["tof"] >= IS_GATE[A][0]) & (d["tof"] < IS_GATE[A][1])
        per = d.assign(h=g.astype(int)).groupby("bunch_id").agg(wn=("wnc", "first"), h=("h", "sum"))
        wn_b.append(per["wn"].to_numpy()); hit_b.append(per["h"].to_numpy())
    wn_b, hit_b = np.concatenate(wn_b), np.concatenate(hit_b)
    e = np.arange(wn_b.min(), wn_b.max() + IS_BIN, IS_BIN)
    nb, _ = np.histogram(wn_b, e); nh, _ = np.histogram(wn_b, e, weights=hit_b)
    xc = 0.5 * (e[1:] + e[:-1]); k = nb > 200
    r, ru = nh[k] / nb[k], np.sqrt(np.maximum(nh[k], 1)) / nb[k]
    # Gaussian + flat for normalization / display only (the IS centroid is the library fit)
    p0 = [r.min(), r.max() - r.min(), xc[k][np.argmax(r)], 0.002]
    P, _ = curve_fit(gm, xc[k], r, p0=p0, sigma=ru, maxfev=20000)
    isd[A] = dict(x=xc[k], r=r, ru=ru, P=P)
    print(f"IS {A}S: {len(wn_b)} bunches, Gaussian peak {P[2]:.4f} (library centroid {wn_is[A]:.4f}), "
          f"FWHM {2.3548*abs(P[3])*2*C*1e3:.0f} MHz doubled")

# ---------------------------------------------------------------- figure
plt.rcParams.update({"font.size": 10.5, "axes.linewidth": 1.0})
COL = {32: "#4a4a4a", 34: "#2c6fbb", 36: "#b0280f"}
fig, a = plt.subplots(figsize=(8.6, 4.2), constrained_layout=True)
xf = np.linspace(12624.2, 12625.45, 8000)
for A in (32, 34):
    s = isd[A]; P = s["P"]
    a.errorbar(s["x"], (s["r"] - P[0]) / P[1], yerr=s["ru"] / P[1], fmt="o", ms=2.6, color=COL[A],
               elinewidth=0.7, capsize=0, alpha=0.8, zorder=3)
    xi = np.linspace(s["x"].min(), s["x"].max(), 600)
    a.plot(xi, (gm(xi, *P) - P[0]) / P[1], color=COL[A], lw=1.4, zorder=2)
r = res[36]["fits"][0]; P = r["P"]
cb = np.round(np.arange(WIN[36][0], WIN[36][1] + 0.03 / 2, 0.03), 3)
j = np.floor((r["x"] - WIN[36][0]) / 0.03 + 1e-6).astype(int)
Dn = np.bincount(j, weights=r["n"], minlength=len(cb) - 1)[:len(cb) - 1]
Dd = np.bincount(j, weights=r["dw"], minlength=len(cb) - 1)[:len(cb) - 1]
xc = 0.5 * (cb[1:] + cb[:-1]) - off; m = Dd > 150
a.plot(xc[m], (Dn[m] / Dd[m] - P[0]) / P[1], "o", ms=3.4, color=COL[36], alpha=0.8, zorder=3)
a.plot(xf, (r["model"](P, xf + off) - P[0]) / P[1], color=COL[36], lw=1.8, zorder=2)
a.axvspan(wn_is[36] - 0.03, wn_is[36] + 0.03, color=COL[36], alpha=0.12, lw=0, zorder=0)
for A in (32, 34, 36):
    a.axvline(wn_is[A], color=COL[A], ls="--", lw=1.0, zorder=1)
    a.text(wn_is[A] + (0.015 if A != 36 else -0.015), 1.32, f"$^{{{A}}}$S",
           color=COL[A], ha="left" if A != 36 else "right", va="top", fontsize=11)
a.text(wn_is[36], -0.2, f"IS window\n{wn_is[36]:.3f}", ha="center", va="bottom", fontsize=8.5, color=COL[36])
a.text(0.985, 0.04, "$^{32,34}$S: injection-seeded\n$^{36}$S: grating", transform=a.transAxes,
       ha="right", va="bottom", fontsize=8.5, color="0.35")
a.set_xlim(12624.25, 12625.42); a.set_ylim(-0.25, 1.38)
a.set_xlabel("laser wavenumber $-$ 12624 (cm$^{-1}$)")
a.set_ylabel("signal (normalized)")
a.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v-12624:.1f}"))
a.xaxis.set_minor_locator(matplotlib.ticker.MultipleLocator(0.02))
a.grid(True, color="0.9", lw=0.7, zorder=0)
a.tick_params(which="both", direction="in", top=True, right=True)
fig.savefig(OUT, dpi=200)
print("wrote", OUT)
