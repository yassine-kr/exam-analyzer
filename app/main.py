"""Exam Analyzer — standalone version.

Users upload exam documents; each document gets an analysis record.
The real analysis/processing pipeline will be plugged in later
(see `analyze_document`). For now it only extracts basic file metadata.
"""

import hashlib
import os
import socket
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "exam_analyzer.db"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg"}
APP_VERSION = "0.1.0"

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Exam Analyzer", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------------------------------------------------------------- storage

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.on_event("startup")
def init_storage() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with closing(db()) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                course TEXT,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                status TEXT NOT NULL,
                analysis TEXT
            )"""
        )


# ---------------------------------------------------------------- analysis

def analyze_document(path: Path, content_type: str | None) -> tuple[str, str]:
    """Placeholder for the future analysis pipeline.

    Returns (status, analysis_text). Later this will hand the document to
    a processing service (e.g. a queue + worker, or an AI model).
    """
    size_kb = path.stat().st_size / 1024
    lines = [
        f"File type: {content_type or 'unknown'}",
        f"Size: {size_kb:.1f} KB",
    ]
    if path.suffix.lower() == ".txt":
        text = path.read_text(errors="ignore")
        lines.append(f"Words: {len(text.split())}")
        lines.append(f"Lines: {len(text.splitlines())}")
    lines.append("Full analysis: not implemented yet (coming in a later lab).")
    return "pending", "\n".join(lines)


# ---------------------------------------------------------------- pages

def common_context(request: Request) -> dict:
    return {
        "request": request,
        "hostname": socket.gethostname(),
        "version": APP_VERSION,
        "scheme": request.headers.get("x-forwarded-proto", request.url.scheme),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with closing(db()) as conn:
        docs = conn.execute(
            "SELECT * FROM documents ORDER BY uploaded_at DESC"
        ).fetchall()
    return templates.TemplateResponse(
        "index.html",
        {**common_context(request), "docs": docs, "max_mb": MAX_UPLOAD_MB,
         "allowed": ", ".join(sorted(ALLOWED_EXTENSIONS))},
    )


@app.post("/upload")
async def upload(
    title: str = Form(...),
    course: str = Form(""),
    file: UploadFile = File(...),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"File type {ext or '?'} not allowed")

    content = await file.read()
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File larger than {MAX_UPLOAD_MB} MB")
    if not content:
        raise HTTPException(400, "Empty file")

    doc_id = uuid.uuid4().hex[:12]
    stored_name = f"{doc_id}{ext}"
    path = UPLOAD_DIR / stored_name
    path.write_bytes(content)

    status, analysis = analyze_document(path, file.content_type)
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc_id, title.strip(), course.strip(), file.filename,
                stored_name, file.content_type, len(content),
                hashlib.sha256(content).hexdigest(),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                status, analysis,
            ),
        )
    return RedirectResponse(f"/documents/{doc_id}", status_code=303)


def get_doc(doc_id: str) -> sqlite3.Row:
    with closing(db()) as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


@app.get("/documents/{doc_id}", response_class=HTMLResponse)
def document_page(request: Request, doc_id: str):
    return templates.TemplateResponse(
        "document.html", {**common_context(request), "doc": get_doc(doc_id)}
    )


@app.get("/documents/{doc_id}/download")
def download(doc_id: str):
    doc = get_doc(doc_id)
    return FileResponse(UPLOAD_DIR / doc["stored_name"], filename=doc["filename"])


@app.post("/documents/{doc_id}/delete")
def delete(doc_id: str):
    doc = get_doc(doc_id)
    (UPLOAD_DIR / doc["stored_name"]).unlink(missing_ok=True)
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- API

@app.get("/api/documents")
def api_documents():
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, title, course, filename, size_bytes, uploaded_at, status "
            "FROM documents ORDER BY uploaded_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/documents/{doc_id}")
def api_document(doc_id: str):
    return dict(get_doc(doc_id))


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION, "host": socket.gethostname()}
