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

# Matplotlib-like styling (white canvas, dark axes, grids) while keeping
# pyqtgraph's fast redraws -- real matplotlib can't keep up with 0.5 s updates.
pg.setConfigOptions(antialias=True, background="w", foreground="k")

MPL_BLUE = (31, 119, 180)
MPL_ORANGE = (255, 127, 14)
MPL_GREEN = (44, 160, 44)
MPL_RED = (214, 39, 40)


def _style(plot, title):
    plot.setTitle(title, color="k", size="11pt")
    plot.showGrid(x=True, y=True, alpha=0.25)
    for ax in ("bottom", "left"):
        plot.getAxis(ax).setPen(pg.mkPen("k"))
        plot.getAxis(ax).setTextPen(pg.mkPen("k"))


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
        self.threads = []      # per-run laser threads (sweeper / park)
        self.park = None
        # Persistent workers (pyepics CA contexts don't survive thread churn;
        # one poller/collector for the app's lifetime, rebound per run).
        self.wn_poller = None
        self.collector = None
        self.wn0 = None        # center wn for the relative-frequency axis

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
        self.in_minutes = dspin(0, 24 * 60, 0, dec=1, step=5)
        self.in_scan_vel = dspin(0.0001, 0.01, self.cfg["scan_velocity"],
                                 dec=5, step=0.0001)
        self.in_park = dspin(10000, 20000, 12625.20)
        self.in_deadband = dspin(0.001, 1.0, self.cfg["park_deadband"],
                                 dec=4, step=0.005)
        form.addRow("Survey wn low", self.in_wn_lo)
        form.addRow("Survey wn high", self.in_wn_hi)
        form.addRow("Spectrum bin (cm^-1)", self.in_bin)
        form.addRow("Scan time (min, 0=inf)", self.in_minutes)
        form.addRow("Scan velocity (units/s)", self.in_scan_vel)
        form.addRow("Park target wn", self.in_park)
        form.addRow("Park deadband (cm^-1)", self.in_deadband)

        self.chk_freq = QtWidgets.QCheckBox(
            "Spectrum x-axis: relative frequency (GHz, v0 = range center)")
        panel.addWidget(self.chk_freq)

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

        self.p_tof = grid.addPlot()
        _style(self.p_tof, "ToF spectrum (all events) + gate")
        self.p_tof.setLabel("bottom", "ToF", units="s")
        self.p_tof.setLabel("left", "counts")
        self.tof_curve = self.p_tof.plot(
            stepMode="center", pen=pg.mkPen(MPL_BLUE, width=1.2),
            fillLevel=0, brush=MPL_BLUE + (60,))
        self.gate_region = pg.LinearRegionItem(
            values=(2e-6, 8e-6), brush=(214, 39, 40, 25),
            pen=pg.mkPen(MPL_RED, width=1.5))
        self.p_tof.addItem(self.gate_region)
        self.gate_region.sigRegionChanged.connect(self._gate_moved)

        self.p_spec = grid.addPlot()
        _style(self.p_spec, "Wn spectrum: gated counts / dwell time")
        self.p_spec.setLabel("bottom", "wavenumber (cm^-1)")
        self.p_spec.setLabel("left", "rate", units="Hz")
        # Line + points on bin centers (matplotlib errorbar-plot style).
        self.spec_curve = self.p_spec.plot(
            pen=pg.mkPen(MPL_GREEN, width=1.2), symbol="o", symbolSize=5,
            symbolPen=pg.mkPen(MPL_GREEN), symbolBrush=MPL_GREEN)

        grid.nextRow()
        self.p_wn = grid.addPlot()
        _style(self.p_wn, "wavenumber vs time")
        self.p_wn.setLabel("bottom", "t", units="s")
        self.p_wn.setLabel("left", "wn (cm^-1)")
        self.wn_curve = self.p_wn.plot(pen=pg.mkPen(MPL_ORANGE, width=1.3))

        self.p_rate = grid.addPlot()
        _style(self.p_rate, "gated rate vs time (lifetime view)")
        self.p_rate.setLabel("bottom", "t", units="s")
        self.p_rate.setLabel("left", "rate", units="Hz")
        self.rate_curve = self.p_rate.plot(pen=pg.mkPen(MPL_BLUE, width=1.3))

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
        self.cfg["scan_velocity"] = self.in_scan_vel.value()
        minutes = self.in_minutes.value()
        self.run_deadline = (time.time() + minutes * 60.0) if minutes > 0 else None
        params = dict(self.cfg)
        params.update({"mode": mode, "wn_lo": wn_lo, "wn_hi": wn_hi,
                       "bin_width": self.in_bin.value(),
                       "scan_minutes": minutes,
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
        self.wn0 = 0.5 * (wn_lo + wn_hi)
        g = self.gate_region.getRegion()
        self.state.gate = (min(g), max(g))
        self.recorder = RunRecorder(mode, params)

        # Persistent workers: start once, rebind to this run.
        if self.wn_poller is None:
            self.wn_poller = WnPoller(self.cfg)
            self.wn_poller.start()
        if self.collector is None:
            self.collector = EventCollector(self.tagger)
            self.collector.start()
        self.wn_poller.set_state(self.state)
        self.collector.rebind(self.state, self.recorder)

        self.threads = []
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
        # Unbind the persistent workers first so nothing writes mid-teardown.
        if self.collector is not None:
            self.collector.rebind(None, None)
        if self.wn_poller is not None:
            self.wn_poller.set_state(None)
        for t in self.threads:
            t.stop_event.set()
        for t in self.threads:
            t.join(timeout=5)
        self.threads = []
        if self.state is not None and self.recorder is not None:
            with self.state.lock:
                self.recorder.save_spectrum(
                    self.state.wn_centers, self.state.dwell,
                    self.state.counts, self.state.gated_spectrum())
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
        # Timed run: auto-stop when the requested scan time elapses.
        if (self.threads and getattr(self, "run_deadline", None)
                and time.time() > self.run_deadline):
            print("[GUI] scan time reached -> stopping run")
            self.stop_run()
            self.lbl_status.setText("stopped (scan time reached)")
        with self.state.lock:
            tof_edges = self.state.tof_edges.copy()
            tof_hist = self.state.tof_hist.copy()
            wn_centers = self.state.wn_centers.copy()
            dwell = self.state.dwell.copy()
            # Re-derived from the 2D histogram under the CURRENT gate: moving
            # the gate re-gates everything collected so far.
            gated = self.state.gated_spectrum()
            wn_trace = list(self.state.wn_trace)
            rate_trace = list(self.state.rate_trace)
            wn = self.state.wn
            total = self.state.total_events
            gtot = self.state.gated_events
            gate = self.state.gate
            sweep_dir = self.state.sweep_dir
            t0 = self.state.start_time

        # ToF display: rebin the fine base histogram so ~100 bins span the
        # current gate (finer gate -> finer ToF display).
        base_bw = tof_edges[1] - tof_edges[0]
        factor = int(max(1, round(((gate[1] - gate[0]) / 100.0) / base_bw)))
        nb = len(tof_hist) // factor
        disp_hist = tof_hist[:nb * factor].reshape(nb, factor).sum(axis=1)
        disp_edges = tof_edges[::factor][:nb + 1]
        self.tof_curve.setData(disp_edges, disp_hist)

        with np.errstate(divide="ignore", invalid="ignore"):
            rate = np.where(dwell > 0, gated / np.maximum(dwell, 1e-9), 0.0)
        # Only draw bins the laser has actually visited (dwell > 0) so the
        # line/point spectrum isn't dragged to zero by untouched bins.
        vis = dwell > 0
        if self.chk_freq.isChecked() and self.wn0 is not None:
            xs = (wn_centers - self.wn0) * 29.9792458  # cm^-1 -> GHz
            self.p_spec.setLabel(
                "bottom", f"rel. frequency (GHz, v0 = {self.wn0:.4f} cm^-1)")
        else:
            xs = wn_centers
            self.p_spec.setLabel("bottom", "wavenumber (cm^-1)")
        self.spec_curve.setData(xs[vis], rate[vis])

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
        for worker in (self.wn_poller, self.collector):
            if worker is not None:
                worker.stop_event.set()
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
