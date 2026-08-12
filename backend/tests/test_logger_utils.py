import json
import logging
from pathlib import Path
from app.utils.logger import get_logger, get_logs_directory, setup_logging

def test_logger_utility_initialization(tmp_path):
    temp_logs_dir = tmp_path / "logs"
    logger = setup_logging("DEBUG", logs_dir=temp_logs_dir, log_format="otel")
    assert logger.name == "consilium"

    # Verify log files created
    assert temp_logs_dir.exists()
    main_log = temp_logs_dir / "backend.log"
    error_log = temp_logs_dir / "backend_error.log"
    assert main_log.exists()
    assert error_log.exists()

    # Log messages
    logger.info("Test info message")
    logger.error("Test error message")

    main_lines = [json.loads(line) for line in main_log.read_text(encoding="utf-8").strip().split("\n") if line.strip()]
    error_lines = [json.loads(line) for line in error_log.read_text(encoding="utf-8").strip().split("\n") if line.strip()]

    # Verify OTel JSON fields
    assert any(record["body"] == "Test info message" for record in main_lines)
    assert any(record["body"] == "Test error message" for record in main_lines)
    assert not any(record["body"] == "Test info message" for record in error_lines)
    assert any(record["body"] == "Test error message" for record in error_lines)
    assert main_lines[0]["service.name"] == "consilium-backend"
    assert "timestamp" in main_lines[0]
    assert "severity_number" in main_lines[0]

def test_get_logger_namespacing():
    chat_logger = get_logger("chat")
    assert chat_logger.name == "consilium.chat"

    already_namespaced = get_logger("consilium.orchestrator")
    assert already_namespaced.name == "consilium.orchestrator"


def test_child_loggers_inherit_the_configured_root():
    """
    get_logger's prefix must match the namespace setup_logging configures.
    If they drift, children stop inheriting handlers and their output silently
    disappears - which is exactly what a careless rename causes.
    """
    from app.utils.logger import ROOT_NAMESPACE, get_logger

    child = get_logger("some_service")
    assert child.name == f"{ROOT_NAMESPACE}.some_service"
    assert child.name.startswith(f"{ROOT_NAMESPACE}.")
    # Walking up the logging hierarchy must reach the configured root.
    assert logging.getLogger(ROOT_NAMESPACE) is child.parent
