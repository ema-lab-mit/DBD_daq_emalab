from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QDoubleSpinBox,
                             QDialogButtonBox, QLabel, QComboBox, QGroupBox)
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QSpinBox

class LaserControlDialog(QDialog):
    def __init__(self, current_settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Laser Control Settings")
        self.resize(350, 400)
        self.settings = current_settings.copy()

        self.layout = QVBoxLayout(self)
        self.form_layout = QFormLayout()

        # Controller mode selector
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["PID", "Bang-Bang"])
        current_mode = self.settings.get("controller_mode", "pid")
        self.mode_combo.setCurrentIndex(0 if current_mode == "pid" else 1)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.form_layout.addRow("Controller Mode:", self.mode_combo)

        # Tolerance
        self.tolerance_spin = QDoubleSpinBox()
        self.tolerance_spin.setRange(0.0001, 1.0)
        self.tolerance_spin.setDecimals(4)
        self.tolerance_spin.setValue(self.settings.get("tolerance", 0.01))
        self.form_layout.addRow("Tolerance (cm\u207b\u00b9):", self.tolerance_spin)

        # Poll Interval
        self.poll_spin = QDoubleSpinBox()
        self.poll_spin.setRange(0.001, 5.0)
        self.poll_spin.setDecimals(3)
        self.poll_spin.setSingleStep(0.01)
        self.poll_spin.setValue(self.settings.get("poll_interval", 0.5))
        self.form_layout.addRow("Poll Interval (s):", self.poll_spin)

        # Stable Samples
        self.stable_samples_spin = QSpinBox()
        self.stable_samples_spin.setRange(1, 20)
        self.stable_samples_spin.setValue(int(self.settings.get("required_stable_samples", 4)))
        self.form_layout.addRow("Stable Samples:", self.stable_samples_spin)

        # Wavemeter Channel
        self.channel_spin = QSpinBox()
        self.channel_spin.setRange(1, 4)
        self.channel_spin.setValue(int(self.settings.get("wavechannel", 3)))
        self.form_layout.addRow("Wavemeter Channel:", self.channel_spin)

        self.layout.addLayout(self.form_layout)

        # --- Voltage Limits ---
        self.voltage_group = QGroupBox("Voltage Limits")
        voltage_layout = QFormLayout()

        self.voltage_min_spin = QDoubleSpinBox()
        self.voltage_min_spin.setRange(0.0, 10.0)
        self.voltage_min_spin.setDecimals(2)
        self.voltage_min_spin.setSingleStep(0.1)
        self.voltage_min_spin.setValue(self.settings.get("voltage_min", 0.0))
        voltage_layout.addRow("Min Voltage (V):", self.voltage_min_spin)

        self.voltage_max_spin = QDoubleSpinBox()
        self.voltage_max_spin.setRange(0.0, 10.0)
        self.voltage_max_spin.setDecimals(2)
        self.voltage_max_spin.setSingleStep(0.1)
        self.voltage_max_spin.setValue(self.settings.get("voltage_max", 5.0))
        voltage_layout.addRow("Max Voltage (V):", self.voltage_max_spin)

        self.voltage_group.setLayout(voltage_layout)
        self.layout.addWidget(self.voltage_group)

        # --- PID Gains ---
        self.pid_group = QGroupBox("PID Gains")
        pid_layout = QFormLayout()
        pid_config = self.settings.get("pid", {})

        self.kp_spin = QDoubleSpinBox()
        self.kp_spin.setRange(0.0, 10.0)
        self.kp_spin.setDecimals(4)
        self.kp_spin.setSingleStep(0.001)
        self.kp_spin.setValue(pid_config.get("kp", 0.02))
        pid_layout.addRow("Kp:", self.kp_spin)

        self.ki_spin = QDoubleSpinBox()
        self.ki_spin.setRange(0.0, 10.0)
        self.ki_spin.setDecimals(4)
        self.ki_spin.setSingleStep(0.001)
        self.ki_spin.setValue(pid_config.get("ki", 0.005))
        pid_layout.addRow("Ki:", self.ki_spin)

        self.kd_spin = QDoubleSpinBox()
        self.kd_spin.setRange(0.0, 10.0)
        self.kd_spin.setDecimals(4)
        self.kd_spin.setSingleStep(0.001)
        self.kd_spin.setValue(pid_config.get("kd", 0.0))
        pid_layout.addRow("Kd:", self.kd_spin)

        self.d_filter_spin = QDoubleSpinBox()
        self.d_filter_spin.setRange(0.01, 1.0)
        self.d_filter_spin.setDecimals(3)
        self.d_filter_spin.setSingleStep(0.01)
        self.d_filter_spin.setValue(pid_config.get("d_filter_coeff", 0.1))
        pid_layout.addRow("D Filter Coeff:", self.d_filter_spin)

        self.pid_group.setLayout(pid_layout)
        self.layout.addWidget(self.pid_group)

        # --- Bang-Bang Steps ---
        self.bangbang_group = QGroupBox("Bang-Bang Steps")
        bb_layout = QFormLayout()

        self.fine_step_spin = QDoubleSpinBox()
        self.fine_step_spin.setRange(0.00001, 1.0)
        self.fine_step_spin.setDecimals(5)
        self.fine_step_spin.setValue(self.settings.get("step_fine", 0.0001))
        bb_layout.addRow("Fine Step (V):", self.fine_step_spin)

        self.coarse_step_spin = QDoubleSpinBox()
        self.coarse_step_spin.setRange(0.001, 10.0)
        self.coarse_step_spin.setDecimals(3)
        self.coarse_step_spin.setValue(self.settings.get("step_coarse", 0.05))
        bb_layout.addRow("Coarse Step (V):", self.coarse_step_spin)

        self.bangbang_group.setLayout(bb_layout)
        self.layout.addWidget(self.bangbang_group)

        # Buttons
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)

        # Set initial visibility
        self._on_mode_changed(self.mode_combo.currentIndex())

    def _on_mode_changed(self, index):
        is_pid = index == 0
        self.pid_group.setVisible(is_pid)
        self.bangbang_group.setVisible(not is_pid)

    def get_settings(self):
        mode = "pid" if self.mode_combo.currentIndex() == 0 else "bangbang"
        return {
            "controller_mode": mode,
            "tolerance": self.tolerance_spin.value(),
            "poll_interval": self.poll_spin.value(),
            "required_stable_samples": self.stable_samples_spin.value(),
            "wavechannel": self.channel_spin.value(),
            "voltage_min": self.voltage_min_spin.value(),
            "voltage_max": self.voltage_max_spin.value(),
            "step_fine": self.fine_step_spin.value(),
            "step_coarse": self.coarse_step_spin.value(),
            "pid": {
                "kp": self.kp_spin.value(),
                "ki": self.ki_spin.value(),
                "kd": self.kd_spin.value(),
                "d_filter_coeff": self.d_filter_spin.value(),
            }
        }
