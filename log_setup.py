"""One readable log for the bot, set up once at startup.

Line format:  HH:MM:SS.mmm LEVEL [category] plain sentence
Goes to stdout (docker compose logs) and to data/logs/bot_<YYYY-MM-DD_HH-MM-SS>.log,
keeping the newest 10 files. DEBUG lines appear only when LOG_LEVEL=DEBUG.
"""
import datetime
import glob
import logging
import os
import sys

LOG_DIR = os.path.join("data", "logs")
KEEP_FILES = 10
SECRET_ENV_VARS = ("DISCORD_TOKEN", "URL_AUTH_USERNAME", "URL_AUTH_PASSWORD")

_LEVEL_NAMES = {"WARNING": "WARN", "CRITICAL": "ERROR"}


class _Format(logging.Formatter):
    """HH:MM:SS.mmm LEVEL [category] message, with a traceback after the line on errors."""

    def format(self, record):
        when = datetime.datetime.fromtimestamp(record.created)
        level = _LEVEL_NAMES.get(record.levelname, record.levelname)
        text = f"{when:%H:%M:%S}.{int(record.msecs):03d} {level:<5} [{record.name.split('.')[0]}] {record.getMessage()}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


class _Redact(logging.Filter):
    """Safety net: blank out the token and tracker login if one ever ends up in a message."""

    def filter(self, record):
        message = record.getMessage()
        for name in SECRET_ENV_VARS:
            value = os.getenv(name)
            if value and len(value) >= 4 and value in message:
                message = message.replace(value, "***")
        record.msg, record.args = message, None
        if record.exc_info:
            record.exc_text = None
        return True


def _prune(folder):
    old = sorted(glob.glob(os.path.join(folder, "bot_*.log")))
    for path in old[:-(KEEP_FILES - 1)]:  # room for the new file
        try:
            os.remove(path)
        except OSError:
            pass


def setup():
    """Install the handlers on the root logger. Returns the log file path, or None if it can't be written."""
    level = getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").strip().upper(), logging.INFO)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)
    formatter, redact = _Format(), _Redact()

    handlers = [logging.StreamHandler(sys.stdout)]
    path = None
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _prune(LOG_DIR)
        path = os.path.join(LOG_DIR, f"bot_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.log")
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    except OSError as e:
        print(f"Could not open a log file in {LOG_DIR}: {e}", file=sys.stderr)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redact)
        root.addHandler(handler)

    # Library chatter (discord, websockets, urllib3) stays at WARNING unless DEBUG is asked for.
    if level > logging.DEBUG:
        for noisy in ("discord", "websockets", "urllib3", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def get(category):
    """A logger whose name is the category shown in brackets, e.g. get('feed')."""
    return logging.getLogger(category)


class Outage:
    """Tracks a repeating job (a poll loop) so each failure and the recovery are logged.

    The first failure of a run gets a traceback at ERROR; further failures are one WARN line
    with a count; the first success afterwards logs the recovery.
    """

    def __init__(self, logger, what):
        self.logger, self.what, self.failures = logger, what, 0

    def failed(self, exc):
        self.failures += 1
        if self.failures == 1:
            self.logger.error("%s failed, will retry: %s", self.what, exc, exc_info=exc)
        else:
            self.logger.warning("%s still failing (attempt %s): %s", self.what, self.failures, exc)

    def ok(self):
        if self.failures:
            self.logger.info("%s recovered after %s failed attempts", self.what, self.failures)
            self.failures = 0
