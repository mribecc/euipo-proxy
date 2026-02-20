import os
import time
from typing import Optional, List, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

# Carica variabili ambiente (.env in locale, Environment su Render)
load_dotenv()

EUIPO_CLIENT_ID = os.getenv("EUIPO_CLIENT_ID", "").strip()
EUIPO_CLIENT_SECRET = os.getenv("EUIPO_CLIENT_SECRET", "").strip()

# EUIPO Sandbox endpoints
AUTH_URL = "https://auth-sandbox.euipo.europa.eu/oidc/accessToken"
API_BASE = "https://api-sandbox.euipo.europa.eu"

app = FastAPI(title="EUIPO Proxy", version="2.0.0")

# Cache semplice token
_token_cache = {"token": None, "exp": 0}


class EuipoSearchRequest(BaseModel):
    text: str
    niceClasses: Optional[List[int]] = None
    page: int = 0
    size: int = 10  # EUIPO richiede >= 10


def _require_env():
    if not EUIPO_CLIENT_ID or not EUIPO_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Missing EUIPO_CLIENT_ID / EUIPO_CLIENT_SECRET env vars",
        )


async def _get_access_token() -> str:
    _require_env()

    now = int(time.time())
    if _token_cache["token"] and now < (_token_cache["exp"] - 30):
        return _token_cache["token"]

    form_data = {
        "grant_type": "client_credentials",
        "client_id": EUIPO_CLIENT_ID,
        "client_secret": EUIPO_CLIENT_SECRET,
        "scope": "uid",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            AUTH_URL,
            data=form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Token request failed: {r.status_code} {r.text}",
        )

    token_json = r.json()
    token = token_json.get("access_token")

    if not token:
        raise HTTPException(
            status_code=502,
            detail=f"Token response missing access_token: {token_json}",
        )

    expires_in = int(token_json.get("expires_in", 3600))
    _token_cache["token"] = token
    _token_cache["exp"] = int(time.time()) + expires_in

    return token


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/euipo/search")
async def euipo_search(payload: EuipoSearchRequest) -> Dict[str, Any]:
    token = await _get_access_token()

    params = {
        "text": payload.text,
        "page": payload.page,
        "size": payload.size,
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "X-IBM-Client-Id": EUIPO_CLIENT_ID,
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{API_BASE}/trademark-search/trademarks",
            params=params,
            headers=headers,
        )

    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    data = r.json()
    results = []

    for t in data.get("trademarks", []):
        status = t.get("status")
        verbal = (t.get("wordMarkSpecification") or {}).get("verbalElement")
        classes = t.get("niceClasses") or []

        # Solo marchi attivi
        if status not in [
            "REGISTERED",
            "APPLICATION_UNDER_EXAMINATION",
            "OPPOSITION",
        ]:
            continue

        # Filtro classi Nice
        if payload.niceClasses:
            if not set(classes).intersection(set(payload.niceClasses)):
                continue

        results.append(
            {
                "applicationNumber": t.get("applicationNumber"),
                "verbalElement": verbal,
                "status": status,
                "niceClasses": classes,
                "markFeature": t.get("markFeature"),
                "markBasis": t.get("markBasis"),
            }
        )

    return {
        "query": payload.text,
        "page": payload.page,
        "size": payload.size,
        "results": results,
    }