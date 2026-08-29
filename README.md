# Antenna Drone Detection System

Interactive SDR dashboard for detecting suspicious drone-like RF activity using
three fabricated antennas connected through an RF MUX to a HackRF One.

This repository was copied from the earlier capstone anti-drone system and is
now being continued as a receive-only detection project. The old modules are
kept as reference code, but the new application entry point is
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
| Yagi-Uda 433 | 433 MHz directional antenna | 433 MHz telemetry / ISM activity |
| LPDA 915-1600 | 915 MHz to 1.6 GHz LPDA | 915 MHz, GNSS bands, other low-band links |
| Vivaldi 2-6 GHz | 2 GHz to 6 GHz Vivaldi | 2.4 GHz and 5.8 GHz drone/control/video activity |

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

- Three live waterfall graph slots.
- Round-robin MUX-style scanning model.
- Clear offline/demo mode indicator.
- ML-style waterfall classifier for noise, steady carrier, and drone-like hopping patterns.
- Suspicious hopping/new-signal alert panel in demo mode.
- Estimated suspected drone count placeholder for later real SDR integration.
- Buttons for Antenna Scan, RFID Detection, Video Detection, and Optimization.

By default the app starts in offline mode. It does not claim real detections
until HackRF/MUX input is connected in the next step.

For UI testing with fake drone-like signals:

```bash
python antenna_dashboard.py --demo-signals
```

For real Vivaldi + HackRF scanning:

```bash
python antenna_dashboard.py --hackrf-vivaldi
```

Close Gqrx before running live mode because only one program can use the HackRF
at a time. Live mode currently feeds real `hackrf_sweep` spectrum data into the
Vivaldi waterfall slot from 2.4 GHz to 6 GHz. The Yagi and LPDA slots remain
marked as not connected until MUX control is added.

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
