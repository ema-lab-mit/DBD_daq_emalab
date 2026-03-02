class PIDController:
    """
    PID controller with anti-windup, derivative-on-measurement, and EMA-filtered derivative.
    """

    def __init__(self, kp=0.0, ki=0.0, kd=0.0, output_min=None, output_max=None,
                 d_filter_coeff=0.1):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_min = output_min
        self.output_max = output_max
        self.d_filter_coeff = d_filter_coeff

        self._integral = 0.0
        self._prev_measurement = None
        self._filtered_derivative = 0.0

    def compute(self, setpoint, measurement, dt):
        """
        Compute PID output (a voltage adjustment).

        Args:
            setpoint: Target value (wavenumber).
            measurement: Current measured value (wavenumber).
            dt: Time step in seconds.

        Returns:
            Clamped PID output (voltage adjustment).
        """
        if dt <= 0:
            return 0.0

        error = setpoint - measurement

        # --- Proportional ---
        p_term = self.kp * error

        # --- Integral with anti-windup ---
        # Only integrate if output is not saturated in the same direction as error
        raw_output = p_term + self.ki * self._integral
        saturated_high = self.output_max is not None and raw_output >= self.output_max
        saturated_low = self.output_min is not None and raw_output <= self.output_min
        if not ((saturated_high and error > 0) or (saturated_low and error < 0)):
            self._integral += error * dt

        i_term = self.ki * self._integral

        # --- Derivative on measurement (avoids derivative kick) ---
        if self._prev_measurement is not None:
            raw_derivative = -(measurement - self._prev_measurement) / dt
            # EMA filter on derivative
            alpha = self.d_filter_coeff
            self._filtered_derivative = (alpha * raw_derivative +
                                         (1.0 - alpha) * self._filtered_derivative)
        self._prev_measurement = measurement

        d_term = self.kd * self._filtered_derivative

        # --- Sum and clamp ---
        output = p_term + i_term + d_term
        return self._clamp(output)

    def reset(self):
        """Reset all internal state."""
        self._integral = 0.0
        self._prev_measurement = None
        self._filtered_derivative = 0.0

    def update_gains(self, kp=None, ki=None, kd=None, d_filter_coeff=None):
        """Update gains without resetting state."""
        if kp is not None:
            self.kp = kp
        if ki is not None:
            self.ki = ki
        if kd is not None:
            self.kd = kd
        if d_filter_coeff is not None:
            self.d_filter_coeff = d_filter_coeff

    def _clamp(self, value):
        if self.output_min is not None and value < self.output_min:
            return self.output_min
        if self.output_max is not None and value > self.output_max:
            return self.output_max
        return value
