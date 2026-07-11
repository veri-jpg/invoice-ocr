import logging
import logging.handlers
import os
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "invoice-ocr.log"

_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> None:
    """Dipanggil sekali di tiap entry point (channels/*, reminder.py, run*.py).
    Log jalan ke file (rotasi otomatis, jadi aman dibiarkan nyala terus di VPS)
    dan ke stdout (biar tetap ketangkep systemd/journalctl kalau dipakai)."""
    root = logging.getLogger()
    if root.handlers:
        return  # sudah di-setup (mis. dipanggil dua kali), jangan dobel handler

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # httpx/httpcore/telegram cukup berisik di level INFO (log tiap request);
    # redam ke WARNING supaya log kita sendiri tidak tenggelam.
    for noisy in ("httpx", "httpcore", "telegram.ext._application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
