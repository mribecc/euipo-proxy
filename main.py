import os
import time
from typing import Optional, List

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

EUIPO_CLIENT_ID = os.getenv("EUIPO_CLIENT_ID", "").strip()
EUIPO_CLIENT_SECRET = os.getenv("EUIPO_CLIENT_SECRET", "").strip()

AUTH_URL = "https://auth-sandbox.euipo.europa.eu/oidc/accessToken"
API_BASE = "https://api-sandbox.euipo.europa.eu"

app = FastAPI(title="EUIPO Proxy", version="1.0.0")

_token_cache = {"token": None, "exp": 0}


class EuipoSearchRequest(BaseModel):
    text: str
    niceClasses: Optional[List[int]] = None
    page: int = 0
    size: int = 10


def _require_env():
    if not EUIPO_CLIENT_ID or not EUIPO_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Missing EUIPO_CLIENT_ID / EUIPO_CLIENT_SECRET env vars"
        )


async def _get_access_token() -> str:
    _require_env()

    now = int(time.time())
    if _token_cache["token"] and now < (_token_cache["exp"] - 30):
        return _token_cache["token"]

    data = {
    "grant_type": "client_credentials",
    "client_id": EUIPO_CLIENT_ID,
    "client_secret": EUIPO_CLIENT_SECRET,
    "scope": "uid"
}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            AUTH_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=r.text)

        j = r.json()
        token = j.get("access_token")
        expires_in = int(j.get("expires_in", 3600))

        _token_cache["token"] = token
        _token_cache["exp"] = int(time.time()) + expires_in

        return token


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/euipo/search")
async def euipo_search(payload: EuipoSearchRequest):
    token = await _get_access_token()

    params = {
        "text": payload.text,
        "page": payload.page,
        "size": payload.size,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{API_BASE}/trademark-search/trademarks",
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "X-IBM-Client-Id": EUIPO_CLIENT_ID,
            },
        )

    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    data = r.json()

    results = []
    for t in data.get("trademarks", []):
        verbal = (t.get("wordMarkSpecification") or {}).get("verbalElement")
        classes = t.get("niceClasses") or []

        if payload.niceClasses:
            if not set(classes).intersection(set(payload.niceClasses)):
                continue

        results.append({
            "applicationNumber": t.get("applicationNumber"),
            "verbalElement": verbal,
            "status": t.get("status"),
            "niceClasses": classes,
            "markFeature": t.get("markFeature"),
        })

    return {
        "query": payload.text,
        "results": results
    }