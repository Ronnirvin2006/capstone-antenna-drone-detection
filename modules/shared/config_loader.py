"""
config_loader.py  —  Loads and validates system_config.yaml.

WHY THIS EXISTS:
  One function (load_config) returns a plain dict that every module can
  access.  Using a central loader means:
    - All modules read from the same file — no drift between configs.
    - Path resolution is done here, so modules get absolute paths back.
    - yaml.safe_load is used (never yaml.load) to prevent code injection.
"""

import os
import yaml
import logging

logger = logging.getLogger(__name__)

# Default location relative to project root
_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "system_config.yaml"
)


def load_config(config_path: str = None) -> dict:
    """
    Load and return the system configuration as a plain dict.

    Args:
        config_path: Optional override path.  Defaults to config/system_config.yaml
                     relative to the project root.

    Returns:
        dict: Parsed configuration.

    Raises:
        FileNotFoundError: If the config file does not exist.
        yaml.YAMLError:    If the YAML is malformed.
    """
    path = config_path or os.path.abspath(_DEFAULT_CONFIG_PATH)

    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    logger.info(f"Config loaded from {path}")
    return cfg
