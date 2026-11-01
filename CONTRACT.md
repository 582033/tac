# 内部接口契约 v1.0（所有模块必须遵守）

本项目是一个**可移植的 A 股研究 Skill**，不依赖 MongoDB / Redis / 原项目 core 模块。
运行时只依赖 Python 3.9+、pandas、numpy、akshare。

## 1. 目录与文件归属

```
scripts/fetch_data.py     # 取数 + 归一化 -> bundle.json
scripts/indicators.py     # 读 bundle.json，追加 technical 段 -> bundle.tech.json
scripts/render.py         # 读 final JSON -> HTML 报告（--format md 可选 Markdown）
scripts/selftest.py       # 离线自检
references/roles.md       # 角色提示词
references/output-schema.json
references/a-share-quirks.md
fixtures/sample_bundle.json
tests/test_fetch_data.py
tests/test_indicators.py
SKILL.md
README.md
requirements.txt
```

## 2. 数据包 schema（bundle.json）

所有数值允许为 `null`；缺数据必须写进 `meta.warnings`，不得静默填 0。

```json
{
  "schema_version": "1.0",
  "meta": {
    "symbol": "600519",
    "name": "贵州茅台",
    "exchange": "SH",
    "as_of": "2026-10-31",
    "source": "akshare",
    "fetched_at": "2026-10-31T12:00:00+08:00",
    "currency": "CNY",
    "warnings": []
  },
  "basic": {
    "industry": "白酒",
    "list_date": "2001-08-27",
    "is_st": false,
    "market_cap": 0.0,
    "float_market_cap": 0.0,
    "total_shares": 0.0,
    "float_shares": 0.0
  },
  "quotes": {
    "adjust": "qfq",
    "period": "daily",
    "rows": [
      {"date": "2026-10-30", "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0,
       "volume": 0.0, "amount": 0.0, "pct_chg": 0.0, "turnover": 0.0}
    ]
  },
  "valuation": {
    "pe_ttm": null, "pb": null, "ps_ttm": null,
    "dividend_yield": null, "total_mv": null, "circ_mv": null
  },
  "fundamentals": {
    "periods": [
      {"period": "2026-06-30", "report_type": "quarter",
       "roe": null, "gross_margin": null, "net_margin": null, "debt_ratio": null,
       "revenue": null, "net_profit": null, "ocf": null}
    ]
  },
  "financials": {
    "income":   [{"period": "2026-06-30", "revenue": null, "net_profit": null, "operating_profit": null}],
    "balance":  [{"period": "2026-06-30", "total_assets": null, "total_liabilities": null, "equity": null}],
    "cashflow": [{"period": "2026-06-30", "ocf": null, "icf": null, "fcf": null}]
  },
  "news": [
    {"date": "2026-10-30", "title": "", "summary": "", "source": "", "url": ""}
  ],
  "sector": {
    "name": null, "index_pct_chg": null,
    "rank": null, "rank_total": null, "main_net_inflow": null
  },
  "market": {
    "indexes": [{"name": "上证指数", "close": null, "pct_chg": null}],
    "up_count": null, "down_count": null,
    "northbound_net": null, "margin_balance": null
  },
  "constraints": {
    "suspended": false,
    "board": "main",
    "price_limit_pct": 0.1,
    "limit_up_price": null,
    "limit_down_price": null,
    "last_trade_date": null
  },
  "technical": {
    "as_of": null,
    "last": {
      "close": null,
      "ma5": null, "ma10": null, "ma20": null, "ma60": null,
      "ema12": null, "ema26": null,
      "rsi14": null,
      "macd": null, "macd_signal": null, "macd_hist": null,
      "boll_upper": null, "boll_mid": null, "boll_lower": null,
      "atr14": null, "kdj_k": null, "kdj_d": null, "kdj_j": null,
      "volume_ratio": null
    },
    "series": {
      "dates": [], "close": [], "ma20": [], "rsi14": [], "macd_hist": []
    },
    "signals": {
      "trend": "up|down|sideways|unknown",
      "ma_alignment": "bullish|bearish|mixed|unknown",
      "rsi_state": "overbought|oversold|neutral|unknown",
      "macd_state": "bullish|bearish|neutral|unknown",
      "volume_state": "expanding|shrinking|normal|unknown"
    }
  }
}
```

`technical.series` 只保留最近 60 个交易日，用于报告里画简表，避免上下文膨胀。

## 3. 命令行契约

### fetch_data.py
```
python scripts/fetch_data.py --symbol 600519 [--as-of YYYY-MM-DD] [--days 400]
       [--start YYYY-MM-DD] [--out PATH] [--source akshare|tushare]
       [--offline FIXTURE] [--strict]
```
- 默认 `--out data/{symbol}/{as_of}/bundle.json`
- `--offline` 时直接读取 fixture 并补齐 meta，**不访问网络**（用于测试/演示）
- 退出码：0 完整；2 部分成功（有 warnings）；3 致命错误（无法得到行情）
- 必须打印一行 `WROTE <path>` 便于脚本解析
- 必须显式把失败模块写入 `meta.warnings`，例如 `"finance": "akshare 接口超时"`

### indicators.py
```
python scripts/indicators.py --input bundle.json [--out PATH] [--window 400]
```
- 默认输出 `{input 去后缀}.tech.json`
- 必须校验 `quotes.rows` 数量 ≥ 30，不足则退出码 3
- 纯计算，不访问网络

### render.py
```
python scripts/render.py --input final.json [--format html|md] [--out report.html]
```
- 输入是第 4 节定义的 final JSON
- 默认输出自包含 HTML 网页（`--format md` 可选 Markdown），固定章节：结论摘要 / 六位分析师观点 / 多空辩论 / 研究经理结论 / 风险辩论 / 最终研究结论 / 数据质量与限制 / 免责声明
- render.py 本身不打开浏览器、不访问网络；是否自动打开报告由宿主流程决定（SKILL.md Phase 4 第 6 步）

## 4. 最终分析结果 schema（final.json）

由宿主 AI 按 SKILL.md 流程生成，render.py 消费。

```json
{
  "schema_version": "1.0",
  "symbol": "600519",
  "name": "贵州茅台",
  "as_of": "2026-10-31",
  "depth": "quick|standard|deep|full",
  "analyst_reports": {
    "index": "markdown", "sector": "markdown", "market": "markdown",
    "fundamentals": "markdown", "news": "markdown", "social": "markdown"
  },
  "debate": {
    "rounds": 1,
    "bull": "markdown", "bear": "markdown",
    "history": [{"role": "bull", "text": "..."}]
  },
  "investment_plan": {
    "action": "乐观|审慎|中性",
    "confidence": 0.0,
    "points": ["..."],
    "reasoning": "..."
  },
  "risk": {
    "risk_level": "低|中|高",
    "risk_score": 0.0,
    "key_risks": ["..."],
    "risk_control": "...",
    "opinions": {"aggressive": "...", "conservative": "...", "neutral": "..."}
  },
  "final": {
    "action": "乐观|审慎|中性",
    "confidence": 0.0,
    "price_analysis_range": {"low": null, "high": null},
    "risk_reference_price": null,
    "risk_exposure_ratio": "低|中|高",
    "reasoning": "...",
    "summary": "...",
    "disclaimer": "本报告仅供研究参考，不构成投资建议。"
  },
  "data_quality": {"completeness": 0.0, "warnings": []}
}
```

约束：
- `action` 只能是三值，禁止"买入/卖出/加仓/减仓"。
- `confidence`、`risk_score` ∈ [0,1]。
- 缺失字段用 null，不要编造数字。
- 所有价格结论必须标注"区间"而非单点目标价。

## 5. 角色与轮次

角色 ID 固定为：`index / sector / market / fundamentals / news / social / bull / bear / research_manager / aggressive / conservative / neutral / risk_manager / trader`。

深度映射：

| depth | 投资辩论轮 | 风险辩论轮 | 说明 |
|---|---|---|---|
| quick | 1 | 1 | 只做核心三步 |
| standard | 1 | 2 | 默认 |
| deep | 2 | 2 | bull/bear 各发言 2 次 |
| full | 3 | 3 | 完整 |

## 6. 编码约束

- Python 3.9 兼容：不要用 `X | Y` 类型语法（用 `Optional`/`Union`），不要用 `match`。
- 所有模块 `from __future__ import annotations`。
- 不 import 原项目 `core.*` / `app.*` / `tradingagents.*`。
- 不使用 Mongo/Redis/FastAPI/LangChain。
- 网络请求必须带超时与重试；单模块失败不得中断整体取数。
- 中文数值单位：元、亿元；百分比用小数（0.12 = 12%）并在展示层转换。

## 7. trend 模式（股价走势分析，v1.1 追加）

`--mode trend` 启用走势研究流水线；`--mode` 缺省为 `research`，research 模式的行为与本契约第 1-6 节完全一致（回归不变）。

### 7.1 角色清单（7 个，提示词见 references/roles.md 追加段）

| 角色 | 职责 | 输入切片 |
|---|---|---|
| trend_structure | 走势结构：趋势标签、支撑/阻力区间、波动与量能 | market（quotes_recent + technical.last/signals） |
| trend_cycle | 周期与相对强弱：近 20 日累计涨跌幅差、大盘约束 | index（market.indexes + quotes_recent） |
| trend_catalyst | 催化与事件：方向、时间窗（短期/中期） | news |
| bull_trend / bear_trend | 围绕"方向判断与关键位"辩论，各 300-500 字 | 三份走势报告 + 对方发言 |
| trend_manager | 裁决并输出 trend JSON | 三份报告 + 多空全文 |
| conservative_trend | 单角色风险：失效条件与证伪位（≤250 字） | trend JSON + 辩论 + meta.warnings |

### 7.2 trend_manager JSON（写在 trend_manager.md 文末 ```json``` 块）

```json
{
  "direction": "上行|震荡|下行",
  "confidence": 0.65,
  "horizon": "1-3 个月",
  "key_levels": {
    "support": [{"price": 1302.0, "type": "强支撑"}],
    "resistance": [{"price": 1400.0, "type": "阻力"}]
  },
  "scenarios": [
    {"name": "基准", "condition": "...", "range": {"low": 1302.0, "high": 1400.0}}
  ],
  "reasoning": "..."
}
```

约束：direction 仅三值；scenarios 2-3 个且 range 必须是区间；所有价位用于刻画区间，禁止当单点目标价。assemble 校验失败退出 3。该对象作为 final.json 顶层可选属性 `trend` 原样放入（schema 见 references/output-schema.json，research 校验不受影响）。

### 7.3 CLI 与文件归属

```
scripts/slice_bundle.py --input bundle.tech.json --out-dir slices \
    [--depth quick|standard|deep|full] [--mode research|trend]
```
- trend 模式 enabled roles = index/market/news/fundamentals；depth 裁剪：quick → market/news，standard/deep/full → 全部 4 个。
- manifest.json 新增 `mode` 字段，并记录 `enabled_roles`。

```
scripts/assemble.py --run-dir RUN_DIR [--depth ...] [--mode research|trend]
```
- trend 必需报告（`RUN_DIR/reports/`）：trend_structure.md、trend_cycle.md、trend_catalyst.md、bull_trend.md、bear_trend.md、trend_manager.md、conservative_trend.md；任一缺失退出 3。
- 仍要求 `RUN_DIR/bundle.tech.json` 存在（读取 meta）。

```
scripts/render.py --input final.json [--format html|md] [--out report.html]
```
- final.json 含非空 `trend` 对象时，在"多空辩论"之后插入"## 走势分析"：走势方向、置信度、时间窗、关键位表（类型/价位）、情景表（名称/条件/区间）、走势推理摘要；direction 仅按 上行/震荡/下行 展示，不得渲染成买卖建议；章节末固定输出"以上为走势研究区间，不构成买卖依据。"；字段缺失按"数据缺失"降级，不中断渲染。

### 7.4 assemble 映射规则（trend 模式，全部机械映射，不生成新判断）

- `analyst_reports`：market←trend_structure、index←trend_cycle、news←trend_catalyst；sector/fundamentals/social 填固定占位"数据缺失（trend 模式未启用）"。
- `debate`：bull/bear ← bull_trend/bear_trend；rounds 沿用第 5 节 depth 映射；history 为两条汇总发言。
- `investment_plan`：action 由 direction 映射（上行→乐观、震荡→中性、下行→审慎）；confidence 沿用；points 取 scenarios 的 name+condition；reasoning 沿用 trend.reasoning。
- `risk`：由 conservative_trend 摘要合成——risk_level 固定"中"（契约下限）、risk_score 固定 0.5、key_risks 取其条目化的失效条件（≤5 条）、risk_control 与 opinions 三键（aggressive/conservative/neutral）均为其摘要并前缀"（来源：conservative_trend）"。
- `final`：action/confidence 同 investment_plan；price_analysis_range 取第一个（基准）情景的 range；risk_reference_price=null；risk_exposure_ratio 同 risk_level；reasoning 沿用 trend.reasoning；summary 由 direction/confidence/horizon/基准情景条件机械拼接；disclaimer 固定。
- 顶层 `trend`：trend_manager JSON 原样放入。
- `data_quality.warnings` 额外记录："mode=trend：研究报告 sector、fundamentals、social 未启用"、"mode=trend：risk 由 conservative_trend 摘要合成（risk_level=中、risk_score=0.5 为契约默认）"。

### 7.5 文件清单（trend 增量）

```
references/roles.md            # 追加 7 个 trend 角色（每个 ≤250 字）
references/output-schema.json  # 顶层新增可选 trend 对象（additionalProperties false）
scripts/slice_bundle.py        # --mode research|trend（默认 research）
scripts/assemble.py            # --mode research|trend（默认 research）
scripts/render.py              # 新增"## 走势分析"章节
tests/test_trend_mode.py       # trend 模式离线测试（可直接 python 运行，兼容 pytest）
```

## 8. 数据质量门禁（机器执行，v1.1.1 追加）

SKILL.md 第 7 节门禁表的前 6 条由 `scripts/assemble.py` 在组装 `final.json` 时自动强制执行，宿主无需也不得手工施加：

- 计算入口：`_compute_gates(bundle)`（只读 bundle，返回 `[{rule, effect}]`）
- 执行入口：`_apply_gates(final_doc, gates)`（在 `_validate_final` 之前调用，research 与 trend 两条路径都会执行）

| rule | 触发条件 | 机器效果 |
|---|---|---|
| `quotes_lt_120` | `quotes.rows` < 120 | confidence 压至 ≤0.65 |
| `quotes_lt_60` | `quotes.rows` < 60 | confidence ≤0.50；action=乐观 时降为中性 |
| `finance_missing` | `fundamentals.periods` 为空或 roe/net_margin/revenue/net_profit 全 null | confidence ≤0.55；乐观降中性 |
| `valuation_missing` | `valuation.pe_ttm` 与 `pb` 均为 null | confidence 再减 0.05 |
| `suspended` | `constraints.suspended == true` | 乐观降中性；`price_analysis_range` 清空为 null；risk_level=低 时抬到中 |
| `is_st` | `basic.is_st == true` | confidence ≤0.60；risk_level=低 时抬到中 |

规则细节：
- 多门禁并存时 confidence 取各上限的最小值；action/risk 的降级只降不升。
- 每条触发都会追加 `门禁触发：<rule>（<effect>）` 到 `data_quality.warnings`，机器降级另记 `门禁降级：…`。
- 触发清单原样写入 `data_quality.gates`（schema 已在 `references/output-schema.json` 增加可选字段）。
- `investment_plan` 与 `final` 的 action/confidence 同步修改，保证两者一致（verifier 校验依赖一致性）。

### 8.1 文件清单（门禁增量）

```
scripts/assemble.py            # _compute_gates / _apply_gates
references/output-schema.json  # data_quality 增加可选 gates 数组
tests/test_gates.py            # 7 项门禁测试（离线，可直接 python 运行）
```
