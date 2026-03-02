import time
from collections import deque
from PyQt5.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtWidgets import QFileDialog, QMessageBox
import csv
from src.utils.settings_manager import SettingsManager
from src.gui.widgets.params_widget import ParamsWidget
from src.gui.widgets.actions_widget import ActionsWidget
from src.gui.widgets.status_widget import StatusWidget
from src.gui.widgets.plot_widget import PlotWidget
from src.gui.widgets.plot_options_widget import PlotOptionsWidget
from src.gui.widgets.laser_control_dialog import LaserControlDialog
from src.gui.widgets.collapsible_box import CollapsibleBox

class MainWindow(QMainWindow):
    def __init__(self, daq_system):
        super().__init__()
        self.daq = daq_system
        if hasattr(self.daq, 'config') and self.daq.config:
            self.settings_manager = SettingsManager() # We still need the manager to save
            self.settings_manager.settings = self.daq.config
        else:
            self.settings_manager = SettingsManager()

        self.scan_settings = self.settings_manager.get_section("scan_settings")

        self.setWindowTitle("DAQ Scanner Control (PyQt5) - Modular")
        self.resize(1200, 800)

        self._init_ui()
        self._init_ui()
        self._init_logic()
        self.update_counter = 0

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter)

        self.controls_widget = QWidget()
        self.controls_layout = QVBoxLayout(self.controls_widget)
        self.controls_layout.setContentsMargins(10, 10, 10, 10)
        self.controls_layout.setSpacing(15)

        self.params_widget = ParamsWidget(settings_config=self.scan_settings)
        self.actions_widget = ActionsWidget()
        self.plot_options_widget = PlotOptionsWidget()
        self.status_widget = StatusWidget()

        self.options_container = CollapsibleBox("Plot Options")
        self.options_container.set_content_widget(self.plot_options_widget)

        self.controls_layout.addWidget(self.params_widget)
        self.controls_layout.addWidget(self.actions_widget)
        self.controls_layout.addWidget(self.options_container)
        self.controls_layout.addWidget(self.status_widget)

        # Offline Mode Button
        from PyQt5.QtWidgets import QPushButton
        self.btn_offline = QPushButton("Open Offline Viewer")
        self.btn_offline.clicked.connect(self.open_offline_mode)
        self.controls_layout.addWidget(self.btn_offline)

        self.controls_layout.addStretch()

        splitter.addWidget(self.controls_widget)

        self.plot_widget = PlotWidget()
        splitter.addWidget(self.plot_widget)
        splitter.setStretchFactor(1, 4) # Plots take more space

    def _init_logic(self):
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_gui)

        gui_settings = self.settings_manager.get_section("gui_settings")
        refresh_interval = gui_settings.get("refresh_rate_ms", 100)
        self.timer.start(refresh_interval)

        self.start_time = time.time()
        self.time_history = deque(maxlen=200)
        self.rate_history = deque(maxlen=200)
        self.wn_history = deque(maxlen=200)
        self.target_wn_history = deque(maxlen=200)
        self.volt_history = deque(maxlen=200)

        self.actions_widget.start_requested.connect(self.on_start)
        self.actions_widget.pause_requested.connect(self.on_pause)
        self.actions_widget.stop_requested.connect(self.on_stop)
        self.actions_widget.reset_requested.connect(self.on_reset)
        self.actions_widget.export_requested.connect(self.on_export)

        self.params_widget.settings_requested.connect(self.on_settings)

        self.plot_options_widget.options_changed.connect(self.plot_widget.set_active_plots)
        self.plot_options_widget.auto_scale_toggled.connect(self.plot_widget.set_auto_scale)
        self.plot_options_widget.theme_toggled.connect(self.plot_widget.set_theme)
        self.plot_options_widget.tof_online_toggled.connect(self._on_tof_online_toggled)
        self.plot_options_widget.tof_bins_changed.connect(self.plot_widget.set_tof_bins)

        self.plot_widget.set_active_plots(self.plot_options_widget.get_options())
        self.plot_widget.set_auto_scale(self.plot_options_widget.chk_auto_scale.isChecked())

        self.was_running = False
        self._voltage_limit_warned = False

    def _on_tof_online_toggled(self, enabled):
        self.daq.tof_online_mode = enabled
        self.daq.tof_buffer.clear()

    def on_start(self):
        status = self.daq.scanner.get_status()
        if status['is_running']:
            QMessageBox.warning(self, "Scan Running",
                                "A scan is currently running, either pause it, or stop it.")
            return

        self.on_reset()

        params = self.params_widget.get_params()

        try:
            self.daq.start_scan(
                params['start_wn'],
                params['end_wn'],
                params['step_size'],
                params['stop_mode'],
                params['stop_val'],
                params['loops']
            )
            self.active_display_params = params['display']

            info_text = "<b>Current Scan Parameters:</b><br>"
            info_text += "<br>".join([f"• {k}: {v}" for k, v in self.active_display_params.items()])
            self.current_info_text = info_text

            self.scan_settings.update({
                'start_wn': params['start_wn'],
                'end_wn': params['end_wn'],
                'step_size': params['step_size'],
                'stop_mode': params['stop_mode'],
                'stop_val': params['stop_val'],
                'loops': params['loops']
            })
            self.settings_manager.save_settings()

        except Exception as e:
            print(f"Error starting scan: {e}")

    def on_stop(self):
        self.daq.scanner.stop(wait=True)

        if self.daq.saver:
            self.daq.saver.stop()
            self.daq.saver = None

    def on_pause(self):
        status = self.daq.scanner.get_status()
        if status['is_paused']:
            self.daq.scanner.resume()
        else:
            self.daq.scanner.pause()

    def on_reset(self):
        status = self.daq.scanner.get_status()

        if status['is_running']:
             self.daq.scanner.stop(wait=True)

        self.daq.scanner.reset()

        self.time_history.clear()
        self.rate_history.clear()
        self.wn_history.clear()
        self.target_wn_history.clear()
        self.volt_history.clear()

        self.start_time = time.time()
        self.daq.event_timestamps.clear()

        self.plot_widget.rebuild_plots()

        if hasattr(self, 'current_info_text'):
            del self.current_info_text

    def on_export(self):


        scan_data = self.daq.scanner.scan_progress
        if not scan_data:
            QMessageBox.warning(self, "Warning", "No scan data.")
            return

        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "", "CSV Files (*.csv)")
        if path:
            try:
                with open(path, 'w', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(["Wavenumber_cm-1", "Rate_events_per_bunch", "Total_Events", "Total_Bunches"])
                    writer.writerows(scan_data)
                QMessageBox.information(self, "Success", f"Exported {len(scan_data)} points.")
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def on_settings(self):

        control_section = self.settings_manager.get_section("control_settings")
        current_laser_settings = control_section.get("laser", {})

        dialog = LaserControlDialog(current_laser_settings, self)
        if dialog.exec_():
            new_settings = dialog.get_settings()

            self.daq.update_laser_settings(new_settings)

            control_section['laser'] = new_settings
            self.settings_manager.settings['control_settings'] = control_section
            self.settings_manager.save_settings()

            self.status_widget.update_status(self.daq.scanner.get_status(), "Laser Settings Updated.")

    def update_gui(self):
        current_time = time.time() - self.start_time
        rate = self.daq.get_instant_rate()

        status = self.daq.scanner.get_status()
        target_wn = status['target_wn']
        measured_wn = status['measured_wn']

        # Use cached voltage to avoid blocking GUI
        # volt_val = self.daq.multimeter.get_voltage()
        volt_val = self.daq.get_latest_voltage()

        self.time_history.append(current_time)
        self.rate_history.append(rate)
        self.wn_history.append(measured_wn)
        self.target_wn_history.append(target_wn)
        self.volt_history.append(volt_val)

        if hasattr(self, 'current_info_text') and status['is_running']:
            info_text = self.current_info_text
        else:
            params = self.params_widget.get_params()
            display_params = params['display']
            info_text = "<b>Pending Scan Parameters:</b><br>"
            info_text += "<br>".join([f"• {k}: {v}" for k, v in display_params.items()])

        self.status_widget.update_status(status, info_text)

        self.actions_widget.update_state(status['is_running'], status['is_paused'])

        self.params_widget.set_enabled(not status['is_running'])

        # Throttling ToF Updates
        self.update_counter += 1
        tof_data = None

        if self.update_counter % 10 == 0:
             tof_data = self.daq.tof_buffer if hasattr(self.daq, 'tof_buffer') else []

        history = {
            'times': list(self.time_history),
            'rate': list(self.rate_history),
            'wn': list(self.wn_history),
            'target_wn': list(self.target_wn_history),
            'volt': list(self.volt_history),
            'scan_data': self.daq.scanner.scan_progress,
            'tof_buffer': tof_data
        }
        history = {
            'times': list(self.time_history),
            'rate': list(self.rate_history),
            'wn': list(self.wn_history),
            'target_wn': list(self.target_wn_history),
            'volt': list(self.volt_history),
            'scan_data': self.daq.scanner.scan_progress,
            'tof_buffer': tof_data
        }
        self.plot_widget.update_plots(history)

        # Detect Scan Completion
        if self.was_running and not status['is_running']:
            if not status['is_stopping']: # Natural Completion
                msg = "Scan Complete Successfully!"
                if self.daq.last_scan_filename:
                   msg += f"\n\nData saved to:\n{self.daq.last_scan_filename}"

                QMessageBox.information(self, "Scan Finished", msg)

        self.was_running = status['is_running']

        # Voltage limit popup
        if hasattr(self.daq, 'laser') and self.daq.laser is not None:
            if self.daq.laser.voltage_limited and not self._voltage_limit_warned:
                QMessageBox.warning(
                    self, "Voltage Limit Reached",
                    "The laser controller has hit a voltage limit "
                    f"({self.daq.laser.voltage_min}V - {self.daq.laser.voltage_max}V).\n\n"
                    "The system may not be able to reach the requested wavenumber."
                )
                self._voltage_limit_warned = True
            elif not self.daq.laser.voltage_limited:
                self._voltage_limit_warned = False

    def closeEvent(self, event):
        params = self.params_widget.get_params()
        self.scan_settings.update({
            'start_wn': params['start_wn'],
            'end_wn': params['end_wn'],
            'step_size': params['step_size'],
            'stop_mode': params['stop_mode'],
            'stop_val': params['stop_val'],
            'loops': params['loops']
        })
        self.settings_manager.save_settings()

        self.daq.stop()
        event.accept()

    def open_offline_mode(self):
        from src.gui.offline_window import OfflineWindow
        self.offline_window = OfflineWindow()
        self.offline_window.show()

