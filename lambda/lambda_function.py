"""Exam processor: AWS Lambda triggered by SQS.

Flow:  S3 (new object under uploads/) --event notification--> SQS queue
       --trigger--> this Lambda --> Claude on Amazon Bedrock --> DynamoDB

For each uploaded document the function:
  1. marks the DynamoDB item as "processing"
  2. downloads the file from S3
  3. sends it to Claude (Sonnet) on Amazon Bedrock with a JSON schema
  4. saves the structured result in the item and marks it "done"
     (or "failed" with the error message)

Only uses boto3 (included in the Lambda Python runtime): no layer needed.

Environment variables:
  DYNAMODB_TABLE            (required) table written by the web app
  MODEL_ID                  (optional) default: anthropic.claude-sonnet-5
  BEDROCK_REGION            (optional) default: the Lambda's own region
  MIN_SECONDS_PER_DOCUMENT  (optional) default: 0. Rate limit: each Lambda
                            instance handles at most 60 / N documents per
                            minute (total = that x SQS trigger max concurrency)

Handler: lambda_function.lambda_handler
"""

import base64
import io
import json
import logging
import os
import re
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

TABLE_NAME = os.environ["DYNAMODB_TABLE"]
MODEL_ID = os.getenv("MODEL_ID", "anthropic.claude-sonnet-5")
BEDROCK_REGION = os.getenv("BEDROCK_REGION") or os.environ["AWS_REGION"]
MIN_SECONDS_PER_DOCUMENT = float(os.getenv("MIN_SECONDS_PER_DOCUMENT", "0"))
MAX_TOKENS = 16000

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(TABLE_NAME)
bedrock = boto3.client(
    "bedrock-runtime",
    region_name=BEDROCK_REGION,
    config=Config(
        read_timeout=150,  # the analysis can take more than the default 60 s
        # "adaptive" retries ThrottlingException with back-off and slows the client down
        retries={"mode": "adaptive", "max_attempts": 4},
    ),
)

# Bedrock error codes that are worth retrying later (quota / capacity)
TRANSIENT_ERRORS = {
    "ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException",
    "InternalServerException", "ModelTimeoutException", "ServiceQuotaExceededException",
}


class RetryLater(Exception):
    """Temporary problem: let SQS deliver the message again later."""


class CannotProcess(Exception):
    """Permanent problem: mark the document as failed, do not retry."""


# ------------------------------------------------------------------ prompt

SYSTEM_PROMPT = """You are an experienced university professor and exam reviewer.
You receive one exam document (the exam paper given to students, possibly with
its answer key). Analyse it carefully and fill in every field of the requested
JSON schema:
- Identify every question/exercise with its number, type, points, topics,
  Bloom's taxonomy level and difficulty.
- Check that the points add up to the announced total and that the time
  allowed is realistic for an average student.
- Report concrete issues (ambiguous wording, missing data, errors, point
  mismatches, duplicated content) and actionable suggestions.
- If a value is not stated in the document, estimate it and say so in the
  text fields; use 0 for unknown numbers.
Write all text in the same language as the exam."""

USER_PROMPT = "Analyse this exam document."

LEVELS = ["easy", "medium", "hard"]
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "exam_title": {"type": "string"},
        "course": {"type": "string"},
        "language": {"type": "string"},
        "duration_minutes": {"type": "integer"},
        "total_points": {"type": "number"},
        "summary": {"type": "string"},
        "overall_difficulty": {"type": "string", "enum": LEVELS},
        "estimated_completion_minutes": {"type": "integer"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "string"},
                    "title": {"type": "string"},
                    "type": {"type": "string", "enum": [
                        "multiple_choice", "true_false", "short_answer",
                        "long_answer", "calculation", "design", "practical",
                        "case_study", "other"]},
                    "points": {"type": "number"},
                    "topics": {"type": "array", "items": {"type": "string"}},
                    "bloom_level": {"type": "string", "enum": [
                        "remember", "understand", "apply", "analyze",
                        "evaluate", "create"]},
                    "difficulty": {"type": "string", "enum": LEVELS},
                },
                "required": ["number", "title", "type", "points", "topics",
                             "bloom_level", "difficulty"],
                "additionalProperties": False,
            },
        },
        "topic_coverage": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "points": {"type": "number"},
                },
                "required": ["topic", "points"],
                "additionalProperties": False,
            },
        },
        "strengths": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["exam_title", "course", "language", "duration_minutes",
                 "total_points", "summary", "overall_difficulty",
                 "estimated_completion_minutes", "questions", "topic_coverage",
                 "strengths", "issues", "suggestions"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------ handler

def lambda_handler(event, context):
    """Called by the SQS trigger. Use batch size 1: if this function raises,
    SQS makes the message visible again and retries it (then sends it to the
    dead-letter queue after the max receive count)."""
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        if body.get("Event") == "s3:TestEvent":  # sent once when you configure the notification
            log.info("S3 test event, ignored")
            continue
        for s3_record in body.get("Records", []):
            bucket = s3_record["s3"]["bucket"]["name"]
            key = urllib.parse.unquote_plus(s3_record["s3"]["object"]["key"])
            started = time.monotonic()
            try:
                process_document(bucket, key)
            finally:
                # Simple rate limit: never take less than MIN_SECONDS_PER_DOCUMENT
                wait = MIN_SECONDS_PER_DOCUMENT - (time.monotonic() - started)
                if wait > 0:
                    time.sleep(wait)
    return {"status": "ok"}


def process_document(bucket: str, key: str) -> None:
    doc_id = PurePosixPath(key).stem  # uploads/<id>.pdf -> <id>
    log.info("Processing s3://%s/%s (document %s)", bucket, key, doc_id)

    item = wait_for_item(doc_id)
    if item is None:
        # The web app writes S3 first, then DynamoDB; give it time.
        raise RetryLater(f"No DynamoDB item for {doc_id} yet")

    set_status(doc_id, "processing", processing_started_at=now())
    try:
        content = download(bucket, key)
        if content is None:
            log.info("Object deleted before processing, nothing to do")
            return
        blocks = build_content(content, item.get("filename", key), item.get("content_type", ""))
        result, usage = ask_claude(blocks)
    except CannotProcess as exc:
        log.warning("Document %s failed: %s", doc_id, exc)
        set_status(doc_id, "failed", error=str(exc), processed_at=now())
        return
    except RetryLater as exc:
        log.warning("Document %s will be retried: %s", doc_id, exc)
        set_status(doc_id, "retrying", error=str(exc))
        raise

    table.update_item(
        Key={"id": doc_id},
        UpdateExpression=(
            "SET #s = :s, analysis = :a, analysis_json = :j, model_id = :m, "
            "processed_at = :p, input_tokens = :i, output_tokens = :o REMOVE #e"
        ),
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={
            ":s": "done",
            ":a": result["summary"],
            ":j": json.dumps(result, ensure_ascii=False),
            ":m": MODEL_ID,
            ":p": now(),
            ":i": usage.get("input_tokens", 0),
            ":o": usage.get("output_tokens", 0),
        },
        ConditionExpression="attribute_exists(id)",  # document deleted meanwhile
    )
    log.info("Document %s done (%s in / %s out tokens)",
             doc_id, usage.get("input_tokens"), usage.get("output_tokens"))


# ------------------------------------------------------------------ steps

def wait_for_item(doc_id: str, attempts: int = 5) -> dict | None:
    for _ in range(attempts):
        item = table.get_item(Key={"id": doc_id}).get("Item")
        if item:
            return item
        time.sleep(1)
    return None


def download(bucket: str, key: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def build_content(content: bytes, filename: str, content_type: str) -> list[dict]:
    """Turn the file into Claude content blocks (PDF / image / text)."""
    ext = PurePosixPath(filename).suffix.lower()
    b64 = lambda data: base64.standard_b64encode(data).decode("ascii")

    if ext == ".pdf":
        doc = {"type": "document",
               "source": {"type": "base64", "media_type": "application/pdf", "data": b64(content)}}
    elif ext in (".png", ".jpg", ".jpeg"):
        media = "image/png" if ext == ".png" else "image/jpeg"
        doc = {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64(content)}}
    elif ext == ".txt":
        doc = {"type": "document",
               "source": {"type": "text", "media_type": "text/plain",
                          "data": content.decode("utf-8", errors="replace")}}
    elif ext == ".docx":
        doc = {"type": "document",
               "source": {"type": "text", "media_type": "text/plain", "data": docx_text(content)}}
    else:
        raise CannotProcess(f"File type {ext or content_type} is not supported for analysis")
    return [doc, {"type": "text", "text": USER_PROMPT}]


def docx_text(content: bytes) -> str:
    """Minimal .docx text extraction with the standard library."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise CannotProcess(f"Invalid .docx file: {exc}") from exc
    xml = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def ask_claude(blocks: list[dict]) -> tuple[dict, dict]:
    request = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": blocks}],
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA},
        },
    }
    try:
        response = bedrock.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps(request),
            contentType="application/json",
            accept="application/json",
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg = exc.response["Error"].get("Message", "")
        if code in TRANSIENT_ERRORS:
            raise RetryLater(f"Bedrock {code}: {msg}") from exc
        # AccessDeniedException (no model access / IAM), ValidationException, ResourceNotFound...
        raise CannotProcess(f"Bedrock {code}: {msg}") from exc
    except (ReadTimeoutError, BotoCoreError) as exc:
        raise RetryLater(f"Bedrock call failed: {exc}") from exc

    message = json.loads(response["body"].read())
    if message.get("stop_reason") == "refusal":
        raise CannotProcess("The model refused to analyse this document")
    if message.get("stop_reason") == "max_tokens":
        raise CannotProcess("The analysis was too long (max_tokens reached)")
    text = next((b["text"] for b in message.get("content", []) if b.get("type") == "text"), "")
    try:
        return json.loads(text), message.get("usage", {})
    except json.JSONDecodeError as exc:
        raise CannotProcess(f"Model returned invalid JSON: {exc}") from exc


def set_status(doc_id: str, status: str, **fields) -> None:
    names = {"#s": "status"}
    values = {":s": status}
    sets = ["#s = :s"]
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"], values[f":f{i}"] = k, v
        sets.append(f"#f{i} = :f{i}")
    try:
        table.update_item(
            Key={"id": doc_id},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(id)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
