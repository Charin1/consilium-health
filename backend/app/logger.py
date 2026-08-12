"""
Backwards-compatible logger wrapper re-exporting app.utils.logger
"""
from app.utils.logger import get_log_files_info, get_logger, get_logs_directory, setup_logging

__all__ = ["setup_logging", "get_logger", "get_logs_directory", "get_log_files_info"]
