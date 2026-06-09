"""logger.py — Structured logging setup"""

import logging
import sys
from pathlib import Path
from datetime import datetime


def setup_logger(name: str, log_dir: str = "logs") -> logging.Logger:
    log_path = Path(log_dir)
    if not log_path.is_absolute():
        log_path = Path(__file__).resolve().parents[2] / log_path
    log_path.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = log_path / f"bot_{today}.log"

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-14s | %(message)s",
        datefmt="%H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger


def get_logger(name: str, log_dir: str = "logs") -> logging.Logger:
    """Alias for setup_logger for convenience"""
    return setup_logger(name, log_dir)
