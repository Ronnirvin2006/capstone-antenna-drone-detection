# Anti-Drone System — Integration Guide

Stealth Jammer System for Drone Signal Denial & Control  
Capstone Project — MIT Anna University × AIAERO INDIA PVT LTD

---

## Project Structure

```
anti_drone_system/
│
├── main.py                          ← Entry point. Wires all modules together.
│
├── requirements.txt                 ← pip install -r requirements.txt
│
├── config/
│   └── system_config.yaml           ← ALL tunable parameters live here.
│                                      No hard-coded values anywhere else.
│
├── modules/
│   ├── shared/
│   │   ├── event_bus.py             ← Central pub/sub message backbone.
│   │   ├── config_loader.py         ← Loads system_config.yaml safely.
│   │   └── logger_setup.py          ← Rotating file + console + JSON logs.
│   │
│   ├── mux/
│   │   └── mux_controller.py        ← RF MUX GPIO switching + lock-on logic.
│   │
│   ├── sdr/
│   │   └── sdr_pipeline.py          ← ADALM-Pluto FMCW radar + FFT range est.
│   │
│   ├── visual/
│   │   ├── drone_tracker.py         ← Your original ByteTrack script (unchanged).
│   │   └── visual_pipeline.py       ← Modular wrapper that publishes bus events.
│   │
│   ├── identity/
│   │   └── identity_classifier.py   ← Reads ESP32 serial → classifies drone.
│   │
│   ├── fusion/
│   │   └── fusion_engine.py         ← Correlates SDR + visual in time window.
│   │
│   └── jammer/
│       └── jammer_controller.py     ← Safety-gated GPIO jammer activation.
│
├── esp32_firmware/
│   └── esp32_ble_scanner.ino        ← Arduino sketch for ESP32 BLE scanning.
│
├── models/
│   └── weights/
│       └── best/                    ← Your YOLOv11n weights (extracted from zip).
│
├── data/
│   └── friendly_ids/
│       └── authorised_drones.json   ← Whitelist of friendly drone/operator IDs.
│
└── logs/                            ← Auto-created. system.log + events.jsonl
```

---

## How the Modules Communicate

All modules talk through the **Event Bus** (`modules/shared/event_bus.py`).  
No module imports another module directly.

```
ESP32 (BLE scan)
    │  USB Serial JSON
    ▼
IdentityClassifier ──────────────────────────────► "identity.result"
                                                          │
                                                          ▼
Antenna Array                                      JammerController
    │                                               (needs BOTH:
    ▼                                               fusion.confirmed
MUXController ──► ADALM-Pluto SDR                  AND identity result
    │  (lock-on)       │                            = NON_FRIENDLY)
    │                  ▼
    │           SDRPipeline ──► "sdr.detection"
    │                                 │
    │                                 ▼
    │           VisualPipeline ──► "visual.detection"
    │           (YOLOv11n +              │
    │            ByteTrack)              ▼
    │                            FusionEngine ──► "fusion.confirmed"
    │                                                     │
    └────────────────────────────────────────────────────-┘
                                              (lock-on feedback to MUX)
```

**Topics published on the bus:**

| Topic | Published by | Payload |
|---|---|---|
| `sdr.detection` | SDRPipeline | `{range_m, rssi_db, freq_hz, ts}` |
| `visual.detection` | VisualPipeline | `{track_id, bbox, confidence, range_est_m, ts}` |
| `identity.result` | IdentityClassifier | `{drone_id, label, rssi, reason, raw, ts}` |
| `fusion.confirmed` | FusionEngine | `{range_m, sdr_fresh, visual_fresh, ts}` |
| `mux.lock` | MUXController | `{antenna, lock}` |
| `jammer.trigger` | JammerController | `{activate, reason, freq_hz, ts}` |

---

## Quickstart

### 1. Install dependencies

```bash
cd anti_drone_system
pip install -r requirements.txt
```

On Raspberry Pi, also uncomment `RPi.GPIO` in `requirements.txt`.

### 2. Flash the ESP32

Open `esp32_firmware/esp32_ble_scanner.ino` in Arduino IDE.  
Select board: **ESP32 Dev Module**.  Upload.  
Verify output on Serial Monitor @ 115200 baud — you should see JSON lines.

### 3. Configure

Edit `config/system_config.yaml`:
- Set `sdr.device_uri` to your Pluto's IP or USB URI.
- Set `identity.serial_port` to your ESP32's port (e.g. `/dev/ttyUSB0`).
- Set `visual.model_path` to `models/weights/best.pt` (already placed).
- Set `mux.antennas` GPIO pin numbers to match your hardware wiring.
- Add your authorised drone IDs to `data/friendly_ids/authorised_drones.json`.

### 4. Run

```bash
python main.py
```

To test without any hardware (all modules in simulation mode):
```bash
python main.py --sim
```

---

## Tuning Parameters (system_config.yaml)

| Parameter | Default | Effect |
|---|---|---|
| `mux.scan_interval_sec` | 10 | How long each antenna is active during round-robin |
| `mux.lock_drop_threshold_sec` | 3 | Seconds of silence before MUX unlocks |
| `sdr.range_detect_threshold` | 0.15 | FFT amplitude floor for detection (0–1) |
| `sdr.max_range_m` | 500 | Detections beyond this are ignored |
| `visual.confidence_threshold` | 0.30 | YOLO minimum confidence |
| `fusion.window_sec` | 2.0 | Sliding window for SDR+visual correlation |
| `fusion.require_both` | true | Both sensors must agree to confirm |
| `jammer.cooldown_sec` | 5 | Min time between jammer activations |
| `jammer.max_active_sec` | 30 | Hard auto-shutoff (safety interlock) |
| `identity.unknown_is_threat` | true | Treat drones with no BLE ID as hostile |

---

## Adding or Disabling a Module

**Disable visual:** In `main.py`, comment out `visual.start()` and the `visual.detection` subscription.  
**Disable SDR:** Comment out `sdr_thread.start()`.  
**Add a new sensor:** Create `modules/new_sensor/new_sensor.py`, publish events to the bus, subscribe to whatever topics you need.  
**Run identity classifier standalone:**
```python
from modules.shared.event_bus import EventBus
from modules.identity.identity_classifier import IdentityClassifier
bus = EventBus()
ic = IdentityClassifier(cfg["identity"], bus)
ic.start()
```

---

## Log Files (auto-created in `logs/`)

| File | Format | Contents |
|---|---|---|
| `system.log` | Plain text | All modules, rotating 10 MB × 5 |
| `events.jsonl` | JSON Lines | One structured JSON per event — parseable with pandas |
| `tracked_output.mp4` | Video | Annotated drone tracking video |

Parse events post-mission:
```python
import json, pandas as pd
events = [json.loads(l) for l in open("logs/events.jsonl")]
df = pd.DataFrame(events)
```

---

## ESP32 Firmware Notes

The firmware in `esp32_firmware/esp32_ble_scanner.ino` scans for BLE advertisements
matching either:
- **Service UUID 0xFFFA** (ASTM F3411 OpenDroneID BT4 legacy format)
- **Company ID 0x02E5 + App Code 0x0D** (manufacturer-specific data format)

Each detected packet is sent as a single-line JSON object over USB serial.  
The Python `IdentityClassifier` reads this with `readline()`.

Message types decoded: `BasicID`, `Location`, `Auth`, `SelfID`, `System`, `OperatorID`, `MessagePack`.

For the full OpenDroneID C library (encode/decode all fields), see:  
https://github.com/opendroneid/opendroneid-core-c
