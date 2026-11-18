"""Virtual-time simulation of grating_daq ParkController against a grating
plant with backlash, slope, stiction and a slow, lagging wavemeter.

usage: python park_sim.py <engine.py path> [wn_per_unit_cfg] [start_wn] [target] [deadband] [T]
"""
import importlib.util
import random
import sys
import threading
import types

import numpy as np

path = sys.argv[1]
cfg_slope = float(sys.argv[2]) if len(sys.argv) > 2 else 70
start_wn = float(sys.argv[3]) if len(sys.argv) > 3 else 12624.52
target = float(sys.argv[4]) if len(sys.argv) > 4 else 12624.60
deadband = float(sys.argv[5]) if len(sys.argv) > 5 else 0.05
T_END = float(sys.argv[6]) if len(sys.argv) > 6 else 400
SEED = int(sys.argv[7]) if len(sys.argv) > 7 else 0
random.seed(SEED)

# ---- stub the hardware imports engine.py makes -------------------------------
for name in ("src", "src.devices", "src.devices.laser", "src.devices.tagger"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["src.devices.laser"].GratingDevice = object
sys.modules["src.devices.tagger"].Tagger = object

CLOCK = [0.0]
fake_time = types.SimpleNamespace(time=lambda: CLOCK[0], sleep=lambda s: None)

spec = importlib.util.spec_from_file_location("engine_sim", path)
eng = importlib.util.module_from_spec(spec)
sys.modules["engine_sim"] = eng
spec.loader.exec_module(eng)
eng.time = fake_time


class Plant:
    """motor m (qPOS), grating g lags m by backlash B; wn = wn0 + S*g."""
    S = 210.0          # true cm-1 per unit
    B = float(__import__("os").environ.get("PLANT_B", 0.02))
    STICK = 0.0004     # grating moves below this per command are eaten (stiction)

    def __init__(self, wn):
        self.m = 0.0
        self.g = 0.0          # grating pushed from the low side: m - g in [0, B]
        self.g0_wn = wn
        self.vel = 0.001
        self.cmd = 0.0
        self.pending_g = 0.0

    def MOV(self, p):
        self.cmd = float(p)

    def qPOS(self):
        return self.m

    def VEL(self, v):
        self.vel = float(v)

    def step(self, dt):
        d = self.cmd - self.m
        mv = float(np.clip(d, -self.vel * dt, self.vel * dt))
        self.m += mv
        # backlash: the grating is dragged only when the motor pushes on a
        # contact side (gap B); stiction: it moves in slips of >= STICK
        if self.m > self.g + self.B:
            gt = self.m - self.B
        elif self.m < self.g:
            gt = self.m
        else:
            gt = self.g
        if abs(gt - self.g) >= self.STICK:
            self.g = gt

    def wn(self):
        return self.g0_wn + self.S * self.g


plant = Plant(start_wn)
plant.m = 0.0
plant.g = 0.0       # last move was upward: motor touching the high-side contact
plant.m = plant.B   # (m - g = B: pushing up)

state = types.SimpleNamespace(lock=threading.Lock(), wn=start_wn, sweep_dir=0, wm_period=0.6,
                              park_target=None, park_deadband=None, wn_v_est=0.0, wn_change_t=0.0, wm_stale=False, wm_stale_since=None)
WM_LAG, WM_PERIOD = 0.7, 0.6
hist = []           # (t, true wn)
next_wm = [0.0]
trace = []


class SimEvent:
    def __init__(self):
        self._set = False

    def is_set(self):
        return self._set or CLOCK[0] >= T_END

    def set(self):
        self._set = True

    def wait(self, dt):
        t_end = CLOCK[0] + dt
        while CLOCK[0] < t_end - 1e-9:
            h = min(0.05, t_end - CLOCK[0])
            plant.step(h)
            CLOCK[0] += h
            hist.append((CLOCK[0], plant.wn()))
            if CLOCK[0] >= next_wm[0]:
                tl = CLOCK[0] - WM_LAG
                old = [w for t, w in hist if t <= tl]
                if old:
                    state.wn = old[-1] + random.gauss(0, 0.002)
                    state.wn_change_t = CLOCK[0]
                next_wm[0] = CLOCK[0] + WM_PERIOD
            if int(CLOCK[0] * 2) != int((CLOCK[0] - h) * 2):
                trace.append((round(CLOCK[0], 1), round(plant.wn(), 3)))
        return self.is_set()


cfg = dict(eng.DEFAULTS)
cfg.update({"wn_per_unit": cfg_slope, "preferred_wn_dir": 1})
lo, hi = target, deadband          # reuse argv: target=wn_lo, deadband=wn_hi
plant.VEL = lambda v: (setattr(plant, "vel", float(v)), True)[1]
sw = eng.SurveySweeper(plant, state, cfg, lo, hi)
sw.stop_event = SimEvent()
sw.run()
w = np.array([x[1] for x in trace]); t = np.array([x[0] for x in trace])
# count reversals of the true wn path between the bounds and time spent stalled
inside = (w >= lo) & (w <= hi)
d = np.diff(w)
stalled = np.mean(np.abs(d) < 1e-4) * 100
sign = np.sign(d[np.abs(d) > 1e-3]); revs = int(np.sum(sign[1:] != sign[:-1]))
print(f"survey cfg {cfg_slope}: wn {w.min():.3f}..{w.max():.3f}, inside {inside.mean()*100:.0f}%, "
      f"stalled {stalled:.0f}% of 0.5 s samples, direction changes {revs}, last 30 s span "
      f"{np.ptp(w[t > t[-1]-30]):.3f}")
