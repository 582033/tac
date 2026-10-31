#!/usr/bin/env python3
"""把 bundle.tech.json 切成角色专用的最小上下文切片。

用法:
    python scripts/slice_bundle.py --input data/600519/bundle.tech.json \
        --out-dir data/600519/slices --depth standard [--mode research|trend]

产出:
    slices/index.json / sector.json / market.json / fundamentals.json /
    news.json / social.json / manifest.json

设计目标:
- 每个 subagent 只读自己那一个切片文件，避免并行时重复载入完整 bundle。
- 切片刻意裁掉 series 全量、新闻正文、多余财务期数，控制单角色上下文。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0"
DEPTH_ROLES = {
    "quick": ["market", "fundamentals", "news"],
    "standard": ["index", "sector", "market", "fundamentals", "news"],
    "deep": ["index", "sector", "market", "fundamentals", "news", "social"],
    "full": ["index", "sector", "market", "fundamentals", "news", "social"],
}
# trend 模式（股价走势分析）：只用 4 个切片；quick 进一步裁到 market/news。
MODES = ("research", "trend")
MODE_DEPTH_ROLES = {
    "research": DEPTH_ROLES,
    "trend": {
        "quick": ["market", "news"],
        "standard": ["index", "market", "news", "fundamentals"],
        "deep": ["index", "market", "news", "fundamentals"],
        "full": ["index", "market", "news", "fundamentals"],
    },
}

MAX_QUOTES = 20
MAX_NEWS = 20
MAX_SUMMARY_CHARS = 200
MAX_FIN_PERIODS = 8
MAX_INDEXES = 6


def _json_safe(value: Any) -> Any:
    """递归把 NaN/inf 转成 None，并统一日期等不可序列化对象为字符串。"""
    import math

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _truncate(text: Any, limit: int) -> Any:
    if not isinstance(text, str):
        return text
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _tail(rows: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    return rows[-n:] if rows else []


def _quote_slim(rows: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    keys = ("date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover")
    return [{k: row.get(k) for k in keys} for row in _tail(rows, n)]


def _news_slim(news: List[Dict[str, Any]], with_summary: bool) -> List[Dict[str, Any]]:
    out = []
    for item in news[:MAX_NEWS]:
        row = {
            "date": item.get("date"),
            "title": item.get("title"),
            "source": item.get("source"),
        }
        if with_summary:
            row["summary"] = _truncate(item.get("summary"), MAX_SUMMARY_CHARS)
            row["url"] = item.get("url")
        out.append(row)
    return out


def build_slices(bundle: Dict[str, Any], depth: str,
                 mode: str = "research") -> Dict[str, Dict[str, Any]]:
    if mode not in MODES:
        raise ValueError("mode 非法: %r" % mode)
    roles = MODE_DEPTH_ROLES[mode][depth]
    meta = bundle.get("meta", {})
    basic = bundle.get("basic", {})
    quotes = (bundle.get("quotes") or {}).get("rows") or []
    technical = bundle.get("technical", {})
    market = bundle.get("market", {})
    indexes = (market.get("indexes") or [])[:MAX_INDEXES]
    constraints = bundle.get("constraints", {})

    common = {
        "schema_version": SCHEMA_VERSION,
        "meta": meta,
        "slice_note": "本文件是角色专用切片；null 表示数据缺失，不得补零或臆测。",
    }

    slices: Dict[str, Dict[str, Any]] = {}

    if "index" in roles:
        slices["index"] = dict(common, purpose="大盘与指数环境", market={
            "indexes": indexes,
            "up_count": market.get("up_count"),
            "down_count": market.get("down_count"),
            "northbound_net": market.get("northbound_net"),
            "margin_balance": market.get("margin_balance"),
        }, quotes_recent=_quote_slim(quotes, MAX_QUOTES))

    if "sector" in roles:
        slices["sector"] = dict(common, purpose="行业与板块", basic={"industry": basic.get("industry")},
                                sector=bundle.get("sector", {}), market={
                                    "indexes": indexes,
                                    "up_count": market.get("up_count"),
                                    "down_count": market.get("down_count"),
                                })

    if "market" in roles:
        slices["market"] = dict(common, purpose="个股量价与技术面",
                                quotes_recent=_quote_slim(quotes, MAX_QUOTES),
                                technical={
                                    "last": technical.get("last"),
                                    "signals": technical.get("signals"),
                                },
                                constraints=constraints, market={
                                    "up_count": market.get("up_count"),
                                    "down_count": market.get("down_count"),
                                    "northbound_net": market.get("northbound_net"),
                                })

    if "fundamentals" in roles:
        fundamentals = bundle.get("fundamentals", {})
        financials = bundle.get("financials", {})
        slices["fundamentals"] = dict(
            common,
            purpose="基本面与估值",
            basic=basic,
            valuation=bundle.get("valuation", {}),
            fundamentals={"periods": (fundamentals.get("periods") or [])[:MAX_FIN_PERIODS]},
            financials={k: (v or [])[:MAX_FIN_PERIODS] for k, v in financials.items()},
        )

    if "news" in roles:
        slices["news"] = dict(common, purpose="新闻与公告",
                              news=_news_slim(bundle.get("news") or [], with_summary=True),
                              constraints=constraints)

    if "social" in roles:
        slices["social"] = dict(common, purpose="舆情与情绪",
                                news_titles=_news_slim(bundle.get("news") or [], with_summary=False),
                                market={
                                    "up_count": market.get("up_count"),
                                    "down_count": market.get("down_count"),
                                    "northbound_net": market.get("northbound_net"),
                                },
                                quotes_recent=_quote_slim(quotes, 10))

    return slices


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="把 bundle.tech.json 切成角色专用切片")
    parser.add_argument("--input", required=True, help="bundle.tech.json 路径")
    parser.add_argument("--out-dir", required=True, help="切片输出目录")
    parser.add_argument("--depth", default="standard",
                        choices=sorted(DEPTH_ROLES.keys()), help="分析深度")
    parser.add_argument("--mode", default="research", choices=list(MODES),
                        help="研究模式：research=默认研究流水线，trend=股价走势分析")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.input):
        print("ERROR: input not found: %s" % args.input, file=sys.stderr)
        return 3
    try:
        with open(args.input, "r", encoding="utf-8") as fh:
            bundle = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print("ERROR: invalid JSON: %s" % exc, file=sys.stderr)
        return 3

    if not isinstance(bundle, dict):
        print("ERROR: bundle must be a JSON object", file=sys.stderr)
        return 3
    rows = (bundle.get("quotes") or {}).get("rows") or []
    if len(rows) < 30:
        print("ERROR: quotes.rows < 30, run fetch_data.py with a longer window", file=sys.stderr)
        return 3

    os.makedirs(args.out_dir, exist_ok=True)
    slices = build_slices(bundle, args.depth, args.mode)
    manifest: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "symbol": (bundle.get("meta") or {}).get("symbol"),
        "as_of": (bundle.get("meta") or {}).get("as_of"),
        "mode": args.mode,
        "depth": args.depth,
        "enabled_roles": sorted(slices.keys()),
        "slices": {},
    }

    for role, payload in slices.items():
        path = os.path.join(args.out_dir, "%s.json" % role)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_json_safe(payload), fh, ensure_ascii=False, indent=2, allow_nan=False)
        manifest["slices"][role] = {"path": path, "bytes": os.path.getsize(path)}
        print("WROTE %s" % path)

    manifest_path = os.path.join(args.out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(_json_safe(manifest), fh, ensure_ascii=False, indent=2, allow_nan=False)
    print("WROTE %s" % manifest_path)
    missing = sorted(set(MODE_DEPTH_ROLES[args.mode]["full"]) - set(slices.keys()))
    if missing:
        print("NOTE: mode=%s depth=%s, disabled roles: %s"
              % (args.mode, args.depth, ",".join(missing)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
