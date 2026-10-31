#!/usr/bin/env python3
"""安装后自检：依赖、离线全链路、网络可达性。

用法:
    python scripts/selftest.py [--network]

退出码 0 = 全部通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def _run(label, cmd, expect=(0,)):
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    ok = proc.returncode in expect
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        tail = (proc.stdout or "")[-400:] + (proc.stderr or "")[-600:]
        print("     " + tail.replace("\n", "\n     "))
    return ok, proc


def _check_imports():
    ok = True
    for mod in ("numpy", "pandas"):
        try:
            __import__(mod)
            print("PASS import " + mod)
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("FAIL import %s: %s" % (mod, exc))
    try:
        import akshare  # noqa: F401
        print("PASS import akshare")
    except Exception as exc:  # noqa: BLE001
        print("WARN import akshare failed (offline demo still works): %s" % exc)
    return ok


def _check_python():
    ok = sys.version_info >= (3, 9)
    print(("PASS " if ok else "FAIL ") + "python>=3.9 (%s)" % sys.version.split()[0])
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--network", action="store_true", help="额外检查行情接口连通性")
    args = parser.parse_args(argv)

    results = [_check_python(), _check_imports()]
    tmp = tempfile.mkdtemp(prefix="ashare-selftest-")
    try:
        bundle = os.path.join(tmp, "bundle.json")
        tech = os.path.join(tmp, "bundle.tech.json")
        slices = os.path.join(tmp, "slices")
        final = os.path.join(tmp, "final.json")
        report = os.path.join(tmp, "report.md")

        ok, _ = _run("fetch_data --offline", [
            PY, "scripts/fetch_data.py", "--symbol", "600519",
            "--offline", "fixtures/sample_bundle.json", "--out", bundle,
        ], expect=(0, 2))
        results.append(ok)

        if ok:
            ok, _ = _run("indicators", [PY, "scripts/indicators.py", "--input", bundle, "--out", tech])
            results.append(ok)

        if os.path.isfile(tech):
            ok, _ = _run("slice_bundle", [
                PY, "scripts/slice_bundle.py", "--input", tech,
                "--out-dir", slices, "--depth", "standard",
            ])
            results.append(ok)

            try:
                with open(tech, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                last = data.get("technical", {}).get("last", {})
                ok = bool(last) and last.get("close") is not None
                print(("PASS " if ok else "FAIL ") + "technical.last.close present")
                results.append(ok)
                ok = len(data.get("quotes", {}).get("rows", [])) >= 30
                print(("PASS " if ok else "FAIL ") + "quotes.rows >= 30")
                results.append(ok)
            except Exception as exc:  # noqa: BLE001
                print("FAIL parse technical: %s" % exc)
                results.append(False)

        minimal = {
            "schema_version": "1.0",
            "symbol": "600519",
            "name": "贵州茅台",
            "as_of": "2026-10-31",
            "depth": "standard",
            "analyst_reports": {k: "自检占位。" for k in
                                ("index", "sector", "market", "fundamentals", "news", "social")},
            "debate": {"rounds": 1, "bull": "占位", "bear": "占位", "history": []},
            "investment_plan": {"action": "审慎", "confidence": 0.5,
                                "points": ["占位"], "reasoning": "占位"},
            "risk": {"risk_level": "中", "risk_score": 0.5, "key_risks": ["占位"],
                     "risk_control": "占位",
                     "opinions": {"aggressive": "占位", "conservative": "占位", "neutral": "占位"}},
            "final": {"action": "审慎", "confidence": 0.5,
                      "price_analysis_range": {"low": None, "high": None},
                      "risk_reference_price": None, "risk_exposure_ratio": "中",
                      "reasoning": "占位", "summary": "自检占位",
                      "disclaimer": "本报告仅供研究参考，不构成投资建议。"},
            "data_quality": {"completeness": 1.0, "warnings": []},
        }
        with open(final, "w", encoding="utf-8") as fh:
            json.dump(minimal, fh, ensure_ascii=False)

        ok, _ = _run("render", [PY, "scripts/render.py", "--input", final, "--out", report])
        results.append(ok)
        if ok and os.path.isfile(report):
            text = open(report, encoding="utf-8").read()
            needed = ["结论摘要", "六位分析师观点", "多空辩论", "风险辩论",
                      "最终研究结论", "数据质量与限制", "免责声明"]
            missing = [s for s in needed if s not in text]
            ok = not missing
            print(("PASS " if ok else "FAIL ") + "report sections" + ("" if ok else ": missing %s" % missing))
            results.append(ok)

        if args.network:
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://qt.gtimg.cn/q=sh600519",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read(64)
                ok = resp.status == 200 and body
            except Exception as exc:  # noqa: BLE001
                ok = False
                print("     network error: %s" % exc)
            print(("PASS " if ok else "FAIL ") + "network: qt.gtimg.cn")
            results.append(ok)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed, total = sum(1 for r in results if r), len(results)
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
