# Antenna Drone Detection System

Interactive SDR dashboard for viewing RF waterfall activity using fabricated
antennas connected through an RF MUX to a HackRF One.

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

The HackRF can observe only one MUX path at a time, so the dashboard displays
three waterfall slots and updates them in a scan sequence.

## Antenna Bands

| Slot | Antenna | Main Use |
|---|---|---|
| 433 MHz | Yagi-Uda 433 MHz | 433 MHz telemetry / ISM activity |
| 915 MHz | LPDA 915 MHz to 1.6 GHz | 902-928 MHz links |
| GPS / GNSS | LPDA 915 MHz to 1.6 GHz | GPS L5, GPS L2, GPS L1 |
| 2.4 GHz | Vivaldi 2 GHz to 6 GHz | 2.4 GHz control / WiFi-like links |
| 5.8 GHz | Vivaldi 2 GHz to 6 GHz | 5.8 GHz video/control links |

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

Start the app:

```bash
python antenna_dashboard.py
```

Open:

```text
http://127.0.0.1:8080
```

## What Works Now

- Five Gqrx-style waterfall graph slots.
- Fixed panes for 433 MHz, 915 MHz, GPS/GNSS, 2.4 GHz, and 5.8 GHz.
- Live HackRF sweep mode using `hackrf_sweep`.
- Peak readout for strong visible carriers.
- Backend gain/filter options for LNA, VGA, RF amp, bin width, and smoothing.
- Buttons for Waterfall, Peak Hold, Filters, RFID Detection, and Video Detection.

By default the app starts in offline mode. It does not claim drone detection or
classification.

For real HackRF waterfall scanning:

```bash
python antenna_dashboard.py --live
```

Close Gqrx before running live mode because only one program can use HackRF at
a time.

Lower noise / less gain:

```bash
python antenna_dashboard.py --live --lna 8 --vga 12
```

More sensitivity:

```bash
python antenna_dashboard.py --live --lna 24 --vga 28
```

Sharper waterfall bins:

```bash
python antenna_dashboard.py --live --bin-width 1000000
```

## Next Build Steps

1. Add real HackRF receive capture.
2. Add Arduino/Raspberry Pi MUX control for antenna selection.
3. Detect new/hopping signals using baseline noise and peak tracking.
4. Estimate number of suspicious emitters from separated signal clusters.
5. Implement RFID/Remote ID and video detection buttons as live modules.

## Train With Your Drone

Close the dashboard before recording training data. Gqrx must also be closed.

Record background first:

```bash
python collect_rf_training_data.py --label background --seconds 30
```

Then turn on the controller and drone, keep the antenna pointed at it, and run:

```bash
python collect_rf_training_data.py --label drone --seconds 30
```

Optional negative examples help reduce false alerts:

```bash
python collect_rf_training_data.py --label controller --seconds 30
python collect_rf_training_data.py --label wifi --seconds 30
```

Train the model:

```bash
python train_signal_classifier.py
```

Then start live scanning:

```bash
python antenna_dashboard.py --hackrf-vivaldi
```

The dashboard automatically uses `models/signal_classifier.json` when it exists.

## Earlier Capstone Reference

The original capstone report covered drone tracking, antenna fabrication, SDR
radar, Remote ID, and jamming. This continued project is changing direction:
the final system will focus on receive-only detection and alerting from antenna
signals.
