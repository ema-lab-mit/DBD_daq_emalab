"""Client-side handles to the LASERLABCOMPUTER laser server and the EPICS
wavemeter PVs. The laser is reached over XML-RPC; EPICS is reached directly.

The laser-lab machine runs ONE of two servers on port 8000, never both:

  * ``matisse_cd_controller.py`` — the Matisse, driven through its CounterDrift
    firmware. Client: ``MatisseDevice`` (cd_* / goto_* / ping).
  * ``grating_controller.py``    — the grating, a bare PI motion stage. Client:
    ``GratingDevice`` (MOV / qPOS).

Which one the DAQ uses is selected by ``control_settings.laser_type`` in
settings.json; daq_system.py builds the matching client + controller. Both
clients share the transport and the outage-logging below: a clearly formatted
banner whenever the server is unreachable, hung, or returning XML-RPC faults,
deduplicated to one short "still unreachable" line so the log isn't flooded,
with a "reachable again" notice on recovery.
"""

import os
import socket
import xmlrpc.client
from threading import Lock

try:
    import epics
except ImportError:
    print("[HW] Warning: epics module not found.")
    epics = None


LAB_COMPUTER_IP = "10.54.6.1"
LAB_COMPUTER_PORT = "8000"

if os.environ.get("SIMULATION", "0") == "1":
    LAB_COMPUTER_IP = "localhost"


# Exception types that mean "the laser server is unreachable / unresponsive".
_NETWORK_ERRORS = (
    xmlrpc.client.Fault,
    xmlrpc.client.ProtocolError,
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
    socket.timeout,
    socket.gaierror,
    OSError,
)


class _LaserServerProxy:
    """Shared XML-RPC transport + health/outage logging for whichever laser
    server is running on LAB_COMPUTER:PORT. Subclasses add the method wrappers
    specific to their server and a startup probe."""

    # Banner tag, overridden per subclass so logs say [Matisse] / [Grating].
    _TAG = "Laser"

    def __init__(self):
        self.url = f"http://{LAB_COMPUTER_IP}:{LAB_COMPUTER_PORT}"
        self.lock = Lock()
        self.proxy = xmlrpc.client.ServerProxy(
            self.url,
            allow_none=True,
            use_builtin_types=True,
        )
        # None = unknown, True = last call OK, False = last call failed.
        # Used to dedupe noisy outage logs.
        self._healthy = None

    def is_healthy(self) -> bool:
        return self._healthy is True

    # ---- Connectivity ----

    def _tcp_reachable(self) -> bool:
        """TCP-level reachability of host:port. Succeeds fast if anything is
        listening, fails fast (ConnectionRefused / EHOSTUNREACH / DNS error)
        otherwise. Independent of which XML-RPC methods the server exposes."""
        host = LAB_COMPUTER_IP
        try:
            port = int(LAB_COMPUTER_PORT)
        except (TypeError, ValueError):
            port = 8000
        try:
            with socket.create_connection((host, port), timeout=2.0):
                pass
        except _NETWORK_ERRORS as e:
            self._log_outage("tcp_connect", (host, port), e)
            self._healthy = False
            return False
        return True

    # ---- Internal call wrapper ----

    def _call(self, method: str, *args):
        """Invoke `method` on the server proxy with logging on failure.

        On any network/XML-RPC error, prints a banner on first occurrence
        (subsequent identical failures get one short line) and re-raises so
        the controller's per-call exception handler can react. On recovery,
        prints a "reachable again" message exactly once per outage."""
        try:
            with self.lock:
                result = getattr(self.proxy, method)(*args)
        except _NETWORK_ERRORS as e:
            self._log_outage(method, args, e)
            self._healthy = False
            raise
        if self._healthy is False:
            print(f"[{self._TAG}] Server reachable again at {self.url}")
        self._healthy = True
        return result

    def _log_outage(self, method, args, err):
        if self._healthy is False:
            print(
                f"[{self._TAG}] {method}{tuple(args)}: still unreachable "
                f"({type(err).__name__}: {err})"
            )
            return
        print(f"[{self._TAG}] =============== LASER SERVER ERROR ===============")
        print(f"[{self._TAG}] URL    : {self.url}")
        print(f"[{self._TAG}] Call   : {method}{tuple(args)}")
        print(f"[{self._TAG}] Error  : {type(err).__name__}: {err}")
        print(f"[{self._TAG}] Hint   : check that the laser server on the laser-lab")
        print(f"[{self._TAG}]          machine is running and {self.url} is reachable.")
        print(f"[{self._TAG}] ==================================================")


class MatisseDevice(_LaserServerProxy):
    """Thin XML-RPC proxy mirroring matisse_cd_controller.LaserServerInterface."""

    _TAG = "Matisse"

    def __init__(self, controller_name: str = "", initialization_params: dict = {}):
        super().__init__()
        # Will be set to True/False by _ping_on_startup if the server
        # supports is_simulation(); None means we couldn't ask.
        self._server_simulation = None
        # Whether the DAQ wants real hardware. Real-mode client + sim-mode
        # server = Frankenstein data; we refuse to operate in that case.
        self._daq_wants_real = (os.environ.get("SIMULATION", "0") != "1")
        print(f"[Matisse] Connecting to laser server at {self.url} ...")
        self._ping_on_startup()

    # ---- Connectivity ----

    def _ping_on_startup(self):
        # Step 1: TCP-level reachability.
        if not self._tcp_reachable():
            return

        # Step 2: server is reachable. Try ping() for a hardware-side health
        # check. An older Matisse server without ping() returns an XML-RPC
        # Fault (NOT a network error) — treat that as "reachable, older API".
        # A *grating* server is also reachable here and has no ping(), so the
        # same Fault path covers "wrong server running"; the controller's
        # cd_* calls will then fail loudly and the operator must fix laser_type.
        try:
            with self.lock:
                ok = bool(self.proxy.ping())
        except xmlrpc.client.Fault:
            print(
                f"[Matisse] Server reachable at {self.url} (older API: no ping). "
                "Hardware health will only surface on first real command. "
                "If laser_type should be 'grating', set it in settings.json."
            )
            self._healthy = True
            return
        except _NETWORK_ERRORS as e:
            self._log_outage("ping", (), e)
            self._healthy = False
            return

        if ok:
            print(f"[Matisse] Server ping OK at {self.url}")
            self._healthy = True
            # Mismatch between client and server simulation mode is a serious
            # bug — it produces Frankenstein data (real tagger vs mock laser).
            self._check_sim_mode()
            return
        else:
            # Server is up but its Matisse handle is None — the laser itself
            # is not initialised. The DAQ can still boot; the operator needs
            # to restart Matisse Commander / the server.
            print("[Matisse] =========== LASER SERVER WARNING ===========")
            print(f"[Matisse] Server at {self.url} is reachable but reports")
            print("[Matisse] that the Matisse handle is NOT initialised.")
            print("[Matisse] Restart the Matisse server with Matisse Commander")
            print("[Matisse] running and the USB device available.")
            print("[Matisse] ============================================")
            self._healthy = False

    def _check_sim_mode(self):
        """Detect server-side simulation mode and shout if the DAQ wants
        real hardware but the server is in sim — that combination silently
        produces real-tagger-vs-mock-laser Frankenstein data."""
        try:
            with self.lock:
                server_is_sim = bool(self.proxy.is_simulation())
        except xmlrpc.client.Fault:
            # Older server build without is_simulation(); we can't tell.
            return
        except _NETWORK_ERRORS:
            return
        self._server_simulation = server_is_sim
        if server_is_sim and self._daq_wants_real:
            print("[Matisse] ========== SIMULATION MODE MISMATCH ==========")
            print(f"[Matisse] Server at {self.url} is running in SIMULATION mode")
            print("[Matisse] (the laser server was launched with SIMULATION=1) but")
            print("[Matisse] this DAQ has simulation_mode=False, expecting REAL")
            print("[Matisse] hardware. Every laser command goes to a MOCK laser")
            print("[Matisse] while the tagger and wavemeter are real — recorded")
            print("[Matisse] data will be Frankenstein.")
            print("[Matisse] Fix on the laser-lab machine:")
            print("[Matisse]   Remove-Item Env:SIMULATION    (PowerShell)")
            print("[Matisse]   or just open a fresh terminal, then relaunch the server.")
            print("[Matisse] ===============================================")
            self._healthy = False
        elif server_is_sim:
            print("[Matisse] Server is in SIMULATION mode (matches DAQ config).")

    # ---- CounterDrift ----

    def cd_open(self) -> bool:
        return bool(self._call("cd_open"))

    def cd_setpoint(self, nm: float) -> bool:
        return bool(self._call("cd_setpoint", float(nm)))

    def cd_activate(self, state: bool) -> bool:
        return bool(self._call("cd_activate", bool(state)))

    def cd_get_wavelength(self) -> float:
        return float(self._call("cd_get_wavelength"))

    # ---- GoTo ----

    def goto_open(self) -> bool:
        return bool(self._call("goto_open"))

    def goto_set(self, nm: float) -> bool:
        return bool(self._call("goto_set", float(nm)))

    def goto_start(self) -> bool:
        return bool(self._call("goto_start"))

    def goto_status(self) -> str:
        return str(self._call("goto_status"))


class GratingDevice(_LaserServerProxy):
    """Thin XML-RPC proxy mirroring grating_controller.LaserServerInterface.

    That server is a bare positioner: ``MOV(axis, target)`` and ``qPOS(axis)``.
    It exposes neither ping() nor is_simulation(), so the startup probe uses a
    qPOS read both to confirm reachability/health and to warn early if the
    *Matisse* server is running instead (qPOS would fault)."""

    _TAG = "Grating"

    def __init__(self, controller_name: str = "", initialization_params: dict = {}):
        super().__init__()
        self.axis = int(initialization_params.get("axis", 1))
        print(f"[Grating] Connecting to laser server at {self.url} ...")
        self._probe_on_startup()

    def _probe_on_startup(self):
        if not self._tcp_reachable():
            return
        # qPOS is the cheapest call that proves we're talking to the grating
        # server. A Fault here means the server on :8000 has no qPOS — almost
        # certainly the Matisse server is running; tell the operator to fix
        # laser_type rather than silently failing on the first MOV.
        try:
            with self.lock:
                pos = float(self.proxy.qPOS(self.axis))
        except xmlrpc.client.Fault:
            print("[Grating] =========== WRONG LASER SERVER ===========")
            print(f"[Grating] Server at {self.url} is reachable but has no qPOS().")
            print("[Grating] laser_type='grating' but the MATISSE server appears")
            print("[Grating] to be running on the laser-lab machine. Start")
            print("[Grating] grating_controller.py there, or set laser_type='matisse'.")
            print("[Grating] ==========================================")
            self._healthy = False
            return
        except _NETWORK_ERRORS as e:
            self._log_outage("qPOS", (self.axis,), e)
            self._healthy = False
            return
        print(f"[Grating] Server qPOS OK at {self.url} (axis {self.axis} = {pos:.4f}).")
        self._healthy = True

    def MOV(self, target) -> bool:
        return bool(self._call("MOV", self.axis, float(target)))

    def qPOS(self) -> float:
        return float(self._call("qPOS", self.axis))

    def VEL(self, velocity) -> bool:
        """Set stage velocity (units/s). Older server builds have no VEL();
        that comes back as an XML-RPC Fault -> log once and report False so
        callers can proceed at the server's startup velocity."""
        try:
            return bool(self._call("VEL", self.axis, float(velocity)))
        except xmlrpc.client.Fault:
            print("[Grating] server has no VEL() method (old build); "
                  "velocity unchanged -- update the laser-lab server to enable "
                  "scan-speed control.")
            return False


class ComClient:
    """Thin wrapper around epics.caget so the controller has one indirection
    point for wavemeter reads. Kept for compatibility with the existing
    daq_system.py wiring. The ``laser_device`` argument is accepted for that
    wiring but unused — wavemeter reads go straight to EPICS."""

    def __init__(self, laser_device, **kwargs):
        self.laser = laser_device

    def caget(self, pvname: str):
        try:
            return epics.caget(pvname)
        except Exception as e:
            print(f"[EPICS Client Error] {e}")
            return 0.0
