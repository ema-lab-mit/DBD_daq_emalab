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
    "park_nudge_gain": 0.5,              # fraction of predicted correction
    "park_nudge_max": 0.003,             # stage units per nudge, hard cap
    "park_nudge_min_interval": 3.0,      # s between nudges
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
        self.sweep_dir = 0                # -1/0/+1, published by the laser thread
        self.gate = (0.0, self.TOF_MAX)   # ToF gate (s); selects LIVE displays
        self.total_events = 0
        self.gated_events = 0
        self.start_time = time.time()
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

    def set_state(self, state):
        self.state = state

    def read_wn(self):
        try:
            v = float(epics.caget(self.pv))
            return v if v > 0 else float("nan")
        except Exception:
            return float("nan")

    def run(self):
        last = time.time()
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
                i = state.bin_of(wn)
                if i is not None and dt < 5 * self.poll + 1.0:
                    state.dwell[i] += dt
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
        bursting = False
        ref_wn = None
        ref_time = time.time()
        print(f"[Survey] sweeping {self.wn_lo:.4f} <-> {self.wn_hi:.4f} "
              f"(scan_v={scan_v}, burst={'on' if vel_ok else 'UNAVAILABLE'})")
        while not self.stop_event.is_set():
            with self.state.lock:
                wn = self.state.wn
                self.state.sweep_dir = wn_dir
            if np.isfinite(wn):
                if wn_dir > 0 and wn >= self.wn_hi:
                    wn_dir = -1
                    leg_target = None
                    print(f"[Survey] top limit ({wn:.4f}) -> reversing down")
                elif wn_dir < 0 and wn <= self.wn_lo:
                    wn_dir = +1
                    leg_target = None
                    print(f"[Survey] bottom limit ({wn:.4f}) -> reversing up")
                # Stall/burst bookkeeping (only when VEL is available).
                now = time.time()
                if ref_wn is None or abs(wn - ref_wn) > 0.01:
                    ref_wn = wn
                    ref_time = now
                    if bursting:
                        bursting = False
                        try:
                            self.laser.VEL(scan_v)
                            print(f"[Survey] wn moving again -> scan speed {scan_v}")
                        except Exception as e:
                            print(f"[Survey] VEL restore failed: {e}")
                elif vel_ok and not bursting and (now - ref_time) > stall_s:
                    bursting = True
                    try:
                        self.laser.VEL(burst_v)
                        print(f"[Survey] wn stalled {stall_s:.0f}s at {wn:.4f} "
                              f"-> burst speed {burst_v}")
                    except Exception as e:
                        print(f"[Survey] VEL burst failed: {e}")
            try:
                pos = self.laser.qPOS()
                sd = self._stage_dir(wn_dir)
                # (Re)issue the glide when there is no active leg or the stage
                # is within 20% of the current leg's end.
                if leg_target is None or abs(leg_target - pos) < 0.2 * self._leg(wn_dir):
                    leg_target = pos + sd * self._leg(wn_dir)
                    self.laser.MOV(leg_target)
            except Exception as e:
                print(f"[Survey] laser I/O error: {e}")
            self.stop_event.wait(1.0)
        # Halt in place on stop; restore the server's default speed.
        try:
            self.laser.MOV(self.laser.qPOS())
            if vel_ok:
                self.laser.VEL(float(self.cfg["stage_velocity"]))
        except Exception:
            pass
        print("[Survey] sweeper stopped")


class ParkController(threading.Thread):
    """Hold the laser at target_wn: deadband + slow proportional nudges.
    Inside the deadband nothing is commanded at all. Outside it, one small
    nudge toward the target (conservative against the preferred direction),
    then a mandatory quiet period. This deliberately under-controls: with
    bimodal stiction, the less you poke the drive, the stiller it sits."""

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

    def run(self):
        buf = deque(maxlen=int(self.cfg["park_avg_samples"]))
        print(f"[Park] holding {self.target_wn:.5f} (deadband "
              f"+/-{self.cfg['park_deadband']})")
        while not self.stop_event.is_set():
            with self.state.lock:
                wn = self.state.wn
                self.state.sweep_dir = 0
            if np.isfinite(wn):
                buf.append(wn)
            if len(buf) == buf.maxlen:
                avg = sum(buf) / len(buf)
                err = self.target_wn - avg
                now = time.time()
                if (abs(err) > float(self.cfg["park_deadband"])
                        and now - self.last_nudge
                        > float(self.cfg["park_nudge_min_interval"])):
                    step = (float(self.cfg["park_nudge_gain"]) * err
                            / self.cfg["wn_per_unit"])
                    cap = float(self.cfg["park_nudge_max"])
                    step = max(-cap, min(cap, step))
                    wn_dir = 1 if err > 0 else -1
                    if wn_dir != int(self.cfg["preferred_wn_dir"]):
                        step *= 0.5  # extra conservative on the lurchy side
                    try:
                        pos = self.laser.qPOS()
                        self.laser.MOV(pos + step)
                        self.last_nudge = now
                        self.n_nudges += 1
                        buf.clear()
                        print(f"[Park] nudge #{self.n_nudges}: wn={avg:.5f} "
                              f"err={err:+.4f} step={step:+.5f}")
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
                    for d in data:
                        bunch, channel, tof = d[0], d[2], d[-1]
                        if channel == -1:
                            continue  # trigger marker, not an event
                        if recorder is not None:
                            recorder.add_event(
                                [now, channel, tof, wn, sweep_dir, bunch])
                        state.total_events += 1
                        ti = np.searchsorted(state.tof_edges, tof) - 1
                        i = state.bin_of(wn)
                        if 0 <= ti < len(state.tof_hist):
                            state.tof_hist[ti] += 1
                            if i is not None:
                                state.hist2d[i, ti] += 1
                        if i is not None:
                            state.counts[i] += 1
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
