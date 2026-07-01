# -*- coding: utf-8 -*-
"""Audit product-intake brand and category configuration.

The audit is read-only. It checks whether category rows can safely drive SKU,
ERP name, and Lingxing product creation.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple

import product_intake


REQUIRED_CATEGORY_FIELDS = (
    "配置名",
    "一级类目",
    "二级类目",
    "叶子类目",
    "品类码",
    "平台码",
    "平台中文",
    "产品类型词",
    "cid",
)


def level(severity: str, table: str, record_id: str, field: str, message: str) -> Dict[str, str]:
    return {
        "severity": severity,
        "table": table,
        "record_id": record_id,
        "field": field,
        "message": message,
    }


def audit_categories(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    pair_seen: Dict[Tuple[str, str], str] = {}
    config_seen: Dict[str, str] = {}
    cid_seen: Dict[str, str] = {}

    for row in rows:
        rid = row.get("record_id", "")
        fields = row.get("fields", {})
        data = {name: product_intake.cell_text(fields.get(name)) for name in REQUIRED_CATEGORY_FIELDS}

        for name, value in data.items():
            if not value:
                issues.append(level("error", "类目配置表", rid, name, f"{name} 为空"))

        config_name = data["配置名"]
        if config_name:
            if config_name in config_seen:
                issues.append(level("error", "类目配置表", rid, "配置名", f"配置名重复，已存在于 {config_seen[config_name]}"))
            config_seen[config_name] = rid

        category_code = data["品类码"]
        if category_code and not re.fullmatch(r"[A-Z0-9]{3,6}", category_code):
            issues.append(level("error", "类目配置表", rid, "品类码", "品类码应为 3-6 位大写字母/数字"))

        platform_code = data["平台码"]
        if platform_code and not re.fullmatch(r"[A-Z0-9]{2,6}", platform_code):
            issues.append(level("error", "类目配置表", rid, "平台码", "平台码应为 2-6 位大写字母/数字"))

        pair = (platform_code, category_code)
        if all(pair):
            if pair in pair_seen:
                issues.append(level("error", "类目配置表", rid, "平台码+品类码", f"组合重复，已存在于 {pair_seen[pair]}"))
            pair_seen[pair] = rid

        cid = data["cid"]
        if cid and not re.fullmatch(r"\d+", cid):
            issues.append(level("error", "类目配置表", rid, "cid", "cid 必须是纯数字"))
        elif cid:
            if cid in cid_seen:
                issues.append(level("warn", "类目配置表", rid, "cid", f"cid 重复，已存在于 {cid_seen[cid]}；如领星同叶子类目共用可忽略"))
            cid_seen[cid] = rid

        product_word = data["产品类型词"]
        if product_word in {"配件", "其他", "其它", "产品"}:
            issues.append(level("warn", "类目配置表", rid, "产品类型词", f"产品类型词过泛：{product_word}"))
        if product_word and re.search(r"[/,，、\s]", product_word):
            issues.append(level("warn", "类目配置表", rid, "产品类型词", "产品类型词含分隔符，可能污染 ERP 品名"))

    return issues


def audit_brands(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    brand_seen: Dict[str, str] = {}
    code_seen: Dict[str, str] = {}

    for row in rows:
        rid = row.get("record_id", "")
        fields = row.get("fields", {})
        enabled = product_intake.cell_bool(fields.get("启用"))
        brand = product_intake.cell_text(fields.get("品牌"))
        code = product_intake.cell_text(fields.get("品牌码")).upper()
        if not enabled:
            continue
        if not brand:
            issues.append(level("error", "品牌配置表", rid, "品牌", "启用品牌的品牌名为空"))
        if not code:
            issues.append(level("error", "品牌配置表", rid, "品牌码", "启用品牌的品牌码为空"))
        elif not re.fullmatch(r"[A-Z0-9]{2,4}", code):
            issues.append(level("error", "品牌配置表", rid, "品牌码", "品牌码应为 2-4 位大写字母/数字"))
        if brand:
            if brand in brand_seen:
                issues.append(level("error", "品牌配置表", rid, "品牌", f"品牌重复，已存在于 {brand_seen[brand]}"))
            brand_seen[brand] = rid
        if code:
            if code in code_seen:
                issues.append(level("error", "品牌配置表", rid, "品牌码", f"品牌码重复，已存在于 {code_seen[code]}"))
            code_seen[code] = rid

    return issues


def print_markdown(issues: List[Dict[str, str]]) -> None:
    grouped: Dict[str, int] = defaultdict(int)
    for item in issues:
        grouped[item["severity"]] += 1
    print("# 产品建档配置审计")
    print()
    print(f"- error: {grouped.get('error', 0)}")
    print(f"- warn: {grouped.get('warn', 0)}")
    print()
    if not issues:
        print("未发现配置问题。")
        return
    print("| 等级 | 表 | record_id | 字段 | 问题 |")
    print("|---|---|---|---|---|")
    for item in issues:
        print(f"| {item['severity']} | {item['table']} | {item['record_id']} | {item['field']} | {item['message']} |")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = parser.parse_args()

    cfg = product_intake.Config.from_env()
    client = product_intake.FeishuClient(cfg.feishu_app2_id, cfg.feishu_app2_secret)
    category_rows = product_intake.list_records(client, product_intake.CATEGORY_TABLE_ID)
    brand_rows = product_intake.list_records(client, product_intake.BRAND_TABLE_ID)
    issues = audit_categories(category_rows) + audit_brands(brand_rows)
    if args.format == "json":
        print(json.dumps({"ok": not any(item["severity"] == "error" for item in issues), "issues": issues}, ensure_ascii=False, indent=2))
    else:
        print_markdown(issues)
    return 1 if any(item["severity"] == "error" for item in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
