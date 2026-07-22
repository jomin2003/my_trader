"""
=====================================================================
 DHAN TOKEN AUTO-REFRESHER
---------------------------------------------------------------------
 Dhan access tokens expire every 24 hours (SEBI mandate). This module
 automates the refresh so your live bot never authenticates manually.

 Uses Dhan's official TOTP endpoint (no browser automation, works
 headlessly on any cloud):

     POST https://auth.dhan.co/app/generateAccessToken
          ?dhanClientId=<id>&pin=<pin>&totp=<code>

 Fallback: if the current token is still valid, tries the cheaper
 RenewToken endpoint (extends by another 24h).

 Storage:
   Persists the fresh token to <DATA_DIR>/.dhan_token.json so it
   survives scheduler restarts. Also updates os.environ so any
   subprocess launched afterwards inherits the fresh token.

 Environment variables required (set on Render dashboard):
   DHAN_CLIENT_ID          your Dhan client id
   DHAN_PIN                your 6-digit Dhan PIN
   DHAN_TOTP_SECRET        base32 secret from Dhan's QR code
   DATA_DIR                (optional) where to persist .dhan_token.json

 Usage:
   # Standalone (cron/manual refresh):
   python dhan_token_manager.py refresh
   python dhan_token_manager.py show

   # Programmatic (called from scheduler.py):
   from dhan_token_manager import ensure_valid_token
   token = ensure_valid_token()
=====================================================================
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:
    import pyotp
except ImportError:
    pyotp = None   # module still loads; will raise clearly at refresh time

# =====================================================================
# CONFIG
# =====================================================================
IST = ZoneInfo("Asia/Kolkata")

DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FP = DATA_DIR / ".dhan_token.json"

GENERATE_URL = "https://auth.dhan.co/app/generateAccessToken"
RENEW_URL    = "https://api.dhan.co/v2/RenewToken"

# Refresh proactively when token has less than this many hours left
REFRESH_IF_LESS_THAN_HOURS = 4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dhan_token")


# =====================================================================
# TOKEN STATE
# =====================================================================
@dataclass
class TokenState:
    access_token: str
    client_id:    str
    expiry_time:  str        # ISO string as returned by Dhan (naive)
    generated_at: str        # our own IST timestamp

    @property
    def expires_at_ist(self) -> datetime | None:
        """Best-effort parse of Dhan's expiryTime; assume IST if naive."""
        try:
            # Dhan returns e.g. "2026-01-02T09:15:00.000"
            iso = self.expiry_time.rstrip("Z").split(".")[0]
            dt  = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return dt.astimezone(IST)
        except Exception:
            # Fallback: assume 24h from generation
            try:
                gen = datetime.fromisoformat(self.generated_at)
                return gen + timedelta(hours=24)
            except Exception:
                return None

    @property
    def hours_left(self) -> float:
        exp = self.expires_at_ist
        if exp is None:
            return 0.0
        return max(0.0, (exp - datetime.now(IST)).total_seconds() / 3600.0)

    def is_valid(self) -> bool:
        return self.access_token and self.hours_left > 0


def load_token() -> TokenState | None:
    if not TOKEN_FP.exists():
        return None
    try:
        d = json.loads(TOKEN_FP.read_text())
        return TokenState(**d)
    except Exception as e:
        log.warning(f"token file unreadable: {e}")
        return None


def save_token(st: TokenState) -> None:
    TOKEN_FP.write_text(json.dumps(st.__dict__, indent=2))
    # Also secure file perms (best-effort; may fail on some FS)
    try:
        os.chmod(TOKEN_FP, 0o600)
    except Exception:
        pass


# =====================================================================
# TOTP GENERATION
# =====================================================================
def _current_totp() -> str:
    if pyotp is None:
        raise SystemExit("pyotp not installed. Run: pip install pyotp")
    secret = os.getenv("DHAN_TOTP_SECRET", "").strip().replace(" ", "")
    if not secret:
        raise SystemExit("DHAN_TOTP_SECRET env var is not set.")
    try:
        return pyotp.TOTP(secret).now()
    except Exception as e:
        raise SystemExit(f"TOTP generation failed (check DHAN_TOTP_SECRET is valid base32): {e}")


# =====================================================================
# REFRESH PATHS
# =====================================================================
def _generate_via_totp() -> TokenState:
    """Primary path: fresh token via TOTP. Always works if creds are valid."""
    client_id = os.getenv("DHAN_CLIENT_ID", "").strip()
    pin       = os.getenv("DHAN_PIN", "").strip()
    if not client_id or not pin:
        raise SystemExit("DHAN_CLIENT_ID and DHAN_PIN env vars are required.")

    totp = _current_totp()
    log.info(f"Requesting fresh token via TOTP for client_id={client_id[:4]}****")

    try:
        r = requests.post(
            GENERATE_URL,
            params={"dhanClientId": client_id, "pin": pin, "totp": totp},
            timeout=15,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"generateAccessToken network error: {e}")

    if r.status_code != 200:
        raise RuntimeError(f"generateAccessToken HTTP {r.status_code}: {r.text[:400]}")

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"generateAccessToken non-JSON response: {r.text[:400]}")

    tok = data.get("accessToken")
    if not tok:
        raise RuntimeError(f"generateAccessToken returned no accessToken: {data}")

    st = TokenState(
        access_token=tok,
        client_id=client_id,
        expiry_time=str(data.get("expiryTime", "")),
        generated_at=datetime.now(IST).isoformat(),
    )
    log.info(f"NEW token acquired. Expires: {st.expires_at_ist} ({st.hours_left:.1f}h left)")
    return st


def _renew_existing(current: TokenState) -> TokenState | None:
    """Cheaper: if current token is still active, extend it by 24h.
    Returns None if renewal is not eligible / fails; caller should fall back."""
    if not current.is_valid():
        return None
    try:
        r = requests.get(
            RENEW_URL,
            headers={"access-token": current.access_token,
                     "dhanClientId": current.client_id},
            timeout=15,
        )
    except requests.RequestException as e:
        log.info(f"RenewToken network error: {e}")
        return None

    if r.status_code != 200:
        log.info(f"RenewToken HTTP {r.status_code}: {r.text[:200]}")
        return None

    try:
        data = r.json()
    except Exception:
        return None

    new_tok = data.get("accessToken")
    if not new_tok:
        return None

    st = TokenState(
        access_token=new_tok,
        client_id=current.client_id,
        expiry_time=str(data.get("expiryTime", "")),
        generated_at=datetime.now(IST).isoformat(),
    )
    log.info(f"Token RENEWED. Expires: {st.expires_at_ist} ({st.hours_left:.1f}h left)")
    return st


# =====================================================================
# PUBLIC API
# =====================================================================
def refresh(force: bool = False, prefer_renew: bool = True) -> TokenState:
    """
    Get a fresh (or freshly-renewed) token.
      * force=True         : always call TOTP endpoint, ignore cache
      * prefer_renew=True  : if cached token still valid, try Renew first
    """
    if not force:
        cached = load_token()
        if cached and cached.is_valid() and cached.hours_left > REFRESH_IF_LESS_THAN_HOURS:
            log.info(f"Cached token still valid ({cached.hours_left:.1f}h left). Skipping refresh.")
            return cached

        # Try cheap renewal first
        if prefer_renew and cached and cached.is_valid():
            renewed = _renew_existing(cached)
            if renewed is not None:
                save_token(renewed)
                os.environ["DHAN_ACCESS_TOKEN"] = renewed.access_token
                return renewed

    # Full TOTP generation
    fresh = _generate_via_totp()
    save_token(fresh)
    os.environ["DHAN_ACCESS_TOKEN"] = fresh.access_token
    return fresh


def ensure_valid_token(force: bool = False) -> str:
    """
    Guarantees os.environ['DHAN_ACCESS_TOKEN'] holds a valid token.
    Called from scheduler.py at boot and daily at 08:30 IST.
    Returns the token string.
    """
    # If env var already set AND we have a fresh cached matching it, trust it
    env_tok = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
    if not force and env_tok:
        cached = load_token()
        if (cached and cached.access_token == env_tok
                and cached.hours_left > REFRESH_IF_LESS_THAN_HOURS):
            return env_tok

    st = refresh(force=force)
    return st.access_token


# =====================================================================
# CLI
# =====================================================================
def _cmd_show():
    st = load_token()
    if not st:
        print("No cached token. Run: python dhan_token_manager.py refresh")
        return
    exp = st.expires_at_ist
    print(f"Client ID:    {st.client_id[:4]}****{st.client_id[-2:] if len(st.client_id) > 6 else ''}")
    print(f"Token prefix: {st.access_token[:12]}...")
    print(f"Generated at: {st.generated_at}")
    print(f"Expires at:   {exp}")
    print(f"Hours left:   {st.hours_left:.2f}")
    print(f"Valid now:    {st.is_valid()}")


def main():
    ap = argparse.ArgumentParser(description="Dhan token auto-refresher")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="Refresh token (or renew if still active)")
    r.add_argument("--force", action="store_true",
                   help="Skip cache + skip renew; always call TOTP endpoint")
    r.add_argument("--no-renew", action="store_true",
                   help="Skip the RenewToken shortcut")

    sub.add_parser("show", help="Show current cached token status")

    args = ap.parse_args()

    if args.cmd == "refresh":
        st = refresh(force=args.force, prefer_renew=not args.no_renew)
        print("=" * 55)
        print(f"  Token acquired.")
        print(f"  Expires: {st.expires_at_ist}  ({st.hours_left:.1f}h left)")
        print(f"  Saved to: {TOKEN_FP}")
        print("=" * 55)
    elif args.cmd == "show":
        _cmd_show()


if __name__ == "__main__":
    main()
