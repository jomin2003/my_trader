"""
DHAN TOKEN AUTO-REFRESHER (FIXED)

Dhan access tokens expire every 24 hours. This module automates refresh
via TOTP endpoint so your bot never authenticates manually.

Environment variables required:
    DHAN_CLIENT_ID
    DHAN_PIN
    DHAN_TOTP_SECRET
    DATA_DIR (optional; default: ./data)
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
    pyotp = None

IST = ZoneInfo("Asia/Kolkata")

DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FP = DATA_DIR / ".dhan_token.json"

GENERATE_URL = "https://auth.dhan.co/app/generateAccessToken"
RENEW_URL    = "https://api.dhan.co/v2/RenewToken"

REFRESH_IF_LESS_THAN_HOURS = 4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dhan_token")


@dataclass
class TokenState:
    access_token: str
    client_id:    str
    expiry_time:  str
    generated_at: str

    @property
    def expires_at_ist(self):
        """Parse Dhan's expiryTime string to an IST-aware datetime."""
        if not self.expiry_time:
            return None
        try:
            s = self.expiry_time.replace("Z", "")
            if "." in s:
                s = s.split(".")[0]
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return dt.astimezone(IST)
        except Exception:
            try:
                gen = datetime.fromisoformat(self.generated_at)
                if gen.tzinfo is None:
                    gen = gen.replace(tzinfo=IST)
                return gen + timedelta(hours=24)
            except Exception:
                return None

    @property
    def hours_left(self) -> float:
        exp = self.expires_at_ist
        if not exp:
            return 0.0
        return max(0.0, (exp - datetime.now(IST)).total_seconds() / 3600.0)

    def is_valid(self) -> bool:
        return self.hours_left > 0.1


def load_token():
    if not TOKEN_FP.exists():
        return None
    try:
        d = json.loads(TOKEN_FP.read_text())
        return TokenState(**d)
    except Exception as e:
        log.warning(f"Token file unreadable: {e}")
        return None


def save_token(st: TokenState):
    TOKEN_FP.write_text(json.dumps(st.__dict__, indent=2))
    try:
        os.chmod(TOKEN_FP, 0o600)
    except Exception:
        pass


def _current_totp() -> str:
    if pyotp is None:
        raise SystemExit("pyotp not installed. Run: pip install pyotp")
    secret = os.getenv("DHAN_TOTP_SECRET", "").strip().replace(" ", "")
    if not secret:
        raise SystemExit("DHAN_TOTP_SECRET env var is not set.")
    try:
        return pyotp.TOTP(secret).now()
    except Exception as e:
        raise SystemExit(f"TOTP generation failed: {e}")


def _generate_via_totp() -> TokenState:
    client_id = os.getenv("DHAN_CLIENT_ID", "").strip()
    pin       = os.getenv("DHAN_PIN", "").strip()
    if not client_id or not pin:
        raise SystemExit("DHAN_CLIENT_ID and DHAN_PIN env vars required.")

    totp = _current_totp()
    log.info(f"Generating fresh token via TOTP for client {client_id[:4]}****")

    try:
        r = requests.post(
            GENERATE_URL,
            params={"dhanClientId": client_id, "pin": pin, "totp": totp},
            timeout=15,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Network error: {e}")

    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"Non-JSON response: {r.text[:200]}")

    tok = data.get("accessToken")
    if not tok:
        raise RuntimeError(f"No accessToken in response: {data}")

    st = TokenState(
        access_token=tok,
        client_id=client_id,
        expiry_time=str(data.get("expiryTime", "")),
        generated_at=datetime.now(IST).isoformat(),
    )
    log.info(f"NEW token acquired. Expires: {st.expires_at_ist} ({st.hours_left:.1f}h left)")
    return st


def _renew_existing(current: TokenState):
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

    tok = data.get("accessToken")
    if not tok:
        return None

    st = TokenState(
        access_token=tok,
        client_id=current.client_id,
        expiry_time=str(data.get("expiryTime", "")),
        generated_at=datetime.now(IST).isoformat(),
    )
    log.info(f"Token RENEWED. Expires: {st.expires_at_ist} ({st.hours_left:.1f}h left)")
    return st


def refresh(force: bool = False, prefer_renew: bool = True) -> TokenState:
    if not force:
        cached = load_token()
        if cached and cached.is_valid() and cached.hours_left > REFRESH_IF_LESS_THAN_HOURS:
            log.info(f"Cached token valid ({cached.hours_left:.1f}h left). Skipping refresh.")
            return cached

        if prefer_renew and cached and cached.is_valid():
            renewed = _renew_existing(cached)
            if renewed:
                save_token(renewed)
                os.environ["DHAN_ACCESS_TOKEN"] = renewed.access_token
                return renewed

    fresh = _generate_via_totp()
    save_token(fresh)
    os.environ["DHAN_ACCESS_TOKEN"] = fresh.access_token
    return fresh


def ensure_valid_token(force: bool = False) -> str:
    env_tok = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
    if not force and env_tok:
        cached = load_token()
        if (cached and cached.access_token == env_tok
                and cached.hours_left > REFRESH_IF_LESS_THAN_HOURS):
            return env_tok

    st = refresh(force=force)
    return st.access_token


def _cmd_show():
    st = load_token()
    if not st:
        print("No cached token. Run: python dhan_token_manager.py refresh")
        return
    exp = st.expires_at_ist
    print(f"Client ID:    {st.client_id[:4]}****")
    print(f"Token prefix: {st.access_token[:12]}...")
    print(f"Generated at: {st.generated_at}")
    print(f"Expires at:   {exp}")
    print(f"Hours left:   {st.hours_left:.2f}")
    print(f"Valid now:    {st.is_valid()}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh")
    r.add_argument("--force", action="store_true")
    r.add_argument("--no-renew", action="store_true")

    sub.add_parser("show")

    args = ap.parse_args()
    if args.cmd == "refresh":
        st = refresh(force=args.force, prefer_renew=not args.no_renew)
        print("=" * 55)
        print(f"  Token acquired.")
        print(f"  Expires: {st.expires_at_ist} ({st.hours_left:.1f}h left)")
        print(f"  Saved to: {TOKEN_FP}")
        print("=" * 55)
    elif args.cmd == "show":
        _cmd_show()


if __name__ == "__main__":
    main()
