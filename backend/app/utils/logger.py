"""
Consilium Logging Utility Module
Supports size-based rotating file handlers, multi-file separation (all vs error logs),
console stdout streaming, and module-level logger retrieval.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional


def get_logs_directory(subfolder: str = "backend") -> Path:
    """
    Resolve and create dedicated subfolder in project logs.
    Priority: LOGS_DIR env var > PROJECT_ROOT/logs/<subfolder>
    """
    env_logs_dir = os.getenv("LOGS_DIR")
    if env_logs_dir:
        base_logs = Path(env_logs_dir).resolve()
    else:
        backend_dir = Path(__file__).resolve().parent.parent.parent
        project_root = backend_dir.parent
        base_logs = project_root / "logs"

    logs_path = base_logs / subfolder if subfolder else base_logs
    logs_path.mkdir(parents=True, exist_ok=True)
    return logs_path


class OTelJsonFormatter(logging.Formatter):
    """
    OpenTelemetry (OTel) Compliant JSON Log Formatter.
    Produces structured JSON logs with trace correlation, severity numbers, and attributes.
    """
    SEVERITY_NUMBERS = {
        "TRACE": 1,
        "DEBUG": 5,
        "INFO": 9,
        "WARN": 13,
        "WARNING": 13,
        "ERROR": 17,
        "CRITICAL": 21,
        "FATAL": 21,
    }

    def __init__(self, service_name: str = "consilium-backend"):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import datetime, timezone

        iso_timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        trace_id = getattr(record, "trace_id", None)
        span_id = getattr(record, "span_id", None)

        otel_record = {
            "timestamp": iso_timestamp,
            "severity_text": record.levelname,
            "severity_number": self.SEVERITY_NUMBERS.get(record.levelname, 9),
            "service.name": self.service_name,
            "logger.name": record.name,
            "body": record.getMessage(),
            "attributes": {
                "code.filepath": record.pathname,
                "code.lineno": record.lineno,
                "code.function": record.funcName,
                "process.id": record.process,
                "thread.name": record.threadName,
            }
        }

        if trace_id:
            otel_record["trace_id"] = str(trace_id)
        if span_id:
            otel_record["span_id"] = str(span_id)

        if record.exc_info:
            otel_record["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Exception",
                "message": str(record.exc_info[1]) if record.exc_info[1] else "",
                "stacktrace": self.formatException(record.exc_info),
            }

        return json.dumps(otel_record, ensure_ascii=False)


# Root logger namespace. get_logger() prefixes with this, and setup_logging
# configures exactly this logger - they must not drift apart.
ROOT_NAMESPACE = "consilium"


def setup_logging(
    log_level_str: str | None = None,
    logs_dir: Path | str | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    log_format: str | None = None,
) -> logging.Logger:
    """
    Configure global logging with console streaming and rotating file handlers.
    Reads defaults from LOG_LEVEL, LOG_MAX_BYTES (default 10MB), LOG_BACKUP_COUNT (default 5), LOG_FORMAT (default otel).
    """
    if log_level_str is None:
        log_level_str = os.getenv("LOG_LEVEL", "INFO")
    if max_bytes is None:
        max_bytes = int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024))
    if backup_count is None:
        backup_count = int(os.getenv("LOG_BACKUP_COUNT", 5))
    if log_format is None:
        log_format = os.getenv("LOG_FORMAT", "otel").lower()

    if logs_dir is None:
        logs_dir = get_logs_directory("backend")
    else:
        logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)

    # OTel JSON or Text Formatter for log files
    if log_format == "otel":
        file_formatter = OTelJsonFormatter(service_name="consilium-backend")
    else:
        file_formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)-24s | [%(filename)s:%(lineno)d] | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # Clean Console Formatter for stdout stream
    console_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Clear pre-existing handlers to prevent duplicate lines
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # 1. Console Stream Handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # Stamps trace_id/span_id from the active OTel span onto every record.
    # OTelJsonFormatter already has slots for both fields (see below) but
    # nothing populated them until this filter existed -- so trace_id in the
    # log JSON was always empty, and Loki had nothing to correlate with Tempo
    # on. Lazy import: app.services.telemetry is not needed until a record is
    # actually emitted, and this keeps app.utils.logger free of an import-time
    # dependency on the OTel stack (it's a much earlier/more foundational
    # module -- main.py configures logging before almost anything else).
    try:
        from app.services.telemetry import TraceContextFilter

        trace_filter = TraceContextFilter()
    except Exception:
        trace_filter = None

    # 2. Main Rotating File Handler (logs/backend.log)
    main_log_file = logs_dir / "backend.log"
    main_file_handler = RotatingFileHandler(
        filename=main_log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    main_file_handler.setLevel(log_level)
    main_file_handler.setFormatter(file_formatter)
    if trace_filter is not None:
        main_file_handler.addFilter(trace_filter)
    root_logger.addHandler(main_file_handler)

    # 3. Error Rotating File Handler (logs/backend_error.log)
    error_log_file = logs_dir / "backend_error.log"
    error_file_handler = RotatingFileHandler(
        filename=error_log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_file_handler.setLevel(logging.ERROR)
    error_file_handler.setFormatter(file_formatter)
    if trace_filter is not None:
        error_file_handler.addFilter(trace_filter)
    root_logger.addHandler(error_file_handler)

    # Configure Uvicorn server loggers to use OTel JSON formatting in log files
    for uvicorn_logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(uvicorn_logger_name)
        uv_logger.handlers.clear()
        uv_logger.addHandler(console_handler)
        uv_logger.addHandler(main_file_handler)
        uv_logger.addHandler(error_file_handler)
        uv_logger.propagate = False

    # Mute noisy 3rd-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    root_logger = logging.getLogger(ROOT_NAMESPACE)
    root_logger.setLevel(log_level)
    root_logger.info(f"Logging initialized -> Folder: {logs_dir} | Level: {log_level_str}")

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Retrieve a named logger under the project namespace.

    The prefix must match the root logger configured in setup_logging, or the
    child never inherits its handlers and level and its output vanishes. Both
    read ROOT_NAMESPACE for exactly that reason.

    Usage: logger = get_logger("chat")  ->  "consilium.chat"
    """
    if not name.startswith(f"{ROOT_NAMESPACE}.") and name != ROOT_NAMESPACE:
        name = f"{ROOT_NAMESPACE}.{name}"
    return logging.getLogger(name)


def get_log_files_info() -> List[Dict[str, str]]:
    """
    Utility function to inspect active log files and their sizes.
    """
    logs_dir = get_logs_directory()
    info = []
    for filepath in logs_dir.glob("*.log*"):
        info.append({
            "name": filepath.name,
            "path": str(filepath),
            "size_kb": f"{filepath.stat().st_size / 1024:.1f} KB",
        })
    return info
