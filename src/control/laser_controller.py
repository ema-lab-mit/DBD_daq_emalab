import time
import threading
from src.simulation.hardware_mocks import MockPIGCSDevice, MockEpicsClient
from src.control.pid import PIDController

class LaserController:
    """
    Encapsulates the logic from the 'go_to' script to control the Laser
    via a PI Stage and a Wavemeter (EPICS).

    Supports two controller modes: 'pid' and 'bangbang'.
    """
    def __init__(self, pi_device, epics_client, axis=1, config: dict = {}):
        self.device = pi_device
        self.epics = epics_client
        self.axis = axis
        self.config = config

        # Control Loop Parameters
        self.tolerance = self.config.get("tolerance", 0.01)
        self.step_fine = self.config.get("step_fine", 0.0001)
        self.step_coarse = self.config.get("step_coarse", 0.05)
        self.poll_interval = self.config.get("poll_interval", 1)
        self.coarse_approach_thresh = self.config.get("coarse_approach_threshold", 1.0)
        self.required_stable_samples = self.config.get("required_stable_samples", 4)

        # Controller mode and voltage limits
        self.controller_mode = self.config.get("controller_mode", "pid")
        self.voltage_min = self.config.get("voltage_min", 0.0)
        self.voltage_max = self.config.get("voltage_max", 5.0)

        # PID controller
        pid_config = self.config.get("pid", {})
        self.pid = PIDController(
            kp=pid_config.get("kp", 0.02),
            ki=pid_config.get("ki", 0.005),
            kd=pid_config.get("kd", 0.0),
            d_filter_coeff=pid_config.get("d_filter_coeff", 0.1),
        )

        self.target_wn = 0.0
        self.current_wn = 0.0
        self.is_moving = False
        self.voltage_limited = False

        # Threading for the control loop
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.control_thread = None

    def update_config(self, config: dict):
        """
        Updates the control loop parameters at runtime.
        """
        with self.lock:
            self.config.update(config)
            self.tolerance = self.config.get("tolerance", 0.01)
            self.step_fine = self.config.get("step_fine", 0.0001)
            self.step_coarse = self.config.get("step_coarse", 0.05)
            self.poll_interval = self.config.get("poll_interval", 0.5)
            self.coarse_approach_thresh = self.config.get("coarse_approach_threshold", 1.0)
            self.required_stable_samples = self.config.get("required_stable_samples", 4)

            self.controller_mode = self.config.get("controller_mode", self.controller_mode)
            self.voltage_min = self.config.get("voltage_min", self.voltage_min)
            self.voltage_max = self.config.get("voltage_max", self.voltage_max)

            pid_config = self.config.get("pid", {})
            if pid_config:
                self.pid.update_gains(
                    kp=pid_config.get("kp"),
                    ki=pid_config.get("ki"),
                    kd=pid_config.get("kd"),
                    d_filter_coeff=pid_config.get("d_filter_coeff"),
                )

            print(f"[LaserController] Config updated: mode={self.controller_mode}, "
                  f"tol={self.tolerance}, poll={self.poll_interval}")

    def set_wavenumber(self, target_wn):
        """
        Starts the background control loop to reach target_wn.
        """
        with self.lock:
            self.target_wn = target_wn
            self.stop_event.clear()
            self.pid.reset()
            self.voltage_limited = False

            if self.control_thread and self.control_thread.is_alive():
                 # Already running, just updated target
                 pass
            else:
                 self.is_moving = True
                 self.control_thread = threading.Thread(target=self._control_loop, daemon=True)
                 self.control_thread.start()

    def get_wavenumber(self):
        """
        Returns the current wavenumber from EPICS.
        """
        return float(self.epics.caget('LaserLab:wavenumber_1'))

    def is_stable(self, tolerance=None):
        """
        Returns True if current WN is within tolerance of Target WN.
        """
        if tolerance is None:
             tolerance = self.tolerance

        wn = self.get_wavenumber()
        return abs(wn - self.target_wn) < tolerance and not self.is_moving

    def stop(self):
        self.stop_event.set()
        if self.control_thread:
            self.control_thread.join()

    def _compute_bangbang(self, current_wn, current_voltage):
        """Compute next voltage using bang-bang (coarse/fine step) logic."""
        step_fine = self.step_fine
        step_coarse = self.step_coarse
        voltage_cmd = 0.0

        if current_wn >= self.target_wn + self.tolerance:
            if abs((current_voltage - step_fine) - self._prev_voltage) > 1e-9:
                voltage_cmd = current_voltage + step_fine
            else:
                voltage_cmd = current_voltage - step_coarse
        else:
            if abs((current_voltage + step_fine) - self._prev_voltage) > 1e-9:
                voltage_cmd = current_voltage - step_fine
            else:
                voltage_cmd = current_voltage + step_coarse

        return voltage_cmd

    def _compute_pid(self, current_wn, current_voltage):
        """Compute next voltage using PID controller."""
        adjustment = self.pid.compute(self.target_wn, current_wn, self.poll_interval)
        return current_voltage + adjustment

    def _clamp_voltage(self, voltage):
        """Clamp voltage to configured limits."""
        return max(self.voltage_min, min(self.voltage_max, voltage))

    def _control_loop(self):
        """
        Main control loop dispatching to PID or bang-bang strategy.
        """
        print(f"[LaserController] Starting control loop for Target {self.target_wn} "
              f"(mode={self.controller_mode})")

        wn = self.get_wavenumber()
        voltage = self.device.qPOS(self.axis)[self.axis]
        self._prev_voltage = voltage

        stable_samples = 0
        REQUIRED_STABLE_SAMPLES = self.required_stable_samples

        while not self.stop_event.is_set():
            wn = self.get_wavenumber()
            voltage = self.device.qPOS(self.axis)[self.axis]

            # Check stability
            if abs(wn - self.target_wn) < self.tolerance:
                stable_samples += 1
                print(f"[LaserController] Within tolerance.. stabilizing "
                      f"({stable_samples}/{REQUIRED_STABLE_SAMPLES})")
                if stable_samples >= REQUIRED_STABLE_SAMPLES:
                    break

                time.sleep(0.5)
                continue
            else:
                stable_samples = 0

            # Dispatch to controller strategy
            if self.controller_mode == "pid":
                voltage_cmd = self._compute_pid(wn, voltage)
            else:
                voltage_cmd = self._compute_bangbang(wn, voltage)

            # Clamp voltage to physical limits
            clamped = self._clamp_voltage(voltage_cmd)
            if clamped != voltage_cmd:
                self.voltage_limited = True
            voltage_cmd = clamped

            self.device.MOV(self.axis, voltage_cmd)

            if self.stop_event.wait(self.poll_interval):
                break

            self._prev_voltage = voltage
            print(f"[LaserController] V: {voltage:.5f}, WN: {wn:.4f} "
                  f"(Target: {self.target_wn})")

        print(f"[LaserController] Target reached or stopped. Final WN: {wn:.4f}")
        self.is_moving = False

if __name__ == "__main__":
    pass
