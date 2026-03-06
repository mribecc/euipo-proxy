import os
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# ---------------------------------------------------
# LOAD .env
# ---------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

EUIPO_ENV = (os.getenv("EUIPO_ENV") or "sandbox").strip()
EUIPO_API_BASE = (
    os.getenv("EUIPO_API_BASE") or "https://api-sandbox.euipo.europa.eu"
).strip()
EUIPO_AUTH_URL = (
    os.getenv("EUIPO_AUTH_URL")
    or "https://auth-sandbox.euipo.europa.eu/oidc/accessToken"
).strip()
EUIPO_SCOPE = (
    os.getenv("EUIPO_SCOPE") or "trademark-search.trademarks.read"
).strip()

EUIPO_API_KEY = (os.getenv("EUIPO_API_KEY") or "").strip()
EUIPO_API_SECRET = (os.getenv("EUIPO_API_SECRET") or "").strip()


# ---------------------------------------------------
# APP
# ---------------------------------------------------
app = FastAPI(
    title="EUIPO Proxy - NOMINIS",
    version="3.1.0"
)

_token_cache: Dict[str, Any] = {
    "access_token": None,
    "expires_at": 0,
}


# ---------------------------------------------------
# MODELS
# ---------------------------------------------------
class EuipoSearchRequest(BaseModel):
    text: str = Field(..., description="Trademark search text")
    page: int = Field(default=0, ge=0)
    size: int = Field(default=10, ge=1, le=100)
    niceClasses: Optional[List[int]] = Field(
        default=None,
        description="Optional Nice classes filter"
    )


class EuipoRawRequest(BaseModel):
    text: str = Field(..., description="Trademark search text")
    page: int = Field(default=0, ge=0)
    size: int = Field(default=10, ge=1, le=100)
    limit: Optional[int] = Field(default=None, ge=1, le=100)


# ---------------------------------------------------
# HELPERS
# ---------------------------------------------------
def _require_env() -> None:
    if not EUIPO_API_KEY or not EUIPO_API_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Missing EUIPO_API_KEY / EUIPO_API_SECRET in .env"
        )


def _extract_name(trademark: Dict[str, Any]) -> Optional[str]:
    word_spec = trademark.get("wordMarkSpecification") or {}
    verbal = word_spec.get("verbalElement")
    if verbal:
        return verbal

    # fallback nel caso la struttura cambi
    return trademark.get("name")


def _extract_applicant_name(trademark: Dict[str, Any]) -> Optional[str]:
    applicants = trademark.get("applicants") or []
    if applicants and isinstance(applicants, list):
        first = applicants[0] or {}
        return first.get("name")
    return None


async def _get_access_token() -> str:
    _require_env()

    now = int(time.time())
    cached_token = _token_cache.get("access_token")
    expires_at = int(_token_cache.get("expires_at", 0) or 0)

    # riusa il token finché non manca meno di 60 secondi alla scadenza
    if cached_token and now < (expires_at - 60):
        return cached_token

    data = {
        "grant_type": "client_credentials",
        "client_id": EUIPO_API_KEY,
        "client_secret": EUIPO_API_SECRET,
        "scope": EUIPO_SCOPE,
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "NOMINIS-EUIPO-Client/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                EUIPO_AUTH_URL,
                data=data,
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"EUIPO auth connection error: {str(exc)}"
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"EUIPO token error: {response.text}"
        )

    payload = response.json()
    access_token = payload.get("access_token")
    expires_in = int(payload.get("expires_in", 3600))

    if not access_token:
        raise HTTPException(
            status_code=502,
            detail=f"EUIPO token missing in response: {payload}"
        )

    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = now + expires_in

    return access_token


async def _euipo_get_trademarks(
    text: str,
    page: int,
    size: int,
    nice_classes: Optional[List[int]] = None,
) -> Dict[str, Any]:
    token = await _get_access_token()

    url = f"{EUIPO_API_BASE}/trademark-search/trademarks"

    params: Dict[str, Any] = {
        "text": text,
        "page": page,
        "size": size,
    }

    if nice_classes:
        params["niceClasses"] = ",".join(str(x) for x in nice_classes)

    headers = {
        "Authorization": f"Bearer {token}",
        "X-IBM-Client-Id": EUIPO_API_KEY,
        "X-IBM-Client-Secret": EUIPO_API_SECRET,
        "Accept": "application/json",
        "User-Agent": "NOMINIS-EUIPO-Client/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                url,
                params=params,
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"EUIPO API connection error: {str(exc)}"
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"EUIPO API error: {response.text}"
        )

    return response.json()


# ---------------------------------------------------
# ROUTES
# ---------------------------------------------------
@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "environment": EUIPO_ENV,
        "api_base": EUIPO_API_BASE,
        "auth_url": EUIPO_AUTH_URL,
        "scope": EUIPO_SCOPE,
        "has_api_key": bool(EUIPO_API_KEY),
        "has_api_secret": bool(EUIPO_API_SECRET),
        "token_cached": bool(_token_cache.get("access_token")),
    }


@app.post("/euipo/search")
async def euipo_search(req: EuipoSearchRequest) -> Dict[str, Any]:
    return await _euipo_get_trademarks(
        text=req.text,
        page=req.page,
        size=req.size,
        nice_classes=req.niceClasses,
    )


@app.post("/euipo/raw")
async def euipo_raw(req: EuipoRawRequest) -> Dict[str, Any]:
    data = await _euipo_get_trademarks(
        text=req.text,
        page=req.page,
        size=req.size,
        nice_classes=None,
    )

    if req.limit and isinstance(data, dict):
        trademarks = data.get("trademarks")
        if isinstance(trademarks, list):
            data["trademarks"] = trademarks[:req.limit]

    return data


@app.post("/euipo/search-clean")
async def euipo_search_clean(req: EuipoSearchRequest) -> Dict[str, Any]:
    data = await _euipo_get_trademarks(
        text=req.text,
        page=req.page,
        size=req.size,
        nice_classes=req.niceClasses,
    )

    cleaned_results = []

    for trademark in data.get("trademarks", []):
        cleaned_results.append(
            {
                "name": _extract_name(trademark),
                "applicationNumber": trademark.get("applicationNumber"),
                "markFeature": trademark.get("markFeature"),
                "markKind": trademark.get("markKind"),
                "markBasis": trademark.get("markBasis"),
                "niceClasses": trademark.get("niceClasses"),
                "status": trademark.get("status"),
                "applicationDate": trademark.get("applicationDate"),
                "registrationDate": trademark.get("registrationDate"),
                "expiryDate": trademark.get("expiryDate"),
                "applicant": _extract_applicant_name(trademark),
            }
        )

    return {
        "query": req.text,
        "page": data.get("page"),
        "size": data.get("size"),
        "total": data.get("totalElements"),
        "totalPages": data.get("totalPages"),
        "results": cleaned_results,
    }