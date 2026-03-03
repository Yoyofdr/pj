"""Logger dual: archivo + consola con rotación."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_logger: logging.Logger | None = None


def setup_logger(logs_dir: Path, level: int = logging.DEBUG) -> logging.Logger:
    """Crea (o retorna) logger con handlers de archivo y consola."""
    global _logger
    if _logger is not None:
        return _logger

    logs_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"run_{stamp}.log"

    logger = logging.getLogger("pjud")
    logger.setLevel(level)
    logger.propagate = False

    # ── Handler archivo ────────────────────────────────────────
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fmt_file = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(fmt_file)
    logger.addHandler(fh)

    # ── Handler consola ────────────────────────────────────────
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    fmt_console = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    ch.setFormatter(fmt_console)
    logger.addHandler(ch)

    _logger = logger
    logger.info("Log iniciado → %s", log_file)
    return logger


def get_logger() -> logging.Logger:
    """Retorna el logger global (debe haberse llamado setup_logger primero)."""
    if _logger is None:
        # Fallback básico
        return setup_logger(Path("./logs"))
    return _logger
