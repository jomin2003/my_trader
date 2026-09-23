"""logging_config.py — Phase 10: secret-redacting log filter."""
from __future__ import annotations
import logging,os,re
SECRET_ENVS=["DHAN_ACCESS_TOKEN","DHAN_PIN","DHAN_TOTP_SECRET","TG_BOT_TOKEN","GITHUB_TOKEN","GIST_TOKEN","CRON_SECRET"]
_PATTERNS=[re.compile(r"ghp_[A-Za-z0-9]{20,}"),re.compile(r"[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}")]
class RedactionFilter(logging.Filter):
    def __init__(self):
        super().__init__(); self._values=[os.getenv(k,"") for k in SECRET_ENVS if len(os.getenv(k,""))>=6]
    def filter(self,record):
        try:
            msg=record.getMessage()
            for v in self._values:
                if v and v in msg: msg=msg.replace(v,"***REDACTED***")
            for pat in _PATTERNS: msg=pat.sub("***REDACTED***",msg)
            record.msg=msg; record.args=()
        except Exception: pass
        return True
def install(logger=None):
    lg=logger or logging.getLogger(); lg.addFilter(RedactionFilter()); return lg
