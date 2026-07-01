# -*- coding: utf-8 -*-
"""FastAPI wrapper for product_intake.py.

Deploy this module on Zeabur, then let n8n call /run on a cron schedule.
Credentials are read from environment variables only.
"""

from __future__ import annotations

import io
import json
import os
import threading
import uuid
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import product_intake


APP_TOKEN = os.getenv("PRODUCT_INTAKE_SERVICE_TOKEN", "")
ALERT_CHAT_ID = os.getenv("PRODUCT_INTAKE_ALERT_CHAT_ID", product_intake.DEFAULT_CONFIRM_CHAT_ID)
ALERT_EMAIL = os.getenv("PRODUCT_INTAKE_ALERT_EMAIL", "frankiepan501@gmail.com")
ALERT_OPEN_ID = os.getenv("PRODUCT_INTAKE_ALERT_OPEN_ID", "")
ALERT_UNION_ID = os.getenv("PRODUCT_INTAKE_ALERT_UNION_ID", "")
ERROR_ACTIONS = {"compose_error", "create_error", "create_failed"}
MUTATING_LOCK = threading.Lock()
ACTIVE_RUN: Dict[str, str] = {}

APP_VERSION = "2026-07-01-p0-guards"

app = FastAPI(title="Product Intake Service", version=APP_VERSION)


class RunRequest(BaseModel):
    dry_run: bool = True
    send_card: bool = True
    record_id: Optional[str] = None


def check_auth(authorization: str = "") -> None:
    if not APP_TOKEN:
        return
    expected = "Bearer " + APP_TOKEN
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def parse_events(stdout: List[str]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for line in stdout:
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            events.append(data)
    return events


def error_events(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    events = parse_events(result.get("stdout") or [])
    return [item for item in events if item.get("action") in ERROR_ACTIONS]


def replay_command(endpoint: str, record_id: str = "") -> str:
    base = "python C:/Users/Administrator/scripts/product_intake/product_intake.py"
    record = f" --record-id {record_id}" if record_id else ""
    if endpoint == "create-confirmed":
        return f"{base}{record} create-confirmed"
    if endpoint == "send-card":
        return f"{base}{record} send-card"
    return f"{base}{record} --send-card run"


def send_text(client: product_intake.FeishuClient, receive_id_type: str, receive_id: str, text: str) -> None:
    if not receive_id:
        return
    client.request(
        "POST",
        f"/im/v1/messages?receive_id_type={receive_id_type}",
        body={
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    )


def resolve_open_id(client: product_intake.FeishuClient, email: str) -> str:
    if not email:
        return ""
    data = client.request(
        "POST",
        "/contact/v3/users/batch_get_id?user_id_type=open_id",
        body={"emails": [email]},
    )
    for item in data.get("data", {}).get("user_list", []):
        if item.get("user_id"):
            return str(item["user_id"])
    return ""


def notify_failure(endpoint: str, result: Dict[str, Any], run_id: str, args: List[str]) -> Dict[str, Any]:
    event_app_id = os.getenv("FEISHU_EVENT_APP_ID", "").strip()
    event_app_secret = os.getenv("FEISHU_EVENT_APP_SECRET", "").strip()
    if not event_app_id or not event_app_secret:
        return {"sent": False, "error": "missing FEISHU_EVENT_APP_ID/SECRET"}

    markers = error_events(result)
    record_ids = []
    lines = []
    for item in markers:
        rid = str(item.get("record_id") or "")
        if rid:
            record_ids.append(rid)
        msg = item.get("error") or item.get("result") or item
        lines.append(f"- {item.get('action')}: {rid or 'no-record'} | {msg}")

    if not lines and result.get("error"):
        lines.append(f"- service_error: {result.get('error')}")
    if not lines and result.get("locked"):
        lines.append(f"- lock_busy: {result.get('error')}")

    first_record = record_ids[0] if record_ids else ""
    text = "\n".join(
        [
            "[PROD·P1] 产品新品建档异常",
            f"endpoint: {endpoint}",
            f"run_id: {run_id}",
            f"record_id: {', '.join(record_ids) if record_ids else '-'}",
            f"ok/code: {result.get('ok')} / {result.get('code')}",
            "errors:",
            *(lines or ["- unknown error"]),
            f"replay: {replay_command(endpoint, first_record)}",
            f"args: {' '.join(args)}",
        ]
    )

    client = product_intake.FeishuClient(event_app_id, event_app_secret)
    sent: List[str] = []
    errors: List[str] = []
    try:
        send_text(client, "chat_id", ALERT_CHAT_ID, text)
        sent.append("chat")
    except Exception as exc:
        errors.append(f"chat: {type(exc).__name__}: {exc}")

    try:
        if ALERT_OPEN_ID:
            send_text(client, "open_id", ALERT_OPEN_ID, text)
            sent.append("open_id")
        elif ALERT_UNION_ID:
            send_text(client, "union_id", ALERT_UNION_ID, text)
            sent.append("union_id")
        else:
            open_id = resolve_open_id(client, ALERT_EMAIL)
            if open_id:
                send_text(client, "open_id", open_id, text)
                sent.append("email_open_id")
    except Exception as exc:
        errors.append(f"user: {type(exc).__name__}: {exc}")

    return {"sent": bool(sent), "targets": sent, "errors": errors}


def invoke(args: List[str]) -> Dict[str, Any]:
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = product_intake.main(args)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else 1
        return {
            "ok": code == 0,
            "code": code,
            "error": str(exc),
            "stdout": buf.getvalue().splitlines(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "code": 1,
            "error": f"{type(exc).__name__}: {exc}",
            "stdout": buf.getvalue().splitlines(),
        }
    result = {"ok": code == 0, "code": code, "stdout": buf.getvalue().splitlines()}
    markers = error_events(result)
    if markers:
        result["ok"] = False
        result["errors"] = markers
    return result


def run_command(endpoint: str, args: List[str], lock_required: bool, notify: bool) -> Dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    if lock_required and not MUTATING_LOCK.acquire(blocking=False):
        result = {
            "ok": False,
            "code": 423,
            "locked": True,
            "error": f"another product-intake run is active: {ACTIVE_RUN.get('run_id', 'unknown')}",
            "stdout": [],
        }
        if notify:
            result["alert"] = notify_failure(endpoint, result, run_id, args)
        return result

    if lock_required:
        ACTIVE_RUN.clear()
        ACTIVE_RUN.update({"run_id": run_id, "endpoint": endpoint})
    try:
        result = invoke(args)
        result["run_id"] = run_id
        if notify and not result.get("ok"):
            result["alert"] = notify_failure(endpoint, result, run_id, args)
        return result
    finally:
        if lock_required:
            ACTIVE_RUN.clear()
            MUTATING_LOCK.release()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "features": {
            "run_lock": True,
            "error_alert": True,
            "resubmit_field": product_intake.FIELD_RESUBMIT,
        },
        "alert_config": {
            "event_app_configured": bool(os.getenv("FEISHU_EVENT_APP_ID", "").strip())
            and bool(os.getenv("FEISHU_EVENT_APP_SECRET", "").strip()),
            "chat_configured": bool(ALERT_CHAT_ID),
            "user_route_configured": bool(ALERT_OPEN_ID or ALERT_UNION_ID or ALERT_EMAIL),
        },
    }


@app.post("/run")
def run(req: RunRequest, authorization: str = Header(default="")) -> Dict[str, Any]:
    check_auth(authorization)
    args: List[str] = []
    if req.record_id:
        args += ["--record-id", req.record_id]
    if not req.dry_run:
        args.append("--commit")
    if req.send_card:
        args.append("--send-card")
    args.append("run")
    return run_command("run", args, lock_required=not req.dry_run, notify=not req.dry_run)


@app.post("/send-card")
def send_card(req: RunRequest, authorization: str = Header(default="")) -> Dict[str, Any]:
    check_auth(authorization)
    if not req.record_id:
        raise HTTPException(status_code=400, detail="record_id is required")
    args: List[str] = ["--record-id", req.record_id]
    if not req.dry_run:
        args.append("--commit")
    args.append("send-card")
    return run_command("send-card", args, lock_required=not req.dry_run, notify=not req.dry_run)


@app.post("/create-confirmed")
def create_confirmed(req: RunRequest, authorization: str = Header(default="")) -> Dict[str, Any]:
    check_auth(authorization)
    args: List[str] = []
    if req.record_id:
        args += ["--record-id", req.record_id]
    if not req.dry_run:
        args.append("--commit")
    args.append("create-confirmed")
    return run_command("create-confirmed", args, lock_required=not req.dry_run, notify=not req.dry_run)
