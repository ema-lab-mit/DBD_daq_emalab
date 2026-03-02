import sys
import os
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.control.pid import PIDController
from src.control.laser_controller import LaserController
from src.simulation.hardware_mocks import MockPIGCSDevice, MockEpicsClient


# ============================================================
# Unit Tests: PIDController
# ============================================================

class TestPIDController:
    def test_p_only_proportional(self):
        pid = PIDController(kp=1.0, ki=0.0, kd=0.0)
        output = pid.compute(setpoint=10.0, measurement=8.0, dt=0.1)
        assert output == pytest.approx(2.0)

    def test_p_only_negative_error(self):
        pid = PIDController(kp=1.0, ki=0.0, kd=0.0)
        output = pid.compute(setpoint=5.0, measurement=7.0, dt=0.1)
        assert output == pytest.approx(-2.0)

    def test_integral_accumulation(self):
        pid = PIDController(kp=0.0, ki=1.0, kd=0.0)
        dt = 0.1
        # First call: integral = error * dt = 2.0 * 0.1 = 0.2
        out1 = pid.compute(setpoint=10.0, measurement=8.0, dt=dt)
        assert out1 == pytest.approx(0.2)
        # Second call: integral = 0.2 + 0.2 = 0.4
        out2 = pid.compute(setpoint=10.0, measurement=8.0, dt=dt)
        assert out2 == pytest.approx(0.4)

    def test_anti_windup_prevents_integration_when_saturated(self):
        pid = PIDController(kp=1.0, ki=1.0, kd=0.0, output_max=1.5)
        dt = 0.1
        # Error = 2.0, P = 2.0 already > output_max
        # Anti-windup should prevent integral from growing when saturated high + error > 0
        for _ in range(20):
            out = pid.compute(setpoint=10.0, measurement=8.0, dt=dt)
        # Output should be clamped, integral should not have grown unbounded
        assert out == pytest.approx(1.5)
        # Integral should be small (didn't wind up)
        assert pid._integral < 1.0

    def test_output_clamping_max(self):
        pid = PIDController(kp=10.0, ki=0.0, kd=0.0, output_max=5.0)
        out = pid.compute(setpoint=10.0, measurement=0.0, dt=0.1)
        assert out == pytest.approx(5.0)

    def test_output_clamping_min(self):
        pid = PIDController(kp=10.0, ki=0.0, kd=0.0, output_min=-5.0)
        out = pid.compute(setpoint=0.0, measurement=10.0, dt=0.1)
        assert out == pytest.approx(-5.0)

    def test_derivative_on_measurement_no_spike_on_setpoint_change(self):
        """D term uses measurement derivative, so setpoint change should NOT cause a spike."""
        pid = PIDController(kp=0.0, ki=0.0, kd=1.0, d_filter_coeff=1.0)
        dt = 0.1
        # Steady measurement at 5.0
        pid.compute(setpoint=5.0, measurement=5.0, dt=dt)
        pid.compute(setpoint=5.0, measurement=5.0, dt=dt)
        # Now change setpoint drastically - measurement stays the same
        out = pid.compute(setpoint=50.0, measurement=5.0, dt=dt)
        # D term should be ~0 because measurement didn't change
        assert abs(out) < 0.01

    def test_derivative_responds_to_measurement_change(self):
        pid = PIDController(kp=0.0, ki=0.0, kd=1.0, d_filter_coeff=1.0)
        dt = 0.1
        pid.compute(setpoint=10.0, measurement=5.0, dt=dt)
        # Measurement jumps up — derivative should be negative (opposing the change)
        out = pid.compute(setpoint=10.0, measurement=6.0, dt=dt)
        # d_term = kd * (-(6-5)/0.1) = 1.0 * (-10) = -10
        assert out == pytest.approx(-10.0)

    def test_reset_clears_state(self):
        pid = PIDController(kp=0.0, ki=1.0, kd=0.0)
        pid.compute(setpoint=10.0, measurement=8.0, dt=0.1)
        pid.compute(setpoint=10.0, measurement=8.0, dt=0.1)
        assert pid._integral != 0.0
        pid.reset()
        assert pid._integral == 0.0
        assert pid._prev_measurement is None
        assert pid._filtered_derivative == 0.0

    def test_update_gains(self):
        pid = PIDController(kp=1.0, ki=2.0, kd=3.0)
        pid.update_gains(kp=10.0, kd=30.0)
        assert pid.kp == 10.0
        assert pid.ki == 2.0  # unchanged
        assert pid.kd == 30.0

    def test_zero_dt_returns_zero(self):
        pid = PIDController(kp=1.0, ki=1.0, kd=1.0)
        out = pid.compute(setpoint=10.0, measurement=5.0, dt=0.0)
        assert out == 0.0


# ============================================================
# Integration Tests: LaserController
# ============================================================

def _make_controller(mode="pid", voltage_min=0.0, voltage_max=5.0,
                     slope=5.0, offset=16666.0, noise=0.0001,
                     kp=0.02, ki=0.005, kd=0.0, tolerance=0.007,
                     poll_interval=0.01, move_speed=500.0):
    """Helper to create a LaserController with mock hardware."""
    pi = MockPIGCSDevice(initialization_params={"move_speed": move_speed})
    pi.ConnectRS232(1, 9600)
    pi.SVO(1, True)

    epics = MockEpicsClient(pi, initialization_params={
        "slope": slope,
        "offset": offset,
        "noise_level": noise,
    })

    config = {
        "controller_mode": mode,
        "tolerance": tolerance,
        "poll_interval": poll_interval,
        "required_stable_samples": 3,
        "voltage_min": voltage_min,
        "voltage_max": voltage_max,
        "step_fine": 0.001,
        "step_coarse": 0.005,
        "pid": {
            "kp": kp,
            "ki": ki,
            "kd": kd,
            "d_filter_coeff": 0.1,
        }
    }
    ctrl = LaserController(pi, epics, axis=1, config=config)
    return ctrl, pi, epics


class TestLaserControllerPID:
    def test_pid_reaches_target(self):
        """PID controller should reach target wavenumber within tolerance."""
        ctrl, pi, _ = _make_controller(mode="pid", kp=0.05, ki=0.01, noise=0.0001)
        # Start at voltage 0 → WN = 16666.0
        target = 16666.1  # small step: needs voltage ~ 0.02
        ctrl.set_wavenumber(target)

        deadline = time.time() + 10.0
        while time.time() < deadline:
            if ctrl.is_stable():
                break
            time.sleep(0.05)

        ctrl.stop()
        final_wn = ctrl.get_wavenumber()
        assert abs(final_wn - target) < ctrl.tolerance, (
            f"PID did not converge: final={final_wn:.4f}, target={target}")

    def test_bangbang_still_works(self):
        """Bang-bang controller runs without errors and dispatches correctly."""
        ctrl, pi, _ = _make_controller(mode="bangbang", noise=0.0001)
        pi.target_position[1] = 2.5
        pi.position[1] = 2.5
        target = 16666.0 + 2.5 * 5.0 + 0.002  # very small step
        ctrl.set_wavenumber(target)

        # Let it run a few iterations to confirm no crash
        time.sleep(0.5)
        ctrl.stop()

        # Verify it ran in bangbang mode and voltage stayed within limits
        voltage = pi.qPOS(1)[1]
        assert ctrl.voltage_min <= voltage <= ctrl.voltage_max + 0.01
        assert ctrl.controller_mode == "bangbang"

    def test_bangbang_convergence_negative_slope(self):
        """Bang-bang converges with negative slope (its native assumption)."""
        ctrl, pi, epics = _make_controller(
            mode="bangbang", noise=0.0001, slope=-5.0, offset=16680.0)
        # slope=-5: voltage up → WN down. Start at V=2.5 → WN=16667.5
        pi.target_position[1] = 2.5
        pi.position[1] = 2.5
        starting_wn = 16680.0 + 2.5 * (-5.0)  # 16667.5
        target = starting_wn - 0.02  # need to increase voltage slightly

        ctrl.set_wavenumber(target)

        deadline = time.time() + 10.0
        while time.time() < deadline:
            if ctrl.is_stable():
                break
            time.sleep(0.05)

        ctrl.stop()
        final_wn = ctrl.get_wavenumber()
        assert abs(final_wn - target) < ctrl.tolerance, (
            f"BangBang did not converge: final={final_wn:.4f}, target={target}")

    def test_voltage_never_exceeds_limits(self):
        """Voltage should stay within [voltage_min, voltage_max] even with large error."""
        ctrl, pi, _ = _make_controller(
            mode="pid", kp=1.0, ki=0.5, voltage_min=0.0, voltage_max=2.0, noise=0.0001)
        # Large target that would require voltage > 2.0
        target = 16680.0  # Would need voltage = (16680 - 16666) / 5 = 2.8
        ctrl.set_wavenumber(target)

        time.sleep(1.0)
        ctrl.stop()

        # Check that physical position (voltage) never exceeded limits
        voltage = pi.qPOS(1)[1]
        assert voltage <= 2.0 + 0.01, f"Voltage exceeded max: {voltage}"
        assert voltage >= -0.01, f"Voltage below min: {voltage}"

    def test_setpoint_change_resets_pid(self):
        """Changing target should reset PID integral to prevent carryover."""
        ctrl, pi, _ = _make_controller(mode="pid", kp=0.05, ki=0.01, noise=0.0001)
        ctrl.set_wavenumber(16666.1)
        time.sleep(0.5)
        # Change setpoint — PID integral should be reset
        ctrl.set_wavenumber(16666.05)
        assert ctrl.pid._integral == 0.0
        time.sleep(0.5)
        ctrl.stop()

    def test_runtime_config_update(self):
        """update_config should change controller parameters."""
        ctrl, _, _ = _make_controller(mode="pid")
        ctrl.update_config({
            "controller_mode": "bangbang",
            "voltage_max": 3.0,
            "pid": {"kp": 0.1},
        })
        assert ctrl.controller_mode == "bangbang"
        assert ctrl.voltage_max == 3.0
        assert ctrl.pid.kp == 0.1


class TestVoltageLimitedFlag:
    def test_voltage_limited_set_when_clamped(self):
        """voltage_limited should be True when PID demands voltage beyond limits."""
        ctrl, pi, _ = _make_controller(
            mode="pid", kp=1.0, ki=0.5, voltage_min=0.0, voltage_max=2.0, noise=0.0001)
        # Target requires voltage well beyond max (2.0)
        target = 16680.0  # needs voltage ~2.8
        ctrl.set_wavenumber(target)

        time.sleep(1.0)
        ctrl.stop()

        assert ctrl.voltage_limited is True

    def test_voltage_limited_false_within_range(self):
        """voltage_limited should remain False when voltage stays in range."""
        ctrl, pi, _ = _make_controller(
            mode="pid", kp=0.05, ki=0.01, noise=0.0001)
        # Small target easily reachable within 0-5V
        target = 16666.05
        ctrl.set_wavenumber(target)

        deadline = time.time() + 10.0
        while time.time() < deadline:
            if ctrl.is_stable():
                break
            time.sleep(0.05)

        ctrl.stop()
        assert ctrl.voltage_limited is False

    def test_voltage_limited_resets_on_new_setpoint(self):
        """voltage_limited should reset when a new setpoint is commanded."""
        ctrl, pi, _ = _make_controller(
            mode="pid", kp=1.0, ki=0.5, voltage_min=0.0, voltage_max=2.0, noise=0.0001)
        ctrl.set_wavenumber(16680.0)
        time.sleep(0.5)
        ctrl.stop()
        assert ctrl.voltage_limited is True

        # New setpoint should reset the flag
        ctrl.set_wavenumber(16666.05)
        assert ctrl.voltage_limited is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
