# -*- coding: utf-8 -*-
"""FastAPI wrapper for product_intake.py.

Deploy this module on Zeabur, then let n8n call /run on a cron schedule.
Credentials are read from environment variables only.
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
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
ACTIVE_RUN: Dict[str, Any] = {}
LAST_LOCK_ALERT_AT: Optional[datetime] = None
LOCK_ALERT_AFTER_SEC = int(os.getenv("PRODUCT_INTAKE_LOCK_ALERT_AFTER_SEC", "600"))
LOCK_ALERT_COOLDOWN_SEC = int(os.getenv("PRODUCT_INTAKE_LOCK_ALERT_COOLDOWN_SEC", "1800"))

APP_VERSION = "2026-07-01-sku-validation-card"

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
    for key in ("results", "errors"):
        rows = result.get(key) or []
        if isinstance(rows, list):
            events.extend([item for item in rows if isinstance(item, dict)])
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


def send_interactive_card(
    client: product_intake.FeishuClient,
    receive_id_type: str,
    receive_id: str,
    card: Dict[str, Any],
) -> None:
    if not receive_id:
        return
    client.request(
        "POST",
        f"/im/v1/messages?receive_id_type={receive_id_type}",
        body={
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
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


def now_bj() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def short_text(value: str, limit: int = 80) -> str:
    value = value.replace("\n", " ").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def active_run_age_sec(active: Dict[str, Any]) -> int:
    started_at = active.get("started_at")
    if not isinstance(started_at, datetime):
        return 0
    return max(0, int((now_utc() - started_at).total_seconds()))


def should_notify_lock_alert(active: Dict[str, Any]) -> bool:
    global LAST_LOCK_ALERT_AT
    age_sec = active_run_age_sec(active)
    if age_sec < LOCK_ALERT_AFTER_SEC:
        return False
    now = now_utc()
    if LAST_LOCK_ALERT_AT and (now - LAST_LOCK_ALERT_AT).total_seconds() < LOCK_ALERT_COOLDOWN_SEC:
        return False
    LAST_LOCK_ALERT_AT = now
    return True


def classify_alert(endpoint: str, result: Dict[str, Any], markers: List[Dict[str, Any]]) -> Dict[str, str]:
    if result.get("locked"):
        return {
            "level": "P2",
            "template": "yellow",
            "title": "🟡 [LOG·P2] 新品建档运行锁 · 自动保护",
            "problem": "系统正在处理上一轮任务，本轮请求被运行锁拦截，避免 SKU 序号并发撞号。",
            "owner_action": "采购无需处理。若连续 10 分钟仍出现，请把本卡转给系统负责人排查上一轮任务是否卡住。",
        }
    actions = {str(item.get("action") or "") for item in markers}
    if "create_failed" in actions:
        if markers_have_lingxing_sku_rule_error(markers):
            problem = "领星拒绝建品：ERP SKU 含中文或非法字符，产品没有写入领星。"
        else:
            problem = "领星建品接口返回失败，产品没有成功写入领星。"
    elif "create_error" in actions:
        problem = "系统无法组装领星建品参数，通常是 SKU/品名/类目或物理字段不完整。"
    elif "compose_error" in actions:
        problem = "系统无法合成 ERP SKU/ERP 品名，通常是品牌、类目配置、款式等关键字段缺失或不符合规则。"
    else:
        problem = result.get("error") or "服务执行失败，未能完成本轮新品建档任务。"
    return {
        "level": "P1",
        "template": "red" if "create_failed" in actions else "orange",
        "title": "🟠 [LOG·P1] 新品建档异常 · 需处理",
        "problem": problem,
        "owner_action": owner_action_for_markers(markers),
    }


def load_product_snapshots(record_ids: List[str]) -> Dict[str, Dict[str, str]]:
    app_id = os.getenv("FEISHU_APP2_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP2_SECRET", "").strip()
    if not app_id or not app_secret:
        return {}
    client = product_intake.FeishuClient(app_id, app_secret)
    snapshots: Dict[str, Dict[str, str]] = {}
    for rid in dict.fromkeys([item for item in record_ids if item]):
        try:
            row = product_intake.get_record(client, rid)
            fields = row.get("fields", {})
            snapshots[rid] = {
                "sku": product_intake.cell_text(fields.get(product_intake.FIELD_ERP_SKU)),
                "name": product_intake.cell_text(fields.get(product_intake.FIELD_ERP_NAME)),
                "brand": product_intake.cell_text(fields.get(product_intake.FIELD_BRAND)),
                "factory_model": product_intake.cell_text(fields.get(product_intake.FIELD_FACTORY_MODEL)),
                "style": product_intake.cell_text(fields.get(product_intake.FIELD_STYLE)),
                "status": product_intake.cell_text(fields.get(product_intake.STATUS_FIELD)),
                "record_url": product_intake.record_url(rid),
            }
        except Exception as exc:
            snapshots[rid] = {
                "sku": "",
                "name": "",
                "brand": "",
                "factory_model": "",
                "style": "",
                "status": "",
                "record_url": product_intake.record_url(rid),
                "load_error": f"{type(exc).__name__}: {exc}",
            }
    return snapshots


def format_product_block(record_ids: List[str], products: Dict[str, Dict[str, str]]) -> str:
    if not record_ids:
        return "- 本次异常没有绑定具体产品记录。"
    lines: List[str] = []
    for rid in record_ids:
        info = products.get(rid, {})
        title = info.get("name") or info.get("sku") or info.get("factory_model") or info.get("style") or "未合成产品"
        details = []
        if info.get("sku"):
            details.append(f"SKU: {info['sku']}")
        if info.get("brand"):
            details.append(f"品牌: {info['brand']}")
        if info.get("factory_model"):
            details.append(f"工厂型号: {info['factory_model']}")
        if info.get("style"):
            details.append(f"款式: {info['style']}")
        if info.get("status"):
            details.append(f"状态: {info['status']}")
        lines.append(f"- [{short_text(title, 40)}]({info.get('record_url') or product_intake.record_url(rid)})")
        if details:
            lines.append("  " + "；".join(details))
    return "\n".join(lines)


def format_error_block(markers: List[Dict[str, Any]], result: Dict[str, Any]) -> str:
    if result.get("locked"):
        return "- 运行锁：上一轮任务仍在执行，本轮被拦截。不是采购资料错误。"
    if not markers and result.get("error"):
        return f"- 服务错误：{short_text(str(result.get('error')), 180)}"
    if markers_have_lingxing_sku_rule_error(markers):
        skus = unique_marker_skus(markers)
        sku_lines = [f"  - `{sku}`" for sku in skus[:8]]
        more = f"\n  - 另有 {len(skus) - 8} 个 SKU 同类问题" if len(skus) > 8 else ""
        return "\n".join(
            [
                "- 根因：领星 SKU 字符规则校验失败，ERP SKU 里出现中文或非法字符。",
                "- 需要修改：检查「颜色变体」和「套餐变体」，改成英文/数字代码；例如黑色=`BK`、白色=`WH`、银色=`SL` 或公司约定代码。",
                "- 本次异常 SKU：",
                *sku_lines,
                more,
                "- 领星规则：SKU 只允许字母、数字、下划线、短横线、英文点、井号、斜杆等字符，不允许中文。",
            ]
        ).strip()
    lines: List[str] = []
    seen: set[tuple[str, str, str]] = set()
    for item in markers:
        action = str(item.get("action") or "error")
        rid = str(item.get("record_id") or "无记录")
        raw = marker_raw_text(item)
        human = humanize_error(action, raw)
        key = (action, rid, human)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {action} / {rid}：{human}")
        if raw and human != raw and len(lines) <= 6:
            lines.append(f"  原始信息：{short_text(raw, 160)}")
    return "\n".join(lines) if lines else "- 未识别到具体错误，请看 replay 命令复现。"


def marker_raw_text(item: Dict[str, Any]) -> str:
    raw = item.get("error") if item.get("error") is not None else item.get("result", "")
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, ensure_ascii=False)


def markers_have_lingxing_sku_rule_error(markers: List[Dict[str, Any]]) -> bool:
    for item in markers:
        text = marker_raw_text(item)
        if "SKU 只允许" in text or ("SKU" in text and "只允许" in text and "英文" in text):
            return True
    return False


def unique_marker_skus(markers: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in markers:
        sku = str(item.get("sku") or "").strip()
        if not sku:
            text = marker_raw_text(item)
            found = re.findall(r"[A-Z]{2,}-[A-Z0-9]{2,}-[A-Z0-9]{2,}-[0-9]{3}(?:-[^\s\"',，；;]+)?", text)
            sku = found[0] if found else ""
        if sku and sku not in seen:
            seen.add(sku)
            out.append(sku)
    return out


def owner_action_for_markers(markers: List[Dict[str, Any]]) -> str:
    if markers_have_lingxing_sku_rule_error(markers):
        return "采购先打开上方产品记录，把「颜色变体/套餐变体」里的中文改成英文 SKU 代码；修好后勾选「采购已修改」重新提交。系统负责人只在不确定代码规则时介入。"
    return "请采购先打开记录，按“具体报错/下一步”修正字段；无法判断时转系统负责人处理。修好后勾选「采购已修改」重新提交。"


def humanize_error(action: str, raw: str) -> str:
    text = raw or ""
    if "品牌缺" in text:
        return "品牌为空。请采购在「品牌」字段填写真实品牌；供应商名不要填进品牌。"
    if "品牌未配置品牌码" in text:
        return "品牌没有配置品牌码。请先确认是否真实品牌；真实品牌需找系统负责人补品牌码。"
    if "品牌疑似供应商名" in text:
        return "品牌疑似填了供应商/公司名。请改成真实品牌；铺货通用款填「白牌」。"
    if "类目配置缺" in text:
        return "类目配置为空。请选择最接近的平台+叶子类目；没有合适项找系统负责人补类目。"
    if "类目配置未找到" in text:
        return "关联的类目配置不存在或被删。请重新选择类目配置。"
    if "款式缺" in text:
        return "款式为空。请填写外观/模具/功能形态短名，不要混入平台、颜色、工厂型号。"
    if "ERP SKU/ERP品名未合成" in text:
        return "记录还没有成功合成 ERP SKU/ERP 品名，不能进入领星建品。"
    if "ERP SKU 含非法字符" in text:
        return "ERP SKU 含中文或非法字符。请把颜色/套餐变体改成英文 SKU 代码后重新提交。"
    if "SKU 只允许" in text:
        return "ERP SKU 含中文或非法字符。请把颜色/套餐变体改成英文 SKU 代码后重新提交。"
    if action == "create_failed":
        return "领星接口拒绝建品。通常需要系统负责人查看领星返回内容。"
    return short_text(text, 180) or "未提供错误明细。"


def build_alert_card(endpoint: str, result: Dict[str, Any], run_id: str, args: List[str]) -> Dict[str, Any]:
    markers = error_events(result)
    record_ids: List[str] = []
    for item in markers:
        rid = str(item.get("record_id") or "")
        if rid:
            record_ids.append(rid)
    record_ids = list(dict.fromkeys(record_ids))
    products = load_product_snapshots(record_ids)
    profile = classify_alert(endpoint, result, markers)
    first_record = record_ids[0] if record_ids else ""
    replay = replay_command(endpoint, first_record)
    product_block = format_product_block(record_ids, products)
    error_block = format_error_block(markers, result)

    elements: List[Dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(
                    [
                        "**时间**：" + now_bj(),
                        "**影响环节**：" + endpoint,
                        "**问题说明**：" + profile["problem"],
                    ]
                ),
            },
        },
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "**影响产品**\n" + product_block}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "**具体报错**\n" + error_block}},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**下一步**\n"
                + "\n".join(
                    [
                        "- " + profile["owner_action"],
                        "- 技术排查用 replay：`" + replay + "`",
                        "- run_id：`" + run_id + "`；code：`" + str(result.get("code")) + "`",
                    ]
                ),
            },
        },
    ]
    if first_record:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开产品记录"},
                        "type": "primary",
                        "url": product_intake.record_url(first_record),
                    }
                ],
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": profile["template"],
            "title": {"tag": "plain_text", "content": profile["title"]},
        },
        "elements": elements,
    }


def notify_failure(endpoint: str, result: Dict[str, Any], run_id: str, args: List[str]) -> Dict[str, Any]:
    event_app_id = os.getenv("FEISHU_EVENT_APP_ID", "").strip()
    event_app_secret = os.getenv("FEISHU_EVENT_APP_SECRET", "").strip()
    if not event_app_id or not event_app_secret:
        return {"sent": False, "error": "missing FEISHU_EVENT_APP_ID/SECRET"}

    client = product_intake.FeishuClient(event_app_id, event_app_secret)
    card = build_alert_card(endpoint, result, run_id, args)
    sent: List[str] = []
    errors: List[str] = []
    try:
        send_interactive_card(client, "chat_id", ALERT_CHAT_ID, card)
        sent.append("chat")
    except Exception as exc:
        errors.append(f"chat: {type(exc).__name__}: {exc}")

    try:
        if ALERT_OPEN_ID:
            send_interactive_card(client, "open_id", ALERT_OPEN_ID, card)
            sent.append("open_id")
        elif ALERT_UNION_ID:
            send_interactive_card(client, "union_id", ALERT_UNION_ID, card)
            sent.append("union_id")
        else:
            open_id = resolve_open_id(client, ALERT_EMAIL)
            if open_id:
                send_interactive_card(client, "open_id", open_id, card)
                sent.append("email_open_id")
    except Exception as exc:
        errors.append(f"user: {type(exc).__name__}: {exc}")

    return {"sent": bool(sent), "targets": sent, "errors": errors, "card_title": card["header"]["title"]["content"]}


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
        active = dict(ACTIVE_RUN)
        result = {
            "ok": False,
            "code": 423,
            "locked": True,
            "error": f"another product-intake run is active: {active.get('run_id', 'unknown')}",
            "active_run_id": active.get("run_id", "unknown"),
            "active_endpoint": active.get("endpoint", "unknown"),
            "active_age_sec": active_run_age_sec(active),
            "stdout": [],
        }
        if notify and should_notify_lock_alert(active):
            result["alert"] = notify_failure(endpoint, result, run_id, args)
        elif notify:
            result["alert"] = {
                "sent": False,
                "suppressed": True,
                "reason": "short_lock_overlap",
                "threshold_sec": LOCK_ALERT_AFTER_SEC,
                "cooldown_sec": LOCK_ALERT_COOLDOWN_SEC,
            }
        return result

    if lock_required:
        ACTIVE_RUN.clear()
        ACTIVE_RUN.update({"run_id": run_id, "endpoint": endpoint, "started_at": now_utc()})
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
            "lock_alert_after_sec": LOCK_ALERT_AFTER_SEC,
            "lock_alert_cooldown_sec": LOCK_ALERT_COOLDOWN_SEC,
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
