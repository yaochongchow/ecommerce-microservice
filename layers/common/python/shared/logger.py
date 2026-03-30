"""
Structured JSON logger with correlation ID propagation.

Every log entry includes:
  - correlation_id: Ties together all logs for a single user request across services.
    Injected by the BFF Lambda (M1) and propagated via EventBridge event metadata.
  - service: Which microservice emitted the log (e.g., "order-service").
  - timestamp, level, message: Standard log fields.

Usage:
    from shared.logger import get_logger
    logger = get_logger("order-service")
    logger.set_correlation_id("abc-123")
    logger.info("Order created", order_id="ord_001", total=59.99)
    # Output: {"timestamp": "...", "level": "INFO", "service": "order-service",
    #          "correlation_id": "abc-123", "message": "Order created",
    #          "order_id": "ord_001", "total": 59.99}
"""

import json
import logging
import sys
from datetime import datetime, timezone


class StructuredLogger:
    """JSON logger that attaches service name and correlation ID to every entry.

    Designed for CloudWatch Logs — each log line is a single JSON object,
    making it easy to query with CloudWatch Insights.
    """

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.correlation_id = None

        # Configure the underlying Python logger to write to stdout
        # (Lambda sends stdout to CloudWatch automatically)
        self._logger = logging.getLogger(service_name)
        self._logger.setLevel(logging.INFO)

        # Avoid duplicate handlers if get_logger is called multiple times
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def set_correlation_id(self, correlation_id: str):
        """Set the correlation ID for the current request/event.

        Called at the start of each Lambda invocation with the ID from
        the API Gateway header or EventBridge event metadata.
        """
        self.correlation_id = correlation_id

    def _log(self, level: str, message: str, **kwargs):
        """Build a structured JSON log entry and emit it.

        Args:
            level: Log level string (INFO, ERROR, WARN, DEBUG).
            message: Human-readable log message.
            **kwargs: Additional key-value pairs to include (e.g., order_id, amount).
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "service": self.service_name,
            "correlation_id": self.correlation_id,
            "message": message,
            # Spread any extra context into the log entry
            **kwargs,
        }
        self._logger.info(json.dumps(entry, default=str))

    def info(self, message: str, **kwargs):
        self._log("INFO", message, **kwargs)

    def error(self, message: str, **kwargs):
        self._log("ERROR", message, **kwargs)

    def warn(self, message: str, **kwargs):
        self._log("WARN", message, **kwargs)

    def debug(self, message: str, **kwargs):
        self._log("DEBUG", message, **kwargs)


def get_logger(service_name: str) -> StructuredLogger:
    """Factory function to create a logger for a given service.

    Args:
        service_name: Identifier for the microservice (e.g., "order-service").

    Returns:
        A StructuredLogger instance configured for that service.
    """
    return StructuredLogger(service_name)
