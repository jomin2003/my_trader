"""logging_config.py — Phase 10: secret-redacting log filter."""
from __future__ import annotations
import logging, os, re

SECRET_ENVS = [
    "DHAN_ACCESS_TOKEN",
    "DHAN_PIN",
    "DHAN_TOTP_SECRET",
    "TG_BOT_TOKEN",
    "TG_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
    "GIST_TOKEN",
    "CRON_SECRET",
]
_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}"),
]


class RedactionFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self._static_values = [
            os.getenv(k, "") for k in SECRET_ENVS if len(os.getenv(k, "")) >= 6
        ]

    def _current_values(self):
        vals = set(self._static_values)
        for k in SECRET_ENVS:
            v = os.getenv(k, "")
            if len(v) >= 6:
                vals.add(v)
        return vals

    def filter(self, record):
        try:
            msg = record.getMessage()
            for v in self._current_values():
                if v and v in msg:
                    msg = msg.replace(v, "***REDACTED***")
            for pat in _PATTERNS:
                msg = pat.sub("***REDACTED***", msg)
            record.msg = msg
            record.args = ()
        except Exception:
            pass
        return True


def install(logger=None):
    lg = logger or logging.getLogger()
    flt = RedactionFilter()
    if not any(isinstance(f, RedactionFilter) for f in lg.filters):
        lg.addFilter(flt)
    for handler in getattr(lg, "handlers", []):
        if not any(isinstance(f, RedactionFilter) for f in handler.filters):
            handler.addFilter(flt)
    return lg

