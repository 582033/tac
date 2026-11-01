# AGENTS.md — TAC（A 股研究与走势分析 Skill）

本仓库是一个可移植的 **Agent Skill**（遵循 agentskills.io 开放标准）。任何读取本文件的 AI agent 都可以执行它。

## 何时使用

当用户提出以下请求时使用本 Skill：

- A 股**个股研究**：基本面、估值、消息面的综合分析（如“帮我研究 600031”）
- A 股**股价走势分析**：趋势方向、支撑阻力、催化事件（提到“走势 / 趋势 / 股价走势”）
- 出现 6 位 A 股代码（600/601/603/605/688/000/001/002/003/300/301/8xx/4xx 开头）

**不要**用于：美股/港股/基金/期货，实时交易决策，或作为投资建议输出。

## 如何执行

1. **先完整阅读本仓库的 `SKILL.md`**——它是唯一入口，定义了两种模式（research / trend）、4 Phase 流程、深度档位、数据质量门禁与输出契约。不要凭印象执行，不要跳过 Phase。
2. 按 SKILL.md 第 0 节定位 Skill 根目录，所有命令以 `scripts/...` 相对路径在该目录下执行。
3. 脚本依赖 Python 3.9+、pandas、numpy、akshare（`pip install -r requirements.txt`）；先跑 `python3 scripts/selftest.py` 自检。
4. 有 subagent/并行能力就按 `references/subagent-protocol.md` 并行调度；没有就按其第 6 节降级为顺序执行。
5. 结果写入 `data/{股票代码}/{日期}/`，最终报告用 `python3 scripts/render.py --input final.json --format html --out report.html` 渲染为自包含网页，并尝试用系统默认浏览器打开（`open` / `xdg-open` / `cmd /c start`，失败不阻塞）。

## 硬性约束（违反即输出无效）

- 最终倾向只能是 **乐观 / 审慎 / 中性**；全文禁止出现“买入、卖出、加仓、减仓、增持、减持、目标价、稳赚、必涨”。
- 价格结论必须是区间 `price_analysis_range: {low, high}`，不得给单点目标价。
- 缺失字段写 `null` 并记入 warnings，**禁止编造、补零或臆测**。
- 数据不足时输出“数据不足，无法形成研究结论”，不要给出三值结论。
- 本 Skill 输出仅供研究参考，不构成投资建议；报告中必须保留免责声明。

## 更多文档

- `README.md` — 安装方式（Claude Code / Codex / 通用）与快速开始
- `CONTRACT.md` — bundle / final.json 字段级接口契约
- `references/` — 角色提示词、subagent 调度协议、输出 schema、A 股特殊规则
