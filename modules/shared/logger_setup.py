"""
logger_setup.py  —  Centralised logging configuration.

WHY THIS EXISTS:
  All modules import this once.  It sets up:
    - Console output (INFO level by default)
    - Rotating file output to logs/system.log (DEBUG level)
    - A structured JSON file to logs/events.jsonl for post-mission analysis

  Call setup_logging() ONCE at the start of main.py.
  Every other module just does:  logger = logging.getLogger(__name__)
"""

import logging
import logging.handlers
import json
import time
import os


class JSONLineHandler(logging.Handler):
    """
    Writes one JSON object per line to a .jsonl file.
    This makes it trivial to parse logs later with pandas or jq.
    """

    def __init__(self, filepath: str):
        super().__init__()
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        self._file = open(filepath, "a", buffering=1)  # line-buffered

    def emit(self, record: logging.LogRecord):
        entry = {
            "ts": time.time(),          # wall-clock time
            "level": record.levelname,
            "module": record.name,
            "msg": self.format(record),
        }
        try:
            self._file.write(json.dumps(entry) + "\n")
        except Exception:
            self.handleError(record)


def setup_logging(log_dir: str = "logs", level: str = "INFO"):
    """
    Call once from main.py before any other import.

    Args:
        log_dir: Directory where log files are written.
        level:   Console log level string ("DEBUG", "INFO", "WARNING", …).
    """
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # capture everything; handlers filter

    fmt = logging.Formatter(
        "%(asctime)s  [%(levelname)-8s]  %(name)-30s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── Console handler ──────────────────────────────────────────────────────
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(fmt)
    root.addHandler(console)

    # ── Rotating plain-text file (10 MB × 5 backups) ────────────────────────
    txt_path = os.path.join(log_dir, "system.log")
    fh = logging.handlers.RotatingFileHandler(
        txt_path, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # ── Structured JSON events file ──────────────────────────────────────────
    json_path = os.path.join(log_dir, "events.jsonl")
    jh = JSONLineHandler(json_path)
    jh.setLevel(logging.INFO)
    root.addHandler(jh)

    logging.getLogger(__name__).info(
        f"Logging initialised — console={level}, file=DEBUG → {txt_path}"
    )
