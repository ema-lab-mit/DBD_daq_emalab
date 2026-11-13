"""Resonance rate vs CEC reservoir temperature (neutralization calibration), 2026-11-13.

Park on a resonance (e.g. 32S 5P2) while the CEC temperature is moved; this script cuts the park runs
into time blocks, keeps only in-deadband time, and joins each block with the TC-08 CEC reservoir
temperature logged by SUGUI (beamline-control/DATA/logs/tc08_temps_YYYYMMDD.csv, 15 s).

Per block:
  live_s      time with the laser inside the park deadband (rebuilt from the per-event wn tags;
              gaps > GAP_S between events are not counted, so it needs a few events/s - fine on 32S)
  res_hz      gated (resonant ToF window) rate in band
  nonres_hz   rate of all other hits in 0-40 us (collisional neutrals of every mass = a
              neutralization monitor that does not need the laser)
  total_hz    all hits 0-40 us (watch the MagneTOF guard)
  T_C, dTdt   CEC reservoir temperature (block mean) and its slope (degC/min)

usage:
  python grating_daq/cec_temp_scan.py                         # today's park runs near 32S 5P2
  python grating_daq/cec_temp_scan.py --date 20261113 --target 12625.19 --gate 3.49 4.39
  python grating_daq/cec_temp_scan.py --runs run_..._park run_..._park --block 120
Outputs data/cec_scan_<date>.csv and .png."""
import argparse, csv, glob, json, os
from datetime import datetime
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "data")
TC_DIR = r"C:\Users\EMALAB\Documents\Jackson\beamline-control\DATA\logs"
GAP_S = 5.0          # an event gap longer than this is not counted as live time
TOF_MAX = 40e-6
BAD_RUNS = {"run_20261113_125555_park"}   # QMF off after the 12:54 PC freeze (Jackson 11-13) - not a 32S measurement

ap = argparse.ArgumentParser()
ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
ap.add_argument("--runs", nargs="*", help="run folder names (default: park runs of --date near --target)")
ap.add_argument("--target", type=float, default=12625.19, help="park target to select runs (32S 5P2)")
ap.add_argument("--tol", type=float, default=0.05, help="max |park_target - target| for run selection")
ap.add_argument("--gate", type=float, nargs=2, default=[3.49, 4.39], help="resonant ToF gate (us)")
ap.add_argument("--block", type=float, default=120.0, help="block length (s)")
ap.add_argument("--max-nonres", type=float, default=5.0,
                help="drop blocks whose non-resonant rate exceeds this (Hz): beam/ToF contamination, e.g. 11-13 12:56")
ap.add_argument("--min-live", type=float, default=30.0, help="drop blocks with less in-band time (s)")
args = ap.parse_args()
G0, G1 = args.gate[0] * 1e-6, args.gate[1] * 1e-6

# ---------------------------------------------------------------- runs
if args.runs:
    runs = args.runs
else:
    runs = []
    for p in sorted(glob.glob(os.path.join(D, f"run_{args.date}_*_park"))):
        try:
            m = json.load(open(os.path.join(p, "meta.json")))
        except (OSError, ValueError):
            m = None          # still running: no meta yet -> use the folder anyway, target unknown
        if os.path.basename(p) in BAD_RUNS:
            print(f"{os.path.basename(p)}: excluded (BAD_RUNS)"); continue
        if m is None or abs(float(m["params"]["park_target"]) - args.target) <= args.tol:
            runs.append(os.path.basename(p))
if not runs:
    raise SystemExit("no park runs found")

# ---------------------------------------------------------------- TC-08 log
tc_t, tc_v = [], []
for f in sorted(glob.glob(os.path.join(TC_DIR, "tc08_temps_*.csv"))):
    with open(f, newline="", encoding="utf-8", errors="ignore") as fh:
        rd = csv.reader(fh)
        head = next(rd, [])
        col = next((i for i, h in enumerate(head) if "(ch1)" in h), None)
        if col is None:
            continue
        for r in rd:
            try:
                tc_t.append(float(r[0])); tc_v.append(float(r[col]))
            except (ValueError, IndexError):
                pass
tc_t, tc_v = np.array(tc_t), np.array(tc_v)
o = np.argsort(tc_t); tc_t, tc_v = tc_t[o], tc_v[o]

# ---------------------------------------------------------------- CEC pressure (tools/vacuum_logger.py, from 11-13 13:00)
vp_t, vp_v = [], []
for f in sorted(glob.glob(os.path.join(TC_DIR, "vacuum_????????.csv"))):
    with open(f, newline="", errors="ignore") as fh:
        for r in csv.DictReader(fh):
            try:
                v = float(r["CEC_mbar"])
                if v > 0:
                    vp_t.append(float(r["epoch_s"])); vp_v.append(v)
            except (KeyError, TypeError, ValueError):
                pass
vp_t, vp_v = np.array(vp_t), np.array(vp_v)
o = np.argsort(vp_t); vp_t, vp_v = vp_t[o], vp_v[o]

# ---------------------------------------------------------------- blocks
rows = []
for run in runs:
    p = os.path.join(D, run)
    try:
        m = json.load(open(os.path.join(p, "meta.json"))); prm = m["params"]
        tgt, db = float(prm["park_target"]), float(prm["park_deadband"])
    except (OSError, ValueError, KeyError):
        tgt, db = args.target, 0.02
        print(f"{run}: no meta.json (still running?) -> target {tgt}, deadband {db}")
    t, tof, wn = [], [], []
    with open(os.path.join(p, "events.csv"), errors="ignore") as f:
        for r in csv.DictReader(f):
            try:
                t.append(float(r["wall_time"])); tof.append(float(r["tof_s"])); wn.append(float(r["wn"]))
            except (TypeError, ValueError):
                pass
    t, tof, wn = np.array(t), np.array(tof), np.array(wn)
    if len(t) < 10:
        print(f"{run}: {len(t)} events, skipped"); continue
    o = np.argsort(t); t, tof, wn = t[o], tof[o], wn[o]
    # in-band state exactly like the DAQ: median of the last 5 wavemeter readings, enter at the
    # deadband, leave only beyond 1.5x (hysteresis) - raw per-event wn chatters at the band edge
    inb = np.zeros(len(t), bool)
    buf, state, last = [], False, None
    for i, w in enumerate(wn):
        if np.isfinite(w) and w != last:
            buf = (buf + [w])[-5:]; last = w
        if buf:
            dev = abs(float(np.median(buf)) - tgt)
            state = dev <= (1.5 * db if state else db)
        inb[i] = state
    # live time: each inter-event interval is credited to the band state of the event that opens it
    dt = np.diff(t, append=t[-1])
    live_w = np.where(inb & (dt < GAP_S), dt, 0.0)
    res = inb & (tof >= G0) & (tof < G1)
    allt = inb & (tof >= 0) & (tof < TOF_MAX)
    edges = np.arange(t[0], t[-1] + args.block, args.block)
    k = np.clip(np.digitize(t, edges) - 1, 0, len(edges) - 2)
    for b in range(len(edges) - 1):
        s = k == b
        live = live_w[s].sum()
        if live < args.min_live:
            continue
        nres, nall = res[s].sum(), allt[s].sum()
        if (nall - nres) / live > args.max_nonres:
            print(f"  {run}: block at {datetime.fromtimestamp(edges[b]):%H:%M} dropped, "
                  f"non-resonant {(nall - nres) / live:.1f} Hz > {args.max_nonres}")
            continue
        t0, t1 = edges[b], edges[b + 1]
        sel = (tc_t >= t0) & (tc_t < t1)
        if sel.sum() >= 2:
            T = tc_v[sel].mean()
            dTdt = np.polyfit((tc_t[sel] - t0) / 60, tc_v[sel], 1)[0]
        else:
            T = np.interp(0.5 * (t0 + t1), tc_t, tc_v) if len(tc_t) else np.nan
            dTdt = np.nan
        sp = (vp_t >= t0) & (vp_t < t1)
        P = float(np.exp(np.log(vp_v[sp]).mean())) if sp.sum() >= 2 else np.nan
        rows.append(dict(run=run, t_mid=0.5 * (t0 + t1), live_s=live, n_res=int(nres),
                         n_nonres=int(nall - nres), res_hz=nres / live, res_err=np.sqrt(max(nres, 1)) / live,
                         nonres_hz=(nall - nres) / live, total_hz=nall / live, T_C=T, dTdt=dTdt, P_CEC_mbar=P))
    print(f"{run}: target {tgt} +/-{db}, {len(t)} events, live {live_w.sum():.0f} s, "
          f"{res.sum()} resonant, {sum(1 for r in rows if r['run'] == run)} blocks")
if not rows:
    raise SystemExit("no blocks with enough in-band time")

out_csv = os.path.join(D, f"cec_scan_{args.date}.csv")
with open(out_csv, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)
print("wrote", out_csv)

# ---------------------------------------------------------------- summary in 5 degC bins
R = {k: np.array([r[k] for r in rows]) for k in rows[0] if k != "run"}
Tb = np.floor(R["T_C"] / 5) * 5
leg = np.where(R["dTdt"] > 0.05, "heat", np.where(R["dTdt"] < -0.05, "cool", "hold"))
for lg in ("heat", "hold", "cool"):
    if not (leg == lg).any():
        continue
    print(f"\n[{lg}]\n{'T bin':>8} {'live s':>7} {'res Hz':>12} {'nonres Hz':>10} {'total Hz':>9} {'P_CEC mbar':>11}")
    for tb in np.unique(Tb[np.isfinite(Tb) & (leg == lg)])[::(-1 if lg == "cool" else 1)]:
        s = (Tb == tb) & (leg == lg)
        L = R["live_s"][s].sum(); nr = (R["res_hz"][s] * R["live_s"][s]).sum(); nn = (R["nonres_hz"][s] * R["live_s"][s]).sum()
        pv = R["P_CEC_mbar"][s]; pv = pv[np.isfinite(pv)]
        ps = f"{np.exp(np.log(pv).mean()):.2e}" if len(pv) else "-"
        print(f"{tb:5.0f}-{tb+5:<3.0f}{L:7.0f} {nr/L:7.2f}({np.sqrt(nr)/L:.2f}) {nn/L:10.2f} {(nr+nn)/L:9.1f} {ps:>11}")

# ---------------------------------------------------------------- figure
plt.rcParams.update({"font.size": 10, "axes.linewidth": 1.0})
fig, ax = plt.subplots(1, 4, figsize=(17, 3.8), constrained_layout=True)
hrs = (R["t_mid"] - R["t_mid"].min()) / 3600
heat = R["dTdt"] > 0.05
a = ax[0]
a.errorbar(hrs, R["res_hz"], yerr=R["res_err"], fmt="o", ms=3, color="#b0280f", elinewidth=0.7)
a.set_xlabel("time (h)"); a.set_ylabel("resonant rate (Hz)")
a2 = a.twinx(); a2.plot(hrs, R["T_C"], color="0.4", lw=1.2); a2.set_ylabel("CEC reservoir (°C)", color="0.4")
for i, (y, lab) in enumerate(((R["res_hz"], "resonant rate (Hz)"), (R["nonres_hz"], "non-resonant rate (Hz)"))):
    a = ax[i + 1]
    a.errorbar(R["T_C"][~heat], y[~heat], yerr=(R["res_err"][~heat] if i == 0 else None), fmt="o", ms=3.5,
               color="#2c6fbb", elinewidth=0.7, label="steady / cooling")
    a.errorbar(R["T_C"][heat], y[heat], yerr=(R["res_err"][heat] if i == 0 else None), fmt="^", ms=3.5,
               color="#e08a1e", elinewidth=0.7, label="heating")
    a.set_xlabel("CEC reservoir (°C)"); a.set_ylabel(lab)
    a.legend(frameon=False, fontsize=8)
a = ax[3]; hp = np.isfinite(R["P_CEC_mbar"])
if hp.any():
    for sel, mk, c, lab in ((~heat & hp, "o", "#2c6fbb", "steady / cooling"), (heat & hp, "^", "#e08a1e", "heating")):
        a.errorbar(R["P_CEC_mbar"][sel], R["res_hz"][sel], yerr=R["res_err"][sel], fmt=mk, ms=3.5, color=c,
                   elinewidth=0.7, label=lab)
    pp = R["P_CEC_mbar"][hp]
    if pp.max() / pp.min() > 3:
        a.set_xscale("log")
    else:                                   # narrow range: linear axis in units of 1e-7 mbar (log ticks collide)
        a.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v*1e7:.2f}"))
        a.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(5))
    a.legend(frameon=False, fontsize=8)
else:
    a.text(0.5, 0.5, "no CEC pressure log\n(logging from 11-13 13:00)", transform=a.transAxes, ha="center", va="center",
           color="0.5", fontsize=9)
a.set_xlabel("CEC pressure (mbar)" if (hp.any() and R["P_CEC_mbar"][hp].max() / R["P_CEC_mbar"][hp].min() > 3) else "CEC pressure (1e-7 mbar)"); a.set_ylabel("resonant rate (Hz)")
for a in ax:
    a.grid(True, color="0.9", lw=0.7)
    a.tick_params(direction="in")
out_png = os.path.join(D, f"cec_scan_{args.date}.png")
fig.savefig(out_png, dpi=170)
print("wrote", out_png)
