import logging
from logging.config import dictConfig


def configure_logging(*, debug: bool) -> None:
    level = "DEBUG" if debug else "INFO"
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                }
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {"level": level, "handlers": ["default"]},
        }
    )
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
