#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render an A-share research ``final.json`` into a Chinese report.

Pure standard library: no pandas / numpy / network access.

Two output formats: a self-contained HTML page (default) and Markdown.

CLI (see CONTRACT.md section 3)::

    python3 scripts/render.py --input final.json --out report.html
    python3 scripts/render.py --input final.json --format md --out report.md
    python3 scripts/render.py --input final.json > report.html

Exit codes::

    0  success
    3  invalid input (unreadable file, malformed JSON, not a final document)
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import math
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

MISSING = "数据缺失"

ALLOWED_ACTIONS = ("乐观", "审慎", "中性")

DEPTH_LABELS = {
    "quick": "快速",
    "standard": "标准",
    "deep": "深度",
    "full": "完整",
}

# Whether the sector / social analysts run at each depth (SKILL.md section 4).
DEPTH_ENABLED = {
    "quick": {"sector": False, "social": False},
    "standard": {"sector": True, "social": False},
    "deep": {"sector": True, "social": True},
    "full": {"sector": True, "social": True},
}

ROLE_LABELS = (
    ("index", "指数与大盘"),
    ("sector", "行业与板块"),
    ("market", "资金与情绪"),
    ("fundamentals", "基本面"),
    ("news", "新闻与公告"),
    ("social", "市场舆情"),
)

RISK_ROLE_LABELS = (
    ("aggressive", "激进视角"),
    ("conservative", "保守视角"),
    ("neutral", "中性视角"),
)

BULLISH_WORDS = (
    "买入", "增持", "加仓", "看多", "强烈推荐", "推荐", "建仓",
    "buy", "bullish", "overweight", "long",
)
BEARISH_WORDS = (
    "卖出", "减持", "减仓", "看空", "回避",
    "sell", "bearish", "underweight", "short",
)
NEUTRAL_WORDS = ("中性", "持有", "观望", "hold", "neutral", "wait")

KNOWN_TOP_LEVEL = (
    "schema_version", "symbol", "name", "as_of", "depth",
    "analyst_reports", "debate", "investment_plan", "risk",
    "final", "data_quality", "key_metrics", "metrics", "trend",
)

TREND_DIRECTIONS = ("上行", "震荡", "下行")

TREND_SECTION_DISCLAIMER = "以上为走势研究区间，不构成买卖依据。"


class InputError(Exception):
    """Raised when the input cannot be treated as a final.json document."""


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def is_number(value: Any) -> bool:
    """True for finite int/float values, excluding bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def fmt_pct(value: Any) -> str:
    """Format a ratio as a percentage.

    The contract stores ratios as 0.12 == 12%.  Values already expressed on a
    0-100 scale are tolerated; anything larger is shown as a plain number.
    """
    if not is_number(value):
        return MISSING
    number = float(value)
    if abs(number) <= 1.0:
        return "{:.1f}%".format(number * 100.0)
    if abs(number) <= 100.0:
        return "{:.1f}%".format(number)
    return "{:,.2f}".format(number)


def fmt_money(value: Any) -> str:
    """Format a CNY amount using 亿元 / 万元 / 元."""
    if not is_number(value):
        return MISSING
    number = float(value)
    magnitude = abs(number)
    if magnitude >= 100000000:
        return "{:.2f} 亿元".format(number / 100000000.0)
    if magnitude >= 10000:
        return "{:.2f} 万元".format(number / 10000.0)
    return "{:,.2f} 元".format(number)


def fmt_price(value: Any) -> str:
    if not is_number(value):
        return MISSING
    return fmt_money(value)


def fmt_price_range(value: Any) -> str:
    if not isinstance(value, dict):
        return MISSING
    low = value.get("low")
    high = value.get("high")
    low_ok = is_number(low)
    high_ok = is_number(high)
    if low_ok and high_ok:
        return "{} – {}".format(fmt_price(low), fmt_price(high))
    if low_ok:
        return "{}（区间不完整，仅给出下界 {}）".format(MISSING, fmt_price(low))
    if high_ok:
        return "{}（区间不完整，仅给出上界 {}）".format(MISSING, fmt_price(high))
    return MISSING


def has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return True


def demote_headings(text: str, levels: int) -> str:
    """Push embedded Markdown headings below the renderer's own headings.

    Analyst reports often start with ``## 结论``; without demotion those lines
    would become top-level report sections.  Fenced code blocks are untouched.
    """
    if levels <= 0:
        return text
    output: List[str] = []
    fenced = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            output.append(line)
            continue
        if not fenced:
            match = re.match(r"^(#{1,6})(\s+.*)$", line)
            if match:
                new_level = min(6, len(match.group(1)) + levels)
                line = "#" * new_level + match.group(2)
        output.append(line)
    return "\n".join(output)


def as_text(value: Any, missing_note: str = "", demote: int = 0) -> str:
    """Render an arbitrary Markdown-ish value, degrading to 数据缺失."""
    note = "（{}）".format(missing_note) if missing_note else ""
    if value is None:
        return "> {}{}".format(MISSING, note)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "> {}{}".format(MISSING, note)
        return demote_headings(text, demote)
    if isinstance(value, (list, tuple)):
        if not value:
            return "> {}{}".format(MISSING, note)
        lines: List[str] = []
        for item in value:
            if item is None or (isinstance(item, str) and not item.strip()):
                lines.append("- {}".format(MISSING))
            elif isinstance(item, (dict, list, tuple)):
                lines.append("- `{}`".format(json.dumps(item, ensure_ascii=False)))
            else:
                lines.append("- {}".format(demote_headings(str(item), demote)))
        return "\n".join(lines)
    if isinstance(value, dict):
        if not value:
            return "> {}{}".format(MISSING, note)
        return "```json\n{}\n```".format(
            json.dumps(value, ensure_ascii=False, indent=2)
        )
    return str(value)


def inline_text(value: Any, missing_note: str = "") -> str:
    """Flatten a short value onto one line for use inside a bullet."""
    text = as_text(value, missing_note)
    if text.startswith(">"):
        return text
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def display_action(value: Any) -> str:
    if isinstance(value, str) and value.strip() in ALLOWED_ACTIONS:
        return "**{}**".format(value.strip())
    if not has_content(value):
        return "**{}**".format(MISSING)
    return str(value)


def warning_text(value: Any) -> str:
    if value is None:
        return MISSING
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def fmt_metric(key: Any, value: Any) -> str:
    """Best-effort formatting for optional key metrics."""
    if not is_number(value):
        return MISSING if value is None else str(value)
    name = str(key).lower()
    pct_tokens = (
        "pct", "percent", "ratio", "margin", "yield", "confidence",
        "score", "completeness", "毛利率", "净利率", "roe", "roa",
        "增长率", "占比", "比例", "资产负债率", "负债率", "收益率",
        "周转率",
    )
    money_tokens = (
        "amount", "revenue", "profit", "income", "sales", "cap", "cash",
        "mv", "value", "equity", "asset", "liabilit", "turnover", "flow",
        "balance", "金额", "市值", "营收", "收入", "净利", "利润", "现金",
        "资产", "负债", "股本", "成交", "资金", "余额", "总额",
    )
    if any(token in name for token in pct_tokens):
        return fmt_pct(value)
    if any(token in name for token in money_tokens):
        return fmt_money(value)
    return "{:,.2f}".format(float(value))


# ---------------------------------------------------------------------------
# Action whitelist normalisation
# ---------------------------------------------------------------------------

def _action_hits(raw: str) -> set:
    lowered = raw.lower()
    hits = set()
    for word in BULLISH_WORDS:
        if word in lowered:
            hits.add("乐观")
    for word in BEARISH_WORDS:
        if word in lowered:
            hits.add("审慎")
    for word in NEUTRAL_WORDS:
        if word in lowered:
            hits.add("中性")
    return hits


def _add_warning(warnings: List[Any], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def normalize_action(value: Any, field: str, warnings: List[Any]) -> Optional[str]:
    """Return one of 乐观/审慎/中性, recording a warning when normalised."""
    if value is None or (isinstance(value, str) and not value.strip()):
        _add_warning(warnings, "{}: 缺失，报告中显示为“{}”".format(field, MISSING))
        return None

    raw = value.strip() if isinstance(value, str) else str(value)
    if raw in ALLOWED_ACTIONS:
        return raw

    hits = _action_hits(raw)
    if len(hits) == 1:
        mapped = next(iter(hits))
        reason = "已归一化"
    elif len(hits) > 1:
        mapped = "中性"
        reason = "含义冲突，已按中性处理"
    else:
        mapped = "中性"
        reason = "无法识别，已按中性处理"

    _add_warning(
        warnings,
        "{}: 原值“{}”不在白名单（乐观/审慎/中性），{}为“{}”".format(
            field, raw, reason, mapped
        ),
    )
    return mapped


def prepare(document: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Any]]:
    """Ensure data_quality.warnings exists and normalise all action fields."""
    data_quality = document.get("data_quality")
    if not isinstance(data_quality, dict):
        data_quality = {}
        document["data_quality"] = data_quality

    warnings = data_quality.get("warnings")
    if not isinstance(warnings, list):
        if warnings is None:
            warnings = []
        elif isinstance(warnings, str):
            warnings = [warnings]
        else:
            warnings = [warnings]
        data_quality["warnings"] = warnings

    for section_name in ("investment_plan", "final"):
        section = document.get(section_name)
        if not isinstance(section, dict) or "action" not in section:
            continue
        normalized = normalize_action(
            section.get("action"), "{}.action".format(section_name), warnings
        )
        if normalized is not None:
            section["action"] = normalized

    return document, warnings


# ---------------------------------------------------------------------------
# Missing-field audit
# ---------------------------------------------------------------------------

def audit_issues(document: Dict[str, Any]) -> List[str]:
    """Collect missing/degraded items for the data-quality section."""
    issues: List[str] = []
    depth = document.get("depth")
    enabled = DEPTH_ENABLED.get(depth, {}) if isinstance(depth, str) else {}

    analysts = document.get("analyst_reports")
    analysts = analysts if isinstance(analysts, dict) else {}
    for role, label in ROLE_LABELS:
        if not enabled.get(role, True):
            issues.append("{}（{}）：depth={} 未启用".format(label, role, depth))
        elif not has_content(analysts.get(role)):
            issues.append("{}（{}）：未生成或为空".format(label, role))

    debate = document.get("debate")
    debate = debate if isinstance(debate, dict) else {}
    if not has_content(debate.get("bull")):
        issues.append("多空辩论：缺少多头汇总观点")
    if not has_content(debate.get("bear")):
        issues.append("多空辩论：缺少空头汇总观点")

    plan = document.get("investment_plan")
    plan = plan if isinstance(plan, dict) else {}
    for key, label in (
        ("action", "研究经理结论：action"),
        ("confidence", "研究经理结论：confidence"),
        ("reasoning", "研究经理结论：reasoning"),
    ):
        if not has_content(plan.get(key)):
            issues.append("{} 缺失".format(label))

    risk = document.get("risk")
    risk = risk if isinstance(risk, dict) else {}
    for key, label in (
        ("risk_level", "风险辩论：risk_level"),
        ("key_risks", "风险辩论：key_risks"),
        ("risk_control", "风险辩论：risk_control"),
    ):
        if not has_content(risk.get(key)):
            issues.append("{} 缺失".format(label))

    final_section = document.get("final")
    final_section = final_section if isinstance(final_section, dict) else {}
    if not has_content(final_section.get("action")):
        issues.append("最终研究结论：action 缺失")
    if not has_content(final_section.get("confidence")):
        issues.append("最终研究结论：confidence 缺失")
    price_range = final_section.get("price_analysis_range")
    if not isinstance(price_range, dict) or not (
        is_number(price_range.get("low")) or is_number(price_range.get("high"))
    ):
        issues.append("最终研究结论：价格区间缺失")
    elif not (is_number(price_range.get("low")) and is_number(price_range.get("high"))):
        issues.append("最终研究结论：价格区间不完整")
    if not has_content(final_section.get("summary")):
        issues.append("最终研究结论：summary 缺失")

    data_quality = document.get("data_quality")
    data_quality = data_quality if isinstance(data_quality, dict) else {}
    if not is_number(data_quality.get("completeness")):
        issues.append("data_quality.completeness 缺失")

    trend = document.get("trend")
    if isinstance(trend, dict) and trend:
        if not has_content(trend.get("direction")):
            issues.append("走势分析：direction 缺失")
        levels = trend.get("key_levels")
        has_levels = isinstance(levels, dict) and any(
            isinstance(levels.get(key), list) and levels[key]
            for key in ("support", "resistance")
        )
        if not has_levels:
            issues.append("走势分析：关键位缺失")
        if not has_content(trend.get("scenarios")):
            issues.append("走势分析：scenarios 缺失")

    return issues


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _meta_table_row(label: str, value: str) -> str:
    return "| {} | {} |".format(label, str(value).replace("|", "\\|"))


def _render_trend_section(trend: Dict[str, Any]) -> List[str]:
    """渲染“## 走势分析”章节；direction 只按原词展示，绝不转写成买卖建议。"""
    lines: List[str] = []
    lines.append("## 走势分析")
    lines.append("")

    direction = trend.get("direction")
    if has_content(direction):
        shown = str(direction).strip()
        if shown not in TREND_DIRECTIONS:
            shown = "{}（非标准取值）".format(shown)
    else:
        shown = MISSING
    lines.append("- **走势方向**：{}".format(shown))
    lines.append("- **置信度**：{}".format(fmt_pct(trend.get("confidence"))))
    horizon = trend.get("horizon")
    lines.append(
        "- **时间窗**：{}".format(horizon if has_content(horizon) else MISSING)
    )
    lines.append("")

    lines.append("### 关键位")
    lines.append("")
    levels = trend.get("key_levels")
    levels = levels if isinstance(levels, dict) else {}
    level_rows: List[Tuple[str, str]] = []
    for side, fallback in (("support", "支撑"), ("resistance", "阻力")):
        items = levels.get(side)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                kind = item.get("type")
                kind = str(kind) if has_content(kind) else fallback
                level_rows.append((kind, fmt_price(item.get("price"))))
            else:
                level_rows.append((fallback, MISSING))
    if level_rows:
        lines.append("| 类型 | 价位 |")
        lines.append("|---|---|")
        for kind, price in level_rows:
            lines.append("| {} | {} |".format(
                kind.replace("|", "\\|"), str(price).replace("|", "\\|")
            ))
        lines.append("")
    else:
        lines.append("> {}（未提供支撑/阻力关键位）".format(MISSING))
        lines.append("")

    lines.append("### 情景")
    lines.append("")
    scenarios = trend.get("scenarios")
    scenario_rows: List[Tuple[str, str, str]] = []
    if isinstance(scenarios, list):
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                continue
            name = scenario.get("name")
            condition = scenario.get("condition")
            scenario_rows.append((
                str(name) if has_content(name) else MISSING,
                " ".join(str(condition).split()) if has_content(condition) else MISSING,
                fmt_price_range(scenario.get("range")),
            ))
    if scenario_rows:
        lines.append("| 名称 | 条件 | 区间 |")
        lines.append("|---|---|---|")
        for name, condition, span in scenario_rows:
            lines.append("| {} | {} | {} |".format(
                name.replace("|", "\\|"),
                condition.replace("|", "\\|"),
                span.replace("|", "\\|"),
            ))
        lines.append("")
    else:
        lines.append("> {}（未提供走势情景）".format(MISSING))
        lines.append("")

    lines.append("### 走势推理摘要")
    lines.append("")
    lines.append(as_text(trend.get("reasoning"), "未提供 reasoning", demote=2))
    lines.append("")
    lines.append(TREND_SECTION_DISCLAIMER)
    lines.append("")
    return lines


def render_document(document: Dict[str, Any], warnings: Sequence[Any]) -> str:
    name = document.get("name")
    symbol = document.get("symbol")
    as_of = document.get("as_of")
    depth = document.get("depth")
    schema_version = document.get("schema_version")

    if has_content(name) and has_content(symbol):
        title = "{}（{}）".format(name, symbol)
    elif has_content(name):
        title = str(name)
    elif has_content(symbol):
        title = str(symbol)
    else:
        title = MISSING

    lines: List[str] = []
    lines.append("# {} A 股研究报告".format(title))
    lines.append("")
    lines.append(
        "> 本报告由 TAC（tac）Skill 基于预取数据快照生成，"
        "仅供研究参考，不构成投资建议。"
    )
    lines.append("")

    # -- metadata ---------------------------------------------------------
    if isinstance(depth, str) and depth in DEPTH_LABELS:
        depth_display = "{}（{}）".format(DEPTH_LABELS[depth], depth)
    elif has_content(depth):
        depth_display = str(depth)
    else:
        depth_display = MISSING

    data_quality = document.get("data_quality")
    data_quality = data_quality if isinstance(data_quality, dict) else {}
    completeness = data_quality.get("completeness")

    lines.append("| 元信息 | 内容 |")
    lines.append("|---|---|")
    lines.append(_meta_table_row("股票代码", symbol if has_content(symbol) else MISSING))
    lines.append(_meta_table_row("股票名称", name if has_content(name) else MISSING))
    lines.append(_meta_table_row("报告日期", as_of if has_content(as_of) else MISSING))
    lines.append(_meta_table_row("分析深度", depth_display))
    lines.append(_meta_table_row("数据完整度", fmt_pct(completeness)))
    lines.append(
        _meta_table_row(
            "schema 版本",
            schema_version if has_content(schema_version) else MISSING,
        )
    )
    lines.append(
        _meta_table_row("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    lines.append("")

    final_section = document.get("final")
    final_section = final_section if isinstance(final_section, dict) else {}
    plan = document.get("investment_plan")
    plan = plan if isinstance(plan, dict) else {}
    risk = document.get("risk")
    risk = risk if isinstance(risk, dict) else {}

    # -- conclusion summary ----------------------------------------------
    lines.append("## 结论摘要")
    lines.append("")
    lines.append("- **研究倾向**：{}".format(display_action(final_section.get("action"))))
    lines.append("- **置信度**：{}".format(fmt_pct(final_section.get("confidence"))))
    lines.append(
        "- **风险等级**：{}（风险评分 {}）".format(
            risk.get("risk_level") if has_content(risk.get("risk_level")) else MISSING,
            fmt_pct(risk.get("risk_score")),
        )
    )
    lines.append(
        "- **价格区间**：{}".format(fmt_price_range(final_section.get("price_analysis_range")))
    )
    lines.append(
        "- **风险敞口**：{}".format(
            final_section.get("risk_exposure_ratio")
            if has_content(final_section.get("risk_exposure_ratio"))
            else MISSING
        )
    )
    lines.append(
        "- **一句话结论**：{}".format(
            inline_text(final_section.get("summary"), "未提供 summary")
        )
    )
    lines.append("")

    metrics = document.get("key_metrics")
    if not isinstance(metrics, dict) or not metrics:
        metrics = document.get("metrics")
    if isinstance(metrics, dict) and metrics:
        lines.append("### 关键数据")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---|")
        for key, value in metrics.items():
            lines.append(
                "| {} | {} |".format(
                    str(key).replace("|", "\\|"),
                    fmt_metric(key, value).replace("|", "\\|"),
                )
            )
        lines.append("")

    # -- six analysts -----------------------------------------------------
    analysts = document.get("analyst_reports")
    analysts = analysts if isinstance(analysts, dict) else {}
    enabled = DEPTH_ENABLED.get(depth, {}) if isinstance(depth, str) else {}

    lines.append("## 六位分析师观点")
    lines.append("")
    for role, label in ROLE_LABELS:
        lines.append("### {}（{}）".format(label, role))
        lines.append("")
        value = analysts.get(role)
        if has_content(value):
            lines.append(as_text(value, demote=2))
        elif not enabled.get(role, True):
            lines.append(
                "> {}（depth={} 未启用该角色）".format(MISSING, depth)
            )
        else:
            lines.append("> {}（未生成该角色报告）".format(MISSING))
        lines.append("")

    # -- bull / bear debate ----------------------------------------------
    debate = document.get("debate")
    debate = debate if isinstance(debate, dict) else {}

    lines.append("## 多空辩论")
    lines.append("")
    if not any(
        has_content(debate.get(key))
        for key in ("bull", "bear", "history", "rounds")
    ):
        lines.append("> {}（未提供多空辩论内容）".format(MISSING))
        lines.append("")
    else:
        rounds = debate.get("rounds")
        lines.append(
            "- **辩论轮次**：{}".format(rounds if has_content(rounds) else MISSING)
        )
        lines.append("")
        lines.append("### 多头观点")
        lines.append("")
        lines.append(as_text(debate.get("bull"), "未提供多头汇总观点", demote=2))
        lines.append("")
        lines.append("### 空头观点")
        lines.append("")
        lines.append(as_text(debate.get("bear"), "未提供空头汇总观点", demote=2))
        lines.append("")

        history = debate.get("history")
        if isinstance(history, list) and history:
            lines.append("### 辩论记录")
            lines.append("")
            for index, item in enumerate(history, 1):
                if isinstance(item, dict):
                    role = item.get("role")
                    role_label = {
                        "bull": "多头",
                        "bear": "空头",
                    }.get(role, role if has_content(role) else "发言")
                    round_no = item.get("round")
                    if has_content(round_no):
                        heading = "第 {} 轮 · {}".format(round_no, role_label)
                    else:
                        heading = "记录 {} · {}".format(index, role_label)
                    text = item.get("text")
                    if not has_content(text):
                        text = item.get("content")
                else:
                    heading = "记录 {}".format(index)
                    text = item
                lines.append("**{}**".format(heading))
                lines.append("")
                lines.append(as_text(text, "该条辩论记录为空", demote=2))
                lines.append("")
        elif has_content(debate.get("bull")) or has_content(debate.get("bear")):
            lines.append("> 未提供逐轮辩论记录，以上为汇总观点。")
            lines.append("")

    # -- trend analysis (optional; present only in trend mode) -------------
    trend = document.get("trend")
    if isinstance(trend, dict) and trend:
        lines.extend(_render_trend_section(trend))

    # -- research manager -------------------------------------------------
    lines.append("## 研究经理结论")
    lines.append("")
    if not any(
        has_content(plan.get(key))
        for key in ("action", "confidence", "points", "reasoning")
    ):
        lines.append("> {}（未提供 investment_plan）".format(MISSING))
        lines.append("")
    else:
        lines.append("- **研究倾向**：{}".format(display_action(plan.get("action"))))
        lines.append("- **置信度**：{}".format(fmt_pct(plan.get("confidence"))))
        lines.append("")
        lines.append("### 核心依据")
        lines.append("")
        lines.append(as_text(plan.get("points"), "未提供 points", demote=2))
        lines.append("")
        lines.append("### 推理")
        lines.append("")
        lines.append(as_text(plan.get("reasoning"), "未提供 reasoning", demote=2))
        lines.append("")

    # -- risk debate ------------------------------------------------------
    lines.append("## 风险辩论")
    lines.append("")
    if not any(
        has_content(risk.get(key))
        for key in ("risk_level", "risk_score", "key_risks", "risk_control", "opinions")
    ):
        lines.append("> {}（未提供风险辩论内容）".format(MISSING))
        lines.append("")
    else:
        lines.append(
            "- **风险等级**：{}".format(
                risk.get("risk_level")
                if has_content(risk.get("risk_level"))
                else MISSING
            )
        )
        lines.append("- **风险评分**：{}".format(fmt_pct(risk.get("risk_score"))))
        lines.append("")
        lines.append("### 关键风险")
        lines.append("")
        lines.append(as_text(risk.get("key_risks"), "未提供 key_risks", demote=2))
        lines.append("")
        lines.append("### 风险控制")
        lines.append("")
        lines.append(as_text(risk.get("risk_control"), "未提供 risk_control", demote=2))
        lines.append("")

        opinions = risk.get("opinions")
        lines.append("### 三方风险观点")
        lines.append("")
        if isinstance(opinions, dict):
            for role, label in RISK_ROLE_LABELS:
                lines.append("#### {}".format(label))
                lines.append("")
                lines.append(as_text(opinions.get(role), "未提供该视角", demote=2))
                lines.append("")
        elif has_content(opinions):
            lines.append(as_text(opinions, demote=2))
            lines.append("")
        else:
            lines.append("> {}（未提供 opinions）".format(MISSING))
            lines.append("")

    # -- final conclusion -------------------------------------------------
    lines.append("## 最终研究结论")
    lines.append("")
    if not any(
        has_content(final_section.get(key))
        for key in (
            "action", "confidence", "price_analysis_range",
            "risk_reference_price", "risk_exposure_ratio",
            "reasoning", "summary",
        )
    ):
        lines.append("> {}（未提供 final 结论）".format(MISSING))
        lines.append("")
    else:
        lines.append("- **研究倾向**：{}".format(display_action(final_section.get("action"))))
        lines.append("- **置信度**：{}".format(fmt_pct(final_section.get("confidence"))))
        lines.append(
            "- **价格区间**：{}".format(
                fmt_price_range(final_section.get("price_analysis_range"))
            )
        )
        lines.append(
            "- **风险参考价位**：{}".format(
                fmt_price(final_section.get("risk_reference_price"))
            )
        )
        lines.append(
            "- **风险敞口**：{}".format(
                final_section.get("risk_exposure_ratio")
                if has_content(final_section.get("risk_exposure_ratio"))
                else MISSING
            )
        )
        lines.append("")
        lines.append("### 推理")
        lines.append("")
        lines.append(as_text(final_section.get("reasoning"), "未提供 reasoning", demote=2))
        lines.append("")
        lines.append("### 摘要")
        lines.append("")
        lines.append(as_text(final_section.get("summary"), "未提供 summary", demote=2))
        lines.append("")
        if has_content(final_section.get("price_analysis_range")):
            lines.append("> 以上价格为研究区间，不是单点目标价。")
            lines.append("")

    # -- data quality -----------------------------------------------------
    lines.append("## 数据质量与限制")
    lines.append("")
    lines.append("- **数据完整度**：{}".format(fmt_pct(completeness)))
    lines.append("")

    lines.append("### 数据告警")
    lines.append("")
    if warnings:
        for item in warnings:
            text = warning_text(item)
            if text:
                lines.append("- {}".format(text))
    else:
        lines.append("未发现数据质量告警。")
    lines.append("")

    lines.append("### 缺失与降级项")
    lines.append("")
    issues = audit_issues(document)
    if issues:
        for item in issues:
            lines.append("- {}".format(item))
    else:
        lines.append("未发现缺失项。")
    lines.append("")

    lines.append("### 已知限制")
    lines.append("")
    lines.append("- 数据来自预取快照，行情、财务与新闻可能存在延迟，不代表实时状态。")
    lines.append("- 缺失字段一律保留为“{}”，未做插值、补零或臆测。".format(MISSING))
    lines.append("- 分析师、辩论与风险观点由模型生成，可能存在偏差或遗漏。")
    lines.append("- 价格结论仅为研究区间，不构成单点目标价或买卖价位。")
    if isinstance(depth, str) and depth in DEPTH_ENABLED:
        disabled = [
            role for role, on in DEPTH_ENABLED[depth].items() if not on
        ]
        if disabled:
            lines.append(
                "- depth={}，未启用角色：{}。".format(depth, "、".join(disabled))
            )
    lines.append("")

    # -- disclaimer -------------------------------------------------------
    lines.append("## 免责声明")
    lines.append("")
    disclaimer = final_section.get("disclaimer")
    if has_content(disclaimer):
        lines.append(as_text(disclaimer, demote=2))
    else:
        lines.append("本报告仅供研究参考，不构成投资建议。")
    lines.append("")
    lines.append(
        "本报告不构成任何证券买卖建议，也不构成对任何人的投资建议，"
        "不承诺收益。数据可能存在延迟或错误，投资者应独立判断并自行承担风险。"
    )
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

# A 股配色语义：红涨绿跌。乐观=红、审慎=绿、中性=琥珀。
ACTION_CLASS = {"乐观": "opt", "审慎": "caut", "中性": "neut"}
RISK_CLASS = {"低": "caut", "中": "neut", "高": "opt"}

HTML_CSS = """
:root{--bg:#f4f6f8;--card:#ffffff;--ink:#1d2733;--muted:#5f6b7a;--line:#e3e7ec;
--accent:#2b5fc7;--opt:#c0392b;--opt-bg:#fceae7;--caut:#0e7a55;--caut-bg:#e4f4ed;
--neut:#a86a00;--neut-bg:#fbf1dd;--code:#f0f2f5}
@media (prefers-color-scheme:dark){:root{--bg:#12161d;--card:#1a2029;--ink:#e4e8ee;
--muted:#96a0ad;--line:#2b3441;--accent:#7da4f0;--opt:#ef8b7e;--opt-bg:#3a241f;
--caut:#6ec9a4;--caut-bg:#173026;--neut:#e0b268;--neut-bg:#352a15;--code:#222a35}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.75
-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC",sans-serif}
main{max-width:880px;margin:0 auto;padding:28px 16px 48px}
header h1{font-size:26px;margin:0 0 6px}
header .lead{color:var(--muted);margin:0 0 12px;font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 4px;padding:0;list-style:none}
.chips span{background:var(--card);border:1px solid var(--line);border-radius:999px;
padding:3px 12px;font-size:12.5px;color:var(--muted)}
.chips b{color:var(--ink);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:22px 24px;margin:18px 0}
.card h2{font-size:19px;margin:0 0 14px;padding-bottom:10px;border-bottom:1px solid var(--line)}
.card h3{font-size:15.5px;margin:18px 0 8px}
.card h4{font-size:14px;margin:16px 0 6px;color:var(--muted)}
.badge{display:inline-block;padding:5px 16px;border-radius:999px;font-weight:700;font-size:15px}
.badge.opt{background:var(--opt-bg);color:var(--opt)}
.badge.caut{background:var(--caut-bg);color:var(--caut)}
.badge.neut{background:var(--neut-bg);color:var(--neut)}
.badge.dim{background:var(--code);color:var(--muted)}
.bar{height:8px;background:var(--code);border-radius:99px;margin:6px 0 2px;overflow:hidden}
.bar i{display:block;height:8px;border-radius:99px;background:var(--accent)}
.bar.opt i{background:var(--opt)}.bar.caut i{background:var(--caut)}.bar.neut i{background:var(--neut)}
.kv{display:grid;grid-template-columns:1fr 1fr;gap:14px 24px;margin:10px 0 4px}
.kv .k{font-size:12.5px;color:var(--muted)}
.kv .v{font-size:15px;font-weight:600}
.range{position:relative;height:12px;background:linear-gradient(90deg,var(--caut-bg),var(--code),var(--opt-bg));
border:1px solid var(--line);border-radius:99px;margin:14px 0 4px}
.range em{position:absolute;top:-3px;height:18px;border-radius:99px;background:var(--accent);opacity:.35}
.range-labels{display:flex;justify-content:space-between;font-size:12.5px;color:var(--muted)}
.note{background:var(--code);border-left:3px solid var(--accent);border-radius:6px;
padding:8px 12px;color:var(--muted);font-size:13.5px;margin:10px 0}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin:8px 0}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12.5px}
ul,ol{margin:8px 0;padding-left:22px}
li{margin:4px 0}
pre{background:var(--code);border:1px solid var(--line);border-radius:8px;padding:12px;
overflow:auto;font-size:12.5px;line-height:1.5}
code{background:var(--code);border-radius:4px;padding:1px 5px;font-size:.92em}
pre code{background:none;padding:0}
.missing{color:var(--muted);font-style:italic}
.sub{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.sub.bull{border-left:3px solid var(--opt)}
.sub.bear{border-left:3px solid var(--caut)}
footer{color:var(--muted);font-size:12.5px;margin-top:24px;line-height:1.7}
@media print{body{background:#fff}.card{border-color:#ccc;break-inside:avoid}}
"""


def _pct_num(value: Any) -> Optional[float]:
    """0-1（或 0-100 容忍）比值 → 0-100 数字，供进度条使用。"""
    if not is_number(value):
        return None
    number = float(value)
    if number <= 1.0:
        return number * 100.0
    return number if number <= 100.0 else None


def _bar(label: str, value: Any, cls: str = "") -> str:
    num = _pct_num(value)
    text = fmt_pct(value)
    width = "width:{:.1f}%".format(num) if num is not None else "width:0"
    return (
        '<div class="k"><span class="k">{}</span>'
        '<div class="bar {}"><i style="{}"></i></div>'
        '<span class="v">{}</span></div>'
    ).format(html_lib.escape(label), cls, width, html_lib.escape(text))


def _inline_md(text: Any) -> str:
    """转义并渲染行内 Markdown：**加粗**、`代码`。"""
    s = html_lib.escape(str(text))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def _md_to_html(value: Any, missing_note: str = "") -> str:
    """把 as_text 风格的 Markdown 块转成 HTML 片段。

    处理：围栏代码、标题、引用、无序/有序列表、管道表格、段落；
    其余行内标记只处理加粗与行内代码。空值降级为“数据缺失”。
    """
    note = "（{}）".format(missing_note) if missing_note else ""
    if not has_content(value):
        return '<p class="missing">{}{}</p>'.format(MISSING, html_lib.escape(note))
    if isinstance(value, (dict, list, tuple)) and not isinstance(value, str):
        value = as_text(value, missing_note)
    lines = str(value).replace("\r\n", "\n").split("\n")

    out: List[str] = []
    para: List[str] = []
    list_buf: List[str] = []
    list_tag = "ul"
    quoted: List[str] = []
    fenced = False
    fence_buf: List[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>{}</p>".format(_inline_md(" ".join(para))))
            para.clear()

    def flush_list() -> None:
        if list_buf:
            out.append("<{t}>{items}</{t}>".format(
                t=list_tag,
                items="".join("<li>{}</li>".format(_inline_md(i)) for i in list_buf),
            ))
            list_buf.clear()

    def flush_quote() -> None:
        if quoted:
            out.append('<div class="note">{}</div>'.format(
                _inline_md(" ".join(quoted))))
            quoted.clear()

    def flush_all() -> None:
        flush_para()
        flush_list()
        flush_quote()

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if fenced:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fenced = False
                out.append("<pre><code>{}</code></pre>".format(
                    html_lib.escape("\n".join(fence_buf))))
                fence_buf.clear()
            else:
                fence_buf.append(line)
            idx += 1
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush_all()
            fenced = True
            idx += 1
            continue
        if not stripped:
            flush_all()
            idx += 1
            continue
        head = re.match(r"^(#{1,6})\s+(.*)$", line)
        if head:
            flush_all()
            level = min(6, max(2, len(head.group(1)) + 1))
            out.append("<h{L}>{t}</h{L}>".format(
                L=level, t=_inline_md(head.group(2))))
            idx += 1
            continue
        if stripped.startswith(">"):
            flush_para()
            flush_list()
            quoted.append(stripped.lstrip("> ").strip())
            idx += 1
            continue
        bullet = re.match(r"^[-*•]\s+(.*)$", stripped)
        if bullet:
            flush_para()
            flush_quote()
            if list_tag != "ul" and list_buf:
                flush_list()
            list_tag = "ul"
            list_buf.append(bullet.group(1))
            idx += 1
            continue
        ordered = re.match(r"^\d+[.、)．]\s+(.*)$", stripped)
        if ordered:
            flush_para()
            flush_quote()
            if list_tag != "ol" and list_buf:
                flush_list()
            list_tag = "ol"
            list_buf.append(ordered.group(1))
            idx += 1
            continue
        if stripped.startswith("|") and idx + 1 < len(lines) \
                and re.match(r"^\|[\s:|-]+\|?\s*$", lines[idx + 1].strip() or ""):
            flush_all()
            rows: List[List[str]] = []
            header = [c.strip() for c in stripped.strip("|").split("|")]
            idx += 2
            while idx < len(lines) and lines[idx].strip().startswith("|"):
                rows.append([c.strip() for c in lines[idx].strip().strip("|").split("|")])
                idx += 1
            out.append("<table><thead><tr>{}</tr></thead><tbody>{}</tbody></table>".format(
                "".join("<th>{}</th>".format(_inline_md(c)) for c in header),
                "".join("<tr>{}</tr>".format(
                    "".join("<td>{}</td>".format(_inline_md(c)) for c in row))
                    for row in rows)))
            continue
        para.append(stripped)
        idx += 1
    flush_all()
    if fenced and fence_buf:
        out.append("<pre><code>{}</code></pre>".format(
            html_lib.escape("\n".join(fence_buf))))
    return "\n".join(out)


def _action_badge(value: Any, label: str) -> str:
    text = str(value).strip() if has_content(value) else MISSING
    cls = ACTION_CLASS.get(text, "")
    if not cls and has_content(value):
        cls = "dim"
    return '<div class="k"><span class="k">{}</span><br><span class="badge {}">{}</span></div>'.format(
        html_lib.escape(label), cls, html_lib.escape(text))


def _range_block(price_range: Any) -> str:
    if not isinstance(price_range, dict) or not (
        is_number(price_range.get("low")) and is_number(price_range.get("high"))
    ):
        return "<p><strong>价格区间</strong>：{}</p>".format(fmt_price_range(price_range))
    low = float(price_range["low"])
    high = float(price_range["high"])
    if low > high:
        low, high = high, low
    span = high - low
    pad = max(span * 0.15, high * 0.01, 0.01)
    lo_scale = low - pad
    hi_scale = high + pad
    left = (low - lo_scale) / (hi_scale - lo_scale) * 100.0
    width = max((high - low) / (hi_scale - lo_scale) * 100.0, 2.0)
    return (
        '<div class="range"><em style="left:{l:.1f}%;width:{w:.1f}%"></em></div>'
        '<div class="range-labels"><span>{lo}</span><span>{hi}</span></div>'
        '<p class="note">以上为研究分析区间，不是单点目标价。</p>'
    ).format(l=left, w=width, lo=fmt_price(low), hi=fmt_price(high))


def _render_trend_html(trend: Dict[str, Any]) -> str:
    parts: List[str] = ['<section class="card"><h2>走势分析</h2>']
    direction = str(trend.get("direction")).strip() \
        if has_content(trend.get("direction")) else MISSING
    if direction not in TREND_DIRECTIONS and direction != MISSING:
        direction = "{}（非标准取值）".format(direction)
    parts.append(
        '<div class="kv">{badge}<div><span class="k">置信度</span>'
        '{bar}</div><div><span class="k">时间窗</span><br><span class="v">{h}</span></div></div>'
    ).format(
        badge=_action_badge(direction, "走势方向"),
        bar=_bar("置信度", trend.get("confidence")),
        h=html_lib.escape(str(trend.get("horizon"))) if has_content(trend.get("horizon")) else MISSING,
    )
    levels = trend.get("key_levels")
    levels = levels if isinstance(levels, dict) else {}
    level_rows: List[Tuple[str, str]] = []
    for side, fallback in (("support", "支撑"), ("resistance", "阻力")):
        items = levels.get(side)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                kind = item.get("type")
                kind = str(kind) if has_content(kind) else fallback
                level_rows.append((kind, fmt_price(item.get("price"))))
            else:
                level_rows.append((fallback, MISSING))
    if level_rows:
        parts.append("<h3>关键位</h3><table><thead><tr><th>类型</th><th>价位</th></tr></thead><tbody>{}</tbody></table>".format(
            "".join("<tr><td>{}</td><td>{}</td></tr>".format(
                html_lib.escape(k), html_lib.escape(p)) for k, p in level_rows)))
    else:
        parts.append('<h3>关键位</h3><p class="missing">{}（未提供支撑/阻力关键位）</p>'.format(MISSING))
    scenarios = trend.get("scenarios")
    scenario_rows: List[Tuple[str, str, str]] = []
    if isinstance(scenarios, list):
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                continue
            name = scenario.get("name")
            condition = scenario.get("condition")
            scenario_rows.append((
                str(name) if has_content(name) else MISSING,
                " ".join(str(condition).split()) if has_content(condition) else MISSING,
                fmt_price_range(scenario.get("range")),
            ))
    if scenario_rows:
        parts.append("<h3>情景</h3><table><thead><tr><th>名称</th><th>条件</th><th>区间</th></tr></thead><tbody>{}</tbody></table>".format(
            "".join("<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                html_lib.escape(n), html_lib.escape(c), html_lib.escape(s))
                for n, c, s in scenario_rows)))
    else:
        parts.append('<h3>情景</h3><p class="missing">{}（未提供走势情景）</p>'.format(MISSING))
    parts.append("<h3>走势推理摘要</h3>{}".format(_md_to_html(trend.get("reasoning"), "未提供 reasoning")))
    parts.append('<div class="note">{}</div>'.format(html_lib.escape(TREND_SECTION_DISCLAIMER)))
    parts.append("</section>")
    return "\n".join(parts)


def render_html_document(document: Dict[str, Any], warnings: Sequence[Any]) -> str:
    """Render final.json into a self-contained HTML page."""
    name = document.get("name")
    symbol = document.get("symbol")
    as_of = document.get("as_of")
    depth = document.get("depth")
    schema_version = document.get("schema_version")

    if has_content(name) and has_content(symbol):
        title = "{}（{}）".format(name, symbol)
    elif has_content(name):
        title = str(name)
    elif has_content(symbol):
        title = str(symbol)
    else:
        title = MISSING

    if isinstance(depth, str) and depth in DEPTH_LABELS:
        depth_display = "{}（{}）".format(DEPTH_LABELS[depth], depth)
    elif has_content(depth):
        depth_display = str(depth)
    else:
        depth_display = MISSING

    data_quality = document.get("data_quality")
    data_quality = data_quality if isinstance(data_quality, dict) else {}
    completeness = data_quality.get("completeness")

    final_section = document.get("final")
    final_section = final_section if isinstance(final_section, dict) else {}
    plan = document.get("investment_plan")
    plan = plan if isinstance(plan, dict) else {}
    risk = document.get("risk")
    risk = risk if isinstance(risk, dict) else {}
    debate = document.get("debate")
    debate = debate if isinstance(debate, dict) else {}
    analysts = document.get("analyst_reports")
    analysts = analysts if isinstance(analysts, dict) else {}
    enabled = DEPTH_ENABLED.get(depth, {}) if isinstance(depth, str) else {}

    parts: List[str] = []
    parts.append(
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>{title} A 股研究报告</title>\n<style>{css}</style>\n</head>\n<body>\n<main>".format(
            title=html_lib.escape(title), css=HTML_CSS))

    # -- header ------------------------------------------------------------
    parts.append("<header><h1>{} A 股研究报告</h1>".format(html_lib.escape(title)))
    parts.append('<p class="lead">本报告由 TAC（tac）Skill 基于预取数据快照生成，'
                 "仅供研究参考，不构成投资建议。</p>")
    chips = [
        "股票代码 <b>{}</b>".format(html_lib.escape(str(symbol) if has_content(symbol) else MISSING)),
        "报告日期 <b>{}</b>".format(html_lib.escape(str(as_of) if has_content(as_of) else MISSING)),
        "分析深度 <b>{}</b>".format(html_lib.escape(depth_display)),
        "数据完整度 <b>{}</b>".format(fmt_pct(completeness)),
        "schema <b>{}</b>".format(html_lib.escape(str(schema_version) if has_content(schema_version) else MISSING)),
        "生成时间 <b>{}</b>".format(datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    parts.append('<ul class="chips">{}</ul></header>'.format(
        "".join("<span>{}</span>".format(c) for c in chips)))

    # -- conclusion summary --------------------------------------------------
    risk_level = risk.get("risk_level") if has_content(risk.get("risk_level")) else MISSING
    risk_cls = RISK_CLASS.get(str(risk_level), "neut")
    parts.append('<section class="card"><h2>结论摘要</h2><div class="kv">')
    parts.append(_action_badge(final_section.get("action"), "研究倾向"))
    parts.append('<div><span class="k">置信度</span>{}</div>'.format(_bar("置信度", final_section.get("confidence"))))
    parts.append('<div><span class="k">风险等级</span><br><span class="badge {}">{}</span> '
                 '<span class="k">评分 {}</span></div>'.format(risk_cls, html_lib.escape(str(risk_level)),
                                                              fmt_pct(risk.get("risk_score"))))
    parts.append('<div><span class="k">风险敞口</span><br><span class="v">{}</span></div>'.format(
        html_lib.escape(str(final_section.get("risk_exposure_ratio")))
        if has_content(final_section.get("risk_exposure_ratio")) else MISSING))
    parts.append("</div>")
    parts.append(_range_block(final_section.get("price_analysis_range")))
    parts.append("<h3>一句话结论</h3>{}".format(_md_to_html(final_section.get("summary"), "未提供 summary")))
    metrics = document.get("key_metrics")
    if not isinstance(metrics, dict) or not metrics:
        metrics = document.get("metrics")
    if isinstance(metrics, dict) and metrics:
        rows = "".join("<tr><td>{}</td><td>{}</td></tr>".format(
            html_lib.escape(str(k)), html_lib.escape(fmt_metric(k, v)))
            for k, v in metrics.items())
        parts.append("<h3>关键数据</h3><table><thead><tr><th>指标</th><th>数值</th></tr></thead><tbody>{}</tbody></table>".format(rows))
    parts.append("</section>")

    # -- six analysts --------------------------------------------------------
    parts.append('<section class="card"><h2>六位分析师观点</h2>')
    for role, label in ROLE_LABELS:
        parts.append("<h3>{}（{}）</h3>".format(html_lib.escape(label), html_lib.escape(role)))
        value = analysts.get(role)
        if has_content(value):
            parts.append(_md_to_html(value))
        elif not enabled.get(role, True):
            parts.append('<p class="missing">{}（depth={} 未启用该角色）</p>'.format(MISSING, html_lib.escape(str(depth))))
        else:
            parts.append('<p class="missing">{}（未生成该角色报告）</p>'.format(MISSING))
    parts.append("</section>")

    # -- bull / bear debate ---------------------------------------------------
    parts.append('<section class="card"><h2>多空辩论</h2>')
    if not any(has_content(debate.get(key)) for key in ("bull", "bear", "history", "rounds")):
        parts.append('<p class="missing">{}（未提供多空辩论内容）</p>'.format(MISSING))
    else:
        rounds = debate.get("rounds")
        parts.append('<ul class="chips"><span>辩论轮次 <b>{}</b></span></ul>'.format(
            html_lib.escape(str(rounds)) if has_content(rounds) else MISSING))
        parts.append('<div class="sub bull"><h3>多头观点</h3>{}</div>'.format(
            _md_to_html(debate.get("bull"), "未提供多头汇总观点")))
        parts.append('<div class="sub bear"><h3>空头观点</h3>{}</div>'.format(
            _md_to_html(debate.get("bear"), "未提供空头汇总观点")))
        history = debate.get("history")
        if isinstance(history, list) and history:
            parts.append("<h3>辩论记录</h3>")
            for index, item in enumerate(history, 1):
                if isinstance(item, dict):
                    role = item.get("role")
                    role_label = {"bull": "多头", "bear": "空头"}.get(
                        role, str(role) if has_content(role) else "发言")
                    round_no = item.get("round")
                    heading = "第 {} 轮 · {}".format(round_no, role_label) \
                        if has_content(round_no) else "记录 {} · {}".format(index, role_label)
                    text = item.get("text")
                    if not has_content(text):
                        text = item.get("content")
                else:
                    heading = "记录 {}".format(index)
                    text = item
                parts.append('<div class="sub"><h4>{}</h4>{}</div>'.format(
                    html_lib.escape(heading), _md_to_html(text, "该条辩论记录为空")))
        elif has_content(debate.get("bull")) or has_content(debate.get("bear")):
            parts.append('<p class="missing">未提供逐轮辩论记录，以上为汇总观点。</p>')
    parts.append("</section>")

    # -- trend analysis (optional) --------------------------------------------
    trend = document.get("trend")
    if isinstance(trend, dict) and trend:
        parts.append(_render_trend_html(trend))

    # -- research manager -------------------------------------------------------
    parts.append('<section class="card"><h2>研究经理结论</h2>')
    if not any(has_content(plan.get(key)) for key in ("action", "confidence", "points", "reasoning")):
        parts.append('<p class="missing">{}（未提供 investment_plan）</p>'.format(MISSING))
    else:
        parts.append('<div class="kv">{}<div><span class="k">置信度</span>{}</div></div>'.format(
            _action_badge(plan.get("action"), "研究倾向"),
            _bar("置信度", plan.get("confidence"))))
        parts.append("<h3>核心依据</h3>{}".format(_md_to_html(plan.get("points"), "未提供 points")))
        parts.append("<h3>推理</h3>{}".format(_md_to_html(plan.get("reasoning"), "未提供 reasoning")))
    parts.append("</section>")

    # -- risk debate -------------------------------------------------------------
    parts.append('<section class="card"><h2>风险辩论</h2>')
    if not any(has_content(risk.get(key)) for key in ("risk_level", "risk_score", "key_risks", "risk_control", "opinions")):
        parts.append('<p class="missing">{}（未提供风险辩论内容）</p>'.format(MISSING))
    else:
        parts.append('<div class="kv">{}<div><span class="k">风险评分</span>{}</div></div>'.format(
            _action_badge(risk_level, "风险等级"),
            _bar("风险评分", risk.get("risk_score"), risk_cls)))
        parts.append("<h3>关键风险</h3>{}".format(_md_to_html(risk.get("key_risks"), "未提供 key_risks")))
        parts.append("<h3>风险控制</h3>{}".format(_md_to_html(risk.get("risk_control"), "未提供 risk_control")))
        opinions = risk.get("opinions")
        parts.append("<h3>三方风险观点</h3>")
        if isinstance(opinions, dict):
            for role, label in RISK_ROLE_LABELS:
                parts.append("<h4>{}</h4>{}".format(html_lib.escape(label),
                                                    _md_to_html(opinions.get(role), "未提供该视角")))
        elif has_content(opinions):
            parts.append(_md_to_html(opinions))
        else:
            parts.append('<p class="missing">{}（未提供 opinions）</p>'.format(MISSING))
    parts.append("</section>")

    # -- final conclusion ----------------------------------------------------------
    parts.append('<section class="card"><h2>最终研究结论</h2>')
    if not any(has_content(final_section.get(key)) for key in (
            "action", "confidence", "price_analysis_range", "risk_reference_price",
            "risk_exposure_ratio", "reasoning", "summary")):
        parts.append('<p class="missing">{}（未提供 final 结论）</p>'.format(MISSING))
    else:
        parts.append('<div class="kv">{}<div><span class="k">置信度</span>{}</div></div>'.format(
            _action_badge(final_section.get("action"), "研究倾向"),
            _bar("置信度", final_section.get("confidence"))))
        parts.append(_range_block(final_section.get("price_analysis_range")))
        parts.append("<p><strong>风险参考价位</strong>：{}</p>".format(
            fmt_price(final_section.get("risk_reference_price"))))
        parts.append("<h3>推理</h3>{}".format(_md_to_html(final_section.get("reasoning"), "未提供 reasoning")))
        parts.append("<h3>摘要</h3>{}".format(_md_to_html(final_section.get("summary"), "未提供 summary")))
    parts.append("</section>")

    # -- data quality -----------------------------------------------------------------
    parts.append('<section class="card"><h2>数据质量与限制</h2>')
    parts.append(_bar("数据完整度", completeness))
    parts.append("<h3>数据告警</h3>")
    if warnings:
        items = "".join("<li>{}</li>".format(_inline_md(warning_text(item)))
                        for item in warnings if warning_text(item))
        parts.append("<ul>{}</ul>".format(items))
    else:
        parts.append("<p>未发现数据质量告警。</p>")
    parts.append("<h3>缺失与降级项</h3>")
    issues = audit_issues(document)
    if issues:
        parts.append("<ul>{}</ul>".format(
            "".join("<li>{}</li>".format(_inline_md(i)) for i in issues)))
    else:
        parts.append("<p>未发现缺失项。</p>")
    parts.append("<h3>已知限制</h3><ul>")
    limits = [
        "数据来自预取快照，行情、财务与新闻可能存在延迟，不代表实时状态。",
        "缺失字段一律保留为“{}”，未做插值、补零或臆测。".format(MISSING),
        "分析师、辩论与风险观点由模型生成，可能存在偏差或遗漏。",
        "价格结论仅为研究区间，不构成单点目标价或买卖价位。",
    ]
    if isinstance(depth, str) and depth in DEPTH_ENABLED:
        disabled = [role for role, on in DEPTH_ENABLED[depth].items() if not on]
        if disabled:
            limits.append("depth={}，未启用角色：{}。".format(depth, "、".join(disabled)))
    parts.append("{} </ul>".format("".join("<li>{}</li>".format(_inline_md(x)) for x in limits)))
    parts.append("</section>")

    # -- disclaimer ---------------------------------------------------------------------
    parts.append('<footer><h2>免责声明</h2>')
    if has_content(final_section.get("disclaimer")):
        parts.append(_md_to_html(final_section.get("disclaimer")))
    else:
        parts.append("<p>本报告仅供研究参考，不构成投资建议。</p>")
    parts.append("<p>本报告不构成任何证券买卖建议，也不构成对任何人的投资建议，"
                 "不承诺收益。数据可能存在延迟或错误，投资者应独立判断并自行承担风险。</p></footer>")
    parts.append("</main>\n</body>\n</html>\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _ArgumentParser(argparse.ArgumentParser):
    """Make usage errors exit with 3, matching the input-error contract."""

    def error(self, message: str) -> None:  # pragma: no cover - argparse path
        self.print_usage(sys.stderr)
        self.exit(3, "{}: error: {}\n".format(self.prog, message))


def load_document(path: str) -> Dict[str, Any]:
    try:
        if path == "-":
            raw = sys.stdin.read()
        else:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read()
    except OSError as exc:
        raise InputError("无法读取输入文件 {}: {}".format(path, exc))
    except UnicodeDecodeError as exc:
        raise InputError("输入文件不是 UTF-8 文本: {}".format(exc))

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError("输入不是合法 JSON: {}".format(exc))

    if not isinstance(document, dict):
        raise InputError("输入 JSON 顶层必须是对象")
    if not any(key in document for key in KNOWN_TOP_LEVEL):
        raise InputError(
            "输入缺少已知顶层字段（symbol/analyst_reports/final 等），不是 final.json"
        )
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="render.py",
        description="将 A 股研究 final.json 渲染为中文报告（默认自包含 HTML 网页）。",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="final.json 路径；使用 - 表示从 stdin 读取",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="输出路径；省略时打印到 stdout",
    )
    parser.add_argument(
        "--format",
        choices=("html", "md"),
        default="html",
        help="输出格式：html（默认，自包含网页）或 md（Markdown）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        document = load_document(args.input)
    except InputError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 3

    document, warnings = prepare(document)
    if args.format == "md":
        output = render_document(document, warnings)
    else:
        output = render_html_document(document, warnings)

    if args.out:
        try:
            parent = os.path.dirname(os.path.abspath(args.out))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(output)
        except OSError as exc:
            print("ERROR: 无法写入 {}: {}".format(args.out, exc), file=sys.stderr)
            return 3
        print("WROTE {}".format(args.out))
    else:
        try:
            sys.stdout.write(output)
        except BrokenPipeError:  # pragma: no cover - pipes
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
