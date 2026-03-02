from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QGroupBox, QListWidget, QListWidgetItem, QAbstractItemView, QCheckBox, QLabel, QSpinBox, QHBoxLayout)
from PyQt5.QtCore import pyqtSignal, Qt

class PlotOptionsWidget(QWidget):
    options_changed = pyqtSignal(list)
    auto_scale_toggled = pyqtSignal(bool)
    theme_toggled = pyqtSignal(bool) # True = Dark, False = Light
    tof_online_toggled = pyqtSignal(bool)
    tof_bins_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.item_map = {}
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        grp_opts = QGroupBox("Plot Options")
        layout_opts = QVBoxLayout()
        grp_opts.setLayout(layout_opts)

        self.chk_auto_scale = QCheckBox("Lock/Auto-Scale Axes")
        self.chk_auto_scale.setChecked(True)
        self.chk_auto_scale.toggled.connect(self.auto_scale_toggled.emit)
        layout_opts.addWidget(self.chk_auto_scale)
        self.chk_theme = QCheckBox("Dark Mode")
        self.chk_theme.setChecked(False)
        self.chk_theme.toggled.connect(self.theme_toggled.emit)
        layout_opts.addWidget(self.chk_theme)

        layout_opts.addWidget(QLabel("Drag to Reorder:"))
        self.list_widget = QListWidget()
        self.list_widget.setDragDropMode(QAbstractItemView.InternalMove)
        self.list_widget.model().rowsMoved.connect(self.emit_options)
        self.list_widget.itemChanged.connect(self.emit_options)

        self.add_item('rate', "Event Rate vs Time", checked=True)
        self.add_item('scan', "Scan Results (Rate vs WN)", checked=True)
        self.add_item('laser', "Measured & Target WN vs Time", checked=False)
        self.add_item('volt', "Voltage vs Time", checked=False)
        self.add_item('tof', "ToF Histogram", checked=False)

        layout_opts.addWidget(self.list_widget)
        layout.addWidget(grp_opts)

        grp_tof = QGroupBox("ToF Settings")
        layout_tof = QVBoxLayout()
        grp_tof.setLayout(layout_tof)

        self.chk_tof_online = QCheckBox("ToF Online Mode")
        self.chk_tof_online.setChecked(False)
        self.chk_tof_online.toggled.connect(self.tof_online_toggled.emit)
        layout_tof.addWidget(self.chk_tof_online)

        bins_row = QHBoxLayout()
        bins_row.addWidget(QLabel("Bins:"))
        self.spin_tof_bins = QSpinBox()
        self.spin_tof_bins.setRange(10, 500)
        self.spin_tof_bins.setValue(50)
        self.spin_tof_bins.valueChanged.connect(self.tof_bins_changed.emit)
        bins_row.addWidget(self.spin_tof_bins)
        layout_tof.addLayout(bins_row)

        layout.addWidget(grp_tof)

    def add_item(self, key, text, checked=False):
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        item.setData(Qt.UserRole, key)
        self.list_widget.addItem(item)

    def emit_options(self):
        active_plots = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.Checked:
                active_plots.append(item.data(Qt.UserRole))
        self.options_changed.emit(active_plots)

    def get_options(self):
        active_plots = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.Checked:
                active_plots.append(item.data(Qt.UserRole))
        return active_plots
