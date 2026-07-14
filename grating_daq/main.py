"""Grating-laser DAQ GUI.

Two modes, selected before starting a run:

  SURVEY  -- continuous back-and-forth glide between two wavenumbers; spectrum
             built as gated counts / dwell time per bin (bird's-eye view).
  PARK    -- deadband hold on one wavenumber for lifetime measurements; counts
             vs time + ToF histogram, with NRI-delay markers logged.

All events are recorded ungated to grating_daq/data/run_<stamp>_<mode>/;
the draggable ToF gate only selects what the live displays count.

Run from the repo root:  python grating_daq/main.py
Requires: grating server (laser lab, :8000), wavemeter IOC, EPICS_CA_ADDR_LIST.
"""

import os
import sys
import time

import numpy as np
from PyQt5 import QtCore, QtWidgets
import pyqtgraph as pg

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from grating_daq.engine import (DEFAULTS, EventCollector, ParkController,
                                RunRecorder, SharedState, SurveySweeper,
                                WnPoller, load_grating_cfg)
from src.devices.laser import GratingDevice
from src.devices.tagger import Tagger

pg.setConfigOptions(antialias=False, background="k", foreground="w")


class GratingDAQWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Grating Ti:sa DAQ -- survey & park")
        self.resize(1400, 850)

        self.cfg = load_grating_cfg()
        self.laser = None
        self.tagger = None
        self.state = None
        self.recorder = None
        self.threads = []
        self.park = None

        self._build_ui()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(500)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)

        # ---- control panel ----
        panel = QtWidgets.QVBoxLayout()
        root.addLayout(panel, 0)

        self.mode_survey = QtWidgets.QRadioButton("Survey (continuous sweep)")
        self.mode_park = QtWidgets.QRadioButton("Park (hold on resonance)")
        self.mode_survey.setChecked(True)
        panel.addWidget(self.mode_survey)
        panel.addWidget(self.mode_park)

        form = QtWidgets.QFormLayout()
        panel.addLayout(form)

        def dspin(lo, hi, val, dec=5, step=0.01):
            s = QtWidgets.QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setDecimals(dec)
            s.setSingleStep(step)
            s.setValue(val)
            return s

        self.in_wn_lo = dspin(10000, 20000, 12624.8)
        self.in_wn_hi = dspin(10000, 20000, 12625.6)
        self.in_bin = dspin(0.001, 1.0, 0.01, dec=4, step=0.005)
        self.in_park = dspin(10000, 20000, 12625.20)
        self.in_deadband = dspin(0.001, 1.0, self.cfg["park_deadband"],
                                 dec=4, step=0.005)
        form.addRow("Survey wn low", self.in_wn_lo)
        form.addRow("Survey wn high", self.in_wn_hi)
        form.addRow("Spectrum bin (cm^-1)", self.in_bin)
        form.addRow("Park target wn", self.in_park)
        form.addRow("Park deadband (cm^-1)", self.in_deadband)

        self.btn_start = QtWidgets.QPushButton("START")
        self.btn_stop = QtWidgets.QPushButton("STOP")
        self.btn_stop.setEnabled(False)
        panel.addWidget(self.btn_start)
        panel.addWidget(self.btn_stop)
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)

        # Park-mode target retune + NRI marker
        panel.addSpacing(10)
        self.btn_retune = QtWidgets.QPushButton("Update park target")
        self.btn_retune.setEnabled(False)
        self.btn_retune.clicked.connect(self._update_park_target)
        panel.addWidget(self.btn_retune)

        nri_row = QtWidgets.QHBoxLayout()
        self.in_nri = QtWidgets.QLineEdit()
        self.in_nri.setPlaceholderText("NRI delay (any units)")
        self.btn_nri = QtWidgets.QPushButton("Log NRI marker")
        self.btn_nri.setEnabled(False)
        self.btn_nri.clicked.connect(self._log_nri)
        nri_row.addWidget(self.in_nri)
        nri_row.addWidget(self.btn_nri)
        panel.addLayout(nri_row)

        panel.addSpacing(10)
        self.lbl_status = QtWidgets.QLabel("idle")
        self.lbl_wn = QtWidgets.QLabel("wn: ---")
        self.lbl_events = QtWidgets.QLabel("events: 0 (gated 0)")
        self.lbl_rate = QtWidgets.QLabel("gated rate: 0.0 /s")
        self.lbl_gate = QtWidgets.QLabel("gate: ---")
        self.lbl_dir = QtWidgets.QLabel("sweep dir: ---")
        self.lbl_folder = QtWidgets.QLabel("data: ---")
        self.lbl_folder.setWordWrap(True)
        for w in (self.lbl_status, self.lbl_wn, self.lbl_events, self.lbl_rate,
                  self.lbl_gate, self.lbl_dir, self.lbl_folder):
            panel.addWidget(w)
        panel.addStretch(1)

        # ---- plots ----
        grid = pg.GraphicsLayoutWidget()
        root.addWidget(grid, 1)

        self.p_tof = grid.addPlot(title="ToF spectrum (all events) + gate")
        self.p_tof.setLabel("bottom", "ToF", units="s")
        self.tof_curve = self.p_tof.plot(stepMode="center",
                                         fillLevel=0, brush=(80, 80, 200, 120))
        self.gate_region = pg.LinearRegionItem(values=(2e-6, 8e-6))
        self.p_tof.addItem(self.gate_region)
        self.gate_region.sigRegionChanged.connect(self._gate_moved)

        self.p_spec = grid.addPlot(
            title="Wn spectrum: gated counts / dwell time")
        self.p_spec.setLabel("bottom", "wavenumber", units="cm-1")
        self.p_spec.setLabel("left", "rate", units="Hz")
        self.spec_curve = self.p_spec.plot(stepMode="center",
                                           fillLevel=0, brush=(80, 200, 80, 120))

        grid.nextRow()
        self.p_wn = grid.addPlot(title="wavenumber vs time")
        self.p_wn.setLabel("bottom", "t", units="s")
        self.wn_curve = self.p_wn.plot(pen=pg.mkPen("y", width=1))

        self.p_rate = grid.addPlot(title="gated rate vs time (lifetime view)")
        self.p_rate.setLabel("bottom", "t", units="s")
        self.p_rate.setLabel("left", "rate", units="Hz")
        self.rate_curve = self.p_rate.plot(pen=pg.mkPen("c", width=1))

    # ----------------------------------------------------------- run control
    def start_run(self):
        mode = "survey" if self.mode_survey.isChecked() else "park"
        wn_lo, wn_hi = self.in_wn_lo.value(), self.in_wn_hi.value()
        if mode == "park":
            t = self.in_park.value()
            wn_lo, wn_hi = t - 0.25, t + 0.25  # local window around the target
        if wn_hi <= wn_lo:
            QtWidgets.QMessageBox.warning(self, "Range", "wn high must exceed wn low")
            return

        self.cfg["park_deadband"] = self.in_deadband.value()
        params = dict(self.cfg)
        params.update({"mode": mode, "wn_lo": wn_lo, "wn_hi": wn_hi,
                       "bin_width": self.in_bin.value(),
                       "park_target": self.in_park.value()})

        try:
            if self.laser is None:
                self.laser = GratingDevice(
                    "Grating", initialization_params={"axis": 1})
            if self.tagger is None:
                self.tagger = Tagger(index=0)
                self.tagger.start_reading()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Hardware", str(e))
            return

        self.state = SharedState(wn_lo, wn_hi, self.in_bin.value())
        g = self.gate_region.getRegion()
        self.state.gate = (min(g), max(g))
        self.recorder = RunRecorder(mode, params)

        self.threads = [WnPoller(self.state, self.cfg),
                        EventCollector(self.tagger, self.state, self.recorder)]
        if mode == "survey":
            self.threads.append(
                SurveySweeper(self.laser, self.state, self.cfg, wn_lo, wn_hi))
            self.park = None
        else:
            self.park = ParkController(self.laser, self.state, self.cfg,
                                       self.in_park.value())
            self.threads.append(self.park)
        for t in self.threads:
            t.start()

        self.lbl_status.setText(f"RUNNING ({mode})")
        self.lbl_folder.setText(f"data: {self.recorder.dir}")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_retune.setEnabled(mode == "park")
        self.btn_nri.setEnabled(True)

    def stop_run(self):
        for t in self.threads:
            t.stop_event.set()
        for t in self.threads:
            t.join(timeout=5)
        self.threads = []
        if self.state is not None and self.recorder is not None:
            with self.state.lock:
                self.recorder.save_spectrum(
                    self.state.wn_centers, self.state.dwell,
                    self.state.counts, self.state.gated_counts)
                extra = {"stopped": time.strftime("%Y%m%d_%H%M%S"),
                         "total_events": int(self.state.total_events),
                         "gated_events": int(self.state.gated_events),
                         "gate": list(self.state.gate)}
            self.recorder.close(extra)
        self.lbl_status.setText("stopped")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_retune.setEnabled(False)
        self.btn_nri.setEnabled(False)
        self.park = None

    def _update_park_target(self):
        if self.park is not None:
            self.park.set_target(self.in_park.value())
            if self.recorder:
                self.recorder.add_marker("park_target", self.in_park.value())

    def _log_nri(self):
        if self.recorder is not None and self.in_nri.text().strip():
            self.recorder.add_marker("nri_delay", self.in_nri.text().strip())
            self.lbl_status.setText(
                f"RUNNING -- NRI marker '{self.in_nri.text().strip()}' logged")

    def _gate_moved(self):
        if self.state is not None:
            g = self.gate_region.getRegion()
            with self.state.lock:
                self.state.gate = (min(g), max(g))

    # -------------------------------------------------------------- display
    def _refresh(self):
        if self.state is None:
            return
        with self.state.lock:
            tof_edges = self.state.tof_edges.copy()
            tof_hist = self.state.tof_hist.copy()
            wn_edges = self.state.wn_edges.copy()
            dwell = self.state.dwell.copy()
            gated = self.state.gated_counts.copy()
            wn_trace = list(self.state.wn_trace)
            rate_trace = list(self.state.rate_trace)
            wn = self.state.wn
            total = self.state.total_events
            gtot = self.state.gated_events
            gate = self.state.gate
            sweep_dir = self.state.sweep_dir
            t0 = self.state.start_time

        self.tof_curve.setData(tof_edges, tof_hist)
        with np.errstate(divide="ignore", invalid="ignore"):
            rate = np.where(dwell > 0, gated / np.maximum(dwell, 1e-9), 0.0)
        self.spec_curve.setData(wn_edges, rate)

        if wn_trace:
            ts, ws = zip(*wn_trace)
            ts = np.asarray(ts) - t0
            ws = np.asarray(ws)
            ok = np.isfinite(ws)
            self.wn_curve.setData(ts[ok], ws[ok])
        if rate_trace:
            ts, rs = zip(*rate_trace)
            self.rate_curve.setData(np.asarray(ts) - t0, np.asarray(rs))

        self.lbl_wn.setText(f"wn: {wn:.5f}" if np.isfinite(wn) else "wn: ---")
        self.lbl_events.setText(f"events: {total} (gated {gtot})")
        if rate_trace:
            self.lbl_rate.setText(f"gated rate: {rate_trace[-1][1]:.1f} /s")
        self.lbl_gate.setText(f"gate: {gate[0]*1e6:.2f} -- {gate[1]*1e6:.2f} us")
        self.lbl_dir.setText({1: "sweep dir: up", -1: "sweep dir: down",
                              0: "sweep dir: parked"}.get(sweep_dir, "---"))

    def closeEvent(self, event):
        if self.threads:
            self.stop_run()
        if self.tagger is not None:
            try:
                self.tagger.stop()
            except Exception:
                pass
        event.accept()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = GratingDAQWindow()
    win.show()
    sys.exit(app.exec_())
