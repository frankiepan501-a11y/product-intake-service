# -*- coding: utf-8 -*-
"""FastAPI wrapper for product_intake.py.

Deploy this module on Zeabur, then let n8n call /run on a cron schedule.
Credentials are read from environment variables only.
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import product_intake


APP_TOKEN = os.getenv("PRODUCT_INTAKE_SERVICE_TOKEN", "")

app = FastAPI(title="Product Intake Service", version="1.0.0")


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


def invoke(args: List[str]) -> Dict[str, Any]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = product_intake.main(args)
    return {"ok": code == 0, "code": code, "stdout": buf.getvalue().splitlines()}


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


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
    return invoke(args)


@app.post("/send-card")
def send_card(req: RunRequest, authorization: str = Header(default="")) -> Dict[str, Any]:
    check_auth(authorization)
    if not req.record_id:
        raise HTTPException(status_code=400, detail="record_id is required")
    args: List[str] = ["--record-id", req.record_id]
    if not req.dry_run:
        args.append("--commit")
    args.append("send-card")
    return invoke(args)


@app.post("/create-confirmed")
def create_confirmed(req: RunRequest, authorization: str = Header(default="")) -> Dict[str, Any]:
    check_auth(authorization)
    args: List[str] = []
    if req.record_id:
        args += ["--record-id", req.record_id]
    if not req.dry_run:
        args.append("--commit")
    args.append("create-confirmed")
    return invoke(args)
