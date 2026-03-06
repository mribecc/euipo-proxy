#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
source .venv/bin/activate

python3 - <<'PY'
import os, httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

url = os.getenv("EUIPO_AUTH_URL")
key = os.getenv("EUIPO_API_KEY")
secret = os.getenv("EUIPO_API_SECRET")
scope = os.getenv("EUIPO_SCOPE", "trademark-search.trademarks.read")

r = httpx.post(
    url,
    data={
        "grant_type": "client_credentials",
        "client_id": key,
        "client_secret": secret,
        "scope": scope,
    },
    headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    },
    timeout=30,
)

print("STATUS:", r.status_code)
print(r.text[:500])
PY