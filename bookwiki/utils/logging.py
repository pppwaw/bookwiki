from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"
_BOOK_LOG_HANDLER_ATTR = "_bookwiki_log_path"


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    return logging.getLogger(name)


def configure_book_file_logging(logs_dir: str | Path) -> Path:
    logs_path = Path(logs_dir)
    logs_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = _next_log_path(logs_path, stamp)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for handler in list(root_logger.handlers):
        if getattr(handler, _BOOK_LOG_HANDLER_ATTR, None) is not None:
            root_logger.removeHandler(handler)
            handler.close()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    setattr(file_handler, _BOOK_LOG_HANDLER_ATTR, log_path.resolve())
    root_logger.addHandler(file_handler)
    return log_path


def _next_log_path(logs_path: Path, stamp: str) -> Path:
    index = 1
    while True:
        candidate = logs_path / f"pipeline-{stamp}-{index:03d}.log"
        if not candidate.exists():
            return candidate
        index += 1
