# Subagent 调度协议

本文件规定**什么时候用 subagent、怎么派、并行还是串行、父 agent 保留什么上下文**。
SKILL.md 的 Phase 2–4 以本文件为准；宿主不支持 subagent 时按第 6 节降级。

---

## 1. 决策表：什么时候用 subagent

| 条件 | 策略 | 理由 |
|---|---|---|
| `depth=quick` | **默认不用 subagent**，顺序扮演 3 个分析师 | 角色少、数据小，spawn 开销大于收益 |
| `depth=standard/deep/full` | 分析师 **全部并行** | 6 份报告互相独立，并行可显著缩短时延 |
| 用户明确要求"多角度/独立评审/不要互相影响" | 强制并行 subagent | 角色隔离能减少观点趋同 |
| 任一角色数据切片缺失 | 该角色不 spawn，直接标"数据缺失" | 避免 subagent 编造 |
| 宿主无 subagent 能力 | 顺序执行，每个角色前清空上下文（见第 6 节） | 保证角色隔离 |
| 上下文已接近上限 | 停止 spawn，改用顺序 + 文件传递 | 防止父 agent 被报告撑爆 |

**硬规则**：父 agent 不得为了"省事"把 6 个角色塞进同一个提示词里一次性生成——那会退化成单角色，失去交叉质询的价值。

---

## 2. 四类 subagent

| 类型 | 角色 | 数量 | 并行时机 |
|---|---|---|---|
| `analyst` | index / sector / market / fundamentals / news / social | 按 depth 启用 | Phase 2 **全部同时** |
| `debater` | bull / bear | 2 | Phase 3 **仅第 1 轮并行**，后续轮串行 |
| `risk` | aggressive / conservative / neutral | 3 | Phase 4 **仅第 1 轮并行**，后续轮串行 |
| `verifier` | 独立校验员 | 1 | `final.json` 生成后、渲染前 |

不要嵌套 subagent（subagent 内不要再 spawn）。并发上限 6。

---

## 3. 统一 spawn 契约

每次 spawn 必须给 subagent 以下五项，缺一不可：

1. **角色提示词**：从 `references/roles.md` 摘出该角色那一段，原文传入。
2. **唯一输入文件**：`slices/{role}.json`（由 `scripts/slice_bundle.py` 生成）。明确写"只读这个文件，不要读 bundle.tech.json、不要读其它切片、不要联网"。
3. **输出文件**：`reports/{role}.md`。要求"只写 Markdown 正文，不写前言、不写解释、不要代码块包裹"。
4. **长度与措辞约束**：分析师 ≤1200 字；禁止"买入/卖出/加仓/减仓/增持/减持/目标价"；结论只能是乐观/审慎/中性。
5. **返回格式**：subagent 完成后只回传：
   ```
   FILE: reports/{role}.md
   SUMMARY: 一句话结论（≤60 字）
   KEY_POINTS: 3 条要点（每条 ≤30 字）
   DATA_GAPS: 使用了哪些 null 字段（没有则写 none）
   ```
   **不要回传报告全文**——父 agent 只靠 SUMMARY/KEY_POINTS 做后续编排，需要细节时再按文件读取。

分析师模板：

```
你是 A 股研究团队中的 {role} 分析师。
只读 /abs/path/slices/{role}.json，不要读取其它文件、不要联网、不要调用脚本。
严格按下面的角色要求工作：
---
{从 references/roles.md 复制的该角色段落}
---
要求：先引用数据字段再下结论；缺失数据写"数据缺失"，不得补零或编造；
每条结论括注依据字段（如 technical.last.rsi14=…）。
把结果写入 /abs/path/reports/{role}.md（≤1200 字，纯 Markdown）。
完成后只回传 FILE/SUMMARY/KEY_POINTS/DATA_GAPS 四行。
```

---

## 4. 各 Phase 的并行/串行时机

### Phase 2 — 分析师：一次性全并行

- 先跑 `slice_bundle.py`，确认 `slices/manifest.json` 的 `enabled_roles`。
- **在同一轮消息里同时 spawn 所有启用角色**（Claude Code：多个 Agent 调用放在同一条消息）。
- 父 agent 收到全部 SUMMARY 后再进入 Phase 3；不要边收边辩论。
- 单个失败：重试 1 次；仍失败则改为父 agent 顺序补写；再失败标"数据缺失"。

### Phase 3 — 多空辩论：首轮并行，后续严格串行

```
Round 1: spawn bull ─┐  并行（各自独立立论，互不可见）
         spawn bear ─┘
Round 2: spawn bull（带 bear 的 round1 全文）
         spawn bear（带 bull 的 round2 全文）   ← 必须串行
Round N: 同上
```

- 只有第 1 轮可以并行。第 2 轮起若并行，双方都看不到对方新观点，辩论会退化成两次独白。
- 每轮发言 ≤800 字，父 agent 只保留"最近 2 轮全文 + 更早的 3 条要点"。
- 辩论结束后 spawn `research_manager`（单个），输入是各分析师 SUMMARY + 辩论记录，输出 `investment_plan`。

### Phase 4 — 风险三角：首轮并行，后续串行

```
Round 1: spawn aggressive ─┐
         spawn conservative ├ 并行，各自只读 investment_plan + 分析师 SUMMARY
         spawn neutral ─────┘
Round 2+: 三个角色串行轮转，每个都能看到前一位本轮发言
```

- 风险经理 `risk_manager` 在辩论结束后单独 spawn，输入 `investment_plan` + 三方最终观点 + `data_quality.warnings`。
- 风险经理不得推翻 `investment_plan.action` 的枚举值，只能调整 `risk_level`、`risk_score`、区间与风险提示；若确需改变倾向，必须在 `reasoning` 说明理由。

### 渲染前 — 校验员：单次 spawn

`verifier` 输入：`final.json` + `references/output-schema.json` + 合规词表。
输出 JSON：
```json
{"ok": true, "issues": [{"path": "final.action", "problem": "...", "fix": "..."}]}
```
父 agent 最多修一轮；仍不通过则把 issue 写入 `data_quality.warnings` 后照常渲染，不得卡死流程。

---

## 5. 父 agent 的上下文纪律

- **父 agent 不读 6 份报告全文**。只保留 SUMMARY/KEY_POINTS；Phase 3/4 需要引用原话时，用 `grep`/定点读取对应段落。
- 父 agent 不把 `bundle.tech.json` 再塞进自己的上下文；切片交给 subagent。
- 编排状态用一个轻量 `run_state.json` 记录：`{phase, roles_done, failed, warnings}`，跨步骤只读这个。
- 最终整合时只读：各角色 SUMMARY、`investment_plan`、`risk`、`data_quality`，以及需要引用的少量原文片段。

---

## 6. 无 subagent 宿主的降级方案

按顺序尝试：

1. **顺序角色 + 显式清空**：每个角色单独一轮，提示词写明"忘记之前的角色设定，你现在只是 {role}"；上一角色只传 SUMMARY，不传全文。
2. **两段式**：先一次性生成 6 份报告到文件（每份独立提示词），再另起一轮做辩论。
3. **最小可行**：`depth=quick`，只做 market / fundamentals / news 三角色 + 一次多空 + 一次风险，输出中注明"未使用并行 subagent"。

降级必须在 `data_quality.warnings` 记录 `未使用 subagent：{原因}`。

---

## 7. 预算与超时

| 项目 | 上限 |
|---|---|
| 单分析师输入切片 | ≤ 15 KB |
| 单分析师输出 | ≤ 1200 字 |
| 单轮辩论输出 | ≤ 800 字 |
| Phase 2 并发 | ≤ 6 |
| 单 subagent 超时 | 300 秒；超时重试 1 次 |
| 角色失败重试 | 1 次，之后降级 |

一次 `standard` 分析的总 token 预算约 60–100k（输入+输出）；超过 150k 说明切片没生效或报告没裁剪，应停止并检查 `slices/` 体积。
