# TAC

**TAC — TradingAgents-CN 的 A 股研究 Skill**：让 Claude Code / Codex / Cursor 等支持 `SKILL.md` 的宿主 AI 自己扮演多智能体，完成取数、技术指标、分析师、多空辩论、风险审阅与最终研究结论，并渲染中文研究报告（默认输出为自包含网页）。支持两种模式：**research**（单股综合研究，默认）与 **trend**（股价走势分析）。

不依赖 LangGraph、MongoDB、Redis，不包含原项目 `core/`、`app/` 代码。运行时只需要 Python 3.9+、pandas、numpy、akshare。

---

## 目录结构

```text
tac/
├── SKILL.md                     # 宿主入口：模式、适用范围、4 个 Phase、深度表、门禁
├── AGENTS.md                    # 通用 agent 入口（Cursor/Gemini CLI 等读取）
├── README.md                    # 本文档
├── CONTRACT.md                  # 内部接口契约（bundle / final schema、CLI）
├── requirements.txt             # 运行时依赖
├── scripts/
│   ├── fetch_data.py            # 取数 + 归一化 -> bundle.json
│   ├── indicators.py            # 技术指标 -> bundle.tech.json
│   ├── slice_bundle.py          # 按角色切出最小上下文 -> slices/{role}.json（--mode research|trend）
│   ├── assemble.py              # 汇总角色报告 -> final.json（含校验，--mode research|trend）
│   ├── render.py                # final.json -> report.html（纯标准库；--format md 可选 Markdown）
│   └── selftest.py              # 离线/在线自检
├── references/
│   ├── roles.md                 # research 与 trend 各角色的提示词
│   ├── subagent-protocol.md     # 何时用 subagent、并行/串行时机、spawn 契约
│   ├── output-schema.json       # final.json 的 JSON Schema
│   └── a-share-quirks.md        # 涨跌停、停牌、ST、除权、北交所等规则
├── fixtures/
│   └── sample_bundle.json       # 离线演示数据
└── tests/
    ├── test_fetch_data.py
    └── test_indicators.py
```

运行后会在 `data/{symbol}/{as_of}/` 下生成（按股票代码分目录、再按日期分子目录）：

```text
data/600519/2026-10-31/
├── bundle.json          # 原始取数结果
├── bundle.tech.json     # 追加 technical 段
├── slices/              # 每个角色一份最小上下文（由 slice_bundle.py 生成）
├── reports/             # 各角色 Markdown 报告
│   ├── index.md
│   ├── sector.md
│   ├── market.md
│   ├── fundamentals.md
│   ├── news.md
│   ├── social.md
│   ├── bull.md
│   ├── bear.md
│   ├── research_manager.md
│   ├── aggressive.md
│   ├── conservative.md
│   ├── neutral.md
│   └── risk_manager.md
├── final.json           # 宿主整合后的结构化结论
└── report.html          # render.py 渲染的最终报告（自包含网页）
```

trend 模式下，`reports/` 为 `trend_structure.md`、`trend_cycle.md`、`trend_catalyst.md`、`bull_trend.md`、`bear_trend.md`、`trend_manager.md`、`conservative_trend.md`；`final.json` 增加可选顶层 `trend` 对象，`report.html` 额外包含“走势分析”章节。

---

## 安装

需要 Python 3.9 或更高版本。

```bash
cd tac
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
```

安装后做一次自检（Python 版本、akshare、网络）：

```bash
python3 scripts/selftest.py
```

若暂时没有 `selftest.py`：

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

---

## 快速开始

以下命令都在 **Skill 根目录**（包含 `SKILL.md` 的目录）执行，脚本路径统一用相对的 `scripts/...`。

### 1. 取数

```bash
RUN_DIR="data/600519/2026-10-31"
mkdir -p "$RUN_DIR/reports"
python3 scripts/fetch_data.py \
  --symbol 600519 \
  --as-of 2026-10-31 \
  --days 400 \
  --out "$RUN_DIR/bundle.json"
```

退出码：`0` 完整；`2` 部分成功（有 warnings，可继续）；`3` 致命（无行情，需按 `SKILL.md` 第 9 节处理）。stdout 会打印 `WROTE <path>`。

离线演示（不访问网络）：

```bash
python3 scripts/fetch_data.py \
  --symbol 600519 \
  --offline fixtures/sample_bundle.json \
  --out "$RUN_DIR/bundle.json"
```

### 2. 计算技术指标

```bash
python3 scripts/indicators.py \
  --input "$RUN_DIR/bundle.json" \
  --out "$RUN_DIR/bundle.tech.json"
```

少于 30 个交易日会退出 `3`；此时应加大 `--days` 重新取数。

### 3. 生成角色切片

```bash
python3 scripts/slice_bundle.py \
  --input "$RUN_DIR/bundle.tech.json" \
  --out-dir "$RUN_DIR/slices" \
  --depth standard
```

每个 subagent 只读 `slices/{role}.json`（1–8 KB），不要直接把整份 `bundle.tech.json` 塞进 6 个并行上下文。trend 模式加 `--mode trend`，只产出 `index` / `market` / `news` / `fundamentals` 四份切片。

### 4. 让宿主 AI 按 SKILL.md 分析

把 `SKILL.md` 交给支持 Skill 的宿主（见下方安装方式），然后提出：

```text
/tac 600519
```

（等价说法：“用 tac 以 standard 深度研究 600519，数据在 data/600519/2026-10-31/。”）

**trend 模式（股价走势分析）**：

```text
/tac 600519 走势        # 或 /tac trend 600519
```

期望产物：`reports/trend_structure.md`、`trend_cycle.md`、`trend_catalyst.md`、`bull_trend.md`、`bear_trend.md`、`trend_manager.md`、`conservative_trend.md`，以及含 `trend` 对象（`direction`、`key_levels` 等）与“走势分析”章节的 `final.json` / `report.html`。

宿主会依次执行：

1. Phase 2：分析师（research 六个 / trend 三个；`standard` 及以上在**同一轮并行 spawn**，各自只读自己的切片；不支持 subagent 时顺序执行），输出 `reports/{role}.md`；
2. Phase 3：多空辩论（仅第 1 轮并行，后续串行）+ 经理裁决（research_manager / trend_manager），输出 `investment_plan` 或 trend 裁决；
3. Phase 4：风险审阅（research：风险三角 + 风险经理；trend：`conservative_trend` 单角色）+ 研究简报；
4. 用 `assemble.py --mode research|trend` 汇总为 `final.json`，再由 `verifier` 独立校验。

并行/串行的完整规则见 `references/subagent-protocol.md`。

### 5. 组装与渲染报告

```bash
python3 scripts/assemble.py \
  --run-dir "$RUN_DIR" \
  --depth standard \
  --mode research \
  --completeness 0.72

# trend 模式改用 --mode trend

python3 scripts/render.py \
  --input "$RUN_DIR/final.json" \
  --format html \
  --out "$RUN_DIR/report.html"
```

Skill 流程（SKILL.md Phase 4 第 6 步）会在渲染写盘成功后用默认浏览器自动打开报告：`open`（macOS）/ `xdg-open`（Linux）/ `cmd.exe /c start`（Windows），打开失败不阻塞流程；CI 与无界面环境可跳过。直接单独运行 `render.py` 脚本本身不会打开任何程序。

`render.py` 只用标准库，可脱离宿主单独运行；默认输出自包含网页，省略 `--out` 时输出到 stdout；需要 Markdown 时加 `--format md`：

```bash
python3 scripts/render.py --input "$RUN_DIR/final.json" > report.html
python3 scripts/render.py --input "$RUN_DIR/final.json" --format md > report.md  # Markdown 备选
```

退出码：`0` 成功；`3` 输入非法（文件不可读、JSON 格式错误、不是 `final.json`）。它还会做最后一道 `action` 白名单校验：如果出现“买入/卖出”等词，会归一化为“乐观/审慎/中性”，并把 warning 追加到 `data_quality.warnings`。

---

## 安装方式

### 方式一：安装为全局 Claude Code Skill

```bash
mkdir -p ~/.claude/skills
cp -R tac ~/.claude/skills/tac
```

之后在任意项目中，Claude Code 都能按 `name: tac`、`/tac` 命令或触发词加载它。注意依赖要装在运行宿主的环境里，或先进入 Skill 目录激活 `.venv`。

### 方式二：安装为项目级 Skill

```bash
mkdir -p .claude/skills
cp -R /path/to/tac .claude/skills/tac
```

适合把研究流程与某个项目绑定，随项目一起版本管理。宿主会优先加载项目内的 `.claude/skills/tac/SKILL.md`。

### 方式三：当普通脚本用

不依赖任何宿主：

```bash
git clone <repo> tac
cd tac
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt

python3 scripts/fetch_data.py --symbol 600519 --out data/600519_bundle.json
python3 scripts/indicators.py --input data/600519_bundle.json --out data/600519_bundle.tech.json

# 自己按 SKILL.md 的 Phase 2-4 组织分析（可用任意 LLM/人工），产出 final.json
python3 scripts/render.py --input final.json --format html --out report.html
```

如果只想验证渲染器，可手写一份最小 `final.json`，或参考 `references/output-schema.json`。

### 方式四：在 OpenAI Codex CLI 中安装

Codex CLI 遵循同一套 Agent Skills 开放标准（agentskills.io），可直接复用本 Skill 的 `SKILL.md`，但扫描目录是 `.agents/skills` 而非 `.claude/skills`：

```bash
# 用户级（跨项目可用）
mkdir -p ~/.agents/skills
cp -R tac ~/.agents/skills/tac

# 或项目级（仅当前仓库可用）
mkdir -p .agents/skills
cp -R /path/to/tac .agents/skills/tac
```

使用方式：

- **显式调用**：在 Codex CLI 中输入 `$tac 600031`（`$` 是 Codex 的技能提及前缀；`/skills` 可列出已安装技能）。
- **隐式触发**：直接说“帮我研究一下 600031”，Codex 按 `SKILL.md` frontmatter 的 description 自动选用。
- Codex 更新技能目录后自动感知，未生效时重启一次 Codex。
- 依赖仍需装在 Codex 的运行环境里（`pip install -r requirements.txt`），或先进入 Skill 目录激活 `.venv`；`python3 scripts/...` 各命令在 Codex 的 shell 中同样可用。

> **常见问题：`/tac` 无法触发。** 这是正常的——`/` 斜杠命令是 Claude Code 的调用方式，Codex 不支持用 `/` 调技能。请改用 `$tac 600031`，或先运行 `/skills` 确认技能已被识别：若列表中没有 `tac`，检查安装路径是否为 `~/.agents/skills/tac`（Codex **不扫描** `~/.codex/skills`），并确认 `SKILL.md` 位于该目录第一层。

若想把技能设为默认关闭，可在 `~/.codex/config.toml` 加 `[[skills.config]]` 条目并置 `enabled = false`（改后需重启）。

### 方式五：通过 AGENTS.md 接入其它 AI（Cursor / Gemini CLI 等）

对于没有 Skill 发现机制、但会读取仓库根目录 `AGENTS.md` 的 agent（Cursor、Gemini CLI、Amp 等），本仓库自带了一份 `AGENTS.md`：它说明何时触发本 Skill、如何定位并完整阅读 `SKILL.md` 执行，以及输出硬约束（三值结论、价格区间、免责声明）。

无需安装动作——只要 agent 在本仓库（或包含本 Skill 的目录）中工作并读取 `AGENTS.md`，即可被引导执行；配合各宿主自身的 rules/记忆机制把 `AGENTS.md` 纳入上下文效果更稳定。对完全没有 agent 能力的场景（网页版对话等），退回方式三：把 `SKILL.md` 内容作为提示词、脚本在本地运行。

---

## 数据源

| 数据源 | 角色 | 说明 |
|---|---|---|
| **AKShare** | 主源（默认） | 免费、无需 token；覆盖 A 股行情、财务、新闻、板块、资金流。接口可能随上游变动 |
| **Tushare** | 可选 | 通过 `--source tushare` 使用，需要自己的 token；财务与资金流字段通常更完整 |
| **Finnhub** | **不支持** | 主要覆盖美股与全球市场，不提供 A 股数据，本 Skill 不接入 |

数据获取失败时，`fetch_data.py` 会把失败模块写入 `meta.warnings`，不会静默填 0。部分成功退出码为 `2`，致命失败为 `3`。

---

## 输出与质量约束

- `final.json` 遵循 `references/output-schema.json`（与 `CONTRACT.md` 第 4 节一致）。
- `action` 只能是 `乐观 / 审慎 / 中性`；禁止“买入 / 卖出 / 加仓 / 减仓 / 目标价”等措辞。
- 价格结论必须写成区间 `price_analysis_range: {low, high}`，不得给单点目标价。
- 缺失字段保留为 `null` 并记入 warning；`render.py` 会显示“数据缺失”，不会编造。
- `confidence`、`risk_score`、`data_quality.completeness` 取值在 `[0,1]`，渲染时转为百分比。
- 大额金额在渲染时自动转换为“亿元 / 万元”。
- trend 模式：`final.json` 在 research schema 基础上增加可选顶层 `trend` 对象（`direction`、`confidence`、`horizon`、`key_levels.support[]/.resistance[]`、`scenarios[]`、`reasoning`）；`action` 由 `direction` 映射：上行→乐观、震荡→中性、下行→审慎。

报告固定章节：标题与元信息、结论摘要、六位分析师观点、多空辩论、研究经理结论、风险辩论、最终研究结论、数据质量与限制、免责声明；trend 模式额外包含“走势分析”章节。

---

## 已知限制

- **快照数据**：行情、财务、新闻均来自取数时点，可能延迟；不做盘中实时更新。
- **上游依赖**：AKShare 接口可能因数据源改版而失效，需关注 `meta.warnings` 并升级依赖。
- **财务口径**：不同报告期/数据源的口径可能不一致；本 Skill 不做追溯重述或审计级校验。
- **特殊标的**：ST、停牌、退市整理、上市不足 60 日、北交所标的的可比数据较少，结论会按 `SKILL.md` 第 7 节降级。
- **模型观点**：分析师、辩论与风险观点由宿主模型生成，可能存在偏差、遗漏或过度自信；报告不构成投资建议。
- **非交易系统**：不连接券商、不下单、不做资金管理，也不提供实时买卖信号。

---

## 许可

本项目采用 **Apache-2.0** 许可。

- 本 Skill 为**独立实现**，不包含原项目 `core/`、`app/` 代码，也不依赖 LangGraph / MongoDB / Redis。
- 技术指标（MA、EMA、RSI、MACD、BOLL、ATR、KDJ 等）的算法参考了采用 Apache-2.0 许可的开源模块；相关实现与许可声明保留在源码中。
- AKShare、Tushare 等第三方数据源受各自许可与服务条款约束；使用者需自行遵守数据源的使用限制。

---

## 免责声明

本 Skill 及其输出仅供研究与学习参考，**不构成任何投资建议、证券买卖建议或收益承诺**。数据和模型输出可能存在延迟、错误或遗漏；投资者应独立判断并自行承担全部风险。作者不对因使用本 Skill 产生的任何直接或间接损失负责。
