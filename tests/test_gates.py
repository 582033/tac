#!/usr/bin/env python3
"""数据质量门禁的机器执行测试：120/60 日、财务缺失、估值缺失、停牌、ST。

全部离线：构造合成 bundle.tech.json + 最小 research 报告集，直接调用 assemble。
运行方式：python tests/test_gates.py（也兼容 pytest 收集）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import assemble  # noqa: E402

SCHEMA_PATH = os.path.join(ROOT, "references", "output-schema.json")


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def make_bundle(rows=200, finance=True, valuation=True,
                suspended=False, is_st=False):
    """构造可控的 bundle.tech.json。"""
    quotes = [{"date": "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
               "close": 100.0 + i} for i in range(rows)]
    bundle = {
        "schema_version": "1.0",
        "meta": {"symbol": "600519", "name": "贵州茅台",
                 "as_of": "2026-10-31", "warnings": []},
        "quotes": {"adjust": "qfq", "period": "daily", "rows": quotes},
        "fundamentals": {"periods": ([{"period": "2026-09-30", "report_type": "quarter",
                                       "roe": 0.2, "net_margin": 0.5,
                                       "revenue": 1.0, "net_profit": 0.5}]
                                     if finance else [])},
        "valuation": ({"pe_ttm": 21.0, "pb": 6.0} if valuation
                      else {"pe_ttm": None, "pb": None}),
        "constraints": {"suspended": suspended, "board": "main",
                        "price_limit_pct": 0.1},
        "basic": {"is_st": is_st},
    }
    return bundle


def make_run_dir(tmp: str, name: str, bundle: dict):
    """构造最小 research 报告集（depth=standard），bundle 可控。"""
    run_dir = os.path.join(tmp, name)
    reports = os.path.join(run_dir, "reports")
    for role in ("index", "sector", "market", "fundamentals", "news"):
        _write(os.path.join(reports, "%s.md" % role),
               "【事实】%s 引用字段。\n【推断】结论。\n" % role)
    _write(os.path.join(reports, "bull.md"), "乐观论证，引用 market 报告“均线多头排列”。\n")
    _write(os.path.join(reports, "bear.md"), "审慎论证，引用 bull 原话“均线多头排列”。\n")
    _write(os.path.join(reports, "research_manager.md"),
           "裁决：\n\n```json\n%s\n```\n" % json.dumps({
               "action": "乐观", "confidence": 0.9,
               "points": ["均线多头排列（market.ma20）"],
               "reasoning": "证据充分。",
           }, ensure_ascii=False))
    _write(os.path.join(reports, "risk_manager.md"),
           "风险：\n\n```json\n%s\n```\n" % json.dumps({
               "risk_level": "低", "risk_score": 0.3,
               "key_risks": ["跌破 ma20 支撑"],
               "risk_control": "事前约束与事后复核说明。",
               "opinions": {"aggressive": "上修触发：放量突破。",
                            "conservative": "下修触发：量能萎缩。",
                            "neutral": "基准情景：区间震荡。"},
           }, ensure_ascii=False))
    _write(os.path.join(reports, "trader.md"),
           "简报：\n\n```json\n%s\n```\n" % json.dumps({
               "summary": "一句话结论：倾向乐观。",
               "reasoning": "研究经理倾向乐观。",
               "price_analysis_range": {"low": 1300.0, "high": 1450.0},
               "risk_reference_price": 1300.0,
           }, ensure_ascii=False))
    _write(os.path.join(run_dir, "bundle.tech.json"),
           json.dumps(bundle, ensure_ascii=False))
    return run_dir


def run_assemble(run_dir: str):
    return assemble.main(["--run-dir", run_dir, "--depth", "standard",
                          "--completeness", "0.9"])


def load_final(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "final.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def schema_errors(document) -> int:
    from jsonschema import Draft7Validator
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    return len(list(Draft7Validator(schema).iter_errors(document)))


def test_no_gate_triggered():
    tmp = tempfile.mkdtemp(prefix="gates-")
    try:
        run_dir = make_run_dir(tmp, "ok", make_bundle(rows=200))
        assert run_assemble(run_dir) == 0
        final = load_final(run_dir)
        assert final["final"]["confidence"] == 0.9
        assert final["final"]["action"] == "乐观"
        assert final["data_quality"].get("gates", []) == []
        assert schema_errors(final) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_quotes_lt_120_caps_confidence():
    tmp = tempfile.mkdtemp(prefix="gates-")
    try:
        run_dir = make_run_dir(tmp, "d120", make_bundle(rows=100))
        assert run_assemble(run_dir) == 0
        final = load_final(run_dir)
        assert final["final"]["confidence"] == 0.65
        assert final["investment_plan"]["confidence"] == 0.65
        assert final["final"]["action"] == "乐观"  # 仅压置信度，不降级
        rules = [g["rule"] for g in final["data_quality"]["gates"]]
        assert rules == ["quotes_lt_120"]
        assert schema_errors(final) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_quotes_lt_60_caps_and_downgrades():
    tmp = tempfile.mkdtemp(prefix="gates-")
    try:
        run_dir = make_run_dir(tmp, "d60", make_bundle(rows=50))
        assert run_assemble(run_dir) == 0
        final = load_final(run_dir)
        assert final["final"]["confidence"] == 0.50
        assert final["final"]["action"] == "中性"  # 乐观被门禁降级
        assert final["investment_plan"]["action"] == "中性"
        assert schema_errors(final) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_finance_missing_and_valuation_missing():
    tmp = tempfile.mkdtemp(prefix="gates-")
    try:
        run_dir = make_run_dir(tmp, "nofin",
                               make_bundle(rows=200, finance=False, valuation=False))
        assert run_assemble(run_dir) == 0
        final = load_final(run_dir)
        # finance_missing -> 0.55；valuation_missing -> 0.9-0.05=0.85；取最严 0.55
        assert final["final"]["confidence"] == 0.55
        assert final["final"]["action"] == "中性"
        rules = sorted(g["rule"] for g in final["data_quality"]["gates"])
        assert rules == ["finance_missing", "valuation_missing"]
        assert schema_errors(final) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_suspended_and_st_hard_rules():
    tmp = tempfile.mkdtemp(prefix="gates-")
    try:
        run_dir = make_run_dir(tmp, "susp",
                               make_bundle(rows=200, suspended=True, is_st=True))
        assert run_assemble(run_dir) == 0
        final = load_final(run_dir)
        assert final["final"]["action"] == "中性"  # 乐观被禁止
        # is_st cap 0.60；suspended 要求 risk 至少中、不给价格区间
        assert final["final"]["confidence"] == 0.60
        assert final["risk"]["risk_level"] == "中"
        assert final["final"]["risk_exposure_ratio"] == "中"
        assert final["final"]["price_analysis_range"] == {"low": None, "high": None}
        rules = sorted(g["rule"] for g in final["data_quality"]["gates"])
        assert rules == ["is_st", "suspended"]
        assert schema_errors(final) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gates_apply_to_trend_mode():
    tmp = tempfile.mkdtemp(prefix="gates-")
    try:
        # 复用 trend 测试的报告构造，但 bundle 换成 50 行行情
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        try:
            import test_trend_mode as tt
        finally:
            sys.path.pop(0)
        run_dir = tt.make_trend_run_dir(tmp)
        _write(os.path.join(run_dir, "bundle.tech.json"),
               json.dumps(make_bundle(rows=50), ensure_ascii=False))
        code = assemble.main(["--run-dir", run_dir, "--depth", "standard",
                              "--mode", "trend"])
        assert code == 0
        final = load_final(run_dir)
        assert final["final"]["confidence"] <= 0.50  # 0.65 被 60 日门禁压到 0.50
        assert schema_errors(final) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_regression_real_600519_bundle():
    """真实 600519 数据（261 日、财务与估值齐全）不应触发任何门禁。"""
    real = os.path.join(ROOT, "data", "600519", "2026-10-31", "bundle.tech.json")
    if not os.path.isfile(real):
        return  # 本地运行产物可能不存在（CI），跳过
    with open(real, "r", encoding="utf-8") as fh:
        bundle = json.load(fh)
    assert assemble._compute_gates(bundle) == [], "600519 不应触发门禁"


def _run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS " + name)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL " + name)
            traceback.print_exc()
    print("\n%d passed, %d failed" % (len(tests) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
