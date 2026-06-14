"""Structured JSON logging + optional App Insights."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    """
    Emits one JSON object per log line.
    All fields are accessed by name in consumers (e.g. customDimensions.service in KQL)
    so adding new fields here is non-breaking.
    """

    def __init__(self, service_name: str = "") -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "time":    datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "service": self._service_name,
            "logger":  record.name,
            "msg":     record.getMessage(),
        }
        if record.exc_info:
            obj["exception"] = self.formatException(record.exc_info)
        for k in ("task_id", "doc_name", "domain", "chunk_count", "chunk_id"):
            if hasattr(record, k):
                obj[k] = getattr(record, k)
        return json.dumps(obj)


def configure_logging(service_name: str = "ingestion") -> None:
    from shared.config import settings
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter(service_name))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    for noisy in ("azure.core", "azure.identity", "httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if settings.APPLICATIONINSIGHTS_CONNECTION_STRING:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            configure_azure_monitor(
                connection_string=settings.APPLICATIONINSIGHTS_CONNECTION_STRING,
                service_name=service_name,
            )
        except ImportError:
            logging.getLogger(__name__).warning(
                "APPLICATIONINSIGHTS_CONNECTION_STRING is set but azure-monitor-opentelemetry "
                "is not installed — telemetry disabled. "
                "Run: pip install azure-monitor-opentelemetry"
            )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
