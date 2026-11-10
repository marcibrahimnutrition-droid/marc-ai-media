"""Weekly token health check + refresh (GitHub Actions).

- Page token from a long-lived user token: never expires -> just verify it is valid.
- User token: if it expires within 14 days, exchange it for a new long-lived one and write it
  back to the repo secret IG_ACCESS_TOKEN (needs GH_PAT secret with 'secrets: write' on this repo).
Exits 1 when the token is invalid or about to expire and cannot be refreshed -> GitHub emails the owner.
"""
from __future__ import annotations

import base64
import os
import sys
import time

import requests

GRAPH = f"https://graph.facebook.com/{os.getenv('GRAPH_API_VERSION', 'v24.0')}"
# .strip(): a value pasted into a GitHub secret often carries an invisible space or line break
APP_ID, SECRET = os.environ.get("META_APP_ID", "").strip(), os.environ.get("META_APP_SECRET", "").strip()
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "").strip()


def update_secret(name: str, value: str) -> None:
    from nacl import encoding, public
    repo, pat = os.environ["GITHUB_REPOSITORY"], os.environ.get("GH_PAT", "")
    if not pat:
        sys.exit("Token needs refresh but GH_PAT secret is not set.")
    h = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    key = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key", headers=h, timeout=30).json()
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    enc = base64.b64encode(box.encrypt(value.encode())).decode()
    r = requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/{name}", headers=h,
                     json={"encrypted_value": enc, "key_id": key["key_id"]}, timeout=30)
    r.raise_for_status()


def main() -> None:
    resp = requests.get(f"{GRAPH}/debug_token", params={"input_token": TOKEN, "access_token": f"{APP_ID}|{SECRET}"},
                        timeout=30).json()
    d = resp.get("data", {})
    if not d.get("is_valid"):
        # lengths only (never values) so a wrongly pasted secret can be spotted; GitHub also masks secret values
        lens = {k: len(os.environ.get(k, "").strip()) for k in ("META_APP_ID", "META_APP_SECRET", "IG_ACCESS_TOKEN")}
        why = (resp.get("error") or d.get("error") or {}).get("message", "no reason given")
        sys.exit(f"TOKEN INVALID: {why} | secret lengths {lens} -> check the secrets (or re-run scripts/get_token.py).")
    exp = d.get("expires_at") or 0
    days = (exp - time.time()) / 86400 if exp else None
    print(f"Token type={d.get('type')} valid=True expires_in_days={'never' if days is None else round(days, 1)}")
    if days is None or days > 14:
        return
    if d.get("type") != "USER":
        sys.exit("Page token expiring — re-run scripts/get_token.py on the PC and update the secret.")
    r = requests.get(f"{GRAPH}/oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": APP_ID,
                                                          "client_secret": SECRET, "fb_exchange_token": TOKEN},
                     timeout=30).json()
    if "access_token" not in r:
        sys.exit(f"Refresh failed: {r}")
    update_secret("IG_ACCESS_TOKEN", r["access_token"])
    print("Token refreshed and secret updated.")


if __name__ == "__main__":
    main()
