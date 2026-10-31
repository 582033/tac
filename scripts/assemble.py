#!/usr/bin/env python3
"""把 reports/ 下的角色报告确定性组装成 final.json。

用法:
    python scripts/assemble.py --run-dir data/600519_2026-10-31 --depth standard \
        --completeness 0.72 --warning "营收存在两个口径"
    python scripts/assemble.py --run-dir data/600519_2026-10-31 --mode trend

职责边界:
- 只做结构化拼装与校验，不生成任何新判断。
- investment_plan / risk / trader 的 JSON 块由对应角色写入，本脚本只提取。
- 任何必需块缺失时退出 3，不猜测、不填默认值。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

ROLE_FILES = {
    "index": "index.md",
    "sector": "sector.md",
    "market": "market.md",
    "fundamentals": "fundamentals.md",
    "news": "news.md",
    "social": "social.md",
}
DEPTH_ROLES = {
    "quick": ["market", "fundamentals", "news"],
    "standard": ["index", "sector", "market", "fundamentals", "news"],
    "deep": ["index", "sector", "market", "fundamentals", "news", "social"],
    "full": ["index", "sector", "market", "fundamentals", "news", "social"],
}
DEPTH_ROUNDS = {"quick": 1, "standard": 1, "deep": 2, "full": 3}
ACTIONS = ("乐观", "审慎", "中性")
RISK_LEVELS = ("低", "中", "高")
DISCLAIMER = "本报告仅供研究参考，不构成投资建议。"

# ---- trend 模式（股价走势分析）常量，见 CONTRACT.md 第 7 节 -----------------
MODES = ("research", "trend")
TREND_ROLE_FILES = {
    "trend_structure": "trend_structure.md",
    "trend_cycle": "trend_cycle.md",
    "trend_catalyst": "trend_catalyst.md",
    "bull_trend": "bull_trend.md",
    "bear_trend": "bear_trend.md",
    "trend_manager": "trend_manager.md",
    "conservative_trend": "conservative_trend.md",
}
TREND_DIRECTIONS = ("上行", "震荡", "下行")
TREND_ACTION_MAP = {"上行": "乐观", "震荡": "中性", "下行": "审慎"}
TREND_PLACEHOLDER = "数据缺失（trend 模式未启用）"
# 走势分析师报告落入 analyst_reports 六槽位的映射，其余槽位填占位符。
TREND_ANALYST_SLOTS = {
    "trend_structure": "market",
    "trend_cycle": "index",
    "trend_catalyst": "news",
}
TREND_SOURCE_NOTE = "（来源：conservative_trend）"


def _read(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def _last_json_block(text: str, label: str) -> Dict[str, Any]:
    blocks = re.findall(r"```json\s*(.*?)```", text, re.S)
    if not blocks:
        raise ValueError("%s 中没有 ```json 代码块" % label)
    try:
        data = json.loads(blocks[-1])
    except Exception as exc:  # noqa: BLE001
        raise ValueError("%s 的 JSON 块无法解析: %s" % (label, exc))
    if not isinstance(data, dict):
        raise ValueError("%s 的 JSON 块必须是对象" % label)
    return data


def _require(obj: Dict[str, Any], keys: List[str], label: str) -> None:
    missing = [k for k in keys if k not in obj]
    if missing:
        raise ValueError("%s 缺字段: %s" % (label, ", ".join(missing)))


def _normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    _require(plan, ["action", "confidence", "points", "reasoning"], "investment_plan")
    if plan["action"] not in ACTIONS:
        raise ValueError("investment_plan.action 非法: %r" % plan["action"])
    return {
        "action": plan["action"],
        "confidence": float(plan["confidence"]),
        "points": [str(p) for p in plan["points"]],
        "reasoning": str(plan["reasoning"]),
    }


def _normalize_risk(risk: Dict[str, Any]) -> Dict[str, Any]:
    _require(risk, ["risk_level", "risk_score", "key_risks", "risk_control", "opinions"],
             "risk")
    if risk["risk_level"] not in RISK_LEVELS:
        raise ValueError("risk.risk_level 非法: %r" % risk["risk_level"])
    opinions = risk["opinions"]
    if not isinstance(opinions, dict):
        raise ValueError("risk.opinions 必须是对象")
    for k in ("aggressive", "conservative", "neutral"):
        if k not in opinions:
            raise ValueError("risk.opinions 缺 %s" % k)
    return {
        "risk_level": risk["risk_level"],
        "risk_score": float(risk["risk_score"]),
        "key_risks": [str(x) for x in risk["key_risks"]],
        "risk_control": str(risk["risk_control"]),
        "opinions": {k: str(opinions[k]) for k in ("aggressive", "conservative", "neutral")},
    }


def _normalize_trend(data: Dict[str, Any]) -> Dict[str, Any]:
    """校验 trend_manager 的 JSON 块，通过后原样返回。"""
    _require(data, ["direction", "confidence", "horizon", "key_levels",
                    "scenarios", "reasoning"], "trend_manager")
    if data["direction"] not in TREND_DIRECTIONS:
        raise ValueError("trend.direction 非法: %r（仅 %s）"
                         % (data["direction"], "/".join(TREND_DIRECTIONS)))
    confidence = float(data["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("trend.confidence 超出 [0,1]: %r" % data["confidence"])
    levels = data["key_levels"]
    if not isinstance(levels, dict):
        raise ValueError("trend.key_levels 必须是对象")
    for side in ("support", "resistance"):
        items = levels.get(side)
        if not isinstance(items, list):
            raise ValueError("trend.key_levels.%s 必须是数组" % side)
        for item in items:
            if not isinstance(item, dict) or "price" not in item or "type" not in item:
                raise ValueError("trend.key_levels.%s 的元素须含 price/type" % side)
    scenarios = data["scenarios"]
    if not isinstance(scenarios, list) or not 2 <= len(scenarios) <= 3:
        raise ValueError("trend.scenarios 必须是 2-3 个情景")
    for sc in scenarios:
        if not isinstance(sc, dict):
            raise ValueError("trend.scenarios 元素必须是对象")
        _require(sc, ["name", "condition", "range"], "trend.scenario")
        rng = sc["range"]
        if not isinstance(rng, dict) or "low" not in rng or "high" not in rng:
            raise ValueError("trend.scenario.range 必须是含 low/high 的区间")
        float(rng["low"])
        float(rng["high"])
    if not str(data["reasoning"]).strip():
        raise ValueError("trend.reasoning 不能为空")
    return data


def _trend_items(text: str) -> List[str]:
    """从 conservative_trend 文本提取条目：优先列表/短行，退化到按句切分。"""
    items: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```") or line.startswith("#"):
            continue
        line = re.sub(r"^[-*•·]\s*", "", line)
        line = re.sub(r"^\d+[.、)．]\s*", "", line)
        if line:
            items.append(line)
    if not items:
        items = [s.strip() for s in re.split(r"[。！？；]", text) if s.strip()]
    return items


def _trend_summary(text: str) -> str:
    """conservative_trend 摘要：去掉代码围栏后的全文（角色输出本身 ≤250 字）。"""
    summary = "\n".join(
        line for line in text.splitlines()
        if not line.strip().startswith("```")
    ).strip()
    return summary or "conservative_trend 未提供内容"


def _validate_final(final: Dict[str, Any]) -> None:
    _require(final, ["schema_version", "symbol", "name", "as_of", "depth",
                     "analyst_reports", "debate", "investment_plan", "risk",
                     "final", "data_quality"], "final")
    if final["final"]["action"] not in ACTIONS:
        raise ValueError("final.action 非法")
    if final["investment_plan"]["action"] not in ACTIONS:
        raise ValueError("investment_plan.action 非法")
    for path, value in (("confidence", final["final"]["confidence"]),
                        ("risk_score", final["risk"]["risk_score"]),
                        ("completeness", final["data_quality"]["completeness"])):
        if not (0.0 <= float(value) <= 1.0):
            raise ValueError("%s 超出 [0,1]: %r" % (path, value))
    for role in ROLE_FILES:
        if role not in final["analyst_reports"]:
            raise ValueError("analyst_reports 缺 %s" % role)


def _run_trend(args, run_dir: str, bundle: Dict[str, Any]) -> int:
    """mode=trend：读取 7 份走势报告，机械映射成 final.json（不生成新判断）。"""
    reports_dir = os.path.join(run_dir, "reports")
    texts: Dict[str, str] = {}
    for role, filename in TREND_ROLE_FILES.items():
        text = _read(os.path.join(reports_dir, filename))
        if not text:
            print("ERROR: 缺少 reports/%s（mode=trend 必需）" % filename, file=sys.stderr)
            return 3
        texts[role] = text

    try:
        trend = _normalize_trend(
            _last_json_block(texts["trend_manager"], "trend_manager.md"))
    except ValueError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3

    meta = bundle.get("meta") or {}
    analyst_reports: Dict[str, str] = {role: TREND_PLACEHOLDER for role in ROLE_FILES}
    for role, slot in TREND_ANALYST_SLOTS.items():
        analyst_reports[slot] = texts[role]

    bull = texts["bull_trend"]
    bear = texts["bear_trend"]
    confidence = float(trend["confidence"])
    plan: Dict[str, Any] = {
        "action": TREND_ACTION_MAP[trend["direction"]],
        "confidence": confidence,
        "points": ["%s：%s" % (sc["name"], sc["condition"]) for sc in trend["scenarios"]],
        "reasoning": str(trend["reasoning"]),
    }

    conservative_summary = _trend_summary(texts["conservative_trend"])
    key_risks = _trend_items(texts["conservative_trend"])[:5]
    if not key_risks:
        key_risks = ["conservative_trend 未给出明确失效条件"]
    risk: Dict[str, Any] = {
        "risk_level": "中",  # trend 契约下限，不替角色上调
        "risk_score": 0.5,   # trend 契约默认
        "key_risks": key_risks,
        "risk_control": TREND_SOURCE_NOTE + conservative_summary,
        "opinions": {k: TREND_SOURCE_NOTE + conservative_summary
                     for k in ("aggressive", "conservative", "neutral")},
    }

    base_range = trend["scenarios"][0].get("range") or {}
    low = base_range.get("low")
    high = base_range.get("high")
    if low is not None and high is not None and float(low) > float(high):
        low, high = high, low

    warnings: List[str] = list(meta.get("warnings") or [])
    warnings.extend(args.warning)
    warnings.append("mode=trend：研究报告 sector、fundamentals、social 未启用")
    warnings.append("mode=trend：risk 由 conservative_trend 摘要合成"
                    "（risk_level=中、risk_score=0.5 为契约默认）")
    warnings = list(dict.fromkeys(warnings))

    completeness = args.completeness
    if completeness is None:
        completeness = max(0.3, 1.0 - 0.05 * len(warnings))

    final_doc: Dict[str, Any] = {
        "schema_version": "1.0",
        "symbol": meta.get("symbol"),
        "name": meta.get("name"),
        "as_of": meta.get("as_of"),
        "depth": args.depth,
        "analyst_reports": analyst_reports,
        "debate": {
            "rounds": DEPTH_ROUNDS[args.depth],
            "bull": bull,
            "bear": bear,
            "history": [{"role": "bull", "text": bull}, {"role": "bear", "text": bear}],
        },
        "investment_plan": plan,
        "risk": risk,
        "trend": trend,
        "final": {
            "action": plan["action"],
            "confidence": confidence,
            "price_analysis_range": {
                "low": None if low is None else float(low),
                "high": None if high is None else float(high),
            },
            "risk_reference_price": None,
            "risk_exposure_ratio": risk["risk_level"],
            "reasoning": str(trend["reasoning"]),
            "summary": "走势方向：%s（置信度 %.2f，时间窗 %s）。基准情景：%s。"
                       "失效条件与证伪位见 conservative_trend。"
                       % (trend["direction"], confidence, trend["horizon"],
                          trend["scenarios"][0].get("condition")),
            "disclaimer": DISCLAIMER,
        },
        "data_quality": {
            "completeness": round(float(completeness), 4),
            "warnings": warnings,
        },
    }

    try:
        _validate_final(final_doc)
    except ValueError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3

    out = args.out or os.path.join(run_dir, "final.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(final_doc, fh, ensure_ascii=False, indent=2, allow_nan=False)
    print("WROTE %s" % out)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="组装 final.json")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--depth", default="standard", choices=sorted(DEPTH_ROLES))
    parser.add_argument("--mode", default="research", choices=list(MODES),
                        help="研究模式：research=默认流水线，trend=股价走势分析")
    parser.add_argument("--completeness", type=float, default=None,
                        help="数据完整度 0-1；缺省时按 warning 数量粗估")
    parser.add_argument("--warning", action="append", default=[],
                        help="额外写入 data_quality.warnings 的说明，可重复")
    parser.add_argument("--out", default=None, help="默认 {run_dir}/final.json")
    args = parser.parse_args(argv)

    run_dir = args.run_dir
    reports_dir = os.path.join(run_dir, "reports")
    bundle_path = os.path.join(run_dir, "bundle.tech.json")
    if not os.path.isfile(bundle_path):
        print("ERROR: bundle.tech.json 不存在: %s" % bundle_path, file=sys.stderr)
        return 3
    with open(bundle_path, "r", encoding="utf-8") as fh:
        bundle = json.load(fh)

    if args.mode == "trend":
        return _run_trend(args, run_dir, bundle)
    meta = bundle.get("meta") or {}

    analyst_reports: Dict[str, str] = {}
    missing_roles: List[str] = []
    enabled = DEPTH_ROLES[args.depth]
    for role, filename in ROLE_FILES.items():
        if role not in enabled:
            analyst_reports[role] = "数据缺失（depth=%s 未启用 %s）" % (args.depth, role)
            continue
        text = _read(os.path.join(reports_dir, filename))
        if not text:
            missing_roles.append(role)
            analyst_reports[role] = "数据缺失（未生成报告）"
        else:
            analyst_reports[role] = text

    bull = _read(os.path.join(reports_dir, "bull.md"))
    bear = _read(os.path.join(reports_dir, "bear.md"))
    rm_text = _read(os.path.join(reports_dir, "research_manager.md"))
    risk_text = _read(os.path.join(reports_dir, "risk_manager.md"))
    trader_text = _read(os.path.join(reports_dir, "trader.md"))
    for name, text in (("bull", bull), ("bear", bear), ("research_manager", rm_text),
                       ("risk_manager", risk_text), ("trader", trader_text)):
        if not text:
            print("ERROR: 缺少 reports/%s.md" % name, file=sys.stderr)
            return 3

    try:
        plan = _normalize_plan(_last_json_block(rm_text, "research_manager.md"))
        risk = _normalize_risk(_last_json_block(risk_text, "risk_manager.md"))
        brief = _last_json_block(trader_text, "trader.md")
        _require(brief, ["summary", "reasoning"], "trader")
    except ValueError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3

    warnings: List[str] = list(meta.get("warnings") or [])
    warnings.extend(args.warning)
    for role in missing_roles:
        warnings.append("分析报告缺失：%s" % role)
    if args.depth != "full":
        disabled = [r for r in ROLE_FILES if r not in enabled]
        if disabled:
            warnings.append("未启用角色：%s（depth=%s）" % (",".join(disabled), args.depth))
    warnings = list(dict.fromkeys(warnings))

    completeness = args.completeness
    if completeness is None:
        completeness = max(0.3, 1.0 - 0.05 * len(warnings))

    price_range = brief.get("price_analysis_range") or {}
    low = price_range.get("low")
    high = price_range.get("high")
    if low is not None and high is not None and float(low) > float(high):
        low, high = high, low

    final_doc: Dict[str, Any] = {
        "schema_version": "1.0",
        "symbol": meta.get("symbol"),
        "name": meta.get("name"),
        "as_of": meta.get("as_of"),
        "depth": args.depth,
        "analyst_reports": analyst_reports,
        "debate": {
            "rounds": DEPTH_ROUNDS[args.depth],
            "bull": bull,
            "bear": bear,
            "history": [{"role": "bull", "text": bull}, {"role": "bear", "text": bear}],
        },
        "investment_plan": plan,
        "risk": risk,
        "final": {
            "action": plan["action"],
            "confidence": float(plan["confidence"]),
            "price_analysis_range": {
                "low": None if low is None else float(low),
                "high": None if high is None else float(high),
            },
            "risk_reference_price": (None if brief.get("risk_reference_price") is None
                                     else float(brief["risk_reference_price"])),
            "risk_exposure_ratio": risk["risk_level"],
            "reasoning": str(brief.get("reasoning") or plan["reasoning"]),
            "summary": str(brief["summary"]),
            "disclaimer": DISCLAIMER,
        },
        "data_quality": {
            "completeness": round(float(completeness), 4),
            "warnings": warnings,
        },
    }

    try:
        _validate_final(final_doc)
    except ValueError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3

    out = args.out or os.path.join(run_dir, "final.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(final_doc, fh, ensure_ascii=False, indent=2, allow_nan=False)
    print("WROTE %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
