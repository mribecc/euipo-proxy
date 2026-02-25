import os
import time
import unicodedata
from typing import Optional, List, Any, Dict

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

EUIPO_CLIENT_ID = os.getenv("EUIPO_CLIENT_ID", "").strip()
EUIPO_CLIENT_SECRET = os.getenv("EUIPO_CLIENT_SECRET", "").strip()

AUTH_URL = "https://auth-sandbox.euipo.europa.eu/oidc/accessToken"
API_BASE = "https://api-sandbox.euipo.europa.eu"

app = FastAPI(title="EUIPO Proxy", version="1.1.0")

_token_cache = {"token": None, "exp": 0}


# ---------- Models ----------
class EuipoSearchRequest(BaseModel):
    text: str = Field(..., description="Trademark search text")
    niceClasses: Optional[List[int]] = Field(default=None, description="Optional Nice classes filter")
    page: int = Field(default=0, ge=0)
    size: int = Field(default=10, ge=10, le=100)


class EuipoRawRequest(BaseModel):
    text: str = Field(..., description="Trademark search text")
    page: int = Field(default=0, ge=0)
    size: int = Field(default=10, ge=10, le=100)
    # Optional: filter raw results by markFeature: WORD / FIGURATIVE / etc.
    markFeature: Optional[str] = Field(default=None, description="Optional filter: WORD / FIGURATIVE")
    # Optional: return only first N results after filtering (handy for debug)
    limit: Optional[int] = Field(default=None, ge=1, le=50)


# ---------- Helpers ----------
def _require_env():
    if not EUIPO_CLIENT_ID or not EUIPO_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Missing EUIPO_CLIENT_ID / EUIPO_CLIENT_SECRET env vars"
        )


def _strip_diacritics(s: str) -> str:
    # "Nuvilù" -> "Nuvilu"
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


async def _get_access_token() -> str:
    _require_env()

    now = int(time.time())
    if _token_cache["token"] and now < (_token_cache["exp"] - 30):
        return _token_cache["token"]

    data = {
        "grant_type": "client_credentials",
        "client_id": EUIPO_CLIENT_ID,
        "client_secret": EUIPO_CLIENT_SECRET,
        "scope": "uid",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            AUTH_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"EUIPO token error: {r.text}")

    j = r.json()
    token = j.get("access_token")
    if not token:
        raise HTTPException(status_code=502, detail=f"EUIPO token missing in response: {j}")

    expires_in = int(j.get("expires_in", 3600))
    _token_cache["token"] = token
    _token_cache["exp"] = int(time.time()) + expires_in
    return token


async def _euipo_get_trademarks(token: str, text: str, page: int, size: int) -> Dict[str, Any]:
    params = {"text": text, "page": page, "size": size}

    headers = {
        "Authorization": f"Bearer {token}",
        "X-IBM-Client-Id": EUIPO_CLIENT_ID,
        # keep it: some gateways rely on this even if they accept id-only
        "X-IBM-Client-Secret": EUIPO_CLIENT_SECRET,
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{API_BASE}/trademark-search/trademarks",
            params=params,
            headers=headers,
        )

    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"EUIPO search error: {r.text}")

    return r.json()


def _extract_label(t: Dict[str, Any]) -> Optional[str]:
    # For WORD marks this is usually present. For FIGURATIVE it may or may not be.
    w = t.get("wordMarkSpecification") or {}
    verbal = w.get("verbalElement")
    if verbal:
        return verbal.strip()

    # Fallbacks: try common fields (depends on EUIPO payload)
    # Keep these light; we mainly use RAW endpoint for inspection anyway.
    for key in ["markText", "verbalElement", "reference", "title", "name"]:
        v = t.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return None


# ---------- Routes ----------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/euipo/raw")
async def euipo_raw(payload: EuipoRawRequest):
    """
    Returns EUIPO response JSON as-is (raw), with optional filtering.
    Useful for debugging fields (FIGURATIVE marks, etc.).
    """
    token = await _get_access_token()

    # run query with original text
    data = await _euipo_get_trademarks(token, payload.text, payload.page, payload.size)

    trademarks = data.get("trademarks", [])
    if payload.markFeature:
        mf = payload.markFeature.strip().upper()
        trademarks = [t for t in trademarks if (t.get("markFeature") or "").upper() == mf]

    if payload.limit:
        trademarks = trademarks[:payload.limit]

    # Return the whole envelope but with possibly filtered "trademarks"
    data["trademarks"] = trademarks
    data["queryEcho"] = {"text": payload.text, "page": payload.page, "size": payload.size,
                         "markFeature": payload.markFeature, "limit": payload.limit}

    return data


@app.post("/euipo/search")
async def euipo_search(payload: EuipoSearchRequest):
    """
    Returns a cleaned response for the GPT:
    - pulls from EUIPO
    - filters by niceClasses (optional)
    - includes basic fields
    - auto fallback: if no results and text has diacritics, retry without diacritics
    """
    token = await _get_access_token()

    def _clean(trademarks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results = []
        for t in trademarks:
            classes = t.get("niceClasses") or []
            if payload.niceClasses:
                if not set(classes).intersection(set(payload.niceClasses)):
                    continue

            results.append({
                "applicationNumber": t.get("applicationNumber"),
                "verbalElement": _extract_label(t),
                "status": t.get("status"),
                "niceClasses": classes,
                "markFeature": t.get("markFeature"),
                "applicationDate": t.get("applicationDate"),
                "registrationDate": t.get("registrationDate"),
            })
        return results

    # First try with original query
    data = await _euipo_get_trademarks(token, payload.text, payload.page, payload.size)
    trademarks = data.get("trademarks", [])
    cleaned = _clean(trademarks)

    # If nothing and query has diacritics, retry stripped
    if not cleaned:
        stripped = _strip_diacritics(payload.text)
        if stripped != payload.text:
            data2 = await _euipo_get_trademarks(token, stripped, payload.page, payload.size)
            trademarks2 = data2.get("trademarks", [])
            cleaned = _clean(trademarks2)

    return {
        "query": payload.text,
        "page": payload.page,
        "size": payload.size,
        "results": cleaned,
    }