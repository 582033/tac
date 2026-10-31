# A 股数据陷阱与处理规则

字段路径均以 bundle.json 为根，取数与归一化由 `scripts/fetch_data.py` 完成。以下每条都给出 skill 中的强制处理规则。

## 1. 前复权口径
除权除息后历史价格会被调整，最新价保持不变，使均线与指标连续。skill 要求 `quotes.adjust` 必须为 `qfq`；若为 `hfq` 或空，写入 `meta.warnings`，并禁止跨除权日的均线、MACD 结论。成交额与换手率不复权，不得与价格混算。

## 2. 停牌
停牌期间没有新 K 线，`constraints.suspended=true`，行情停留在 `constraints.last_trade_date`。skill 中：停止输出“当前”趋势与量价判断；报告注明停牌起始日与数据截止日；`limit_up_price`、`limit_down_price` 置 null；`risk_level` 至少为“中”。

## 3. ST / *ST
`basic.is_st=true` 表示被实施风险警示，主板 ST 涨跌幅 5%，退市与流动性风险高。skill 中：`constraints.price_limit_pct` 主板取 0.05、创业板/科创板取 0.20；风险提示置顶；不得因低 PE/PB 判定“低估”；技术信号降权，并在 `key_risks` 列示退市风险。

## 4. 涨跌停
主板 10%，创业板与科创板 20%，ST 5%（创业板、科创板风险警示股仍为 20%），北交所 30%；沪深新股上市前 5 个交易日、北交所首日不设涨跌幅。skill 中：按 `constraints.board` 与 `price_limit_pct` 判定，不硬编码记忆；`limit_up_price`/`limit_down_price` 以前收盘价乘 (1±pct) 并四舍五入到 0.01；收盘封板时量价信号失真，须标注“封板”。

## 5. 财报披露与“最新已披露”
年报与一季报 4 月 30 日前、半年报 8 月 31 日前、三季报 10 月 31 日前披露；A 股利润表为年初至今累计值。skill 中：只选 `period <= as_of` 的最新已披露报告期；用 `report_type` 标注口径；Q2/Q3 不得直接相减当单季，除非显式换算；超过法定期限仍未更新时在 `meta.warnings` 标注“披露延迟”。

## 6. 交易日历与节假日
周末与春节、国庆等休市，`as_of` 可能不是交易日。skill 中：以 `constraints.last_trade_date` 作为实际估值日；`quotes.rows` 末行日期必须与其一致；不足 30 行时 indicators 退出码 3；长假前数据须标注“节前口径”。

## 7. 除权除息
除权除息日股价自然下跳，不是利空。skill 中：统一前复权后再算涨跌幅与均线；检查除权日前后 `pct_chg` 是否异常；股息率用 `valuation.dividend_yield`，不得用价差自行推算；涨跌停价以除权参考价为基准。

## 8. 北向资金
2024 年 8 月起港交所停止披露每日北向成交额及个股成交明细，前后口径不可比。skill 中：`market.northbound_net` 为 null 时不得推断“外资流入/流出”，不得进入核心证据；仅作辅助时注明日期与口径；若数据源仍返回旧口径，须在 `meta.warnings` 说明。

## 9. 龙虎榜
龙虎榜只覆盖触发涨跌幅偏离、换手率、振幅等条件的个股，未上榜不代表无资金关注。skill 中：bundle 无专用字段时从 `news[]` 提取并标注来源；不得把上榜等同“主力买入”；机构专用席位与游资席位须分开表述。

## 10. 板块与概念
`basic.industry` 是行业分类；概念板块可跨行业、成分重叠且调整频繁。skill 中：`sector.name` 以行业为准，概念只作事件线索；`sector.rank/rank_total` 必须同一口径，缺失时不排名；不得把板块资金流当个股资金流，也不得假设概念成分稳定。

## 11. T+1
当日买入次一交易日才能卖出，无法日内止损。skill 中：`risk_control` 只写观察、复核与失效条件，不写日内止损或操作节奏；涨跌停封板时无法保证成交，价格区间结论须注明流动性约束。

## 12. 最小交易单位
买入为 100 股整数倍；科创板 200 股起、北交所 100 股起，均可以 1 股递增；卖出可一次性卖零股；价格最小变动 0.01 元。skill 中：报告不计算具体股数与仓位金额；举例时注明单位与价格 tick；低价股与高价股的可操作性差异写入限制。

## 数据质量门禁清单

出现以下任一情况，必须在最终报告中降级结论或标注不确定性：

- `quotes.rows` 不足 60 行（无法覆盖 ma60），或末行日期不等于 `constraints.last_trade_date`：不输出技术结论，`final.action` 只能“中性”，confidence ≤ 0.3。
- `constraints.suspended=true` 或 `basic.is_st=true`：`risk.risk_level` 不得为“低”，并在摘要首句提示。
- 最新财报 `period` 晚于 `as_of`，或超过法定期限未更新：标注“最新已披露”与“披露延迟”，不得称“当前业绩”。
- `valuation` 快照日期与 `as_of` 不一致：引用 PE/PB 时注明快照日，不写“截至分析日”。
- `market.northbound_net` 为 null：不进入核心证据，只在资料边界说明。
- `news` 为空或全部早于 `as_of` 前 30 天：新闻维度写“资料不足”，不得用模型记忆补写。
- 收盘处于涨停或跌停：价格分析区间标注“封板不可成交”。
- 复权口径缺失或不一致：禁用跨除权日的均线、MACD 结论。
- `data_quality.completeness < 0.6`：`final.action` 只能“中性”或“审慎”，confidence ≤ 0.4，并在 `summary` 首句声明。
- `meta.warnings` 含 finance/news/sector 任一失败：对应维度降权，`completeness` 扣分，报告中逐项列明。
