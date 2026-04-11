"""
gpio_stub.py  —  Safe GPIO loader for cross-platform use.

REAL MODE (Raspberry Pi):
  RPi.GPIO is imported normally and real pins are driven.

SIM MODE (Windows / Mac / headless Linux without GPIO):
  A stub object is returned that prints pin changes to the log instead of
  touching hardware.  The rest of the code is completely unaware of the difference.

HOW TO USE:
  from modules.shared.gpio_stub import gpio, GPIO_AVAILABLE

  gpio.setup(pin, gpio.OUT)
  gpio.output(pin, True)

  if GPIO_AVAILABLE:
      print("Running on real hardware")
  else:
      print("Running in simulation — no GPIO")
"""

import logging
logger = logging.getLogger(__name__)


class _GPIOStub:
    """
    Drop-in replacement for RPi.GPIO when not on a Raspberry Pi.
    Every call is a no-op except output(), which logs the pin change.
    """
    BCM = "BCM"
    OUT = "OUT"
    IN  = "IN"
    HIGH = True
    LOW  = False

    def setmode(self, mode):
        pass  # silently ignored in sim mode

    def setup(self, pin, mode, **kwargs):
        logger.debug(f"[GPIO-SIM] setup pin={pin} mode={mode}")

    def output(self, pin, state):
        label = "HIGH" if state else "LOW"
        logger.debug(f"[GPIO-SIM] pin {pin} → {label}")

    def input(self, pin):
        return False  # always reads LOW in sim

    def cleanup(self):
        logger.debug("[GPIO-SIM] cleanup called")

    def setwarnings(self, flag):
        pass


# ── Try to import the real library ───────────────────────────────────────────
GPIO_AVAILABLE = False
gpio = _GPIOStub()  # default: stub

try:
    import RPi.GPIO as _real_gpio
    _real_gpio.setmode(_real_gpio.BCM)
    _real_gpio.setwarnings(False)
    gpio = _real_gpio
    GPIO_AVAILABLE = True
    logger.info("[GPIO] RPi.GPIO loaded — real hardware GPIO active")
except Exception:
    # ImportError on non-Pi, or RuntimeError if not run as root on Pi
    logger.info("[GPIO] RPi.GPIO not available — using simulation stub")
