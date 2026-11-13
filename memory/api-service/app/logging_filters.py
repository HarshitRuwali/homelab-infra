"""Logging filters that keep successful health probes out of the logs."""

import logging

HEALTH_PATH = "/health"


class HealthAccessFilter(logging.Filter):
    """Drop uvicorn access records for successful ``GET /health`` requests.

    uvicorn.access records carry ``args = (client, method, path, http_version,
    status_code)``. Failed probes (status >= 400) and every other path are kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path = str(args[2]).split("?", 1)[0]
        status = args[4]
        return not (path == HEALTH_PATH and isinstance(status, int) and status < 400)
