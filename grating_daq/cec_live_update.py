"""Keep the CEC-temperature calibration current: re-run cec_temp_scan.py every PERIOD s so
data/cec_scan_<date>.csv/.png always include every park taken so far (heating + cooling legs).
One status line per pass goes to data/cec_live_<date>.log. Stops at midnight or when killed.
usage: pythonw grating_daq/cec_live_update.py [period_s]   (default 300)"""
import os, subprocess, sys, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PERIOD = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0
DATE = datetime.now().strftime("%Y%m%d")
PY = sys.executable.replace("pythonw.exe", "python.exe")
LOG = os.path.join(HERE, "data", f"cec_live_{DATE}.log")
env = dict(os.environ, PYTHONIOENCODING="utf-8")

while datetime.now().strftime("%Y%m%d") == DATE:
    t0 = time.time()
    try:
        r = subprocess.run([PY, os.path.join(HERE, "cec_temp_scan.py"), "--date", DATE], cwd=HERE,
                           capture_output=True, text=True, timeout=240, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout + r.stderr).strip().splitlines()
        tail = [l for l in out if l.strip()[:3].isdigit() or "Error" in l or "Traceback" in l][-2:]
        msg = f"rc={r.returncode} " + " | ".join(s.strip() for s in tail)
    except Exception as exc:  # noqa: BLE001
        msg = f"FAILED {exc!r}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S} {msg}\n")
    time.sleep(max(10.0, PERIOD - (time.time() - t0)))
