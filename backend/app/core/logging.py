import logging
import sys

from app.core.config import settings
from app.core.request_context import REQUEST_ID_PLACEHOLDER, get_request_id

REQUEST_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or REQUEST_ID_PLACEHOLDER
        return True


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format=REQUEST_LOG_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    request_id_filter = RequestIdFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(request_id_filter)

    # Provider validation requests can carry Meta access tokens in query strings.
    # Keep third-party HTTP client request logging above INFO so credentials never
    # appear in application logs while preserving our own application logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
