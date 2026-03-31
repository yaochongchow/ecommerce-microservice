import logging
import os

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "[%(levelname)s] %(name)s - %(message)s"
        ))
        logger.addHandler(handler)
    logger.setLevel(LOG_LEVEL)
    return logger
