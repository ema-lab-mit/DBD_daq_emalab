"""Frozen-wavemeter guard: WnPoller flags it, Park and Survey issue no moves while flagged."""
import sys, threading, time, types
sys.path.insert(0, r"C:\Users\EMALAB\Desktop\DBD_daq_emalab")
for n in ("src.devices.laser", "src.devices.tagger"):
    m = types.ModuleType(n); sys.modules[n] = m
sys.modules["src.devices.laser"].GratingDevice = object
sys.modules["src.devices.tagger"].Tagger = object
from grating_daq import engine as eng

# 1) WnPoller: constant readings -> stale after stale_s, recovers on a new value
cfg = dict(eng.DEFAULTS, wm_stale_s=1.0, wn_poll_s=0.05)
st = eng.SharedState(12624.0, 12625.0, 0.01)
vals = iter([12624.5 + i * 1e-4 for i in range(5)])
cur = [12624.5]
wp = eng.WnPoller(cfg)
def rd():
    return cur[0]
wp.read_wn = rd
wp.set_state(st); wp.start()
for v in vals:
    cur[0] = v; time.sleep(0.1)
assert not st.wm_stale
time.sleep(1.6)
assert st.wm_stale, "not flagged after 1.6 s frozen"
cur[0] = 12624.6; time.sleep(0.3)
assert not st.wm_stale, "did not recover"
wp.stop_event.set()
print("WnPoller stale flag OK")

# 2) Park + Survey hold while stale
class Laser:
    def __init__(self): self.movs = []; self.p = 0.0
    def MOV(self, p): self.movs.append(p); self.p = p
    def qPOS(self): return self.p
    def VEL(self, v): return True
for cls, args in ((eng.ParkController, (12624.6,)), (eng.SurveySweeper, (12624.3, 12624.9))):
    st = eng.SharedState(12624.0, 12625.0, 0.01)
    st.wn = 12623.0; st.wm_period = 0.6; st.wn_change_t = time.time(); st.wm_stale = True
    st.wm_stale_since = time.time()
    las = Laser()
    c = cls(las, st, dict(eng.DEFAULTS, wn_per_unit=210, park_deadband=0.05), *args)
    c.start(); time.sleep(2.0)
    n_held = len(las.movs)
    c.stop_event.set(); c.join(3)
    # Survey issues exactly one hold MOV (to its own position); Park none
    held_ok = (n_held == 0) if cls is eng.ParkController else (n_held == 1 and las.movs[0] == 0.0)
    assert held_ok, (cls.__name__, las.movs)
    print(f"{cls.__name__}: held while frozen ({n_held} MOV)")
