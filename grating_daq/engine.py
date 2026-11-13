"""Engine for the grating-laser DAQ: threads, state, and data recording.

Two goal functions, matched to what the sticky grating drive can actually do:

SURVEY -- a continuous back-and-forth glide between two wavenumber limits.
    The stage NEVER stops mid-range (a moving stage has no stick-slip), so the
    laser sweeps smoothly; every tagger event is stamped with the measured wn
    at that moment, and the spectrum is built as gated_counts / dwell_seconds
    per wn bin. Dwell normalization makes nonuniform sweep speed, lurches, and
    direction asymmetry irrelevant -- a bin the laser rushed through just has
    less dwell, not a fake dip.

PARK -- sit on a resonance for lifetime measurements. Deadband + slow gentle
    nudges ("subtle PID"): inside the deadband we do NOTHING (a stage that
    isn't commanded can't stick-slip); outside it, one small proportional
    nudge toward the target, extra-conservative when moving against the
    drive's preferred (smooth) direction, with a minimum interval between
    nudges. Counts vs time + ToF histogram feed radiative-lifetime analysis;
    NRI-delay markers are logged so runs can be segmented.

Everything is recorded, gated or not: events.csv gets every event (wall time,
channel, ToF, wn, sweep direction); the ToF gate only selects what feeds the
LIVE spectrum and rate displays. Data goes to grating_daq/data/run_<stamp>/.
"""

import csv
import json
import os
import sys
import threading
import time
from collections import deque

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

try:
    import epics
except ImportError:
    epics = None

from src.devices.laser import GratingDevice  # XML-RPC MOV/qPOS to the laser lab
from src.devices.tagger import Tagger


# ----------------------------------------------------------------------------
# Configuration defaults (grating numbers established 2026-07; see memory/docs)
# ----------------------------------------------------------------------------
DEFAULTS = {
    "wm_pv": "LaserLab:wavenumber_3",   # grating Ti:sa is HighFinesse ch3
    "wn_per_unit": 11.0,                 # signed slope, cm^-1 per stage unit
    "preferred_wn_dir": +1,              # smooth (motor-driven) wn direction
    "stage_velocity": 0.001,             # units/s (set by the server at startup)
    "survey_leg": 0.05,                  # glide leg length (stage units)
    "reverse_leg_scale": 0.2,            # conservative leg against preferred dir
    "wn_poll_s": 0.1,                    # wavemeter poll (dwell integration step)
    # Adaptive sweep speed (needs the server's VEL() method):
    #   scan_velocity  : slow glide where the laser responds (dense sampling)
    #   burst_velocity : temporary speed to punch through sticky dead zones
    #   stall_burst_s  : wn flat for this long while gliding -> burst
    "scan_velocity": 0.0005,
    "burst_velocity": 0.004,
    "stall_burst_s": 6.0,
    "park_deadband": 0.01,               # cm^-1: do nothing inside this
    "park_settle_wait": 12.0,            # s CONTINUOUSLY outside before a poke
    "park_nudge_min_interval": 12.0,     # s between pokes
    "park_poke": 0.0005,                 # stage units: fixed tiny poke size
    "park_poke_max": 0.002,              # escalation cap if persistently out
    "park_avg_samples": 5,               # wn averaging for the park loop
}


class RunRecorder:
    """Owns the run folder and files: events.csv (every event), markers.csv
    (NRI-delay / user markers), meta.json, and a spectrum.csv snapshot on stop.
    Writes from a dedicated thread draining a queue so DAQ threads never block
    on disk."""

    def __init__(self, mode: str, params: dict):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.dir = os.path.join(_HERE, "data", f"run_{stamp}_{mode}")
        os.makedirs(self.dir, exist_ok=True)
        self.events_path = os.path.join(self.dir, "events.csv")
        self.markers_path = os.path.join(self.dir, "markers.csv")
        self.meta_path = os.path.join(self.dir, "meta.json")
        self._queue = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.n_written = 0

        with open(self.meta_path, "w") as f:
            json.dump({"mode": mode, "started": stamp, "params": params}, f, indent=2)
        with open(self.events_path, "w", newline="") as f:
            csv.writer(f).writerow(
                ["wall_time", "channel", "tof_s", "wn", "sweep_dir", "bunch_id"])
        with open(self.markers_path, "w", newline="") as f:
            csv.writer(f).writerow(["wall_time", "label", "value"])

        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()

    def add_event(self, row):
        with self._lock:
            self._queue.append(row)

    def add_marker(self, label: str, value):
        with open(self.markers_path, "a", newline="") as f:
            csv.writer(f).writerow([time.time(), label, value])

    def _writer_loop(self):
        while not self._stop.is_set() or self._queue:
            batch = []
            with self._lock:
                while self._queue:
                    batch.append(self._queue.popleft())
            if batch:
                with open(self.events_path, "a", newline="") as f:
                    csv.writer(f).writerows(batch)
                self.n_written += len(batch)
            time.sleep(0.5)

    def save_spectrum(self, centers, dwell, counts, gated):
        path = os.path.join(self.dir, "spectrum.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["wn_center", "dwell_s", "counts_all", "counts_gated",
                        "rate_gated_hz"])
            for c, d, n, g in zip(centers, dwell, counts, gated):
                rate = (g / d) if d > 0 else 0.0
                w.writerow([c, d, n, g, rate])

    def close(self, extra_meta: dict = None):
        self._stop.set()
        self._thread.join(timeout=5)
        if extra_meta:
            try:
                with open(self.meta_path) as f:
                    meta = json.load(f)
                meta.update(extra_meta)
                with open(self.meta_path, "w") as f:
                    json.dump(meta, f, indent=2)
            except Exception as e:
                print(f"[Recorder] meta update failed: {e}")


class SharedState:
    """Thread-shared live values + histograms, all under one lock."""

    # Fine ToF base resolution: 2000 bins over 20 us (10 ns). The DISPLAY
    # rebins from this base (gate_width/100 per shown bin) and the wn spectrum
    # is re-derived from the 2D histogram under the CURRENT gate -- so moving
    # the gate re-gates ALL data collected so far, not just future events.
    N_TOF = 2000
    TOF_MAX = 20e-6

    def __init__(self, wn_lo, wn_hi, bin_width):
        self.lock = threading.Lock()
        self.wn = float("nan")            # latest wavemeter reading
        # Slow-wavemeter support (2026-07-31, 735 nm coupling updates only
        # every ~0.2-1 s): freshness/velocity estimates published by WnPoller
        # so the sweeper can predict wn between updates instead of trusting a
        # stale value.
        self.wm_period = 0.1              # rolling median wavemeter update period (s)
        self.wn_v_est = 0.0               # cm-1/s from the last two CHANGED readings
        self.wn_change_t = 0.0            # wall time of the last CHANGED reading
        # Events waiting for wn assignment: (timestamp, tof_bin or -1). The
        # collector queues them and WnPoller retro-bins each batch along the
        # SAME interpolated wn path used for dwell, so the live spectrum's
        # numerator and denominator can never disagree (a mismatch fakes
        # spikes in low-dwell bins).
        self.pending = deque()
        self.sweep_dir = 0                # -1/0/+1, published by the laser thread
        self.gate = (0.0, self.TOF_MAX)   # ToF gate (s); selects LIVE displays
        self.total_events = 0
        self.gated_events = 0
        self.start_time = time.time()
        # Park-mode live gating: when park_target is set, live counting is
        # enabled ONLY while |wn - park_target| <= park_deadband, and
        # park_live_s integrates the seconds actually spent in the deadband
        # (the lifetime measurement's true measurement time).
        self.park_target = None
        self.park_deadband = 0.0
        self.park_active = False
        self.park_live_s = 0.0
        self.tof_edges = np.linspace(0.0, self.TOF_MAX, self.N_TOF + 1)
        self.tof_hist = np.zeros(self.N_TOF, dtype=np.int64)
        self.rebin(wn_lo, wn_hi, bin_width)
        # strip-chart deques: (t, value)
        self.wn_trace = deque(maxlen=3000)
        self.rate_trace = deque(maxlen=3000)

    def rebin(self, wn_lo, wn_hi, bin_width):
        n = max(2, int(round((wn_hi - wn_lo) / bin_width)))
        self.wn_edges = np.linspace(wn_lo, wn_hi, n + 1)
        self.wn_centers = 0.5 * (self.wn_edges[:-1] + self.wn_edges[1:])
        self.dwell = np.zeros(n)          # seconds the laser spent in each bin
        self.counts = np.zeros(n, dtype=np.int64)         # all events
        # 2D histogram (wn bin x fine ToF bin): the gated spectrum is derived
        # from this under whatever gate is set at display time.
        self.hist2d = np.zeros((n, self.N_TOF), dtype=np.int32)

    def gated_spectrum(self):
        """Gated counts per wn bin under the CURRENT gate, from all data."""
        i0 = int(np.searchsorted(self.tof_edges, self.gate[0]) - 1)
        i1 = int(np.searchsorted(self.tof_edges, self.gate[1]))
        i0 = max(0, i0)
        return self.hist2d[:, i0:i1].sum(axis=1)

    def bin_of(self, wn):
        if not np.isfinite(wn):
            return None
        i = np.searchsorted(self.wn_edges, wn) - 1
        if 0 <= i < len(self.wn_centers):
            return int(i)
        return None


class WnPoller(threading.Thread):
    """Reads the wavemeter continuously; publishes state.wn, integrates dwell
    time into the current wn bin, and feeds the wn strip chart.

    PERSISTENT: created once per app and rebound to each run's SharedState via
    set_state(). pyepics CA contexts do not survive thread churn -- spawning a
    fresh caget thread per run made the wn trace die on the second run."""

    def __init__(self, cfg: dict):
        super().__init__(daemon=True)
        self.state = None
        self.pv = cfg["wm_pv"]
        self.poll = float(cfg["wn_poll_s"])
        self.stop_event = threading.Event()
        # Median filter for the park in/out-of-band decision: raw wavemeter
        # jitter (~+/-0.003) is comparable to the deadband, so single-sample
        # gating chatters (randomly dropping counts / undercounting live time
        # while genuinely parked).
        self._park_buf = deque(maxlen=5)

    def set_state(self, state):
        self.state = state
        self._park_buf.clear()

    def read_wn(self):
        try:
            v = float(epics.caget(self.pv))
            return v if v > 0 else float("nan")
        except Exception:
            return float("nan")

    def run(self):
        last = time.time()
        # Slow-wavemeter bookkeeping: a reading only counts as FRESH when the
        # value changes. Dwell is attributed at each fresh reading by
        # distributing the elapsed time along the linear wn path from the
        # previous fresh reading (for a fast wavemeter this reduces to the
        # old per-poll behavior). A genuinely parked laser produces identical
        # readings too, so after PLATEAU_S of no change we fall back to
        # crediting the current bin directly (real plateau, not staleness).
        PLATEAU_S = 5.0
        prev_val, prev_t = float("nan"), time.time()
        chg_intervals = deque(maxlen=20)
        plateau_credited_until = time.time()
        while not self.stop_event.is_set():
            state = self.state
            if state is None:
                last = time.time()
                self.stop_event.wait(self.poll)
                continue
            wn = self.read_wn()
            now = time.time()
            dt = now - last
            last = now
            with state.lock:
                state.wn = wn
                state.wn_trace.append((now, wn))
                dt_ok = dt < 5 * self.poll + 1.0
                changed = (np.isfinite(wn) and
                           (not np.isfinite(prev_val) or wn != prev_val))
                if changed:
                    if np.isfinite(prev_val):
                        span = now - prev_t
                        chg_intervals.append(span)
                        state.wm_period = float(np.median(chg_intervals))
                        if 0 < span < 10.0:
                            state.wn_v_est = (wn - prev_val) / span
                            # distribute the un-credited part of the interval
                            # along the path prev_val -> wn
                            t0 = max(prev_t, plateau_credited_until)
                            credit = now - t0
                            if 0 < credit < 10.0:
                                nsub = max(1, int(credit / self.poll))
                                for k in range(nsub):
                                    frac = (k + 0.5) / nsub
                                    w = prev_val + frac * (wn - prev_val)
                                    i = state.bin_of(w)
                                    if i is not None:
                                        state.dwell[i] += credit / nsub
                        # retro-bin queued events along the same path
                        while state.pending and state.pending[0][0] <= now:
                            te, ti = state.pending.popleft()
                            frac = ((te - prev_t) / span if span > 0 else 1.0)
                            frac = min(max(frac, 0.0), 1.0)
                            w = prev_val + frac * (wn - prev_val)
                            i = state.bin_of(w)
                            if i is not None:
                                state.counts[i] += 1
                                if 0 <= ti < state.hist2d.shape[1]:
                                    state.hist2d[i, ti] += 1
                    else:
                        # first finite reading: assign any backlog to it
                        while state.pending:
                            te, ti = state.pending.popleft()
                            i = state.bin_of(wn)
                            if i is not None:
                                state.counts[i] += 1
                                if 0 <= ti < state.hist2d.shape[1]:
                                    state.hist2d[i, ti] += 1
                    state.wn_change_t = now
                    prev_val, prev_t = wn, now
                elif np.isfinite(wn) and dt_ok and (now - prev_t) > PLATEAU_S:
                    # true plateau (parked/stuck long): credit directly
                    i = state.bin_of(wn)
                    if i is not None:
                        state.dwell[i] += dt
                    plateau_credited_until = now
                    while state.pending and state.pending[0][0] <= now:
                        te, ti = state.pending.popleft()
                        if i is not None:
                            state.counts[i] += 1
                            if 0 <= ti < state.hist2d.shape[1]:
                                state.hist2d[i, ti] += 1
                # Park live-time accounting: in the deadband or not? Decide on
                # the MEDIAN of the last few readings (jitter immunity) with
                # hysteresis: enter at the deadband, leave only at 1.5x, so
                # noise at the edge doesn't chatter the live gate.
                if state.park_target is not None:
                    if np.isfinite(wn):
                        self._park_buf.append(wn)
                    if self._park_buf:
                        dev = abs(float(np.median(self._park_buf))
                                  - state.park_target)
                        if state.park_active:
                            state.park_active = dev <= 1.5 * state.park_deadband
                        else:
                            state.park_active = dev <= state.park_deadband
                    if state.park_active and dt_ok:
                        state.park_live_s += dt
            self.stop_event.wait(self.poll)


class SurveySweeper(threading.Thread):
    """Continuous back-and-forth glide between wn_lo and wn_hi. Keeps one long
    MOV alive in the current direction (re-extended before it runs out) and
    reverses at the limits. The stage only ever stops for the instant of a
    reversal, so stiction has nothing to grab."""

    def __init__(self, laser: GratingDevice, state: SharedState, cfg: dict,
                 wn_lo: float, wn_hi: float):
        super().__init__(daemon=True)
        self.laser = laser
        self.state = state
        self.cfg = cfg
        self.wn_lo = float(wn_lo)
        self.wn_hi = float(wn_hi)
        self.stop_event = threading.Event()

    def _stage_dir(self, wn_dir: int) -> float:
        slope_sign = 1.0 if self.cfg["wn_per_unit"] >= 0 else -1.0
        return wn_dir * slope_sign

    def _leg(self, wn_dir: int) -> float:
        leg = float(self.cfg["survey_leg"])
        if wn_dir != int(self.cfg["preferred_wn_dir"]):
            leg *= float(self.cfg["reverse_leg_scale"])
        return leg

    def run(self):
        wn_dir = int(self.cfg["preferred_wn_dir"])  # start in the smooth direction
        leg_target = None
        # Anti-dead-time: the drive has spots where the optic sticks while the
        # motor winds through (wn plateaus = wasted scan time). Glide slowly for
        # dense sampling where the laser responds; when wn has been flat for
        # stall_burst_s, temporarily raise the velocity to punch through, then
        # drop back the moment wn moves again.
        scan_v = float(self.cfg["scan_velocity"])
        burst_v = float(self.cfg["burst_velocity"])
        stall_s = float(self.cfg["stall_burst_s"])
        vel_ok = False
        try:
            vel_ok = self.laser.VEL(scan_v)
        except Exception as e:
            print(f"[Survey] VEL failed: {e}")
        if not vel_ok:
            # 2026-10-30: the controller refuses VEL below ~0.001. Without this
            # the stage kept the LAST velocity set - after a park that is the
            # 0.004 burst, so the survey ran 4x too fast. Fall back to the
            # known-good stage velocity and sweep at that.
            try:
                if self.laser.VEL(float(self.cfg["stage_velocity"])):
                    scan_v = float(self.cfg["stage_velocity"])
                    vel_ok = True
                    print(f"[Survey] scan_velocity refused -> using {scan_v}")
            except Exception as e:
                print(f"[Survey] fallback VEL failed: {e}")
        bursting = False
        ref_wn = None
        ref_time = time.time()
        print(f"[Survey] sweeping {self.wn_lo:.4f} <-> {self.wn_hi:.4f} "
              f"(scan_v={scan_v}, burst={'on' if vel_ok else 'UNAVAILABLE'})")
        burst_t0 = 0.0
        # Reversal burst (2026-10-30): after each reversal the optic sits still
        # while the drive takes up its backlash (~20 s at 0.001 units/s = 40% of
        # survey time on 10-30). Take it up at burst speed for a FIXED time
        # (reversal_burst_s), ignoring wn, then drop back to scan speed.
        rev_s = float(self.cfg.get("reversal_burst_s", 5.0))
        rev_until = 0.0
        # Stick-slip defense (2026-07-31): the optic's instantaneous speed
        # during a slip (~150 mK/s measured) far exceeds any commanded VEL, so
        # reversal margins alone cannot prevent overshoot. Anchor a wn<->pos
        # mapping at each fresh wavemeter reading and CLAMP the commanded MOV
        # target to the stage position of the survey bound: the motor itself
        # then stops at the bound, and a slip can only carry the optic up to
        # the motor, not past it.
        loop_s = 0.3
        anchor_pos = None
        anchor_wn = float("nan")
        last_anchor_t = -1.0
        while not self.stop_event.is_set():
            with self.state.lock:
                wn = self.state.wn
                wm_period = self.state.wm_period
                v_est = self.state.wn_v_est
                chg_t = self.state.wn_change_t
                self.state.sweep_dir = wn_dir
            now = time.time()
            if np.isfinite(wn):
                # Slow-wavemeter support: the latest reading can be up to
                # wm_period stale, and this loop adds up to 1 s more. Predict
                # where the laser actually is, and reverse with a margin so a
                # stale edge check cannot overshoot the survey bounds.
                age = min(max(now - chg_t, 0.0), 3.0 * max(wm_period, 0.1))
                wn_pred = wn + v_est * age
                cur_v = (burst_v if bursting else scan_v) * abs(float(self.cfg["wn_per_unit"]))
                # stick-slip: the measured slip velocity can exceed the
                # commanded one several-fold -- use whichever is larger
                eff_v = max(cur_v, abs(v_est))
                margin = max(0.0, eff_v * (max(wm_period, 0.1) + loop_s))
                # 2026-11-12: cap the margin at a quarter of the range. At burst
                # speed (0.004 x 70 = 0.28 cm-1/s) with the 0.75 s wavemeter the
                # margin was ~0.29 cm-1; on a 0.5 cm-1 survey both limit zones
                # overlapped in the middle (12624.51-.59), each reversal's burst
                # re-armed the other limit and the sweep flip-flopped in place
                # for minutes (run_20261112_133321_survey). Capped, the two
                # zones can never overlap. (The MOV clamp to the bound's stage
                # position still guards against overshoot.)
                margin = min(margin, 0.25 * (self.wn_hi - self.wn_lo))
                if wn_dir > 0 and wn_pred >= self.wn_hi - margin:
                    wn_dir = -1
                    leg_target = None
                    print(f"[Survey] top limit (pred {wn_pred:.4f}) -> reversing down")
                    rev_until = now + rev_s
                elif wn_dir < 0 and wn_pred <= self.wn_lo + margin:
                    wn_dir = +1
                    leg_target = None
                    print(f"[Survey] bottom limit (pred {wn_pred:.4f}) -> reversing up")
                    rev_until = now + rev_s
                if vel_ok and rev_until > now and not bursting and rev_s > 0:
                    bursting = True
                    burst_t0 = now
                    try:
                        self.laser.VEL(burst_v)
                        print(f"[Survey] reversal: backlash take-up at {burst_v} for {rev_s:g} s")
                    except Exception as e:
                        print(f"[Survey] VEL burst failed: {e}")
                # Stall/burst bookkeeping (only when VEL is available). With a
                # slow wavemeter, require the stall window to cover several
                # update periods, and hard-cap burst duration so a stale
                # "still flat" reading cannot let a burst run away.
                eff_stall = max(stall_s, 4.0 * max(wm_period, 0.1))
                if now < rev_until:
                    ref_wn, ref_time = wn, now   # reversal burst runs its fixed time
                elif ref_wn is None or abs(wn - ref_wn) > 0.01:
                    ref_wn = wn
                    ref_time = now
                    if bursting:
                        bursting = False
                        try:
                            self.laser.VEL(scan_v)
                            print(f"[Survey] wn moving again -> scan speed {scan_v}")
                        except Exception as e:
                            print(f"[Survey] VEL restore failed: {e}")
                elif bursting and (now - burst_t0) > max(2.0, 2.0 * wm_period,
                                                         rev_s if burst_t0 < rev_until else 0.0):
                    bursting = False
                    ref_time = now      # restart the stall clock after the cap
                    try:
                        self.laser.VEL(scan_v)
                        print(f"[Survey] burst time-cap -> scan speed {scan_v}")
                    except Exception as e:
                        print(f"[Survey] VEL restore failed: {e}")
                elif vel_ok and not bursting and (now - ref_time) > eff_stall:
                    bursting = True
                    burst_t0 = now
                    try:
                        self.laser.VEL(burst_v)
                        print(f"[Survey] wn stalled {eff_stall:.0f}s at {wn:.4f} "
                              f"-> burst speed {burst_v} (cap {max(2.0, 2.0*wm_period):.1f}s)")
                    except Exception as e:
                        print(f"[Survey] VEL burst failed: {e}")
            try:
                pos = self.laser.qPOS()
                sd = self._stage_dir(wn_dir)
                # Refresh the wn<->pos anchor whenever a NEW wavemeter reading
                # arrived (chg_t moved). Anchoring at the reading -- not every
                # loop -- matters: during a stick the motor winds forward while
                # wn is flat, and re-anchoring on stale wn would let the clamp
                # creep outward with the wind-up.
                if np.isfinite(wn) and chg_t != last_anchor_t:
                    anchor_pos, anchor_wn = pos, wn
                    last_anchor_t = chg_t
                # (Re)issue the glide when there is no active leg or the stage
                # is within 20% of the current leg's end.
                tgt = leg_target
                if tgt is None or abs(tgt - pos) < 0.2 * self._leg(wn_dir):
                    tgt = pos + sd * self._leg(wn_dir)
                # Clamp the commanded target to the survey bound's stage
                # position (mapped through wn_per_unit from the anchor).
                if anchor_pos is not None and np.isfinite(anchor_wn):
                    bound = self.wn_hi if wn_dir > 0 else self.wn_lo
                    pos_bound = anchor_pos + \
                        (bound - anchor_wn) / float(self.cfg["wn_per_unit"])
                    tgt = min(tgt, pos_bound) if sd > 0 else max(tgt, pos_bound)
                # re-issue only on meaningful change (avoid MOV spam while the
                # clamp jitters with wavemeter noise)
                if tgt is not None and (leg_target is None
                                        or abs(tgt - leg_target) > 0.002):
                    leg_target = tgt
                    self.laser.MOV(leg_target)
            except Exception as e:
                print(f"[Survey] laser I/O error: {e}")
            self.stop_event.wait(loop_s)
        # Halt in place on stop; restore the server's default speed.
        try:
            self.laser.MOV(self.laser.qPOS())
            if vel_ok:
                self.laser.VEL(float(self.cfg["stage_velocity"]))
        except Exception:
            pass
        print("[Survey] sweeper stopped")


class ParkController(threading.Thread):
    """Hold the laser at target_wn: nudge INTO the deadband, then FREEZE.

    Every commanded move risks a stick-slip lurch far bigger than the
    deadband, so an eager controller ping-pongs the laser across resonance
    (each correction overshoots, triggering the next). Patience over action:

      * inside the deadband -> command NOTHING, ever;
      * drifts out -> WAIT (park_settle_wait of CONTINUOUS out-time) -- it
        usually wanders back on its own;
      * still out -> one tiny FIXED poke toward the target (park_poke, not
        error-proportional: a small poke either does nothing, which is safe,
        or gently releases; a big one lurches), halved against the smooth
        direction, then another long quiet period;
      * persistently out -> escalate the poke slowly (x1.5, capped); any
        re-entry into the deadband resets it to minimum."""

    def __init__(self, laser: GratingDevice, state: SharedState, cfg: dict,
                 target_wn: float):
        super().__init__(daemon=True)
        self.laser = laser
        self.state = state
        self.cfg = cfg
        self.target_wn = float(target_wn)
        self.stop_event = threading.Event()
        self.last_nudge = 0.0
        self.n_nudges = 0

    def set_target(self, wn):
        self.target_wn = float(wn)
        with self.state.lock:
            self.state.park_target = self.target_wn

    def run(self):
        buf = deque(maxlen=int(self.cfg["park_avg_samples"]))
        deadband = float(self.cfg["park_deadband"])
        settle = float(self.cfg["park_settle_wait"])
        interval = float(self.cfg["park_nudge_min_interval"])
        poke_min = float(self.cfg["park_poke"])
        poke_max = float(self.cfg["park_poke_max"])
        poke = poke_min
        out_since = None            # wall time the laser left the deadband
        approach_ok = 0.0           # earliest time for the next coarse slew
        appr_gain, appr_last_err = 1.0, None   # stiction escalation (2026-10-30)
        # 2026-11-13: wait for each MOV to FINISH before measuring again. The old
        # fixed wait (|step|/burst_v + 3 wm periods) was shorter than the real
        # move (a 2 cm-1 slew took 13-17 s), so the controller re-measured
        # mid-move, saw "stuck", doubled the gain and stacked more steps on top
        # -> 4-6 cm-1 swings around a 32S park. Now: poll qPOS until the stage
        # is at the commanded position (or has stopped moving), then let the
        # wavemeter catch up, then measure fresh.
        moving = None               # (commanded position, t0, last qPOS, n still polls)
        with self.state.lock:
            self.state.park_target = self.target_wn
            self.state.park_deadband = deadband
        print(f"[Park] holding {self.target_wn:.5f} (deadband +/-{deadband}; "
              f"freeze-in-band, poke only after {settle:.0f}s out)")
        while not self.stop_event.is_set():
            with self.state.lock:
                wn = self.state.wn
                self.state.sweep_dir = 0
            if moving is not None:
                now = time.time()
                try:
                    p = float(self.laser.qPOS())
                except Exception:
                    p = None
                tgt_pos, t0, last_p, still = moving
                if p is not None and last_p is not None and abs(p - last_p) < 2e-5:
                    still += 1
                else:
                    still = 0
                arrived = p is not None and abs(p - tgt_pos) < 2e-4
                if arrived or still >= 4 or now - t0 > 90.0:
                    if not arrived:
                        print(f"[Park] stage stopped at {p} (commanded {tgt_pos:.5f}"
                              f"{', timeout' if now - t0 > 90.0 else ''})")
                    with self.state.lock:
                        wm_period = self.state.wm_period
                    approach_ok = now + 3.0 * max(wm_period, 0.1) + 1.0
                    self.last_nudge = now
                    buf.clear()
                    moving = None
                else:
                    moving = (tgt_pos, t0, p, still)
                self.stop_event.wait(0.3)
                continue
            if np.isfinite(wn) and time.time() >= approach_ok:
                buf.append(wn)
            if len(buf) == buf.maxlen:
                avg = sum(buf) / len(buf)
                err = self.target_wn - avg
                now = time.time()
                # 2026-11-12: moves against preferred_wn_dir are eaten by
                # backlash/stiction (tiny pokes never got 12624.94 down to a
                # 12624.90 target in minutes). Approach from the smooth side
                # instead: slew PAST the target by park_overshoot_db deadbands,
                # then the normal pokes bring it back up in the smooth direction.
                pref = int(self.cfg["preferred_wn_dir"])
                lurchy = abs(err) > deadband and int(np.sign(err)) != pref
                over_db = float(self.cfg.get("park_overshoot_db", 3.0))
                if abs(err) <= deadband:
                    appr_gain, appr_last_err = 1.0, None
                    # In the band: freeze. Reset all escalation/waiting.
                    if out_since is not None:
                        print(f"[Park] back in the deadband (wn={avg:.5f}) -> frozen")
                    out_since = None
                    poke = poke_min
                elif abs(err) > 5 * deadband or (
                        out_since is not None and now - out_since > settle):
                    # Coarse approach (2026-07-31): a park started far from the
                    # target would need many minutes of tiny pokes, and
                    # stiction eats pokes entirely. Slew most of the estimated
                    # error in one real MOV through the wn->stage slope, wait
                    # out the move plus a few wavemeter update periods, then
                    # re-measure and repeat. Hands off to the poke logic once
                    # within 5 deadbands. The 0.8 factor and step cap keep a
                    # slope mismatch from overshooting badly.
                    # 2026-11-12 (2): fixed tiny pokes did not add up (a 0.01
                    # deadband park sat 0.046 below target for minutes): close
                    # in, after the settle wait, use the same escalating slews
                    # as the coarse approach (gain x2 while wn does not move).
                    if abs(err) > 5 * deadband or out_since is None:
                        out_since = now
                    # aim past the target on the smooth side when going the lurchy way
                    aim = err - pref * over_db * deadband if lurchy else err
                    if now >= approach_ok:
                        with self.state.lock:
                            wm_period = self.state.wm_period
                        burst_v = float(self.cfg["burst_velocity"])
                        # Stiction escalation (2026-10-30): small slews (~0.002
                        # units) can be eaten entirely and the approach then
                        # repeats the same useless step forever. If the last
                        # slew moved wn by < 30% of what it asked for (same
                        # direction), double the gain (cap 8); reset it as
                        # soon as a slew works or the direction flips.
                        if (appr_last_err is not None
                                and np.sign(err) == np.sign(appr_last_err[0])
                                and abs(appr_last_err[0] - err)
                                < 0.3 * 0.8 * abs(appr_last_err[1])):
                            appr_gain = min(appr_gain * 2.0, 8.0)
                        else:
                            appr_gain = 1.0
                        appr_last_err = (err, aim)
                        step = appr_gain * 0.8 * aim / float(self.cfg["wn_per_unit"])
                        step = float(np.clip(step, -0.05, 0.05))
                        try:
                            try:
                                self.laser.VEL(burst_v)
                            except Exception:
                                pass
                            pos = self.laser.qPOS()
                            self.laser.MOV(pos + step)
                            buf.clear()
                            self.last_nudge = now
                            moving = (pos + step, now, None, 0)
                            approach_ok = now + abs(step) / burst_v   # (estimate, for the log)
                            print(f"[Park] approach{' (overshoot, lurchy side)' if lurchy else ''}: wn={avg:.5f} "
                                  f"err={err:+.4f} aim={aim:+.4f} -> slew {step:+.5f} (gain {appr_gain:g}) "
                                  f"(move ~{approach_ok - now:.0f}s, then re-measure)")
                        except Exception as e:
                            print(f"[Park] laser I/O error: {e}")
                else:
                    if out_since is None:
                        out_since = now
                    # (lurchy-side errors wait here for settle, then take the
                    # overshoot slew above instead of a poke)
                    # Poke only after a long CONTINUOUS excursion + quiet period.
                    if (not lurchy and now - out_since > settle
                            and now - self.last_nudge > interval):
                        # Fixed tiny poke; wn direction -> stage direction via
                        # the slope sign.
                        wn_dir = 1 if err > 0 else -1
                        step = wn_dir * poke * \
                            (1.0 if self.cfg["wn_per_unit"] >= 0 else -1.0)
                        if wn_dir != int(self.cfg["preferred_wn_dir"]):
                            step *= 0.5  # extra conservative on the lurchy side
                        try:
                            pos = self.laser.qPOS()
                            self.laser.MOV(pos + step)
                            moving = (pos + step, now, None, 0)
                            self.last_nudge = now
                            self.n_nudges += 1
                            buf.clear()
                            print(f"[Park] poke #{self.n_nudges}: wn={avg:.5f} "
                                  f"err={err:+.4f} step={step:+.5f} "
                                  f"(out {now - out_since:.0f}s)")
                            poke = min(poke * 1.5, poke_max)  # slow escalation
                        except Exception as e:
                            print(f"[Park] laser I/O error: {e}")
            self.stop_event.wait(0.3)
        print("[Park] controller stopped")


class EventCollector(threading.Thread):
    """Drains the tagger, stamps each event with the current wn and sweep
    direction, records EVERYTHING, and fills the base histograms (ToF + the
    wn x ToF 2D histogram; the display derives the gated spectrum from the 2D
    histogram under the CURRENT gate, so gate moves re-gate all data).

    PERSISTENT: created once per app; rebind(state, recorder) per run,
    rebind(None, None) between runs."""

    def __init__(self, tagger: Tagger):
        super().__init__(daemon=True)
        self.tagger = tagger
        self.state = None
        self.recorder = None
        self.stop_event = threading.Event()
        self._rate_window = deque(maxlen=4000)  # gated event wall-times

    def rebind(self, state, recorder):
        self.state = state
        self.recorder = recorder
        self._rate_window.clear()

    def run(self):
        while not self.stop_event.is_set():
            state, recorder = self.state, self.recorder
            if state is None:
                # No active run: keep draining the tagger so its buffer never
                # backs up, but discard the packets.
                try:
                    self.tagger.get_data()
                except Exception:
                    pass
                self.stop_event.wait(0.2)
                continue
            try:
                data = self.tagger.get_data()
            except Exception as e:
                print(f"[Collector] tagger error: {e}")
                data = []
            now = time.time()
            if data:
                with state.lock:
                    wn = state.wn
                    sweep_dir = state.sweep_dir
                    g_lo, g_hi = state.gate
                    # Park mode: LIVE counting only while inside the deadband.
                    # (Everything is still recorded to events.csv; wn is saved
                    # per event, so offline cuts can always be re-made.)
                    counting = (state.park_target is None) or state.park_active
                    for d in data:
                        bunch, channel, tof = d[0], d[2], d[-1]
                        if channel == -1:
                            continue  # trigger marker, not an event
                        if recorder is not None:
                            recorder.add_event(
                                [now, channel, tof, wn, sweep_dir, bunch])
                        if not counting:
                            continue
                        state.total_events += 1
                        ti = np.searchsorted(state.tof_edges, tof) - 1
                        if 0 <= ti < len(state.tof_hist):
                            state.tof_hist[ti] += 1
                        else:
                            ti = -1
                        # queue for retro wn assignment by WnPoller (same
                        # interpolated path as dwell); cap the backlog in case
                        # the wavemeter stops publishing entirely
                        if len(state.pending) > 5000:
                            state.pending.popleft()
                        state.pending.append((now, ti))
                        if g_lo <= tof <= g_hi:
                            state.gated_events += 1
                            self._rate_window.append(now)
            # gated rate over the last 5 s
            cutoff = now - 5.0
            while self._rate_window and self._rate_window[0] < cutoff:
                self._rate_window.popleft()
            with state.lock:
                state.rate_trace.append((now, len(self._rate_window) / 5.0))
            self.stop_event.wait(0.05)


def load_grating_cfg() -> dict:
    """DEFAULTS overlaid with control_settings.grating from the main DAQ's
    settings.json, so both apps share one source of truth for the hardware."""
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(_REPO, "settings.json")) as f:
            g = json.load(f)["control_settings"].get("grating", {})
        for k in ("wn_per_unit", "preferred_wn_dir", "reverse_leg_scale",
                  "wavechannel"):
            if k in g:
                cfg[k] = g[k]
        if "wavechannel" in g:
            cfg["wm_pv"] = f"LaserLab:wavenumber_{int(g['wavechannel'])}"
    except Exception as e:
        print(f"[Config] settings.json not merged ({e}); using defaults")
    return cfg
