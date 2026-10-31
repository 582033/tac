---
name: tac
description: 'Use when the user asks for A-share single-stock research or stock price trend analysis, 个股分析, 走势分析, 股价走势, 趋势分析, 多空辩论, bull/bear debate, 研究结论, trend, or mentions a 6-digit A-share ticker such as 600519, 000001, 300750. 触发场景: A股个股研究、投资价值分析、股价走势与趋势分析、支撑阻力区间、多空辩论、风险评估、研究报告生成. Two modes: research（六位分析师、多空辩论、风险审阅的单股综合研究）and trend（走势结构、周期与相对强弱、催化事件的股价走势分析）. Produces a Chinese report with a final 乐观/审慎/中性 conclusion. Do not use for US/HK stocks, real-time trading, or investment advice.'
license: Apache-2.0
metadata:
  version: "1.1.0"
  language: zh-CN
  runtime: "python>=3.9"
---

# TAC — A 股研究与走势分析 Skill（tac）

本 Skill 让宿主 AI 自己扮演多智能体，对单只 A 股做结构化研究（**research** 模式，默认）或股价走势分析（**trend** 模式）。**不依赖 LangGraph、MongoDB、Redis 或原项目 core/app 代码**。数据由 `scripts/fetch_data.py` 预取，指标由 `scripts/indicators.py` 计算，宿主负责分析师、辩论、裁决与整合，最后由 `scripts/render.py` 渲染中文报告。

> 渐进披露：本文只保留主流程。角色提示词见 `references/roles.md`，A 股特殊规则见 `references/a-share-quirks.md`，输出 schema 见 `references/output-schema.json`。按需读取，不要一次性全部载入上下文。

frontmatter 中的 `license` 与 `metadata` 仅供记录，流程不依赖宿主解析这些非标准字段。

## 0. Skill 根目录与命令约定

- **Skill 根目录** = 包含本 `SKILL.md` 的目录。
- 宿主可能把它放在 `~/.claude/skills/tac/`、项目 `.claude/skills/tac/`，或任意克隆目录。
- 执行任何命令前，先 `cd` 到 Skill 根目录；本文所有脚本命令均使用相对路径 `scripts/...`。
- 定位方法（在 shell 中按顺序尝试）：

```bash
for d in "$PWD" ".claude/skills/tac" "$HOME/.claude/skills/tac"; do
  if [ -f "$d/scripts/fetch_data.py" ]; then cd "$d" && echo "SKILL_ROOT=$PWD" && break; fi
done
```

- 运行目录约定：`data/{symbol}_{as_of}/` 存放本次运行的 `bundle.json`、`bundle.tech.json`、`reports/`、`final.json`、`report.md`。

## 1. 适用范围与边界

- **只做 A 股**：沪深北交易所 6 位代码（600/601/603/605/688/000/001/002/003/300/301/8xx/4xx）。不做美股、港股、基金、期货、期权、外汇。
- **不构成投资建议**：输出是研究参考，不是荐股，也不是代客决策。
- **不做实时交易**：不连接券商、不下单、不做盘中实时盯盘；数据是预取快照，可能延迟。
- **禁止编造**：缺失字段写 `null` 并记入 `meta.warnings` / `data_quality.warnings`；不得补零、不得臆测财务数字。
- **结论措辞**：最终倾向只能是 `乐观 / 审慎 / 中性`，禁止“买入 / 卖出 / 加仓 / 减仓 / 目标价”。

## 2. 调用方式与模式

用户在宿主（如 Claude Code）中输入 `/tac <6 位代码>` 触发本 Skill；自然语言命中 frontmatter description 中的触发词同样触发。

| 用户输入 | 模式 | 说明 |
|---|---|---|
| `/tac 600519` | research | 默认模式，单股综合研究 |
| `/tac 600519 走势` | trend | 输入含“走势 / 趋势 / 股价走势”等关键词即进入 trend |
| `/tac trend 600519` | trend | 显式 `trend` 子命令 |

- **可选参数**（两种模式共用）：`depth`（`quick` / `standard` / `deep` / `full`，默认 `standard`）；`as_of`（默认最近交易日）。例如 `/tac 600519 走势 deep`、`/tac 600519 as_of=2026-10-31`。

两种模式走完全相同的 4 Phase 流程与脚本管线，仅角色集与输出侧重不同：

| | research（默认） | trend |
|---|---|---|
| 目的 | 单股综合研究：基本面、技术面、消息面的多空权衡 | 股价走势分析：方向、结构、关键价位 |
| 分析师（Phase 2） | `index` / `sector` / `market` / `fundamentals` / `news` / `social`（按 depth 启用） | 固定 3 个：`trend_structure`（趋势标签 上行/震荡/下行、支撑阻力区间）、`trend_cycle`（周期与相对强弱）、`trend_catalyst`（催化与事件） |
| 辩论与裁决（Phase 3） | `bull` / `bear` + `research_manager`，输出 `investment_plan` | `bull_trend` / `bear_trend` + `trend_manager`，输出 `{direction, confidence, horizon, key_levels, scenarios, reasoning}` |
| 风险（Phase 4） | `aggressive` / `conservative` / `neutral` 三角 + `risk_manager` | `conservative_trend` 单角色审阅 |
| 切片 | `slices/{index,sector,market,fundamentals,news,social}.json`（按 depth 启用） | 复用 `slices/{index,market,news,fundamentals}.json`（`slice_bundle.py --mode trend`） |
| 输出差异 | `final.json` / `report.md`（research schema） | research schema 基础上增加可选顶层 `trend` 对象；`final.action` 由 `direction` 映射（上行→乐观、震荡→中性、下行→审慎）；`report.md` 增加“走势分析”章节 |
| 适用问题 | “这只股票基本面如何、值不值得关注、风险在哪” | “现在是什么趋势、支撑阻力在哪、可能往哪边走” |

## 3. 前置检查

| 检查项 | 要求 | 不满足时 |
|---|---|---|
| Python | 3.9+ | 停止，提示升级 |
| 依赖 | `akshare>=1.14`、`pandas`、`numpy`、`requests` | `python3 -m pip install -r requirements.txt` |
| 网络 | 可访问 AKShare 数据源 | 用 `--offline fixtures/sample_bundle.json` 演示或停止 |
| 数据目录 | `data/` 可写 | 用 `--out` 指定可写目录 |

一条自检命令（版本 + akshare + 网络）：

```bash
python3 scripts/selftest.py
```

若本包暂未提供 `selftest.py`，用下面的等价命令：

```bash
python3 - <<'PY'
import sys, urllib.request
import akshare as ak
print("python", sys.version.split()[0])
print("akshare", getattr(ak, "__version__", "unknown"))
urllib.request.urlopen("https://www.baidu.com", timeout=5)
print("network ok")
PY
```

## 4. 执行流程

| Phase | 角色 | 主要产出 | 完成判据 |
|---|---|---|---|
| 1 取数 | 脚本 | `bundle.json` / `bundle.tech.json` | 文件存在且 warnings 已登记 |
| 2 分析师 | research 6 角色 / trend 3 角色 | `reports/{role}.md` | 已启用角色均有非空报告 |
| 3 辩论与裁决 | research: bull / bear / research_manager；trend: bull_trend / bear_trend / trend_manager | 辩论记录 + `investment_plan` / trend 裁决 | 轮次达标、action 合法 |
| 4 风险与整合 | research: 3 风险角色 + risk_manager；trend: conservative_trend；+ 宿主 | `final.json` / `report.md` | render 退出码 0 |

### Phase 1：取数与指标

**输入**：6 位股票代码、`as_of`（默认最近交易日）、`depth`（默认 `standard`）、模式（research / trend）。

**动作**：

```bash
RUN_DIR="data/600519_2026-10-31"
mkdir -p "$RUN_DIR/reports"
python3 scripts/fetch_data.py --symbol 600519 --as-of 2026-10-31 --days 400 --out "$RUN_DIR/bundle.json"
python3 scripts/indicators.py --input "$RUN_DIR/bundle.json" --out "$RUN_DIR/bundle.tech.json"
python3 scripts/slice_bundle.py --input "$RUN_DIR/bundle.tech.json" --out-dir "$RUN_DIR/slices" --depth standard
```

`slice_bundle.py` 产出 `$RUN_DIR/slices/{role}.json` 与 `manifest.json`。**Phase 2 的每个 subagent 只读自己那一个切片**，不得再读完整 bundle。trend 模式给 `slice_bundle.py` 加 `--mode trend`，只产出 `index` / `market` / `news` / `fundamentals` 四份切片（复用 research 的字段映射）。

- `fetch_data.py` 退出码：0 完整；2 部分成功（有 warnings）；3 致命（无行情）。`2` 可继续，`3` 按第 9 节处理。
- 解析 stdout 的 `WROTE <path>`，以实际路径为准。
- 读取 `bundle.tech.json` 的 `meta.warnings`，逐条登记；按第 7 节门禁决定降级。
- 仅做离线演示时加 `--offline fixtures/sample_bundle.json`，不访问网络。

**产出**：`$RUN_DIR/bundle.json`、`$RUN_DIR/bundle.tech.json`。

**完成判据**：两文件存在；`schema_version == "1.0"`；`meta.symbol` 与请求一致；`quotes.rows` ≥ 30（indicators 会强制）；`technical.last` 与 `technical.signals` 存在；所有 warnings 已记录。

### Phase 2：分析师并行（research 六位 / trend 三位）

**输入**：只读 `bundle.tech.json`，按角色切片；`depth` 决定是否启用 `sector` / `social`。

**动作**：

1. 先读 `references/subagent-protocol.md`（调度规则）；角色提示词按需从 `references/roles.md` 摘取对应段落，不要一次载入全部 references。
2. 确认 `$RUN_DIR/slices/manifest.json` 的 `enabled_roles`；未启用角色不 spawn。
3. **`depth=quick` 默认不用 subagent**，由宿主顺序扮演 3 个角色；`standard/deep/full` 在**同一轮消息中并行 spawn 所有启用分析师**（并发上限 6）。
4. 每个 subagent 只读 `$RUN_DIR/slices/{role}.json`，不读完整 bundle、不联网、不读其它切片；结果写入 `$RUN_DIR/reports/{role}.md`，只回传 `FILE/SUMMARY/KEY_POINTS/DATA_GAPS` 四行，**不回传全文**。
5. 单个角色失败：重试 1 次 → 父 agent 顺序补写 → 标“数据缺失”。宿主无 subagent 能力时按协议第 6 节降级。

**trend 模式**：并行 spawn 的 3 个分析师是 `trend_structure`（走势结构：趋势标签 上行/震荡/下行、支撑阻力区间）、`trend_cycle`（周期与相对强弱）、`trend_catalyst`（催化与事件），角色提示词见 `references/roles.md`；它们从 `--mode trend` 产出的 `slices/{index,market,news,fundamentals}.json` 取数，同样只读切片、不读完整 bundle。`quick` 深度同样默认不用 subagent。产出 `$RUN_DIR/reports/trend_structure.md`、`trend_cycle.md`、`trend_catalyst.md`。

切片映射（research 模式，只传这些字段，`null` 字段可省略）：

| role | 允许读取的 bundle 字段 | 关注点 |
|---|---|---|
| index | `meta`、`market.indexes`、`quotes.rows` 最近 20 日 | 大盘环境、指数趋势 |
| sector | `meta`、`basic.industry`、`sector`、`market` | 行业景气、板块排名、资金流 |
| market | `meta`、`market`、`quotes.rows` 最近 20 日、`technical.signals` | 量价、情绪、北向/两融 |
| fundamentals | `meta`、`basic`、`valuation`、`fundamentals`、`financials` | 估值、盈利、负债、现金流 |
| news | `meta`、`news`（最多 20 条）、`constraints` | 公告、新闻、停复牌 |
| social | `meta`、`news` 标题、`market`、`quotes.rows` 最近 10 日 | 舆情热度、散户情绪 |

**产出**：research 为 `$RUN_DIR/reports/index.md`、`sector.md`、`market.md`、`fundamentals.md`、`news.md`、`social.md`；trend 为 `trend_structure.md`、`trend_cycle.md`、`trend_catalyst.md`。

**完成判据**：所有已启用角色文件存在且非空；每份报告 200–1200 字并含“结论/倾向”段；未启用角色在 `data_quality` 中注明；任何角色失败写“数据缺失”而非编造。

### Phase 3：多空辩论与裁决（research / trend）

**输入**：Phase 2 的已启用报告、`technical.signals`、`depth`。不要重新读取完整 bundle。

**动作**：

1. 按第 5 节深度表确定投资辩论轮数。
2. **仅第 1 轮并行** spawn `bull` 与 `bear`（各自独立立论、互不可见）；第 2 轮起严格串行——每轮把对方上一轮全文传给下一位再发言，避免双方看不到新观点。每轮 ≤ 800 字。
3. 逐轮发言写入 `debate.history`（`role` 为 `bull` / `bear`），汇总观点写入 `debate.bull` / `debate.bear`。
4. spawn `research_manager`：只读分析师报告与辩论记录，输出 `investment_plan`（`action`、`confidence`、`points`、`reasoning`）。
5. 把辩论与裁决写入 `$RUN_DIR/reports/bull.md`、`bear.md`、`research_manager.md`。

**trend 模式**：辩论双方为 `bull_trend` / `bear_trend`，并行/串行规则与上相同；裁决 spawn `trend_manager`，只读分析师报告与辩论记录，输出 `{direction, confidence, horizon, key_levels: {support[], resistance[]}, scenarios[], reasoning}`，其中 `direction ∈ {上行, 震荡, 下行}`、`confidence ∈ [0,1]`。辩论与裁决写入 `$RUN_DIR/reports/bull_trend.md`、`bear_trend.md`、`trend_manager.md`。`final.action` 由 `direction` 映射：上行→乐观、震荡→中性、下行→审慎。

**产出**：辩论记录、`investment_plan`（research）或 trend 裁决（trend）、各角色报告。

**完成判据**：轮数达到深度表；research 的 `investment_plan.action ∈ {乐观, 审慎, 中性}`，trend 的 `direction ∈ {上行, 震荡, 下行}` 且映射后的 action 合法；`confidence ∈ [0,1]`；`points` 3–6 条；全文无“买入/卖出/加仓/减仓/目标价”。

### Phase 4：风险审阅与整合（research / trend）

**输入**：`investment_plan`（或 trend 裁决）、Phase 2/3 报告、`constraints`、`depth`。

**动作**：

1. 按深度表确定风险辩论轮数：**仅第 1 轮并行** spawn `aggressive`、`conservative`、`neutral`（各自独立质询研究计划）；第 2 轮起串行轮转，每位都能看到前一位本轮发言。各 ≤ 600 字，写入 `$RUN_DIR/reports/{role}.md`。**trend 模式**不做风险三角，只 spawn `conservative_trend` 单角色做一次风险审阅（≤ 600 字），写入 `$RUN_DIR/reports/conservative_trend.md`。
2. spawn `risk_manager`：汇总三方观点与数据质量，输出 `risk`（`risk_level`、`risk_score`、`key_risks`、`risk_control`、`opinions`），写入 `reports/risk_manager.md`。（trend 模式无此步，风险结论由 `conservative_trend` 汇入整合。）
3. 用 `scripts/assemble.py` 确定性拼装 `final.json`（从各报告提取 JSON 块并做结构校验），不要手写 JSON：
   ```bash
   python3 scripts/assemble.py --run-dir "$RUN_DIR" --depth standard --mode research \
     --completeness 0.72 --warning "营收存在两个口径"
   ```
   trend 模式改用 `--mode trend`：`final.json` 在 research schema 基础上增加可选顶层 `trend` 对象。脚本退出 3 时按报错补齐对应角色报告，不要绕过校验或手改 `final.json`。
4. spawn 独立 `verifier`：只读 `final.json` 与 `output-schema.json`，返回 `{ok, issues}`；宿主最多修一轮，仍不通过则把 issues 写入 `data_quality.warnings` 后继续，不得卡死流程。
5. 渲染报告：

```bash
python3 scripts/render.py --input "$RUN_DIR/final.json" --out "$RUN_DIR/report.md"
```

当 `final.json` 含 `trend` 对象时，`render.py` 会在报告中追加“走势分析”章节。

**产出**：`$RUN_DIR/final.json`、`$RUN_DIR/report.md`。

**完成判据**：`final.json` 可被 `render.py` 解析；render 退出码 0；`report.md` 含全部固定章节（trend 模式含“走势分析”章节）；`final.action` 与 `investment_plan.action` 均为三值之一（trend 模式下 `final.trend.direction` 为三值之一且 `final.action` 与映射一致）；价格结论为区间。

## 5. 深度表

| depth | 投资辩论轮（research） | trend 辩论轮（trend） | 风险辩论轮（research） | social | sector | 说明 |
|---|---|---|---|---|---|---|
| quick | 1 | 1 | 1 | 否 | 否 | 核心三步，最快 |
| standard | 1 | 1 | 2 | 否 | 是 | 默认 |
| deep | 2 | 2 | 2 | 是 | 是 | 多空各发言 2 次 |
| full | 3 | 3 | 3 | 是 | 是 | 完整流程，最慢 |

trend 模式的辩论轮数（`bull_trend` / `bear_trend`）与 research 相同映射；`social` / `sector` 两列仅对 research 生效，trend 固定 4 份切片、3 个分析师，风险侧只有 `conservative_trend` 单角色（无轮次概念）。

未启用的角色不要 spawn；在 `data_quality.warnings` 记一条 `未启用：sector/social（depth=...）`。
`quick` 深度默认**不使用 subagent**（角色少、开销大于收益），由宿主顺序扮演 3 个角色；其余深度按 `references/subagent-protocol.md` 并行调度。

## 6. 上下文预算规则

- 分析师只读自己的切片；禁止把完整 `bundle.tech.json` 塞进提示词。切片必须由 `scripts/slice_bundle.py` 生成，不要临时手写。
- 切片先裁剪：`news` ≤ 20 条、每条 summary ≤ 200 字；`quotes.rows` 只取最近 60 日，提示词里再压到 10–20 行；`financials` ≤ 8 期；`market.indexes` ≤ 6 个。
- `technical.series` 只使用最近 60 个交易日；报告中最多画 10 行简表，其余用 `technical.signals` 与 `last` 概括。
- 报告长度上限：分析师 1200 字、单轮辩论 800 字、研究经理 1000 字、风险经理 800 字、最终摘要 300 字。超限先压缩再写入文件。
- 后续 Phase 不重新读取原始数据，只读上一阶段报告与结构化 JSON。
- 辩论超过 2 轮时，提示词只带最近 2 轮；更早内容压成 3 条要点。
- 工具输出超过 2000 字时先摘要再入上下文；不要把整段 DataFrame、整页新闻或 HTML 原样带入。
- 接近上下文上限时，优先保留结论、关键数字、风险、数据警告；删除过程性推理。

## 7. 数据质量门禁

| 条件（检查 bundle / meta.warnings） | 降级动作 |
|---|---|
| `quotes.rows` < 120 日 | 长期均线/趋势结论不可靠；`confidence` 上限 0.65，注明“样本不足 120 日” |
| `quotes.rows` < 60 日 | 技术结论仅参考；`confidence` 上限 0.50，`action` 不得为“乐观” |
| 财务缺失（`fundamentals.periods` 为空或核心字段全 `null`） | 不得基于基本面给出“乐观”；`confidence` 上限 0.55 |
| `constraints.suspended == true` | `action` 不得为“乐观”；不给价格区间；`risk_level` 至少“中” |
| `basic.is_st == true` | `risk_level` 至少“中”；必须提示退市/流动性风险；`confidence` 上限 0.60 |
| 估值字段（PE/PB）全缺失 | 注明“估值不可比”；`confidence` 减 0.05 |
| 任一 warning 存在 | 原样写入 `data_quality.warnings`，并在报告“数据质量与限制”列出 |

- `quotes.rows` < 30 时 `indicators.py` 会退出 3，按第 9 节处理，不要硬跑。
- 多个门禁同时触发时取最严格的上限；不得为了得到“乐观”而忽略门禁。
- `data_quality.completeness` 按可用核心字段比例估算（0–1），不得虚报。

## 8. 输出契约

`final.json` 必须符合 `references/output-schema.json`（与 `CONTRACT.md` 第 4 节一致）。关键约束：

- `action` 三值：`乐观` / `审慎` / `中性`；`investment_plan.action` 与 `final.action` 都要守约。
- trend 模式：`final.json` 在 research schema 基础上增加可选顶层 `trend` 对象（`direction`、`confidence`、`horizon`、`key_levels.support[]/.resistance[]`、`scenarios[]`、`reasoning`）；`direction ∈ {上行, 震荡, 下行}`，`final.action` 由 `direction` 映射：上行→乐观、震荡→中性、下行→审慎，映射结果仍受三值白名单与第 7 节门禁约束。
- 禁止措辞：买入、卖出、加仓、减仓、增持、减持、目标价、稳赚、必涨。需要表达倾向时用“乐观/审慎/中性”。
- 价格必须用区间 `price_analysis_range: {low, high}`，不得给单点目标价；无法给区间时写 `null` 并说明。
- `confidence`、`risk_score`、`data_quality.completeness` ∈ [0,1]。
- 缺失字段一律 `null`，并写入 `data_quality.warnings`；不要用 0 或占位文本冒充数据。
- `render.py` 会做最后一道 `action` 白名单校验：发现“买入/卖出”等词会归一化为三值之一，并把 warning 追加到 `data_quality.warnings`。
- 固定报告章节由 `render.py` 生成：标题与元信息、结论摘要、六位分析师观点、多空辩论、研究经理结论、风险辩论、最终研究结论、数据质量与限制、免责声明；trend 模式额外包含“走势分析”章节。

## 9. 失败处理

| 失败 | 重试/降级 |
|---|---|
| `fetch_data.py` 退出 3（无行情） | 换上一交易日重试 1 次；可加大 `--days 800`；仍失败则停止并说明，不要编造 |
| `fetch_data.py` 退出 2（部分成功） | 继续；warnings 写入 `data_quality` 并按第 7 节降级 |
| `indicators.py` 退出 3（<30 行） | 用更长区间（`--days 800`）重新取数后再算 1 次；仍不足则停止 |
| 网络超时 | 由脚本内部重试；宿主最多再重试 2 次并退避，避免密集重试 |
| subagent 失败/超时 | 重试 1 次；仍失败改为顺序执行；再失败该角色标“数据缺失”并下调置信度 |
| 模型输出不是合法 JSON | 用同一提示词重试 1 次，明确“只输出 JSON，不要解释”；仍失败则提取 JSON 片段修复；无法修复则标缺失，禁止编造 |
| `render.py` 退出 3 | 检查 `final.json` 是否为合法对象且含已知字段；修复后重跑，不要手改 `report.md` |
| 数据全部缺失 | 输出“数据不足，无法形成研究结论”，不要给出三值结论 |

## 10. 参考文件与按需加载

| 文件 | 何时读取 |
|---|---|
| `references/subagent-protocol.md` | Phase 2 开始前必读；决定并行/串行、spawn 契约与降级 |
| `scripts/slice_bundle.py` | Phase 1 生成角色切片（`--mode research|trend`）；Phase 2 前必须执行 |
| `scripts/assemble.py` | Phase 4 拼装并校验 `final.json`（`--mode research|trend`） |
| `references/roles.md` | Phase 2 开始前读一次，取角色提示词 |
| `references/output-schema.json` | Phase 4 整合前校验 `final.json` |
| `references/a-share-quirks.md` | 遇到涨跌停、停牌、ST、除权、北交所等规则时 |
| `fixtures/sample_bundle.json` | 离线演示/测试 |
| `scripts/selftest.py` | 安装后自检 |
| `CONTRACT.md`（若随包提供） | 需要精确字段定义时 |

不要把这些文件一次性全部载入上下文；用到哪个读哪个。

## 11. 最小执行清单

1. `cd` 到 Skill 根目录，跑第 3 节自检。
2. 解析用户输入：确定模式（默认 research；“走势”关键词或 `trend` 子命令 → trend）、`depth`、`as_of`。
3. Phase 1：取数 + 指标，检查 warnings；`slice_bundle.py` 按 mode 生成 slices，确认 `manifest.json` 的 `enabled_roles`。
4. Phase 2：`standard` 及以上并行 spawn 分析师（research 六位 / trend 三位）；`quick` 顺序执行。
5. Phase 3：按 depth 跑多空辩论（首轮并行、后续串行），经理裁决产出 `investment_plan`（research）或 trend 裁决（trend）。
6. Phase 4：research 走风险三角（首轮并行、后续串行）+ 风险经理，trend 只做 `conservative_trend` 审阅；随后 verifier 校验，`assemble.py --mode` 整合 `final.json`。
7. `python3 scripts/render.py --input "$RUN_DIR/final.json" --out "$RUN_DIR/report.md"`。
8. 检查 `action` 三值、价格区间、免责声明；trend 模式再查 `trend.direction` 与映射。
