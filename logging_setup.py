import logging
import os
from logging.handlers import RotatingFileHandler

from config import LOG_DIR


LOGGER_NAME = "classassist"


def get_logger():
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    os.makedirs(LOG_DIR, exist_ok=True)
    logger.setLevel(logging.INFO)

    handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"),
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger
