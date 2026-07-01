# -*- coding: utf-8 -*-
"""Product intake automation for Feishu product information records.

This replaces the temporary scratchpad scripts used during the first test:

- backfill empty "建档状态" to "待合成" for valid form submissions
- compose ERP SKU / ERP product name for rows in "待合成"
- send a Feishu confirmation card for human review

All credentials must come from environment variables. The script never stores
secrets in source code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


FEISHU_BASE = "https://open.feishu.cn/open-apis"

BASE_TOKEN = os.getenv("PRODUCT_INTAKE_BASE_TOKEN", "MvtZb6OE9aJFaisO913cWSErnFe")
PRODUCT_TABLE_ID = os.getenv("PRODUCT_INTAKE_TABLE_ID", "tblTvqipcTBFRUkr")
CATEGORY_TABLE_ID = os.getenv("PRODUCT_INTAKE_CATEGORY_TABLE_ID", "tbluZxoiRo1L0BLT")
BRAND_TABLE_ID = os.getenv("PRODUCT_INTAKE_BRAND_TABLE_ID", "tblYKn7n7DURgwBM")

DEFAULT_CONFIRM_EMAIL = os.getenv("PRODUCT_INTAKE_CONFIRM_EMAIL", "")
DEFAULT_CONFIRM_UNION_ID = os.getenv("PRODUCT_INTAKE_CONFIRM_UNION_ID", "")
DEFAULT_CONFIRM_OPEN_ID = os.getenv("PRODUCT_INTAKE_CONFIRM_OPEN_ID", "")
DEFAULT_CONFIRM_CHAT_ID = os.getenv("PRODUCT_INTAKE_CONFIRM_CHAT_ID", "oc_73d455d69842f2104da68201dc282677")

STATUS_FIELD = "建档状态"
STATUS_TODO = "待合成"
STATUS_CONFIRM = "待确认"
STATUS_FAILED = "失败"
STATUS_CREATE = "确认建品"
STATUS_BUILT = "已建领星"
STATUS_REVISE = "待修改"

FIELD_ERP_SKU = "ERP SKU"
FIELD_ERP_NAME = "ERP品名"
FIELD_BRAND = "品牌"
FIELD_CATEGORY = "类目配置"
FIELD_FACTORY_MODEL = "工厂型号"
FIELD_STYLE = "款式"
FIELD_FEATURE = "特性"
FIELD_SPEC = "规格"
FIELD_COLOR = "颜色变体"
FIELD_BUNDLE = "套餐变体"
FIELD_MAIN_PLATFORM = "主平台"
FIELD_COMPAT_PLATFORM = "兼容平台"
FIELD_DATA_GAP = "数据缺口"
FIELD_L1 = "一级分类"
FIELD_L2 = "二级分类"
FIELD_PRODUCT_TYPE = "产品类型"
FIELD_STATUS = "状态"
FIELD_RESUBMIT = os.getenv("PRODUCT_INTAKE_RESUBMIT_FIELD", "采购已修改")
FIELD_SUBMITTER = os.getenv("PRODUCT_INTAKE_SUBMITTER_FIELD", "录入采购")

LX_CF_MAIN_PLATFORM = os.getenv("LINGXING_CF_MAIN_PLATFORM", "207716196418609155")
LX_CF_COMPAT_PLATFORM = os.getenv("LINGXING_CF_COMPAT_PLATFORM", "207716196418609153")


DEFAULT_BRAND_CODES: Dict[str, str] = {
    "FUNLAB": "FL",
    "POWKONG": "PK",
    "联游": "LY",
    "白牌": "WB",
    # 万利是品牌，不是供应商名；按用户确认纳入品牌口径。
    "万利": "WL",
}

BRAND_CODES = DEFAULT_BRAND_CODES


SUPPLIER_WORDS = (
    "供应商",
    "工厂",
    "贸易",
    "科技",
    "电子",
    "公司",
    "有限公司",
    "实业",
)

SKU_ALLOWED_RE = re.compile(r"^[A-Za-z0-9_.#/\-]+$")
COLOR_CODE_ALIASES: Dict[str, str] = {
    "黑": "BK",
    "黑色": "BK",
    "雅黑": "BK",
    "白": "WH",
    "白色": "WH",
    "米白": "WH",
    "红": "RD",
    "红色": "RD",
    "蓝": "BL",
    "蓝色": "BL",
    "绿": "GN",
    "绿色": "GN",
    "粉": "PK",
    "粉色": "PK",
    "灰": "GY",
    "灰色": "GY",
    "深灰": "GY",
    "浅灰": "GY",
    "黄": "YE",
    "黄色": "YE",
    "橙": "OR",
    "橙色": "OR",
    "紫": "PU",
    "紫色": "PU",
    "银": "SL",
    "银色": "SL",
    "金": "GD",
    "金色": "GD",
    "透明": "CT",
    "透明色": "CT",
    "清透": "CT",
}


@dataclass
class Config:
    feishu_app2_id: str
    feishu_app2_secret: str
    feishu_event_app_id: str
    feishu_event_app_secret: str
    lingxing_proxy_url: str
    lingxing_proxy_token: str

    @classmethod
    def from_env(cls, require_event: bool = False, require_lingxing: bool = False) -> "Config":
        def env(name: str, required: bool = True) -> str:
            value = os.getenv(name, "").strip()
            if required and not value:
                raise SystemExit(f"Missing required env: {name}")
            return value

        return cls(
            feishu_app2_id=env("FEISHU_APP2_ID"),
            feishu_app2_secret=env("FEISHU_APP2_SECRET"),
            feishu_event_app_id=env("FEISHU_EVENT_APP_ID", require_event),
            feishu_event_app_secret=env("FEISHU_EVENT_APP_SECRET", require_event),
            lingxing_proxy_url=env("LINGXING_PROXY_URL", require_lingxing),
            lingxing_proxy_token=env("LINGXING_PROXY_TOKEN", require_lingxing),
        )


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: Optional[str] = None

    def token(self) -> str:
        if self._token:
            return self._token
        data = self.request(
            "POST",
            "/auth/v3/tenant_access_token/internal",
            body={"app_id": self.app_id, "app_secret": self.app_secret},
            auth=False,
        )
        self._token = data["tenant_access_token"]
        return self._token

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        url = FEISHU_BASE + path
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if auth:
            req.add_header("Authorization", "Bearer " + self.token())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Feishu HTTP {exc.code}: {detail}") from exc
        if data.get("code") not in (None, 0):
            raise RuntimeError(f"Feishu API error: {json.dumps(data, ensure_ascii=False)}")
        return data


class LingxingProxy:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token

    def call(self, method: str, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps({"method": method, "path": path, "params": params}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Lingxing proxy HTTP {exc.code}: {detail}") from exc


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        if not value:
            return ""
        parts: List[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("text") is not None:
                    parts.append(str(item.get("text")))
                elif item.get("name") is not None:
                    parts.append(str(item.get("name")))
                elif item.get("value") is not None:
                    parts.append(str(item.get("value")))
            else:
                parts.append(str(item))
        return ",".join([p.strip() for p in parts if p and p.strip()])
    if isinstance(value, dict):
        if isinstance(value.get("value"), list):
            return cell_text(value.get("value"))
        if value.get("text") is not None:
            return str(value.get("text")).strip()
        if value.get("name") is not None:
            return str(value.get("name")).strip()
    return str(value).strip()


def cell_list(value: Any) -> List[str]:
    text = cell_text(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,，/、]+", text) if part.strip()]


def link_record_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        ids = value.get("link_record_ids") or value.get("record_ids") or []
        return ids[0] if ids else None
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            ids = first.get("record_ids") or first.get("link_record_ids") or []
            if ids:
                return ids[0]
            return first.get("record_id") or first.get("id")
        return str(first)
    return None


def list_records(client: FeishuClient, table_id: str, page_size: int = 500) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    page_token = ""
    while True:
        qs = f"?page_size={page_size}"
        if page_token:
            qs += "&page_token=" + page_token
        data = client.request(
            "POST",
            f"/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search{qs}",
            body={"automatic_fields": True},
        )
        payload = data.get("data", {})
        items.extend(payload.get("items", []))
        if not payload.get("has_more"):
            return items
        page_token = payload.get("page_token") or ""


def update_record(client: FeishuClient, record_id: str, fields: Dict[str, Any]) -> None:
    client.request(
        "PUT",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{PRODUCT_TABLE_ID}/records/{record_id}",
        body={"fields": fields},
    )


def get_record(client: FeishuClient, record_id: str) -> Dict[str, Any]:
    data = client.request(
        "GET",
        f"/bitable/v1/apps/{BASE_TOKEN}/tables/{PRODUCT_TABLE_ID}/records/{record_id}",
    )
    return data.get("data", {}).get("record", {})


def load_categories(client: FeishuClient) -> Dict[str, Dict[str, str]]:
    rows = list_records(client, CATEGORY_TABLE_ID)
    out: Dict[str, Dict[str, str]] = {}
    wanted = ("品类码", "平台码", "平台中文", "产品类型词", "一级类目", "二级类目", "叶子类目", "配置名", "cid")
    for row in rows:
        fields = row.get("fields", {})
        out[row["record_id"]] = {key: cell_text(fields.get(key)) for key in wanted}
    return out


def load_brand_codes(client: FeishuClient) -> Dict[str, str]:
    try:
        rows = list_records(client, BRAND_TABLE_ID)
    except Exception as exc:
        print(json.dumps({"action": "brand_config_fallback", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return dict(DEFAULT_BRAND_CODES)

    out: Dict[str, str] = {}
    for row in rows:
        fields = row.get("fields", {})
        if not cell_bool(fields.get("启用")):
            continue
        brand = cell_text(fields.get("品牌"))
        code = cell_text(fields.get("品牌码")).upper()
        if not brand or not code:
            continue
        out[brand] = code
    if not out:
        print(json.dumps({"action": "brand_config_fallback", "error": "empty enabled brand table"}, ensure_ascii=False), file=sys.stderr)
        return dict(DEFAULT_BRAND_CODES)
    return out


def product_rows_from_lingxing(proxy: LingxingProxy) -> List[Dict[str, Any]]:
    data = proxy.call("GET", "/erp/sc/routing/data/local_inventory/productList", {})
    payload = data.get("data")
    rows = payload if isinstance(payload, list) else payload.get("list", []) if isinstance(payload, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def product_skus_from_lingxing(proxy: LingxingProxy) -> List[str]:
    rows = product_rows_from_lingxing(proxy)
    return [str(row.get("sku")).strip() for row in rows if isinstance(row, dict) and row.get("sku")]


def brand_bid_map(product_rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for row in product_rows:
        brand_name = row.get("brand_name")
        bid = row.get("bid")
        if brand_name and bid and brand_name not in out:
            out[str(brand_name)] = bid
    return out


def next_sequence(prefix: str, existing_skus: Iterable[str]) -> str:
    max_num = 0
    pattern = re.compile("^" + re.escape(prefix) + r"(\d{3})(?:-|$)")
    for sku in existing_skus:
        match = pattern.match(sku)
        if match:
            max_num = max(max_num, int(match.group(1)))
    return f"{max_num + 1:03d}"


def validate_erp_sku(sku: str) -> None:
    if not sku:
        raise ValueError("ERP SKU 缺")
    if SKU_ALLOWED_RE.fullmatch(sku):
        return
    invalid = "".join(dict.fromkeys(ch for ch in sku if not re.match(r"[A-Za-z0-9_.#/\-]", ch)))
    raise ValueError(
        "ERP SKU 含非法字符"
        + (f"：{invalid}" if invalid else "")
        + "。颜色会由系统自动转成 SKU 代码；若仍报错，请补充颜色映射或检查套餐变体是否为英文/数字代码。"
    )


def sku_variant_code(value: str, kind: str = "variant") -> str:
    text = value.strip()
    if not text:
        return ""
    compact = re.sub(r"[\s_/／]+", "", text)
    if kind == "color":
        mapped = COLOR_CODE_ALIASES.get(compact)
        if mapped:
            return mapped
    if SKU_ALLOWED_RE.fullmatch(text):
        return text.upper() if kind == "color" else text
    raise ValueError(
        f"{'颜色变体' if kind == 'color' else '套餐变体'}无法转换为 SKU 代码：{text}。"
        + ("请在系统颜色映射表补充该颜色口径。" if kind == "color" else "请使用英文或数字代码。")
    )


def looks_like_supplier_as_brand(brand: str, brand_codes: Optional[Dict[str, str]] = None) -> bool:
    if not brand:
        return False
    if brand in (brand_codes or DEFAULT_BRAND_CODES):
        return False
    return any(word in brand for word in SUPPLIER_WORDS)


def validate_row(fields: Dict[str, Any], brand_codes: Optional[Dict[str, str]] = None) -> Tuple[List[str], List[str]]:
    brand_codes = brand_codes or DEFAULT_BRAND_CODES
    errors: List[str] = []
    warnings: List[str] = []
    brand = cell_text(fields.get(FIELD_BRAND))
    if not brand:
        errors.append("品牌缺")
    elif brand not in brand_codes:
        if looks_like_supplier_as_brand(brand, brand_codes):
            errors.append(f"品牌疑似供应商名: {brand}")
        else:
            errors.append(f"品牌未配置品牌码: {brand}")

    if not link_record_id(fields.get(FIELD_CATEGORY)):
        errors.append("类目配置缺")
    if not cell_text(fields.get(FIELD_STYLE)):
        errors.append("款式缺")
    main = cell_text(fields.get(FIELD_MAIN_PLATFORM))
    compatible = cell_list(fields.get(FIELD_COMPAT_PLATFORM))
    if main and compatible and main not in compatible:
        warnings.append(f"兼容平台未包含主平台: 主平台={main}, 兼容平台={','.join(compatible)}")

    factory_model = cell_text(fields.get(FIELD_FACTORY_MODEL))
    color = cell_text(fields.get(FIELD_COLOR))
    suffix = re.search(r"-(BK|WH|RD|BL|GN|PK|GY|GR|YE|OR|PU|SL|GD)$", factory_model, re.I)
    if factory_model and suffix and not color:
        warnings.append(f"工厂型号疑似含颜色尾缀 {suffix.group(1).upper()}，建议拆到颜色变体")

    return errors, warnings


def has_required_intake_fields(fields: Dict[str, Any]) -> bool:
    required = (
        FIELD_BRAND,
        FIELD_CATEGORY,
        FIELD_STYLE,
        FIELD_MAIN_PLATFORM,
        FIELD_COMPAT_PLATFORM,
    )
    return all(fields.get(name) not in (None, "", []) for name in required)


def cell_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = cell_text(value).lower()
    return text in ("true", "1", "yes", "y", "是", "已修改", "重新提交")


def should_default_to_todo(fields: Dict[str, Any]) -> bool:
    status = cell_text(fields.get(STATUS_FIELD))
    if not status:
        if cell_text(fields.get(FIELD_ERP_SKU)) or cell_text(fields.get(FIELD_ERP_NAME)):
            return False
        return has_required_intake_fields(fields)
    if status == STATUS_REVISE and cell_bool(fields.get(FIELD_RESUBMIT)):
        return has_required_intake_fields(fields)
    return False


def compose_row(
    fields: Dict[str, Any],
    categories: Dict[str, Dict[str, str]],
    existing_skus: Iterable[str],
    brand_codes: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    brand_codes = brand_codes or DEFAULT_BRAND_CODES
    errors, warnings = validate_row(fields, brand_codes)
    if errors:
        raise ValueError("; ".join(errors))

    brand = cell_text(fields.get(FIELD_BRAND))
    category_id = link_record_id(fields.get(FIELD_CATEGORY))
    category = categories.get(category_id or "")
    if not category:
        raise ValueError(f"类目配置未找到: {category_id}")

    prefix = f"{brand_codes[brand]}-{category['品类码']}-{category['平台码']}-"
    sequence = next_sequence(prefix, existing_skus)
    variants = [
        sku_variant_code(cell_text(fields.get(FIELD_COLOR)), "color"),
        sku_variant_code(cell_text(fields.get(FIELD_BUNDLE)), "bundle"),
    ]
    variants = [item for item in variants if item]
    sku = prefix + sequence + (("-" + "-".join(variants)) if variants else "")
    validate_erp_sku(sku)

    head = cell_text(fields.get(FIELD_FACTORY_MODEL)) or category["平台中文"]
    style = cell_text(fields.get(FIELD_STYLE))
    descriptors = [cell_text(fields.get(FIELD_FEATURE)), cell_text(fields.get(FIELD_SPEC))]
    descriptors = [item for item in descriptors if item]
    erp_name = f"{head}{category['产品类型词']}-{style}"
    if descriptors:
        erp_name += "(" + "/".join(descriptors) + ")"

    return {
        FIELD_ERP_SKU: sku,
        FIELD_ERP_NAME: erp_name,
        FIELD_L1: category["一级类目"],
        FIELD_L2: category["二级类目"],
        FIELD_PRODUCT_TYPE: category["叶子类目"],
        STATUS_FIELD: STATUS_CONFIRM,
    }, warnings


def record_url(record_id: str) -> str:
    return (
        f"https://u1wpma3xuhr.feishu.cn/base/{BASE_TOKEN}"
        f"?table={PRODUCT_TABLE_ID}&record={record_id}"
    )


def compact_value(value: str) -> str:
    return value if value else "未填"


def size_text(fields: Dict[str, Any], prefix: str) -> str:
    if prefix == "产品净":
        keys = ["产品净长(cm)", "产品净宽(cm)", "产品净高(cm)"]
    elif prefix == "包装":
        keys = ["包装长(cm)", "包装宽(cm)", "包装高(cm)"]
    elif prefix == "外箱":
        keys = ["外箱长(cm)", "外箱宽(cm)", "外箱高(cm)"]
    else:
        keys = []
    values = [cell_text(fields.get(key)) for key in keys]
    return " x ".join(values) if all(values) else "未填"


def missing_lines(fields: Dict[str, Any]) -> List[str]:
    gap = cell_text(fields.get(FIELD_DATA_GAP))
    if not gap:
        return []
    parts = [item.strip() for item in re.split(r"\s+", gap) if item.strip()]
    return [part[:-1] if part.endswith("缺") else part for part in parts]


def md_line(label: str, value: str) -> str:
    return f"- **{label}**：{compact_value(value)}"


def build_card(record_id: str, fields: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    sku = cell_text(fields.get(FIELD_ERP_SKU))
    erp_name = cell_text(fields.get(FIELD_ERP_NAME))
    brand = cell_text(fields.get(FIELD_BRAND))
    category = cell_text(fields.get(FIELD_CATEGORY))
    style = cell_text(fields.get(FIELD_STYLE))
    factory_model = cell_text(fields.get(FIELD_FACTORY_MODEL))
    color = cell_text(fields.get(FIELD_COLOR))
    bundle = cell_text(fields.get(FIELD_BUNDLE))
    main = cell_text(fields.get(FIELD_MAIN_PLATFORM))
    compatible = cell_text(fields.get(FIELD_COMPAT_PLATFORM))
    missing = missing_lines(fields)
    missing_text = "\n".join([f"- {item}" for item in missing]) if missing else "无"
    warning_text = "\n".join([f"- {item}" for item in warnings]) if warnings else "无"

    generated = "\n".join(
        [
            md_line("ERP SKU", sku),
            md_line("ERP品名", erp_name),
            md_line("一级分类", cell_text(fields.get(FIELD_L1))),
            md_line("二级分类", cell_text(fields.get(FIELD_L2))),
            md_line("产品类型", cell_text(fields.get(FIELD_PRODUCT_TYPE))),
        ]
    )
    filled = "\n".join(
        [
            md_line("品牌", brand),
            md_line("类目配置", category),
            md_line("工厂型号", factory_model),
            md_line("款式", style),
            md_line("颜色变体", color),
            md_line("套餐变体", bundle),
            md_line("主平台", main),
            md_line("兼容平台", compatible),
            md_line("装箱数", cell_text(fields.get("装箱数"))),
            md_line("带电", cell_text(fields.get("带电"))),
            md_line("材质", cell_text(fields.get("材质"))),
            md_line("产品净尺寸", size_text(fields, "产品净")),
            md_line("包装尺寸", size_text(fields, "包装")),
            md_line("外箱尺寸", size_text(fields, "外箱")),
            md_line("净重(g)", cell_text(fields.get("净重(g)"))),
            md_line("毛重(g)", cell_text(fields.get("毛重(g)"))),
            md_line("外箱重量(kg)", cell_text(fields.get("外箱重量(kg)"))),
        ]
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "新品建档待采购确认"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**生成结果**\n" + generated}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**采购已填字段**\n" + filled}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**缺失字段**\n" + missing_text}},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**系统提示**\n" + warning_text}},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "请采购核对：生成结果是否符合你提交的信息，缺失字段是否可接受。退回后改完资料需勾选「采购已修改」，系统才会重新合成并发新卡。",
                    }
                ],
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认建品"},
                        "type": "primary",
                        "value": {"action": "confirm_build", "rid": record_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "退回修改"},
                        "type": "danger",
                        "value": {"action": "reject_build", "rid": record_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开记录"},
                        "type": "default",
                        "url": record_url(record_id),
                    },
                ],
            },
        ],
    }


def user_cell_items(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("id") or item.get("user_id") or item.get("open_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if user_id:
            out.append({"id": user_id, "name": name})
    return out


def resolve_union_id(client: FeishuClient, open_id: str) -> str:
    if not open_id:
        return ""
    try:
        data = client.request("GET", f"/contact/v3/users/{open_id}?user_id_type=open_id")
    except Exception as exc:
        print(json.dumps({"action": "resolve_union_id_failed", "open_id": open_id, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return ""
    user = data.get("data", {}).get("user", {})
    return str(user.get("union_id") or "").strip()


def submitter_target(client: FeishuClient, fields: Dict[str, Any]) -> Tuple[str, str]:
    for item in user_cell_items(fields.get(FIELD_SUBMITTER)):
        name = item.get("name", "")
        open_id = item.get("id", "")
        if not open_id or "分身" in name or name.lower().endswith("bot"):
            continue
        union_id = resolve_union_id(client, open_id)
        if union_id:
            return union_id, name
    return "", ""


def build_group_summary_card(record_id: str, fields: Dict[str, Any], receiver_name: str, personal_sent: bool) -> Dict[str, Any]:
    sku = cell_text(fields.get(FIELD_ERP_SKU))
    erp_name = cell_text(fields.get(FIELD_ERP_NAME))
    brand = cell_text(fields.get(FIELD_BRAND))
    summary = "\n".join(
        [
            md_line("产品", erp_name or sku or record_id),
            md_line("ERP SKU", sku),
            md_line("品牌", brand),
            md_line("确认人", receiver_name or "未识别，已改群内兜底"),
            md_line("状态", "已私聊录入采购" if personal_sent else "群内兜底确认"),
        ]
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue" if personal_sent else "orange",
            "title": {"tag": "plain_text", "content": "🟡 [LOG·P2] 新品建档确认卡 · 采购确认"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开记录"},
                        "type": "default",
                        "url": record_url(record_id),
                    }
                ],
            },
        ],
    }


def send_interactive(event_client: FeishuClient, receive_id_type: str, receive_id: str, card: Dict[str, Any]) -> str:
    resp = event_client.request(
        "POST",
        f"/im/v1/messages?receive_id_type={receive_id_type}",
        body={
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
    )
    return resp.get("data", {}).get("message_id", "")


def send_card(
    event_client: FeishuClient,
    identity_client: FeishuClient,
    email: str,
    union_id: str,
    open_id: str,
    chat_id: str,
    record_id: str,
    fields: Dict[str, Any],
    warnings: List[str],
) -> str:
    card = build_card(record_id, fields, warnings)
    target_union_id = union_id
    target_name = ""
    if not target_union_id and not open_id and not email:
        target_union_id, target_name = submitter_target(identity_client, fields)

    personal_msg_id = ""
    personal_error = ""
    if open_id:
        try:
            personal_msg_id = send_interactive(event_client, "open_id", open_id, card)
        except Exception as exc:
            personal_error = f"open_id: {exc}"
    elif target_union_id:
        try:
            personal_msg_id = send_interactive(event_client, "union_id", target_union_id, card)
        except Exception as exc:
            personal_error = f"union_id: {exc}"
    elif email:
        try:
            user_resp = event_client.request(
                "POST",
                "/contact/v3/users/batch_get_id?user_id_type=open_id",
                body={"emails": [email]},
            )
            user_id = ""
            for item in user_resp.get("data", {}).get("user_list", []):
                if item.get("user_id"):
                    user_id = item["user_id"]
                    break
            if user_id:
                personal_msg_id = send_interactive(event_client, "open_id", user_id, card)
            else:
                personal_error = "email: cannot resolve user"
        except Exception as exc:
            personal_error = f"email: {exc}"

    if chat_id:
        if personal_msg_id:
            summary_msg_id = send_interactive(
                event_client,
                "chat_id",
                chat_id,
                build_group_summary_card(record_id, fields, target_name or "录入采购", True),
            )
            return personal_msg_id + (f",{summary_msg_id}" if summary_msg_id else "")
        group_msg_id = send_interactive(event_client, "chat_id", chat_id, card)
        print(json.dumps({"action": "send_card_group_fallback", "record_id": record_id, "reason": personal_error or "no_submitter"}, ensure_ascii=False))
        return group_msg_id

    if personal_msg_id:
        return personal_msg_id
    raise RuntimeError("Cannot resolve card receiver. Provide submitter/confirm-open-id/confirm-union-id or confirm-chat-id.")


def select_target_records(records: List[Dict[str, Any]], record_id: Optional[str]) -> List[Dict[str, Any]]:
    if not record_id:
        return records
    return [row for row in records if row.get("record_id") == record_id]


def cmd_default_status(client: FeishuClient, args: argparse.Namespace) -> int:
    brand_codes = load_brand_codes(client)
    records = select_target_records(list_records(client, PRODUCT_TABLE_ID), args.record_id)
    changed = 0
    for row in records:
        rid = row["record_id"]
        fields = row.get("fields", {})
        if not should_default_to_todo(fields):
            continue
        errors, warnings = validate_row(fields, brand_codes)
        payload = {STATUS_FIELD: STATUS_TODO}
        if FIELD_RESUBMIT in fields:
            payload[FIELD_RESUBMIT] = False
        action = "resubmit_status" if cell_text(fields.get(STATUS_FIELD)) == STATUS_REVISE else "default_status"
        print(json.dumps({"action": action, "record_id": rid, "fields": payload, "errors": errors, "warnings": warnings}, ensure_ascii=False))
        if not args.dry_run:
            update_record(client, rid, payload)
        changed += 1
    print(f"default_status candidates={changed} dry_run={args.dry_run}")
    return 0


def cmd_compose(client: FeishuClient, cfg: Config, args: argparse.Namespace) -> int:
    categories = load_categories(client)
    brand_codes = load_brand_codes(client)
    proxy = LingxingProxy(cfg.lingxing_proxy_url, cfg.lingxing_proxy_token)
    existing_skus = product_skus_from_lingxing(proxy)
    event_client = None
    if args.send_card and not args.dry_run:
        event_client = FeishuClient(cfg.feishu_event_app_id, cfg.feishu_event_app_secret)

    records = select_target_records(list_records(client, PRODUCT_TABLE_ID), args.record_id)
    handled = 0
    for row in records:
        rid = row["record_id"]
        fields = row.get("fields", {})
        if cell_text(fields.get(STATUS_FIELD)) != STATUS_TODO:
            continue
        try:
            payload, warnings = compose_row(fields, categories, existing_skus, brand_codes)
        except Exception as exc:
            print(json.dumps({"action": "compose_error", "record_id": rid, "error": str(exc)}, ensure_ascii=False))
            if args.mark_failed and not args.dry_run:
                update_record(client, rid, {STATUS_FIELD: STATUS_FAILED, FIELD_DATA_GAP: str(exc)})
            continue

        print(json.dumps({"action": "compose", "record_id": rid, "fields": payload, "warnings": warnings}, ensure_ascii=False))
        if not args.dry_run:
            update_record(client, rid, payload)
            fields = {**fields, **payload}
            if args.send_card:
                assert event_client is not None
                msg_id = send_card(
                    event_client,
                    client,
                    args.confirm_email,
                    args.confirm_union_id,
                    args.confirm_open_id,
                    args.confirm_chat_id,
                    rid,
                    fields,
                    warnings,
                )
                print(json.dumps({"action": "send_card", "record_id": rid, "message_id": msg_id}, ensure_ascii=False))
        handled += 1
        existing_skus = list(existing_skus) + [payload[FIELD_ERP_SKU]]
        if not args.dry_run:
            time.sleep(0.5)
    print(f"compose handled={handled} dry_run={args.dry_run} send_card={args.send_card}")
    return 0


def cmd_run(client: FeishuClient, cfg: Config, args: argparse.Namespace) -> int:
    cmd_default_status(client, args)
    return cmd_compose(client, cfg, args)


def cmd_send_card(client: FeishuClient, cfg: Config, args: argparse.Namespace) -> int:
    if not args.record_id:
        raise SystemExit("send-card requires --record-id")
    row = get_record(client, args.record_id)
    fields = row.get("fields", {})
    errors, warnings = validate_row(fields, load_brand_codes(client))
    if not cell_text(fields.get(FIELD_ERP_SKU)) or not cell_text(fields.get(FIELD_ERP_NAME)):
        errors.append("ERP SKU/ERP品名未合成")
    if errors:
        raise SystemExit("; ".join(errors))
    print(json.dumps({"action": "send_card", "record_id": args.record_id, "warnings": warnings}, ensure_ascii=False))
    if not args.dry_run:
        event_client = FeishuClient(cfg.feishu_event_app_id, cfg.feishu_event_app_secret)
        msg_id = send_card(
            event_client,
            client,
            args.confirm_email,
            args.confirm_union_id,
            args.confirm_open_id,
            args.confirm_chat_id,
            args.record_id,
            fields,
            warnings,
        )
        print(json.dumps({"action": "send_card_done", "record_id": args.record_id, "message_id": msg_id}, ensure_ascii=False))
    return 0


def maybe_number(fields: Dict[str, Any], field_name: str) -> Optional[float]:
    text = cell_text(fields.get(field_name))
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def build_lingxing_payload(
    fields: Dict[str, Any],
    categories: Dict[str, Dict[str, str]],
    bidmap: Dict[str, Any],
) -> Dict[str, Any]:
    sku = cell_text(fields.get(FIELD_ERP_SKU))
    name = cell_text(fields.get(FIELD_ERP_NAME))
    brand = cell_text(fields.get(FIELD_BRAND))
    if not sku or not name:
        raise ValueError("ERP SKU/ERP品名未合成")
    validate_erp_sku(sku)

    payload: Dict[str, Any] = {"sku": sku, "product_name": name, "status": 2}
    if brand in bidmap:
        payload["bid"] = bidmap[brand]

    category = categories.get(link_record_id(fields.get(FIELD_CATEGORY)) or "", {})
    cid = category.get("cid")
    if cid:
        try:
            payload["cid"] = int(float(cid))
        except ValueError:
            pass

    physical_map = {
        "cg_box_pcs": "装箱数",
        "cg_product_gross_weight": "毛重(g)",
        "cg_product_net_weight": "净重(g)",
        "cg_box_length": "外箱长(cm)",
        "cg_box_width": "外箱宽(cm)",
        "cg_box_height": "外箱高(cm)",
        "cg_package_length": "包装长(cm)",
        "cg_package_width": "包装宽(cm)",
        "cg_package_height": "包装高(cm)",
        "cg_product_length": "产品净长(cm)",
        "cg_product_width": "产品净宽(cm)",
        "cg_product_height": "产品净高(cm)",
        "cg_box_weight": "外箱重量(kg)",
    }
    for lx_key, field_name in physical_map.items():
        value = maybe_number(fields, field_name)
        if value is None:
            continue
        payload[lx_key] = int(value) if lx_key == "cg_box_pcs" else value

    material = cell_text(fields.get("材质"))
    if material:
        payload["cg_product_material"] = material
    unit = cell_text(fields.get("单位"))
    if unit:
        payload["unit"] = unit
    if cell_text(fields.get("带电")) == "带电":
        payload["special_attr"] = ["1"]

    custom_fields: List[Dict[str, str]] = []
    main_platform = cell_text(fields.get(FIELD_MAIN_PLATFORM))
    compatible = cell_list(fields.get(FIELD_COMPAT_PLATFORM))
    if main_platform:
        custom_fields.append({"id": LX_CF_MAIN_PLATFORM, "val": main_platform})
    if compatible:
        custom_fields.append({"id": LX_CF_COMPAT_PLATFORM, "val": ",".join(compatible)})
    if custom_fields:
        payload["custom_fields"] = custom_fields

    return payload


def cmd_create_confirmed(client: FeishuClient, cfg: Config, args: argparse.Namespace) -> int:
    categories = load_categories(client)
    proxy = LingxingProxy(cfg.lingxing_proxy_url, cfg.lingxing_proxy_token)
    lx_rows = product_rows_from_lingxing(proxy)
    bidmap = brand_bid_map(lx_rows)
    records = select_target_records(list_records(client, PRODUCT_TABLE_ID), args.record_id)
    handled = 0
    for row in records:
        rid = row["record_id"]
        fields = row.get("fields", {})
        if cell_text(fields.get(STATUS_FIELD)) != STATUS_CREATE:
            continue
        try:
            payload = build_lingxing_payload(fields, categories, bidmap)
        except Exception as exc:
            print(json.dumps({"action": "create_error", "record_id": rid, "error": str(exc)}, ensure_ascii=False))
            if args.mark_failed and not args.dry_run:
                update_record(client, rid, {STATUS_FIELD: STATUS_FAILED})
            continue

        print(json.dumps({"action": "create_confirmed", "record_id": rid, "payload": payload}, ensure_ascii=False))
        if args.dry_run:
            handled += 1
            continue

        result = proxy.call("POST", "/erp/sc/routing/storage/product/set", payload)
        if result.get("code") == 0:
            update_record(client, rid, {STATUS_FIELD: STATUS_BUILT, FIELD_STATUS: "开发中"})
            print(json.dumps({"action": "create_done", "record_id": rid, "sku": payload["sku"], "result": result.get("data")}, ensure_ascii=False))
        else:
            update_record(client, rid, {STATUS_FIELD: STATUS_FAILED})
            print(json.dumps({"action": "create_failed", "record_id": rid, "sku": payload["sku"], "result": result}, ensure_ascii=False))
        handled += 1
        time.sleep(0.5)
    print(f"create_confirmed handled={handled} dry_run={args.dry_run}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Product information intake automation.")
    parser.add_argument("--record-id", help="Only process one Feishu record id.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview without writing. Default: true.")
    parser.add_argument("--commit", dest="dry_run", action="store_false", help="Write changes / send card.")
    parser.add_argument("--send-card", action="store_true", help="Send Feishu confirmation card after composing.")
    parser.add_argument("--confirm-email", default=DEFAULT_CONFIRM_EMAIL, help="Receiver email for confirmation cards.")
    parser.add_argument("--confirm-union-id", default=DEFAULT_CONFIRM_UNION_ID, help="Receiver union_id for confirmation cards.")
    parser.add_argument("--confirm-open-id", default=DEFAULT_CONFIRM_OPEN_ID, help="Receiver open_id for confirmation cards.")
    parser.add_argument("--confirm-chat-id", default=DEFAULT_CONFIRM_CHAT_ID, help="Receiver chat_id for confirmation cards.")
    parser.add_argument("--mark-failed", action="store_true", help="Mark compose failures as 失败.")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("default-status", help="Backfill empty 建档状态 to 待合成.")
    sub.add_parser("compose", help="Compose ERP SKU/name for 待合成 records.")
    sub.add_parser("send-card", help="Send confirmation card for an already composed record.")
    sub.add_parser("create-confirmed", help="Create Lingxing product for 确认建品 records.")
    sub.add_parser("run", help="Run default-status then compose.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    require_event = (args.command in ("compose", "run") and args.send_card and not args.dry_run) or (
        args.command == "send-card" and not args.dry_run
    )
    require_lingxing = args.command in ("compose", "run", "create-confirmed")
    cfg = Config.from_env(require_event=require_event, require_lingxing=require_lingxing)
    client = FeishuClient(cfg.feishu_app2_id, cfg.feishu_app2_secret)
    if args.command == "default-status":
        return cmd_default_status(client, args)
    if args.command == "compose":
        return cmd_compose(client, cfg, args)
    if args.command == "send-card":
        return cmd_send_card(client, cfg, args)
    if args.command == "create-confirmed":
        return cmd_create_confirmed(client, cfg, args)
    if args.command == "run":
        return cmd_run(client, cfg, args)
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
