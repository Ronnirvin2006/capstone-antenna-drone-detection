# Antenna Drone Detection System

Interactive SDR dashboard for viewing the 2.4 GHz RF waveform and waterfall
using a Vivaldi antenna connected to a HackRF One.

This repository was copied from the earlier capstone anti-drone system and is
now being continued as a receive-only RF monitoring project. The old modules
are kept as reference code, but the new application entry point is
`antenna_dashboard.py`.

## Current Hardware Plan

```text
Yagi-Uda 433 MHz      \
LPDA 915 MHz-1.6 GHz  -> RF MUX -> HackRF One -> Laptop/System
Vivaldi 2 GHz-6 GHz  /
```

The current dashboard is focused only on the Vivaldi + HackRF path for a
Gqrx-like 2.4 GHz display. MUX control and the other antennas will come later.

## Antenna Bands

| Slot | Antenna | Main Use |
|---|---|---|
| 2.4 GHz Drone Band | Vivaldi 2 GHz to 6 GHz | 2.4 GHz control / WiFi-like links |

## Run The Dashboard

Create a virtual environment if needed:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For the current dashboard only, use the lightweight install:

```bash
pip install -r requirements-dashboard.txt
```

Start the offline app:

```bash
python antenna_dashboard.py
```

Open:

```text
http://127.0.0.1:8080
```

## What Works Now

- One large 2.4 GHz spectrum waveform.
- One wide 2.4 GHz waterfall below the waveform.
- 2400 MHz centered in the graph.
- Live HackRF IQ mode using `hackrf_transfer`.
- Peak readout for strong visible carriers.
- Backend gain/filter options for LNA, VGA, RF amp, sample rate/span, FFT size, and smoothing.
- Buttons for Waterfall, Peak Hold, Filters, RFID Detection, and Video Detection.

By default the app starts in offline mode. It does not claim drone detection or
classification.

For real HackRF waterfall scanning:

```bash
python antenna_dashboard.py --live --backend iq --center-hz 2400000000 --sample-rate 10000000 --fft-size 1024
```

Close Gqrx before running live mode because only one program can use HackRF at
a time.

The default IQ live view is focused on 2.4 GHz with 2400 MHz in the centre:

```text
2395-2405 MHz
```

That 10 MHz span is similar to the Gqrx-style view. HackRF cannot show
100 MHz left and 100 MHz right as one live IQ view; for that, the app must use
slower sweep mode.

Lower noise / less gain:

```bash
python antenna_dashboard.py --live --lna 8 --vga 12 --db-min -90 --db-max -35
```

More sensitivity:

```bash
python antenna_dashboard.py --live --lna 24 --vga 28 --db-min -90 --db-max -35
```

Sharper waterfall bins:

```bash
python antenna_dashboard.py --live --fft-size 2048
```

The browser render loop targets 60 Hz, and the IQ backend publishes waterfall
rows at about 60 Hz.

## Next Build Steps

1. Add real HackRF receive capture.
2. Add Arduino/Raspberry Pi MUX control for antenna selection.
3. Detect new/hopping signals using baseline noise and peak tracking.
4. Estimate number of suspicious emitters from separated signal clusters.
5. Implement RFID/Remote ID and video detection buttons as live modules.

## Earlier Capstone Reference

The original capstone report covered drone tracking, antenna fabrication, SDR
radar, Remote ID, and jamming. This continued project is changing direction:
the final system will focus on receive-only detection and alerting from antenna
signals.
