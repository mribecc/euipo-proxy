import os
import time
import uuid
import unicodedata
from datetime import datetime
from typing import Optional, List, Any, Dict

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

load_dotenv()

EUIPO_CLIENT_ID = os.getenv("EUIPO_CLIENT_ID", "").strip()
EUIPO_CLIENT_SECRET = os.getenv("EUIPO_CLIENT_SECRET", "").strip()

AUTH_URL = "https://auth-sandbox.euipo.europa.eu/oidc/accessToken"
API_BASE = "https://api-sandbox.euipo.europa.eu"

REPORTS_DIR = os.getenv("REPORTS_DIR", "./reports")

app = FastAPI(title="EUIPO Proxy", version="1.2.0")

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
    markFeature: Optional[str] = Field(default=None, description="Optional filter: WORD / FIGURATIVE")
    limit: Optional[int] = Field(default=None, ge=1, le=50)


class ReportPDFRequest(BaseModel):
    # Minimal fields for a client-friendly report
    brand: str = Field(..., description="Trademark/name to assess")
    niceClass: int = Field(..., ge=1, le=45, description="Nice class number")
    niceClassLabel: Optional[str] = Field(default=None, description="Optional class label (e.g., Clothing, footwear)")
    riskIndex: int = Field(..., ge=0, le=100, description="NOMINIS Risk Index (0-100)")
    riskLevel: str = Field(..., description="LOW / MEDIUM / HIGH / CRITICAL")
    summary: str = Field(..., description="Short technical summary paragraph")
    disclaimer: Optional[str] = Field(
        default="Automated analysis based on EUIPO data. This is not legal advice.",
        description="Optional disclaimer"
    )
    locale: str = Field(default="en", description="en / it (affects headings)")


# ---------- Helpers ----------
def _require_env():
    if not EUIPO_CLIENT_ID or not EUIPO_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Missing EUIPO_CLIENT_ID / EUIPO_CLIENT_SECRET env vars"
        )


def _strip_diacritics(s: str) -> str:
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
        # keep it: sometimes gateways behave better with both
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
    w = t.get("wordMarkSpecification") or {}
    verbal = w.get("verbalElement")
    if isinstance(verbal, str) and verbal.strip():
        return verbal.strip()

    # soft fallbacks (depends on payload)
    for key in ["markText", "verbalElement", "reference", "title", "name"]:
        v = t.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _ensure_reports_dir():
    os.makedirs(REPORTS_DIR, exist_ok=True)


def _build_report_filename(prefix: str = "NOM") -> str:
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:6].upper()
    return f"{prefix}-{ts}-{short}.pdf"


def _risk_badge(risk_level: str) -> str:
    rl = (risk_level or "").strip().upper()
    if rl in ["LOW", "BASSO"]:
        return "LOW"
    if rl in ["MEDIUM", "MEDIO"]:
        return "MEDIUM"
    if rl in ["HIGH", "ALTO"]:
        return "HIGH"
    return "CRITICAL"


def _draw_wrapped_text(c: canvas.Canvas, text: str, x: float, y: float, max_width: float, line_height: float):
    # minimal wrap without external libs
    words = (text or "").split()
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if c.stringWidth(test, "Helvetica", 10) <= max_width:
            line = test
        else:
            c.drawString(x, y, line)
            y -= line_height
            line = w
    if line:
        c.drawString(x, y, line)
        y -= line_height
    return y


def _generate_pdf(path: str, payload: ReportPDFRequest):
    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4

    # margins
    left = 50
    right = 50
    top = 60
    y = height - top

    # header
    c.setFont("Helvetica-Bold", 16)
    title = "NOMINIS — Preliminary Trademark Assessment" if payload.locale == "en" else "NOMINIS — Valutazione preliminare marchio"
    c.drawString(left, y, title)
    y -= 26

    c.setFont("Helvetica", 10)
    date_line = datetime.now().strftime("%d %B %Y") if payload.locale == "en" else datetime.now().strftime("%d/%m/%Y")
    c.drawString(left, y, f"Date: {date_line}" if payload.locale == "en" else f"Data: {date_line}")
    y -= 18

    # key facts
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y, "Trademark:" if payload.locale == "en" else "Marchio:")
    c.setFont("Helvetica", 11)
    c.drawString(left + 85, y, payload.brand)
    y -= 16

    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y, "Nice class:" if payload.locale == "en" else "Classe di Nizza:")
    c.setFont("Helvetica", 11)
    class_label = f"{payload.niceClass}"
    if payload.niceClassLabel:
        class_label += f" — {payload.niceClassLabel}"
    c.drawString(left + 85, y, class_label)
    y -= 16

    # risk line
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y, "Result:" if payload.locale == "en" else "Esito:")
    c.setFont("Helvetica", 11)
    badge = _risk_badge(payload.riskLevel)
    c.drawString(left + 85, y, f"{badge} risk — NOMINIS Risk Index™: {payload.riskIndex} / 100" if payload.locale == "en"
                 else f"Rischio {badge} — NOMINIS Risk Index™: {payload.riskIndex} / 100")
    y -= 22

    # divider
    c.line(left, y, width - right, y)
    y -= 18

    # summary
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y, "Technical summary" if payload.locale == "en" else "Sintesi tecnica")
    y -= 14

    c.setFont("Helvetica", 10)
    y = _draw_wrapped_text(c, payload.summary, left, y, width - left - right, 13)
    y -= 10

    # disclaimer
    c.setFont("Helvetica", 9)
    disc = payload.disclaimer or ""
    if payload.locale != "en" and disc.strip() == "Automated analysis based on EUIPO data. This is not legal advice.":
        disc = "Analisi automatizzata su banca dati EUIPO. Non costituisce parere legale."
    y = _draw_wrapped_text(c, f"⚠ {disc}", left, y, width - left - right, 12)

    # footer
    c.setFont("Helvetica", 9)
    c.drawString(left, 40, "Powered by NOMINIS")
    c.showPage()
    c.save()


# ---------- Routes ----------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/report/health")
def report_health():
    return {"ok": True, "reportsDir": REPORTS_DIR}


@app.post("/euipo/raw")
async def euipo_raw(payload: EuipoRawRequest):
    token = await _get_access_token()

    data = await _euipo_get_trademarks(token, payload.text, payload.page, payload.size)
    trademarks = data.get("trademarks", [])

    if payload.markFeature:
        mf = payload.markFeature.strip().upper()
        trademarks = [t for t in trademarks if (t.get("markFeature") or "").upper() == mf]

    if payload.limit:
        trademarks = trademarks[:payload.limit]

    data["trademarks"] = trademarks
    data["queryEcho"] = {
        "text": payload.text,
        "page": payload.page,
        "size": payload.size,
        "markFeature": payload.markFeature,
        "limit": payload.limit,
    }
    return data


@app.post("/euipo/search")
async def euipo_search(payload: EuipoSearchRequest):
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

    data = await _euipo_get_trademarks(token, payload.text, payload.page, payload.size)
    cleaned = _clean(data.get("trademarks", []))

    # fallback: retry without diacritics if nothing
    if not cleaned:
        stripped = _strip_diacritics(payload.text)
        if stripped != payload.text:
            data2 = await _euipo_get_trademarks(token, stripped, payload.page, payload.size)
            cleaned = _clean(data2.get("trademarks", []))

    return {"query": payload.text, "page": payload.page, "size": payload.size, "results": cleaned}


@app.post("/report/pdf")
def report_pdf(payload: ReportPDFRequest):
    """
    Generates a PDF report and returns a download URL.
    (Option B) GPT-friendly: link-based retrieval.
    """
    _ensure_reports_dir()
    filename = _build_report_filename(prefix="NOM")
    path = os.path.join(REPORTS_DIR, filename)

    try:
        _generate_pdf(path, payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

    return {
        "ok": True,
        "filename": filename,
        "download_url": f"/report/file/{filename}",
    }


@app.get("/report/file/{filename}")
def report_file(filename: str):
    _ensure_reports_dir()
    path = os.path.join(REPORTS_DIR, filename)

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Report not found (maybe expired or server restarted).")

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
    )