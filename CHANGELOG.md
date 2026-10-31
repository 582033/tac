# Changelog

本项目所有显著变更均记录于此。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-11-01

### 变更

- **改名**：项目更名 `a-share-research` → **`tac`**，安装目录相应变为 `~/.claude/skills/tac/`，SKILL 入口名与触发词同步更新。

### 新增

- **trend 走势分析模式**：新增轻量走势分析模式，在完整六分析师流程之外提供单股走势快速研判路径。
- **subagent 调度协议**：新增 `references/subagent-protocol.md`，定义何时使用 subagent、并行/串行时机与 spawn 契约。
- **切片/组装脚本**：新增 `scripts/slice_bundle.py`（按角色切出最小上下文 `slices/{role}.json`）与 `scripts/assemble.py`（汇总角色报告并校验为 `final.json`）。

## [1.0.0] - 2026-10-31

首版发布。

### 新增

- **取数**：AKShare + 腾讯行情双通道取数与归一化，输出 `bundle.json`；支持 `--offline` 离线演示（`fixtures/sample_bundle.json`）；部分成功/致命失败分级退出码。
- **技术指标**：MA、EMA、RSI、MACD、BOLL、ATR、KDJ 等指标计算，输出 `bundle.tech.json`。
- **六位分析师**：指数、板块、市场、基本面、新闻、社交六位分析师角色与提示词（`references/roles.md`）。
- **辩论机制**：多空辩论 + 研究经理裁决，输出 `investment_plan`。
- **风险三角**：激进/保守/中立三方风险辩论 + 风险经理，输出研究简报。
- **渲染**：`scripts/render.py` 将 `final.json` 渲染为中文 Markdown 研究报告（纯标准库）。
- **schema 校验**：`references/output-schema.json` 定义 `final.json` 的 JSON Schema，组装阶段强制校验。
