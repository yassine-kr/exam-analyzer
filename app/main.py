"""Exam Analyzer — v0.2: documents in Amazon S3, metadata in Amazon DynamoDB.

Users upload exam documents; each document gets an analysis record.
The real analysis/processing pipeline will be plugged in later
(see `analyze_document`). For now it only extracts basic file metadata.

AWS credentials are NOT stored in the app: on EC2 they come from the
instance's IAM role (boto3 finds them automatically).
"""

import hashlib
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------- config

S3_BUCKET = os.getenv("S3_BUCKET", "")
DYNAMODB_TABLE = os.getenv("DYNAMODB_TABLE", "")
AWS_REGION = os.getenv("AWS_REGION", "eu-central-1")
S3_PREFIX = os.getenv("S3_PREFIX", "uploads/")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg"}
APP_VERSION = "0.2.0"

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Exam Analyzer", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

s3 = boto3.client("s3", region_name=AWS_REGION)
table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DYNAMODB_TABLE or "unset")


class AWSConfigError(Exception):
    """Raised when the app cannot reach S3 / DynamoDB."""


def explain_aws_error(exc: Exception) -> str:
    """Turn common AWS errors into a hint students can act on."""
    if not S3_BUCKET or not DYNAMODB_TABLE:
        return "S3_BUCKET and DYNAMODB_TABLE environment variables must be set."
    if isinstance(exc, NoCredentialsError):
        return ("No AWS credentials found. Attach an IAM role to the EC2 instance, "
                "and set the instance metadata 'hop limit' to 2 so Docker "
                "containers can reach it.")
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        hints = {
            "AccessDenied": "The IAM role is missing a permission (see iam-policy.json).",
            "AccessDeniedException": "The IAM role is missing a permission (see iam-policy.json).",
            "NoSuchBucket": f"Bucket '{S3_BUCKET}' does not exist (check name/region).",
            "ResourceNotFoundException": f"Table '{DYNAMODB_TABLE}' not found in {AWS_REGION}.",
            "ValidationException": "Table key must be 'id' (String).",
        }
        aws_message = exc.response.get("Error", {}).get("Message", "")
        hint = hints.get(code)
        return f"{code}: {hint} AWS says: {aws_message}" if hint else f"{code}: {aws_message}"
    return str(exc)


def aws_call(fn, *args, **kwargs):
    try:
        if not S3_BUCKET or not DYNAMODB_TABLE:
            raise AWSConfigError()
        return fn(*args, **kwargs)
    except (AWSConfigError, BotoCoreError, ClientError) as exc:
        raise AWSConfigError(explain_aws_error(exc)) from exc


@app.exception_handler(AWSConfigError)
def aws_error_page(request: Request, exc: AWSConfigError):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": str(exc)}, status_code=503)
    return templates.TemplateResponse(
        "error.html", {**common_context(request), "message": str(exc)}, status_code=503
    )


# ---------------------------------------------------------------- analysis

def analyze_document(content: bytes, filename: str, content_type: str | None) -> tuple[str, str]:
    """Placeholder for the future analysis pipeline.

    Returns (status, analysis_text). Later this will hand the document to
    a processing service (e.g. a queue + worker, or an AI model).
    """
    lines = [
        f"File type: {content_type or 'unknown'}",
        f"Size: {len(content) / 1024:.1f} KB",
    ]
    if Path(filename).suffix.lower() == ".txt":
        text = content.decode(errors="ignore")
        lines.append(f"Words: {len(text.split())}")
        lines.append(f"Lines: {len(text.splitlines())}")
    lines.append("Full analysis: not implemented yet (coming in a later lab).")
    return "pending", "\n".join(lines)


# ---------------------------------------------------------------- data access

def list_docs() -> list[dict]:
    items, kwargs = [], {}
    while True:  # Scan is fine for a small table; paginate anyway
        page = aws_call(table.scan, **kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted((normalize(i) for i in items), key=lambda d: d["uploaded_at"], reverse=True)


def get_doc(doc_id: str) -> dict:
    item = aws_call(table.get_item, Key={"id": doc_id}).get("Item")
    if item is None:
        raise HTTPException(404, "Document not found")
    return normalize(item)


def normalize(item: dict) -> dict:
    """DynamoDB returns numbers as Decimal; convert for display/JSON."""
    item = dict(item)
    item["size_bytes"] = int(item.get("size_bytes", 0))
    return item


# ---------------------------------------------------------------- pages

def common_context(request: Request) -> dict:
    return {
        "request": request,
        "hostname": socket.gethostname(),
        "version": APP_VERSION,
        "scheme": request.headers.get("x-forwarded-proto", request.url.scheme),
        "bucket": S3_BUCKET or "—",
        "table_name": DYNAMODB_TABLE or "—",
        "region": AWS_REGION,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {**common_context(request), "docs": list_docs(), "max_mb": MAX_UPLOAD_MB,
         "allowed": ", ".join(sorted(ALLOWED_EXTENSIONS))},
    )


@app.post("/upload")
async def upload(
    title: str = Form(...),
    course: str = Form(""),
    file: UploadFile = File(...),
):
    filename = Path(file.filename or "").name
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"File type {ext or '?'} not allowed")

    content = await file.read()
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File larger than {MAX_UPLOAD_MB} MB")
    if not content:
        raise HTTPException(400, "Empty file")

    doc_id = uuid.uuid4().hex[:12]
    s3_key = f"{S3_PREFIX}{doc_id}{ext}"
    content_type = file.content_type or "application/octet-stream"

    # 1) the file itself goes to S3
    aws_call(
        s3.put_object, Bucket=S3_BUCKET, Key=s3_key, Body=content,
        ContentType=content_type, Metadata={"document-id": doc_id},
    )

    # 2) the metadata + analysis results go to DynamoDB
    #    (if this fails, remove the S3 object so no orphan file stays behind)
    status, analysis = analyze_document(content, filename, content_type)
    try:
        aws_call(table.put_item, Item={
            "id": doc_id,
            "title": title.strip(),
            "course": course.strip(),
            "filename": filename,
            "s3_bucket": S3_BUCKET,
            "s3_key": s3_key,
            "content_type": content_type,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": status,
            "analysis": analysis,
        })
    except AWSConfigError:
        try:
            s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
        except (BotoCoreError, ClientError):
            pass
        raise
    return RedirectResponse(f"/documents/{doc_id}", status_code=303)


@app.get("/documents/{doc_id}", response_class=HTMLResponse)
def document_page(request: Request, doc_id: str):
    return templates.TemplateResponse(
        "document.html", {**common_context(request), "doc": get_doc(doc_id)}
    )


@app.get("/documents/{doc_id}/download")
def download(doc_id: str):
    """Redirect to a temporary (5 min) pre-signed S3 URL: the file is served
    directly by S3, the bucket itself stays private."""
    doc = get_doc(doc_id)
    url = aws_call(
        s3.generate_presigned_url, "get_object",
        Params={
            "Bucket": doc["s3_bucket"], "Key": doc["s3_key"],
            "ResponseContentDisposition": f'attachment; filename="{doc["filename"]}"',
        },
        ExpiresIn=300,
    )
    return RedirectResponse(url, status_code=302)


@app.post("/documents/{doc_id}/delete")
def delete(doc_id: str):
    doc = get_doc(doc_id)
    aws_call(s3.delete_object, Bucket=doc["s3_bucket"], Key=doc["s3_key"])
    aws_call(table.delete_item, Key={"id": doc_id})
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- API

@app.get("/api/documents")
def api_documents():
    return [
        {k: d.get(k) for k in ("id", "title", "course", "filename", "size_bytes",
                               "uploaded_at", "status", "s3_key")}
        for d in list_docs()
    ]


@app.get("/api/documents/{doc_id}")
def api_document(doc_id: str):
    return get_doc(doc_id)


@app.get("/health")
def health():
    """Liveness: the process is up (does not call AWS)."""
    return {"status": "ok", "version": APP_VERSION, "host": socket.gethostname()}


@app.get("/health/aws")
def health_aws():
    """Readiness: can we reach the bucket and the table?"""
    checks = {}
    for name, fn in (
        ("s3", lambda: aws_call(s3.head_bucket, Bucket=S3_BUCKET)),
        ("dynamodb", lambda: aws_call(table.load)),
    ):
        try:
            fn()
            checks[name] = "ok"
        except AWSConfigError as exc:
            checks[name] = str(exc)
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "error", "region": AWS_REGION,
         "bucket": S3_BUCKET, "table": DYNAMODB_TABLE, **checks},
        status_code=200 if ok else 503,
    )
