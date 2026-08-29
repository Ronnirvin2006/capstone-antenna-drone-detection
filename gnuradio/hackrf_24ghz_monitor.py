#!/usr/bin/env python3
import signal
import sys

from gnuradio import eng_notation, gr, qtgui
from gnuradio.filter import firdes
from PyQt5 import Qt
import osmosdr
import sip


class HackRF24GHzMonitor(gr.top_block, Qt.QWidget):
    def __init__(self):
        gr.top_block.__init__(self, "HackRF 2.4 GHz Monitor", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("HackRF 2.4 GHz Drone Signal Monitor")

        self.samp_rate = 10e6
        self.center_freq = 2.4e9
        self.lna_gain = 16
        self.vga_gain = 20
        self.rf_amp = False
        self.fft_size = 4096

        self._build_radio()
        self._build_ui()
        self._connect_blocks()

    def _build_radio(self):
        self.source = osmosdr.source(args="numchan=1 hackrf=0")
        self.source.set_sample_rate(self.samp_rate)
        self.source.set_center_freq(self.center_freq, 0)
        self.source.set_freq_corr(0, 0)
        self.source.set_dc_offset_mode(0, 0)
        self.source.set_iq_balance_mode(0, 0)
        self.source.set_gain_mode(False, 0)
        self.source.set_gain(0, 0)
        self.source.set_if_gain(self.lna_gain, 0)
        self.source.set_bb_gain(self.vga_gain, 0)
        self.source.set_bandwidth(self.samp_rate, 0)

        self.freq_sink = qtgui.freq_sink_c(
            self.fft_size,
            firdes.WIN_BLACKMAN_hARRIS,
            self.center_freq,
            self.samp_rate,
            "RF Spectrum / Waveform",
            1,
        )
        self.freq_sink.set_update_time(1.0 / 30)
        self.freq_sink.set_y_axis(-110, -20)
        self.freq_sink.set_y_label("Power", "dB")
        self.freq_sink.enable_grid(True)
        self.freq_sink.enable_axis_labels(True)
        self.freq_sink.set_fft_average(0.15)
        self.freq_sink.set_line_label(0, "HackRF")
        self.freq_sink.set_line_color(0, "white")
        self.freq_sink.set_line_width(0, 1)

        self.waterfall_sink = qtgui.waterfall_sink_c(
            self.fft_size,
            firdes.WIN_BLACKMAN_hARRIS,
            self.center_freq,
            self.samp_rate,
            "Waterfall",
            1,
        )
        self.waterfall_sink.set_update_time(1.0 / 30)
        self.waterfall_sink.set_intensity_range(-98, -38)
        self.waterfall_sink.enable_grid(True)
        self.waterfall_sink.enable_axis_labels(True)
        self.waterfall_sink.set_fft_average(0.05)

    def _build_ui(self):
        root = Qt.QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        plots = Qt.QVBoxLayout()
        plots.setSpacing(4)
        root.addLayout(plots, 1)

        freq_widget = sip.wrapinstance(self.freq_sink.qwidget(), Qt.QWidget)
        waterfall_widget = sip.wrapinstance(self.waterfall_sink.qwidget(), Qt.QWidget)
        plots.addWidget(freq_widget, 1)
        plots.addWidget(waterfall_widget, 3)

        controls = Qt.QGroupBox("Receiver Options")
        controls.setFixedWidth(310)
        form = Qt.QFormLayout(controls)
        form.setLabelAlignment(Qt.Qt.AlignRight)
        root.addWidget(controls)

        self.status_label = Qt.QLabel("Input: HackRF One via osmosdr")
        self.status_label.setStyleSheet("font-weight: 600; color: #0a7a2f;")
        form.addRow(self.status_label)

        self.freq_spin = Qt.QDoubleSpinBox()
        self.freq_spin.setDecimals(6)
        self.freq_spin.setRange(2300.0, 2500.0)
        self.freq_spin.setSingleStep(0.1)
        self.freq_spin.setSuffix(" MHz")
        self.freq_spin.setValue(self.center_freq / 1e6)
        self.freq_spin.valueChanged.connect(lambda mhz: self.set_center_freq(mhz * 1e6))
        form.addRow("Center freq", self.freq_spin)

        quick_freqs = Qt.QHBoxLayout()
        for label, mhz in (("2400", 2400.0), ("2404.045", 2404.0453), ("2420", 2420.0)):
            button = Qt.QPushButton(label)
            button.clicked.connect(lambda checked=False, value=mhz: self.freq_spin.setValue(value))
            quick_freqs.addWidget(button)
        form.addRow("Quick tune", quick_freqs)

        self.span_label = Qt.QLineEdit("10.000 MHz")
        self.span_label.setReadOnly(True)
        form.addRow("Visible span", self.span_label)

        self.lna_slider = self._gain_slider(self.lna_gain, 0, 40, 8, self.set_lna_gain)
        form.addRow("LNA gain", self.lna_slider)

        self.vga_slider = self._gain_slider(self.vga_gain, 0, 62, 2, self.set_vga_gain)
        form.addRow("VGA gain", self.vga_slider)

        self.amp_checkbox = Qt.QCheckBox("RF amp")
        self.amp_checkbox.setChecked(False)
        self.amp_checkbox.toggled.connect(self.set_rf_amp)
        form.addRow("Amp", self.amp_checkbox)

        self.low_spin = Qt.QSpinBox()
        self.low_spin.setRange(-140, 0)
        self.low_spin.setValue(-98)
        self.low_spin.setSuffix(" dB")
        self.low_spin.valueChanged.connect(self.set_waterfall_floor)
        form.addRow("Waterfall floor", self.low_spin)

        self.high_spin = Qt.QSpinBox()
        self.high_spin.setRange(-140, 0)
        self.high_spin.setValue(-38)
        self.high_spin.setSuffix(" dB")
        self.high_spin.valueChanged.connect(self.set_waterfall_ceiling)
        form.addRow("Waterfall ceiling", self.high_spin)

        note = Qt.QLabel(
            "Close Gqrx before running.\n"
            "Start with RF amp off.\n"
            "Use the same antenna path:\n"
            "Vivaldi -> HackRF RX."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #444;")
        form.addRow(note)

    def _gain_slider(self, value, low, high, step, callback):
        box = Qt.QWidget()
        layout = Qt.QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = Qt.QSlider(Qt.Qt.Horizontal)
        slider.setRange(low, high)
        slider.setSingleStep(step)
        slider.setPageStep(step)
        slider.setValue(value)
        number = Qt.QLabel(f"{value} dB")
        number.setMinimumWidth(48)

        def update(next_value):
            callback(next_value)
            number.setText(f"{next_value} dB")

        slider.valueChanged.connect(update)
        layout.addWidget(slider, 1)
        layout.addWidget(number)
        return box

    def _connect_blocks(self):
        self.connect((self.source, 0), (self.freq_sink, 0))
        self.connect((self.source, 0), (self.waterfall_sink, 0))

    def set_center_freq(self, freq_hz):
        self.center_freq = freq_hz
        self.source.set_center_freq(self.center_freq, 0)
        self.freq_sink.set_frequency_range(self.center_freq, self.samp_rate)
        self.waterfall_sink.set_frequency_range(self.center_freq, self.samp_rate)
        self.status_label.setText(
            f"Input: HackRF One, center {eng_notation.num_to_str(self.center_freq)}Hz"
        )

    def set_lna_gain(self, gain):
        self.lna_gain = gain
        self.source.set_if_gain(gain, 0)

    def set_vga_gain(self, gain):
        self.vga_gain = gain
        self.source.set_bb_gain(gain, 0)

    def set_rf_amp(self, enabled):
        self.rf_amp = bool(enabled)
        self.source.set_gain(14 if enabled else 0, 0)

    def set_waterfall_floor(self, floor):
        high = max(self.high_spin.value(), floor + 5)
        self.high_spin.setValue(high)
        self.waterfall_sink.set_intensity_range(floor, high)

    def set_waterfall_ceiling(self, ceiling):
        low = min(self.low_spin.value(), ceiling - 5)
        self.low_spin.setValue(low)
        self.waterfall_sink.set_intensity_range(low, ceiling)


def main():
    app = Qt.QApplication(sys.argv)
    tb = HackRF24GHzMonitor()
    tb.start()
    tb.show()

    def quit_handler(*_):
        tb.stop()
        tb.wait()
        app.quit()

    signal.signal(signal.SIGINT, quit_handler)
    signal.signal(signal.SIGTERM, quit_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    app.exec_()
    tb.stop()
    tb.wait()


if __name__ == "__main__":
    main()
