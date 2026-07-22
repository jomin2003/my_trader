"""
=====================================================================
 GITHUB GIST STORAGE for Render free tier (ephemeral filesystem)
---------------------------------------------------------------------
 Problem:
   Render free tier has NO persistent disk. Every cold start wipes
   live_config.json, state.json, and any saved trade blotters.

 Solution:
   Use a PRIVATE GitHub Gist as free key-value storage.
   - Small files (< 100 KB each), API-accessible, unlimited restores.
   - Free with any GitHub account.
   - Not for bulk data (CSVs) — those get re-downloaded from yfinance
     on every cold start via /trigger/weekly.

 Files backed up:
   * live_config.json    (winning config from param_sweep)

 Environment variables:
   GITHUB_TOKEN     personal access token with "gist" scope
   GITHUB_GIST_ID   id of your PRIVATE gist (created once, manually)

 One-time setup:
   1. Go to https://gist.github.com → "New gist" → mark as SECRET
   2. Filename: live_config.json  Content: {}
   3. Create secret gist. URL is https://gist.github.com/<user>/<GIST_ID>
   4. Copy the GIST_ID (long hex string) to Render env vars
   5. Generate a fine-grained PAT with "Gists: read/write" scope
   6. Copy the token to Render as GITHUB_TOKEN

 Usage in app.py:
   from gist_storage import restore_from_gist, backup_to_gist
   restore_from_gist(BASE_DIR)                    # at boot
   backup_to_gist(BASE_DIR, files=["live_config.json"])  # after promote
=====================================================================
"""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import requests

log = logging.getLogger("gist")

GIST_API = "https://api.github.com/gists"


def _headers() -> dict:
    tok = os.getenv("GITHUB_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("GITHUB_TOKEN env var not set")
    return {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gist_id() -> str:
    gid = os.getenv("GITHUB_GIST_ID", "").strip()
    if not gid:
        raise RuntimeError("GITHUB_GIST_ID env var not set")
    return gid


def restore_from_gist(base_dir: Path) -> int:
    """Download all files in the gist to base_dir. Returns count restored."""
    try:
        r = requests.get(f"{GIST_API}/{_gist_id()}",
                         headers=_headers(), timeout=10)
        if r.status_code != 200:
            log.warning(f"Gist fetch HTTP {r.status_code}: {r.text[:200]}")
            return 0
        files = r.json().get("files", {})
    except RuntimeError as e:
        log.info(f"Gist restore skipped: {e}")
        return 0
    except Exception as e:
        log.warning(f"Gist fetch error: {e}")
        return 0

    n = 0
    for fname, meta in files.items():
        content = meta.get("content", "")
        if not content or content.strip() in ("", "{}", "null"):
            continue
        try:
            (base_dir / fname).write_text(content)
            log.info(f"Restored {fname} from Gist ({len(content)} bytes)")
            n += 1
        except Exception as e:
            log.warning(f"Failed to write {fname}: {e}")
    return n


def backup_to_gist(base_dir: Path, files: list[str]) -> bool:
    """Upload the specified files (must exist locally) to the gist."""
    try:
        payload_files = {}
        for f in files:
            fp = base_dir / f
            if fp.exists():
                payload_files[f] = {"content": fp.read_text()}
            else:
                log.warning(f"Backup: {f} not found locally, skipping")
        if not payload_files:
            return False

        r = requests.patch(
            f"{GIST_API}/{_gist_id()}",
            headers=_headers(),
            json={"files": payload_files},
            timeout=10,
        )
        if r.status_code in (200, 201):
            log.info(f"Backed up {list(payload_files.keys())} to Gist")
            return True
        log.error(f"Gist patch HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        log.error(f"Gist backup error: {e}")
        return False


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    base = Path(__file__).parent
    if len(sys.argv) < 2:
        print("Usage: python gist_storage.py [restore|backup <file1> <file2> ...]")
        sys.exit(1)
    if sys.argv[1] == "restore":
        n = restore_from_gist(base)
        print(f"Restored {n} file(s)")
    elif sys.argv[1] == "backup":
        ok = backup_to_gist(base, sys.argv[2:])
        print("OK" if ok else "FAILED")
