"""
Uvicorn & Application Logging Dictionary Configuration for OpenTelemetry (OTel) JSON logging.
This ensures Uvicorn's master process, reloader process, and worker processes ALL use OTel JSON format from line 1.
"""
from __future__ import annotations

import os
from pathlib import Path

# Resolve logs directory
env_logs_dir = os.getenv("LOGS_DIR")
if env_logs_dir:
    backend_logs_dir = Path(env_logs_dir).resolve() / "backend"
else:
    backend_dir = Path(__file__).resolve().parent.parent.parent
    project_root = backend_dir.parent
    backend_logs_dir = project_root / "logs" / "backend"

backend_logs_dir.mkdir(parents=True, exist_ok=True)

main_log_path = str(backend_logs_dir / "backend.log")
error_log_path = str(backend_logs_dir / "backend_error.log")

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "otel_json": {
            "()": "app.utils.logger.OTelJsonFormatter",
            "service_name": "consilium-backend",
        },
        "console_text": {
            "format": "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
            "datefmt": "%H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "console_text",
            "stream": "ext://sys.stdout",
        },
        "file_main": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "INFO",
            "formatter": "otel_json",
            "filename": main_log_path,
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
        },
        "file_error": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "ERROR",
            "formatter": "otel_json",
            "filename": error_log_path,
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "": {
            "handlers": ["console", "file_main", "file_error"],
            "level": "INFO",
        },
        "uvicorn": {
            "handlers": ["console", "file_main", "file_error"],
            "level": "INFO",
            "propagate": False,
        },
        "uvicorn.error": {
            "handlers": ["console", "file_main", "file_error"],
            "level": "INFO",
            "propagate": False,
        },
        "uvicorn.access": {
            "handlers": ["console", "file_main", "file_error"],
            "level": "INFO",
            "propagate": False,
        },
        "consilium": {
            "handlers": ["console", "file_main", "file_error"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
