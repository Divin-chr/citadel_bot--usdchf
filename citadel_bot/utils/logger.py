"""logger.py - Structured logging setup + lightweight latency Timer."""

import logging
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Optional


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


class Timer:
    """
    Lightweight latency context manager. Logs only when the elapsed time
    exceeds `threshold_ms` so normal-speed operations don't spam the log.

    Usage:
        with Timer(log, "signal_gen", threshold_ms=500):
            signal = generator.generate(sym, df)
    """

    def __init__(
        self,
        log: logging.Logger,
        label: str,
        threshold_ms: Optional[float] = 500.0,
    ):
        self.log = log
        self.label = label
        self.threshold_ms = threshold_ms
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed_ms = (time.perf_counter() - self._t0) * 1000.0
        if self.threshold_ms is None or elapsed_ms > self.threshold_ms:
            level = logging.WARNING if elapsed_ms > (self.threshold_ms or 0) * 4 else logging.INFO
            self.log.log(level, "timing %s=%.1fms", self.label, elapsed_ms)
        return False  # never swallow exceptions
