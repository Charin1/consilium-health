import json
from app.utils.logger import get_logs_directory

def test_client_otel_log_ingestion():
    logs_dir = get_logs_directory("frontend")
    log_file = logs_dir / "frontend.log"

    sample_otel_record = {
        "timestamp": "2026-08-05T23:08:00.000Z",
        "severity_text": "ERROR",
        "severity_number": 17,
        "service.name": "consilium-frontend",
        "logger.name": "consilium.frontend",
        "body": "Uncaught UI Exception test",
        "attributes": {
            "page.url": "http://localhost:3000/",
            "user_agent": "Mozilla/5.0 Test"
        }
    }

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(sample_otel_record, ensure_ascii=False) + "\n")

    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").strip().split("\n") if line.strip().startswith("{")]
    assert any(rec["body"] == "Uncaught UI Exception test" for rec in lines)
