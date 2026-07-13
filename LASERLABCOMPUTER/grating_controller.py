import os
import sys
import threading
import time
from xmlrpc.server import SimpleXMLRPCServer
from socketserver import ThreadingMixIn
from pylablib.devices import Sirah

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))

SIMULATION = os.environ.get('SIMULATION', '0') == '1'

if SIMULATION:
    print("[Server] SIMULATION MODE ENABLED")
    from simulation import get_mock_device, mock_caget
    GCSDevice = None
    epics = None
else:
    try:
        from pipython import GCSDevice,pitools
        from pipython.pidevice.gcserror import GCSError
        import epics
    except ImportError as e:
        print(f"[Server] Hardware libraries missing: {e}. Use SIMULATION=1 for testing.")
        sys.exit(1)

CONTROLLERNAME = 'HydraPollux'
COM_PORT = 5
BAUD_RATE = 19200
SERVER_IP = '0.0.0.0'
SERVER_PORT = 8000

class ThreadedXMLRPCServer(ThreadingMixIn, SimpleXMLRPCServer):
    """Allows the server to handle multiple requests (like non-blocking queries) simultaneously."""
    pass

class LaserServerInterface:
    def __init__(self):
        self.lock = threading.Lock()
        self.laser = True
        try:
            print(f"[Server] Initializing {CONTROLLERNAME}...")
            if SIMULATION:
                self.pi = get_mock_device()
                self.pi.ConnectRS232(comport=COM_PORT, baudrate=BAUD_RATE)
                self.pi.SVO(1, 1)
                return
            elif self.laser:
                self.pi = GCSDevice(CONTROLLERNAME)
                self.pi.ConnectRS232(comport=COM_PORT, baudrate=BAUD_RATE)
                # The Pollux GCS DLL can fire a spurious GCSError(0) on the first
                # command after connect (stale error buffer). Drain it, then run
                # the post-connect setup tolerantly so a harmless error does NOT
                # reach the outer except and null self.pi -- which would silently
                # break every MOV/qPOS afterwards (grating never moves, qPOS 0.0).
                try:
                    self.pi.qERR()
                except GCSError:
                    pass
                try:
                    print(f"[Server] Connected: {self.pi.qIDN().strip()}")
                except GCSError as e:
                    print(f"[Server] qIDN threw {e} (tolerated).")
                self._enable_servo_tolerant()
                try:
                    print(f"[Server] Position (axis 1): {self.pi.qPOS(1)[1]}")
                except GCSError as e:
                    print(f"[Server] qPOS threw {e} (tolerated).")
            else:
                self.sirah = Sirah.SirahMatisse("USB0::0x17E7::0x0102::24-50-09::INSTR")
        except Exception as e:
            print(f"[Server] CRITICAL HARDWARE ERROR: {e}")
            self.pi = None

    def _enable_servo_tolerant(self):
        """Enable the servo on axis 1, tolerating the spurious GCSError(0) the
        Pollux GCS DLL fires on the first command after connect. Mirrors
        jog_grating.py: drain qERR(), retry once, then verify the real servo
        state (qSVO) rather than dying on a harmless error."""
        try:
            self.pi.SVO(1, 1)
            print("[Server] Servo enabled (Axis 1).")
            return
        except GCSError as e:
            pass
        try:
            self.pi.qERR()
            self.pi.SVO(1, 1)  # one clean retry after draining the error buffer
            print("[Server] Servo enabled (Axis 1) after retry.")
            return
        except GCSError as e2:
            try:
                servo_on = bool(self.pi.qSVO(1)[1])
            except Exception:
                servo_on = False
            if servo_on:
                print(f"[Server] SVO threw {e2}, but servo reads ON -- continuing.")
            else:
                print(f"[Server] WARNING: could not enable servo ({e2}); "
                      "moves may be rejected. Continuing anyway.")

    def MOV(self, axis, target):
        print(f"[CMD] MOV Axis {axis} -> {target}")
        try:
            with self.lock:
                if self.laser:
                    self.pi.MOV(axis, float(target))
                    time.sleep(0.5) # Critical: Small pause
                else:
                    print()
                    self.sirah.ask(f'SCAN:NOW {float(target)}')
            return True
        except Exception as e:
            print(f"Hardware Error in MOV: {e}")
            return False

    def qPOS(self, axis):
        try:
            with self.lock:
                if self.laser:
                    val = self.pi.qPOS(axis)[axis]
                    time.sleep(0.5) # Critical: Small pause after serial talk
                else:
                    val = self.sirah.ask(f'SCAN:NOW?')
            return float(val)   
        except Exception as e:
            print(f"Hardware Error in qPOS: {e}")
            return 0.0

    def close(self):
        if self.laser:
            self.pi.CloseConnection()
        else:
            self.sirah.close()

if __name__ == "__main__":
    import time
    import socket
    socket.setdefaulttimeout(120)

    server = ThreadedXMLRPCServer((SERVER_IP, SERVER_PORT), allow_none=True)
    server.register_instance(LaserServerInterface())

    print(f"==========================================")
    print(f" LASER SERVER RUNNING ON PORT {SERVER_PORT}")
    print(f"==========================================")
    try:
        print("Server active. Press Ctrl+C to stop.")
        server.serve_forever()
    except Exception as e:
        with open("server_crash_log.txt", "a") as f:
            f.write(f"Crash at {time.ctime()}: {str(e)}\n")
        print(f"CRITICAL SERVER ERROR: {e}")
    finally:
        print("Shutting down...")