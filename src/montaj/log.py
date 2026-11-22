"""File logging that rotates and does not keep API keys."""

import logging
from logging.handlers import RotatingFileHandler

from montaj.config import Settings

_LOG_BYTES = 1_000_000
_LOG_BACKUPS = 5


class RedactSecrets(logging.Filter):
    """Replace configured API key values. Transcript text is never logged."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if len(secret) >= 8)

    def filter(self, record: logging.LogRecord) -> bool:
        redacted = record.getMessage()
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[redacted]")
        record.msg = redacted
        record.args = ()
        return True


def configure_logging(settings: Settings) -> None:
    """Attach a rotating log file. Skip quietly when the data directory cannot be used."""
    logger = logging.getLogger("montaj")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    target = settings.data_dir / "logs" / "montaj.log"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            target,
            maxBytes=_LOG_BYTES,
            backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError:
        return
    secrets: list[str] = []
    for key in (settings.deepgram_api_key, settings.anthropic_api_key):
        if key is not None:
            secrets.append(key.get_secret_value())
    handler.addFilter(RedactSecrets(tuple(secrets)))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    logger.addHandler(handler)
