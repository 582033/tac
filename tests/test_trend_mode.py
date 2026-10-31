#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trend 模式（股价走势分析）测试。

同时支持两种运行方式::

    pytest tests/test_trend_mode.py
    python tests/test_trend_mode.py

全部离线：assemble / render 用合成报告，slice_bundle 用 fixtures/sample_bundle.json
（该 fixture 自带 technical 段，无需先跑 indicators）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import assemble  # noqa: E402
import render  # noqa: E402
import slice_bundle  # noqa: E402

from jsonschema import Draft7Validator  # noqa: E402

FIXTURE_BUNDLE = os.path.join(ROOT, "fixtures", "sample_bundle.json")
SCHEMA_PATH = os.path.join(ROOT, "references", "output-schema.json")

TREND_MANAGER_JSON = {
    "direction": "上行",
    "confidence": 0.65,
    "horizon": "1-3 个月",
    "key_levels": {
        "support": [{"price": 1302.0, "type": "强支撑"}],
        "resistance": [{"price": 1400.0, "type": "阻力"}],
    },
    "scenarios": [
        {"name": "基准", "condition": "量能维持且大盘不走弱",
         "range": {"low": 1302.0, "high": 1400.0}},
        {"name": "乐观", "condition": "放量突破 1400 阻力",
         "range": {"low": 1400.0, "high": 1480.0}},
    ],
    "reasoning": "均线多头排列（technical.last.ma5>ma20），近 20 日跑赢上证指数，结构偏上行。",
}

BUNDLE_TECH = {
    "schema_version": "1.0",
    "meta": {
        "symbol": "600519",
        "name": "贵州茅台",
        "as_of": "2026-10-31",
        "warnings": [],
    },
}


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def make_trend_run_dir(tmp: str, manager_json=None, omit=()):
    """构造最小 trend 报告集：7 份 md + bundle.tech.json，返回 run_dir。"""
    run_dir = os.path.join(tmp, "trend_run")
    reports = os.path.join(run_dir, "reports")
    contents = {
        "trend_structure": "【事实】收盘 1352 元，ma5>ma20>ma60（technical.last）。\n"
                           "【推断】趋势标签上行，支撑区 1300-1310，阻力区 1395-1405。\n",
        "trend_cycle": "【事实】上证指数近 20 日 +2.1%，个股近 20 日累计 +5.4%"
                       "（quotes_recent.pct_chg 求和），差值 +3.3pct。\n【推断】强于大盘。\n",
        "trend_catalyst": "【事实】news[0] 提及提价公告（2026-10-20）。\n"
                          "【推断】利多，中期时间窗。\n",
        "bull_trend": "趋势结构报告称“支撑区 1300-1310”，量能配合（volume_state=expanding），"
                      "方向判断为上行，突破确认条件是放量站稳 1400 上方。\n",
        "bear_trend": "多方引用的量能扩张依赖单周数据，若 volume_ratio 回落到 1 以下，"
                      "上行结构失效；阻力区 1395-1405 两次未破。\n",
        "trend_manager": "综合裁决：\n\n```json\n%s\n```\n"
                         % json.dumps(manager_json or TREND_MANAGER_JSON,
                                      ensure_ascii=False),
        "conservative_trend": "失效条件：\n- 跌破支撑区 1300-1310 则上行判断失效\n"
                              "- 量比持续低于 0.8（technical.last.volume_ratio）\n"
                              "- 提价落地不及 news[0] 预期\n证伪位：1295-1305 区间下沿。\n",
    }
    for role, text in contents.items():
        if role in omit:
            continue
        _write(os.path.join(reports, "%s.md" % role), text)
    _write(os.path.join(run_dir, "bundle.tech.json"),
           json.dumps(BUNDLE_TECH, ensure_ascii=False))
    return run_dir


def make_research_run_dir(tmp: str):
    """构造最小 research 报告集（depth=standard），用于回归对照。"""
    run_dir = os.path.join(tmp, "research_run")
    reports = os.path.join(run_dir, "reports")
    for role in ("index", "sector", "market", "fundamentals", "news"):
        _write(os.path.join(reports, "%s.md" % role),
               "【事实】%s 引用字段。\n【推断】结论。\n" % role)
    _write(os.path.join(reports, "bull.md"), "乐观论证，引用 market 报告“均线多头排列”。\n")
    _write(os.path.join(reports, "bear.md"), "审慎论证，引用 bull 原话“均线多头排列”。\n")
    _write(os.path.join(reports, "research_manager.md"),
           "裁决：\n\n```json\n%s\n```\n" % json.dumps({
               "action": "审慎", "confidence": 0.55,
               "points": ["均线多头排列（market.ma20）", "估值偏高等同业对照（fundamentals）"],
               "reasoning": "先共识后分歧，证据相当偏审慎。",
           }, ensure_ascii=False))
    _write(os.path.join(reports, "risk_manager.md"),
           "风险：\n\n```json\n%s\n```\n" % json.dumps({
               "risk_level": "中", "risk_score": 0.5,
               "key_risks": ["跌破 ma20 支撑"],
               "risk_control": "事前约束与事后复核说明。",
               "opinions": {"aggressive": "上修触发：放量突破。",
                            "conservative": "下修触发：量能萎缩。",
                            "neutral": "基准情景：区间震荡。"},
           }, ensure_ascii=False))
    _write(os.path.join(reports, "trader.md"),
           "简报：\n\n```json\n%s\n```\n" % json.dumps({
               "summary": "一句话结论：倾向审慎，最大限制是数据窗口短。",
               "reasoning": "研究经理倾向审慎，风险中性。",
               "price_analysis_range": {"low": 1300.0, "high": 1450.0},
           }, ensure_ascii=False))
    _write(os.path.join(run_dir, "bundle.tech.json"),
           json.dumps(BUNDLE_TECH, ensure_ascii=False))
    return run_dir


def load_schema():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
        return Draft7Validator(json.load(handle))


def schema_errors(document):
    return ["%s: %s" % ("/".join(map(str, err.absolute_path)) or "<root>", err.message)
            for err in load_schema().iter_errors(document)]


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

def test_assemble_trend_happy_path():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = make_trend_run_dir(tmp)
        final_path = os.path.join(tmp, "final_trend.json")
        code = assemble.main(["--run-dir", run_dir, "--depth", "standard",
                              "--mode", "trend", "--out", final_path])
        assert code == 0, "assemble --mode trend 应成功，退出码 %r" % code
        with open(final_path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)

        # direction 上行 -> 乐观
        assert doc["investment_plan"]["action"] == "乐观"
        assert abs(doc["investment_plan"]["confidence"] - 0.65) < 1e-9
        assert doc["investment_plan"]["points"] == [
            "基准：量能维持且大盘不走弱", "乐观：放量突破 1400 阻力"]
        # 顶层 trend 对象原样存在
        trend = doc["trend"]
        assert trend["direction"] == "上行"
        assert trend["key_levels"]["support"][0]["price"] == 1302.0
        assert len(trend["scenarios"]) == 2
        # 辩论与槽位映射
        assert "支撑区 1300-1310" in doc["debate"]["bull"]
        assert "两次未破" in doc["debate"]["bear"]
        assert doc["analyst_reports"]["market"].startswith("【事实】收盘 1352 元")
        assert doc["analyst_reports"]["index"].startswith("【事实】上证指数")
        assert doc["analyst_reports"]["news"].startswith("【事实】news[0]")
        assert doc["analyst_reports"]["sector"] == "数据缺失（trend 模式未启用）"
        assert doc["analyst_reports"]["fundamentals"] == "数据缺失（trend 模式未启用）"
        assert doc["analyst_reports"]["social"] == "数据缺失（trend 模式未启用）"
        # risk 由 conservative_trend 合成
        assert doc["risk"]["risk_level"] in ("中", "高")
        assert abs(doc["risk"]["risk_score"] - 0.5) < 1e-9
        assert doc["risk"]["key_risks"], "key_risks 不应为空"
        for key in ("aggressive", "conservative", "neutral"):
            assert "conservative_trend" in doc["risk"]["opinions"][key]
        assert "conservative_trend" in doc["risk"]["risk_control"]
        # final 摘要机械拼接
        assert doc["final"]["action"] == "乐观"
        assert doc["final"]["price_analysis_range"] == {"low": 1302.0, "high": 1400.0}
        assert doc["final"]["summary"].startswith("走势方向：上行")
        # schema 校验 0 错误
        errors = schema_errors(doc)
        assert not errors, "schema 错误: %s" % errors


def test_assemble_trend_invalid_direction():
    with tempfile.TemporaryDirectory() as tmp:
        bad = dict(TREND_MANAGER_JSON, direction="上涨")
        run_dir = make_trend_run_dir(tmp, manager_json=bad)
        code = assemble.main(["--run-dir", run_dir, "--mode", "trend",
                              "--out", os.path.join(tmp, "f.json")])
        assert code == 3, "direction 非法应退出 3，实际 %r" % code


def test_assemble_trend_missing_report():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = make_trend_run_dir(tmp, omit=("trend_cycle",))
        code = assemble.main(["--run-dir", run_dir, "--mode", "trend",
                              "--out", os.path.join(tmp, "f.json")])
        assert code == 3, "缺 trend_cycle.md 应退出 3，实际 %r" % code


def test_render_trend_section():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = make_trend_run_dir(tmp)
        final_path = os.path.join(run_dir, "final.json")
        assert assemble.main(["--run-dir", run_dir, "--depth", "standard",
                              "--mode", "trend", "--out", final_path]) == 0
        report_path = os.path.join(run_dir, "report.md")
        assert render.main(["--input", final_path, "--out", report_path]) == 0
        with open(report_path, "r", encoding="utf-8") as handle:
            text = handle.read()

        assert "## 走势分析" in text
        assert "以上为走势研究区间，不构成买卖依据。" in text
        assert "| 类型 | 价位 |" in text
        assert "1,302.00 元" in text and "强支撑" in text
        assert "| 名称 | 条件 | 区间 |" in text
        assert "基准" in text and "1,400.00 元" in text
        # 章节位置：多空辩论之后、研究经理结论之前
        assert (text.find("## 多空辩论") < text.find("## 走势分析")
                < text.find("## 研究经理结论"))
        # direction 不得渲染成买卖建议
        for word in ("买入", "卖出", "加仓", "减仓", "建仓"):
            assert word not in text, "报告中不得出现 %r" % word


def test_render_trend_degrades_gracefully():
    doc = {
        "schema_version": "1.0", "symbol": "600519", "name": "贵州茅台",
        "as_of": "2026-10-31", "depth": "standard",
        "analyst_reports": {k: "占位。" for k in
                            ("index", "sector", "market", "fundamentals", "news", "social")},
        "debate": {"rounds": 1, "bull": "占位", "bear": "占位", "history": []},
        "investment_plan": {"action": "中性", "confidence": 0.5,
                            "points": ["占位"], "reasoning": "占位"},
        "risk": {"risk_level": "中", "risk_score": 0.5, "key_risks": ["占位"],
                 "risk_control": "占位",
                 "opinions": {"aggressive": "占位", "conservative": "占位",
                              "neutral": "占位"}},
        "final": {"action": "中性", "confidence": 0.5,
                  "price_analysis_range": {"low": None, "high": None},
                  "risk_reference_price": None, "risk_exposure_ratio": "中",
                  "reasoning": "占位", "summary": "占位",
                  "disclaimer": "本报告仅供研究参考，不构成投资建议。"},
        "data_quality": {"completeness": 1.0, "warnings": []},
        "trend": {"direction": "震荡"},  # 其余字段全部缺失
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "final.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False)
        out = os.path.join(tmp, "report.md")
        assert render.main(["--input", path, "--out", out]) == 0
        with open(out, "r", encoding="utf-8") as handle:
            text = handle.read()
        assert "## 走势分析" in text
        assert "- **走势方向**：震荡" in text
        assert text.count("数据缺失") >= 3  # 时间窗/关键位/情景/reasoning 降级
        assert "以上为走势研究区间，不构成买卖依据。" in text


def test_research_mode_regression():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = make_research_run_dir(tmp)
        default_out = os.path.join(tmp, "default.json")
        explicit_out = os.path.join(tmp, "explicit.json")
        assert assemble.main(["--run-dir", run_dir, "--depth", "standard",
                              "--out", default_out]) == 0
        assert assemble.main(["--run-dir", run_dir, "--depth", "standard",
                              "--mode", "research", "--out", explicit_out]) == 0
        with open(default_out, "r", encoding="utf-8") as handle:
            default = handle.read()
        with open(explicit_out, "r", encoding="utf-8") as handle:
            explicit = handle.read()
        assert default == explicit, "--mode research 与缺省行为必须逐字节一致"

        doc = json.loads(default)
        assert "trend" not in doc
        assert doc["investment_plan"]["action"] == "审慎"
        assert doc["final"]["price_analysis_range"] == {"low": 1300.0, "high": 1450.0}
        assert doc["analyst_reports"]["social"] == "数据缺失（depth=standard 未启用 social）"
        errors = schema_errors(doc)
        assert not errors, "schema 错误: %s" % errors


def test_slice_bundle_trend_roles():
    assert os.path.isfile(FIXTURE_BUNDLE), "缺少 fixtures/sample_bundle.json"
    with tempfile.TemporaryDirectory() as tmp:
        def run(name, extra):
            out_dir = os.path.join(tmp, name)
            code = slice_bundle.main(["--input", FIXTURE_BUNDLE,
                                      "--out-dir", out_dir] + extra)
            assert code == 0, "slice_bundle %s 失败: %r" % (name, code)
            with open(os.path.join(out_dir, "manifest.json"), "r",
                      encoding="utf-8") as handle:
                return json.load(handle)

        manifest = run("trend_standard", ["--depth", "standard", "--mode", "trend"])
        assert manifest["mode"] == "trend"
        assert manifest["enabled_roles"] == ["fundamentals", "index", "market", "news"]

        manifest = run("trend_quick", ["--depth", "quick", "--mode", "trend"])
        assert manifest["enabled_roles"] == ["market", "news"]

        # research 缺省行为回归：standard 启用 5 角色
        manifest = run("research_default", ["--depth", "standard"])
        assert manifest["mode"] == "research"
        assert manifest["enabled_roles"] == [
            "fundamentals", "index", "market", "news", "sector"]

        # research 全量对照：full 启用 6 角色
        manifest = run("research_full", ["--depth", "full", "--mode", "research"])
        assert manifest["enabled_roles"] == [
            "fundamentals", "index", "market", "news", "sector", "social"]


# ---------------------------------------------------------------------------
# 直接运行入口（无 pytest 时）
# ---------------------------------------------------------------------------

def _run_all():
    tests = sorted(
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    )
    failed = []
    for name, func in tests:
        try:
            func()
        except Exception:
            failed.append(name)
            print("FAIL %s" % name)
            traceback.print_exc()
        else:
            print("PASS %s" % name)

    print("\n%d passed, %d failed" % (len(tests) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
