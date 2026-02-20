import os
import time
from io import BytesIO
from datetime import datetime
from typing import Optional, List, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib import colors


load_dotenv()

EUIPO_CLIENT_ID = os.getenv("EUIPO_CLIENT_ID", "").strip()
EUIPO_CLIENT_SECRET = os.getenv("EUIPO_CLIENT_SECRET", "").strip()

AUTH_URL = "https://auth-sandbox.euipo.europa.eu/oidc/accessToken"
API_BASE = "https://api-sandbox.euipo.europa.eu"

app = FastAPI(title="EUIPO Proxy", version="1.2.0")

_token_cache: Dict[str, Any] = {"token": None, "exp": 0}

# folder temporanea per PDF su Render (ephemeral ma ok)
REPORT_DIR = "/tmp/nominis_reports"
os.makedirs(REPORT_DIR, exist_ok=True)


# -----------------------------
# Models
# -----------------------------
class EuipoSearchRequest(BaseModel):
    text: str = Field(..., description="Trademark search text")
    niceClasses: Optional[List[int]] = Field(default=None, description="Optional Nice classes filter (post-filter)")
    page: int = Field(default=0, ge=0, description="Page index (0-based)")
    size: int = Field(default=10, ge=10, le=100, description="Page size (EUIPO requires >= 10; keep <= 100)")


class ReportEntry(BaseModel):
    name: str
    risk_index: int = Field(..., ge=0, le=100)
    risk_level: str = Field(..., description='One of: "BASSO", "MEDIO", "ALTO"')
    summary: str


class PdfReportRequest(BaseModel):
    report_code: Optional[str] = None
    date: Optional[str] = None  # e.g. "20 February 2026"
    nice_class: int
    class_description: str
    entries: List[ReportEntry]
    conclusion: Optional[str] = None


# -----------------------------
# Helpers
# -----------------------------
def _require_env() -> None:
    if not EUIPO_CLIENT_ID or not EUIPO_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Missing EUIPO_CLIENT_ID / EUIPO_CLIENT_SECRET env vars")


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
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Token error: {r.status_code} {r.text}")

    j = r.json()
    token = j.get("access_token")
    if not token:
        raise HTTPException(status_code=502, detail=f"Token error: missing access_token. Body: {r.text}")

    expires_in = int(j.get("expires_in", 3600))
    _token_cache["token"] = token
    _token_cache["exp"] = int(time.time()) + expires_in
    return token


def _safe_date_en(date_str: Optional[str]) -> str:
    if date_str and date_str.strip():
        return date_str.strip()
    return datetime.now().strftime("%d %B %Y")


def _build_pdf_bytes(payload: PdfReportRequest) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
    )
    styles = getSampleStyleSheet()

    title_style = styles["Heading1"]
    section_style = styles["Heading3"]
    normal_style = styles["BodyText"]
    small_style = styles["Normal"]

    report_date = _safe_date_en(payload.date)
    report_code = (payload.report_code or f"NOM-{datetime.now().strftime('%Y%m%d-%H%M%S')}").strip()

    elements = []
    elements.append(Paragraph("N O M I N I S", title_style))
    elements.append(Spacer(1, 0.08 * inch))
    elements.append(Paragraph("Trademark Intelligence Platform", small_style))
    elements.append(Spacer(1, 0.15 * inch))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.black))
    elements.append(Spacer(1, 0.30 * inch))

    elements.append(Paragraph("Comparative Trademark Assessment Report", section_style))
    elements.append(Spacer(1, 0.20 * inch))

    elements.append(Paragraph(f"<b>Report Code:</b> {report_code}", normal_style))
    elements.append(Paragraph(f"<b>Date:</b> {report_date}", normal_style))
    elements.append(Spacer(1, 0.20 * inch))

    elements.append(Paragraph(
        f"<b>Nice Class:</b> {payload.nice_class} – {payload.class_description}",
        normal_style
    ))
    elements.append(Spacer(1, 0.35 * inch))

    elements.append(Paragraph("Comparative analysis", section_style))
    elements.append(Spacer(1, 0.22 * inch))

    for e in payload.entries:
        name = (e.name or "").strip()
        risk_level = (e.risk_level or "").strip().upper()
        summary = (e.summary or "").strip()

        elements.append(Paragraph(f"<b>{name}</b>", normal_style))
        elements.append(Paragraph(
            f"NOMINIS Risk Index™: {int(e.risk_index)} / 100 — Risk: {risk_level}",
            normal_style
        ))
        elements.append(Paragraph(summary, normal_style))
        elements.append(Spacer(1, 0.26 * inch))

    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    elements.append(Spacer(1, 0.18 * inch))

    elements.append(Paragraph("Conclusion", section_style))
    elements.append(Spacer(1, 0.12 * inch))

    conclusion = (payload.conclusion or "").strip()
    if not conclusion:
        conclusion = (
            "Automatically generated document for preliminary assessment purposes. "
            "A broader clearance search is recommended prior to any filing."
        )
    elements.append(Paragraph(conclusion, normal_style))

    elements.append(Spacer(1, 0.32 * inch))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    elements.append(Spacer(1, 0.14 * inch))

    elements.append(Paragraph("⚠️ Automatically generated document for preliminary assessment.", normal_style))
    elements.append(Paragraph("It does not constitute legal advice.", normal_style))
    elements.append(Spacer(1, 0.08 * inch))
    elements.append(Paragraph("Powered by NOMINIS.", normal_style))

    doc.build(elements)
    return buf.getvalue()


# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/euipo/search")
async def euipo_search(payload: EuipoSearchRequest):
    token = await _get_access_token()

    params = {"text": payload.text, "page": payload.page, "size": payload.size}

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
            "applicationDate": t.get("applicationDate"),
            "registrationDate": t.get("registrationDate"),
        })

    return {"query": payload.text, "page": payload.page, "size": payload.size, "results": results}


@app.post("/report/pdf")
async def report_pdf(payload: PdfReportRequest):
    """
    Generate PDF and return a JSON with a download URL (Actions-friendly).
    """
    report_code = (payload.report_code or f"NOM-{datetime.now().strftime('%Y%m%d-%H%M%S')}").strip()
    payload.report_code = report_code  # normalize

    pdf_bytes = _build_pdf_bytes(payload)
    file_path = os.path.join(REPORT_DIR, f"{report_code}.pdf")
    with open(file_path, "wb") as f:
        f.write(pdf_bytes)

    # URL that the user can open to download
    report_url = f"/report/file/{report_code}.pdf"
    return JSONResponse({"ok": True, "report_code": report_code, "report_url": report_url})


@app.get("/report/file/{filename}")
def report_file(filename: str):
    """
    Download endpoint for generated PDFs.
    """
    # basic safety
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = os.path.join(REPORT_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Report not found")

    def file_iter():
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                yield chunk

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(file_iter(), media_type="application/pdf", headers=headers)