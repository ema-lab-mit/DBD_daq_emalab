"""qc_bridge.py - runs on the LASER-LAB PC (10.54.6.1). Owns that lab's Quantum Composer
(inter-laser delay) on its USB-serial COM port and lets SUGUI on the beamline PC
(10.54.5.139) read it and set channel A over the network. 2026-11-13.

Protocol: plain TCP, one SCPI command per line (CR/LF), one reply line back - exactly
what the QC itself answers on its serial port ('ok' to settings, the value to queries,
'?n' errors). SUGUI's 'qc_remote' driver speaks it (same code as its COM15 RFQ QC).

Safety:
  * only clients in --allow may connect (default: the beamline PC + this PC);
  * queries are allowed for every channel, but SETTINGS only for the channels in
    --write (default: 1 = channel A). T0 / system commands (:PULSE0:..., :INST:STATE)
    are always refused, so nothing remote can stop this lab's QC outputs;
  * every accepted setting is logged to qc_bridge_log.csv next to this file.

The COM port is exclusive: close QC_GUI.py before starting the bridge (and stop the
bridge, Ctrl+C, before using QC_GUI.py again).

usage (on the laser-lab PC):
    python qc_bridge.py --com COM3                 # real QC on COM3
    python qc_bridge.py --sim                      # no hardware (test)
options: --baud 115200  --tcp-port 8010  --allow 10.54.5.139,127.0.0.1  --write 1
needs:   pip install pyserial   (not for --sim)
One-time firewall rule on the laser-lab PC (admin PowerShell):
    New-NetFirewallRule -DisplayName "QC bridge 8010" -Direction Inbound -Protocol TCP `
        -LocalPort 8010 -RemoteAddress 10.54.5.139 -Action Allow
"""
import argparse
import csv
import os
import re
import socketserver
import threading
import time
from datetime import datetime

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--com", default="COM3", help="QC serial port on this PC (see Device Manager)")
ap.add_argument("--baud", type=int, default=115200)
ap.add_argument("--tcp-port", type=int, default=8010)
ap.add_argument("--listen", default="0.0.0.0")
ap.add_argument("--allow", default="10.54.5.139,127.0.0.1", help="comma list of client IPs")
ap.add_argument("--write", default="1", help="comma list of writable pulse numbers (1 = A)")
ap.add_argument("--sim", action="store_true", help="emulate a QC (no hardware)")
args = ap.parse_args()

ALLOW = {a.strip() for a in args.allow.split(",") if a.strip()}
WRITABLE = {w.strip() for w in args.write.split(",") if w.strip()}
QUERY = re.compile(r"^(\*IDN\?|:INST:STATE\?|:PULSE\d:[A-Z:]{2,12}\?)$")
WRITE = re.compile(r"^:PULSE([1-8]):(DELAY|WIDT|STATE|POL|CMOD|PCO|OCO) [0-9A-Z.+\-]{1,24}$")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qc_bridge_log.csv")


class SimQc:
    """Tiny emulator: 4 channels, 50 Hz T0, replies like the real 9514+."""
    def __init__(self):
        self.ch = {n: {"STATE": "0", "DELAY": 0.0, "WIDT": 1e-6, "POL": "NORM", "SYNC": "T0",
                       "CMOD": "NORM", "PCO": "1", "OCO": "0"} for n in "1234"}

    def query(self, c):
        if c == "*IDN?":
            return "9514+-SIM-BRIDGE"
        if c == ":INST:STATE?":
            return "1"
        if c.startswith(":PULSE0:"):
            return {"PER?": "0.02000000000", "MOD?": "NORM", "TRIG:MOD?": "DIS"}.get(c[8:], "?1")
        n, key = c[6], c[8:]
        if n not in self.ch:
            return "?1"
        d = self.ch[n]
        if key.endswith("?"):
            v = d.get(key[:-1])
            return "?1" if v is None else (f"{v:.11f}" if isinstance(v, float) else str(v))
        k, _, a = key.partition(" ")
        if k in ("DELAY", "WIDT"):
            d[k] = round(float(a), 11)
        elif k == "STATE":
            d[k] = "1" if a in ("1", "ON") else "0"
        else:
            d[k] = a
        return "ok"

    def close(self):
        pass


class SerialQc:
    def __init__(self, port, baud):
        self.port, self.baud, self.ser = port, baud, None

    def _open(self):
        import serial
        self.ser = serial.Serial(self.port, self.baud, timeout=1.0, write_timeout=1.0)

    def query(self, c):
        for attempt in (1, 2):                # one reopen after a serial fault
            try:
                if self.ser is None:
                    self._open()
                self.ser.reset_input_buffer()
                self.ser.write((c + "\r\n").encode())
                r = self.ser.read_until(b"\n")
                if not r.endswith(b"\n"):
                    raise TimeoutError(f"no reply from the QC to {c!r}")
                return r.decode(errors="replace").strip()
            except Exception as exc:          # noqa: BLE001
                self.close()
                if attempt == 2:
                    return f"?99 bridge serial error: {exc}"

    def close(self):
        try:
            if self.ser is not None:
                self.ser.close()
        finally:
            self.ser = None


qc = SimQc() if args.sim else SerialQc(args.com, args.baud)
io_lock = threading.Lock()


def log_write(client, cmd, reply):
    new = not os.path.exists(LOG)
    try:
        with open(LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "client", "command", "reply"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), client, cmd, reply])
    except OSError:
        pass


class Handler(socketserver.StreamRequestHandler):
    timeout = 600                              # drop idle clients after 10 min

    def handle(self):
        client = self.client_address[0]
        if client not in ALLOW:
            print(f"[qc_bridge] refused connection from {client}")
            return
        print(f"[qc_bridge] {client} connected")
        while True:
            try:
                raw = self.rfile.readline(256)
            except OSError:
                break
            if not raw:
                break
            cmd = raw.decode(errors="replace").strip().upper()
            if not cmd:
                continue
            m = WRITE.match(cmd)
            if QUERY.match(cmd):
                with io_lock:
                    reply = qc.query(cmd)
            elif m and m.group(1) in WRITABLE:
                with io_lock:
                    reply = qc.query(cmd)
                log_write(client, cmd, reply)
                print(f"[qc_bridge] {datetime.now():%H:%M:%S} {client}: {cmd} -> {reply}")
            else:
                reply = "?7 refused by qc_bridge (not a query or not a writable channel)"
                print(f"[qc_bridge] refused from {client}: {cmd}")
            try:
                self.wfile.write((reply + "\r\n").encode())
            except OSError:
                break
        print(f"[qc_bridge] {client} disconnected")


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    with io_lock:
        idn = qc.query("*IDN?")
    print(f"[qc_bridge] QC on {'SIM' if args.sim else args.com}: {idn}")
    if idn.startswith("?"):
        print("[qc_bridge] WARNING: the QC did not answer *IDN? - wrong COM port, or QC_GUI.py still open?")
    print(f"[qc_bridge] listening on {args.listen}:{args.tcp_port}; clients {sorted(ALLOW)}; "
          f"writable pulses {sorted(WRITABLE)} (1 = A). Ctrl+C to stop.")
    srv = Server((args.listen, args.tcp_port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        qc.close()
        print("[qc_bridge] stopped, COM port closed")
