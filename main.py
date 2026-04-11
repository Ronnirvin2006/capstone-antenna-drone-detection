"""
main.py  â€”  Anti-drone system entry point.

WHAT THIS DOES:
  Instantiates and starts every module in the correct order:
    1. Logging
    2. Config
    3. Event bus (shared message backbone)
    4. MUX controller (antenna switching)
    5. SDR pipeline (FMCW radar)
    6. Visual pipeline (YOLOv11n + ByteTrack)
    7. Fusion engine (SDR + visual correlation)
    8. Identity classifier (ESP32 serial â†’ OpenDroneID)
    9. Jammer controller (action decision + GPIO)

  Everything communicates ONLY through the event bus â€” no module imports
  another module.  This makes it trivially easy to:
    - Disable one sensor: just comment out its start() call.
    - Run in simulation: each module has a sim mode when hardware isn't present.
    - Unit-test any module in isolation by injecting a mock bus.

STARTUP ORDER MATTERS:
  The event bus must exist before any module that subscribes to it.
  The jammer must be the LAST thing started (it shouldn't activate until
  all sensors have had a chance to warm up).

GRACEFUL SHUTDOWN:
  Ctrl-C triggers the shutdown sequence in reverse order.  The jammer is
  deactivated first (safety), then cameras and SDR are stopped, then GPIO
  is cleaned up.

USAGE:
  python main.py
  python main.py --config config/system_config.yaml
  python main.py --sim          # force simulation mode for all hardware modules
"""

import argparse
import signal
import sys
import time
import logging

# â”€â”€ Internal imports (project modules) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Note: use sys.path manipulation so this works when run from the project root
import os
sys.path.insert(0, os.path.dirname(__file__))

from modules.shared.logger_setup   import setup_logging
from modules.shared.config_loader  import load_config
from modules.shared.event_bus      import EventBus

from modules.mux.mux_controller         import MUXController
from modules.sdr.sdr_pipeline           import SDRPipeline
from modules.visual.visual_pipeline     import VisualPipeline
from modules.fusion.fusion_engine       import FusionEngine
from modules.identity.identity_classifier import IdentityClassifier
from modules.jammer.jammer_controller   import JammerController


def parse_args():
    p = argparse.ArgumentParser(description="Anti-drone detection and response system")
    p.add_argument("--config", default="config/system_config.yaml",
                   help="Path to system_config.yaml")
    p.add_argument("--sim", action="store_true",
                   help="Force simulation mode (no hardware required)")
    return p.parse_args()


def main():
    args = parse_args()

    # â”€â”€ 1. Logging â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Must be set up BEFORE any logger = logging.getLogger(__name__) calls.
    setup_logging(log_dir="logs", level="INFO")
    logger = logging.getLogger("main")
    logger.info("=" * 60)
    logger.info("  Anti-Drone System  â€”  starting up")
    logger.info("=" * 60)

    # â”€â”€ 2. Config â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    cfg = load_config(args.config)
    logger.info(f"Config loaded from '{args.config}'")

    # â”€â”€ 3. Event bus â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # All modules share ONE bus instance.  It lives here in main so nothing
    # else needs to import it as a global.
    bus = EventBus()

    # Wire a system-level logger so every bus event above WARNING goes to log
    def _log_alerts(topic, payload):
        if topic == "system.alert":
            lvl = payload.get("level", "INFO").upper()
            msg = payload.get("msg", "")
            getattr(logger, lvl.lower(), logger.info)(f"[ALERT] {msg}")
    bus.subscribe("system.alert", _log_alerts)

    # Also log every jammer trigger to the console prominently
    def _log_jammer(topic, payload):
        if payload.get("activate"):
            logger.warning(f"[JAMMER FIRED] reason='{payload.get('reason')}'")
        else:
            logger.info(f"[JAMMER OFF] reason='{payload.get('reason')}'")
    bus.subscribe("jammer.trigger", _log_jammer)

    # â”€â”€ 4. Instantiate all modules â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Instantiation sets up subscriptions; nothing starts running yet.
    logger.info("Instantiating modules â€¦")

    mux       = MUXController(cfg["mux"], bus)
    sdr       = SDRPipeline(cfg["sdr"], bus)
    visual    = VisualPipeline(cfg["visual"], bus)
    fusion    = FusionEngine(cfg["fusion"], bus)           # subscribes immediately
    identity  = IdentityClassifier(cfg["identity"], bus)
    jammer    = JammerController(cfg["jammer"], cfg["identity"], bus)

    # â”€â”€ 5. Start modules (order matters â€” sensors before decision layers) â”€â”€â”€â”€â”€â”€
    logger.info("Starting modules â€¦")

    mux.start()          # antenna switcher background thread
    sdr.connect()        # open SDR hardware connection

    # SDR runs in a background thread (it has its own blocking loop)
    import threading
    sdr_thread = threading.Thread(target=sdr.run, daemon=True, name="SDRPipeline")
    sdr_thread.start()

    visual.start()       # opens cameras, starts YOLO thread
    identity.start()     # opens ESP32 serial port, starts reader thread
    # fusion and jammer don't have their own threads â€” they react via bus callbacks

    logger.info("All modules running.  Press Ctrl-C to stop.")

    # â”€â”€ 6. Graceful shutdown on Ctrl-C / SIGTERM â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _shutdown(sig, frame):
        logger.info("Shutdown signal received â€” stopping all modules â€¦")

        # Safety first: jammer OFF before anything else
        jammer.cleanup()

        # Stop sensors
        visual.stop()
        sdr.stop()
        mux.stop()
        identity.stop()

        logger.info("All modules stopped cleanly.  Goodbye.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # â”€â”€ 7. Keep main thread alive â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # The actual work happens in daemon threads/callbacks triggered by the bus.
    # Main just waits so those threads keep running.
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
