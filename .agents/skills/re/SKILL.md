---
name: re
description: Unified research entrypoint for routing A-share company research or industry/sector research through this project's multi-agent workflow, with industry research defaulting to industry-first evidence collection before company validation.
argument-hint: "mode=company|industry target=<公司/股票代码/行业> [fiscal_year=YYYY] [depth=quick|standard|deep] [anchor_companies=代码或公司列表] [deliverable_type=framework_only|evidence_pack|investment_research|theme_event_study|company_screening] [event_name=事件] [event_type=事件类型] [event_window=观察窗口] [impact_variables=price|supply|demand|profit|logistics|capex|competition|sentiment] [baseline_period=事件前基线窗口] [geography_scope=区域] [pricing_variable=价格变量] [counterfactual=无事件时的基线假设]"
---

# /re

统一研究入口。这个 skill 只负责识别用户意图并把任务路由到正确链路，不直接承担信息收集、信息处理、财务分析或行业研究职能。

## 执行原则

- 主会话只负责：解析请求、先盘点已有产物、分派 custom agents、处理回流、汇总结果；公司研究必须继承 `/rec` 的 `research_state` 预检和复用规则。
- 具体职能必须优先委派给项目内已定义的 custom agents，而不是让主会话临时扮演角色。
- 若任务已落入既有角色边界，必须优先使用：`information-collector`、`information-processor`、`financial-analyst`、`valuation-analyst`、`market-context-collector`、`industry-info-collector`、`industry-researcher`，不得用 generic / general-purpose / Explore 代替。
- 只有当任务不落入任何已定义角色边界时，才退回 generic subagent 或主会话直接处理。
- `/re` 自身不展开长文档全文，不在这一层生成正式公司或行业研究正文。
- 如果用户没有特别说明，行业研究默认 `deliverable_type=investment_research`，即以“可供 PM/IC 讨论的买方行业研究”为默认交付，而不是默认只交行业框架。

## 参数

- `mode`：`company` 或 `industry`。如果用户自然语言已经明确，可不显式传。
- `target`：公司名、股票代码、行业名或板块名。
- `fiscal_year`：财报年度；未指定时先查已有产物并选择最近可用年度。
- `depth`：`quick`、`standard`、`deep`，默认 `standard`。
- `run_industry`：公司研究后是否顺带做行业位置分析，默认 `false`。
- `run_valuation`：当前阶段只记录需求，不默认执行估值链路。
- `anchor_companies`：行业研究的可选验证样本，支持 1-3 家公司或股票代码。
- `deliverable_type`：
  - `framework_only`：只要行业框架；
  - `evidence_pack`：只要证据包和变量表；
  - `investment_research`：需要形成可讨论的买方行业判断；
  - `theme_event_study`：重点研究政策、战争、热点、供应冲击、技术范式迁移、资本开支主线等事件或外部驱动影响；
  - `company_screening`：从行业出发筛选或比较公司。
- `event_name`：事件名，例如战争、集采、出口管制、资本开支主线、技术突破。
- `event_type`：事件类型，例如 `war_geopolitics`、`policy_regulation`、`trade_restriction`、`supply_disruption`、`technology_paradigm_shift`、`frontier_technology_breakthrough`、`capex_cycle`、`social_hotspot`。
- `event_window`：事件观察窗口，例如未来 3 个月、6-12 个月或特定日期区间。
- `impact_variables`：希望重点判断的变量，例如 `price`、`supply`、`demand`、`profit`、`logistics`、`capex`、`competition`、`sentiment`。
- `baseline_period`：事件前基线窗口，用于比较“有事件”和“无事件时本应如何”。
- `geography_scope`：事件影响的区域范围，例如全球、中国进口链、北美云厂商。
- `pricing_variable`：若用户关心定价、ASP、费率、价差或中标价，显式指定目标价格变量。
- `counterfactual`：无事件时的基线假设，用于构造反事实比较。

## 路由规则

### 公司研究

当用户说“研究某家公司”“分析某个股票”“看一下某公司基本面”时：

1. 主会话只负责识别目标、年份、重点、`as_of_date`、`force_refresh` 和已有产物状态。
2. 主会话把任务分派到 `/rec` 对应链路；不得在 `/re` 层绕过 `/rec` 的 `research_state` 审计。
3. `/rec` 必须先运行 `research_orchestrator_scripts/audit_company_research_state.py`，并按 `research_state.reusable`、`research_state.skipped_actions`、`research_state.next_actions` 继续委派。
4. 由公司链路按需继续委派：
   - `information-collector`
   - `information-processor`
   - `financial-analyst`
   - `valuation-analyst`
   - `market-context-collector`
5. 对 `status=ready` 的公司研究层，`/re` 只能复用并汇总，不能升级为重新执行；只有 `missing`、`partial`、`stale`、`incompatible` 或用户显式 `force_refresh=true` 时才允许补跑对应层。市场上下文层即使 `ready`，也只能作为公开网页预期代理，不得被包装成正式一致预期。
6. 主会话最后只汇总结果，不在 `/re` 这一层自己做财务研究判断。

### 行业研究

当用户说“研究某个行业”“看一下某个板块”“从行业出发找公司”时：

1. 主会话先识别行业主题、当前时点、`deliverable_type`、重点变量、必答问题和可用行业层证据，默认先走“行业证据优先”链路，不要默认先找锚点公司。
2. 如果用户没有说明交付形态，默认按 `deliverable_type=investment_research` 执行，不默认只输出框架。
3. 主会话把任务分派到 `/rei` 对应链路。
4. 若用户请求的是“指定事件 + 指定行业”的影响研究，例如“战争对氦气未来定价的影响”“集采对医疗耗材行业的影响”，主会话必须路由为：
   - `mode=industry`
   - `deliverable_type=theme_event_study`
   - `target=<行业>`
   - 并尽量补齐 `event_name`、`event_type`、`impact_variables`、`baseline_period`、`event_window`、`geography_scope`、`pricing_variable`。
5. 由行业链路继续委派：
   - `industry-info-collector` 先收集政策、价格、统计、供需、库存、开工率、装机、进出口、贸易与事件等行业层证据，并优先结构化核心量化变量；若是 `theme_event_study`，必须额外组装事件时间线、基线/反事实、传导链、观察指标与证伪指标；
   - 若用户明确问“这个事件对高相关公司股价的影响”，或给出了 1-2 家高相关公司，主会话必须把这些公司视为必需验证样本，而不是可选样本；
   - 按需再补公司研究链路，用锚点公司验证产业链位置、业务敞口、盈利弹性、股价反应和反例；
   - `industry-researcher` 最后输出正式行业判断或事件驱动部分研究，并区分截至 `as_of_date` 的已发生事实与 `as_of_date` 之后的未来推测。
6. 主会话最后只汇总结果，不在 `/re` 这一层自己拼行业结论，也不能把公司财报直接当成行业研究起点。

## 产物优先级

优先复用这些路径，不要无脑重跑；公司研究必须先以 `research_state` 为准：

- `research_orchestrator_scripts/orchestrator_workspace/company_state/<stock_code>/<report_year>/research_state.json`
- `info_collector_scripts/collector_workspace/manifests/cninfo_all_reports.json`
- `info_processor_scripts/processor_workspace/parsed_reports/.../<report>/content.json`
- `info_processor_scripts/processor_workspace/parsed_reports/.../<report>/llm_digest.json`
- `info_processor_scripts/processor_workspace/parsed_reports/.../<report>/digest_audit.json`
- `info_processor_scripts/processor_workspace/parsed_reports/.../<report>/rag_index/rag_chunks.jsonl`
- `info_processor_scripts/processor_workspace/parsed_reports/.../<report>/summary_comparison.json`
- `financial_analyst_scripts/analyst_workspace/reports/.../<report>/analyst_report.json`
- `financial_analyst_scripts/analyst_workspace/reports/.../<report>/formal_financial_analysis.json`
- `valuation_analyst_scripts/valuation_workspace/reports/<stock_code>/<as_of_date>/valuation_report.json`
- `market_context_collector_scripts/collector_workspace/packages/<stock_code>/<as_of_date>/market_context_package.json`
- `industry_info_collector_scripts/collector_workspace/packages/<code>/<date>/industry_input_package.json`

## 行业研究降级协议

当 `mode=industry` 时，`/re` 必须带着“证据不够就降级”的协议往下走：

- 默认 `deliverable_type=investment_research` 只表示“目标是做正式研究”，不表示“证据一定够，结论一定能给”。
- 如果行业链路返回的结果是“部分研究”“只给框架”“证据不足”“先观察”或同义状态，`/re` 只能原样汇总或继续下调，不能升级包装成正式行业判断。
- 如果关键证据不够，`/re` 允许的最高输出只能是：
  - “只给框架”；
  - “证据清单”；
  - “部分研究”；
  - “先观察”。
- 只要 `/rei` 明确说行业层证据没成形，`/re` 就不得把结果继续包装成 `investment_research` 已完成，只能明确写出缺口和下一步补证方向。
- 一旦结果被降级，`/re` 不能停在“证据不足”这四个字上，必须明确给出下一步怎么继续。下一步只能是三种之一：
  - `继续补证`；
  - `用替代证据给临时判断`；
  - `转成观察清单`。

## 输出要求

最终回复必须结论前置：先给用户能直接对照的判断，再给支撑证据，最后才是可靠性与状态字段。禁止把 `research_state`、调用清单或补数清单堆在开头。

**结论区（置顶）**：

- 公司研究：按 `/rec` 第 10 节和 CLAUDE.md 7.1 输出一句话估值判断、现价与估值位置、三档合理价值、上下行空间、核心假设和估值证伪条件；缺数据时按 CLAUDE.md 7.2 最佳估计强制条款给低置信结论，不许只给框架。
- 行业研究：先给当前最可用的行业判断（景气方向、关键变量位置、受益/受损映射或"证据不足下的临时判断"），再交代这个判断靠什么支撑、还缺什么。

**可靠性与状态（置于结论和研究正文之后）**：

- 当前目标。
- `deliverable_type`。
- `research_state`：若为公司研究，列出状态文件路径、各层状态、`reusable`、`skipped_actions` 和 `next_actions`。
- 已调用哪些 custom agents，且只列出本次实际调用的角色。
- 跳过了哪些动作，以及跳过原因。
- 复用了哪些关键产物。
- 新生成了哪些关键产物。
- `research_status`：完整研究、部分研究、框架草稿或证据不足。
- `quantification_status`：已量化的核心变量数量、未量化的关键变量。
- `comparability_status`：是否完成纵向或横向可比。
- `actionability_status`：可执行、仅观察、需补证或不可执行。
- 当前还有哪些缺口。
- 结论的置信度。
- `next_best_step`：如果结果被降级，明确写出接下来是继续补证、用替代证据给临时判断，还是转成观察清单。
- 建议下一步。

如果证据不足，必须明确降级为“初步框架”“证据包”或“部分结论”，不能把半成品包装成高置信最终研究；但降级只限定结论强度，不允许因此取消结论区、把状态字段挪回开头。若最终输出包含行业研究结论，必须带上关键跟踪变量和证伪条件，不能只输出泛化风险提示。

对 `theme_event_study`，主会话必须特别区分：
- 事件事实；
- 事件前基线；
- 截至 `as_of_date` 已经发生的基本面影响；
- 截至 `as_of_date` 已经发生的高相关公司股价/成交/估值/公司公告变化；
- `as_of_date` 之后仍只是预期传导或未来推测的部分；
- 哪些变量会证伪当前判断。

如果用户要求看到高相关公司的股价影响，主会话不得只给行业结论，必须额外说明：
- 事件发生前该公司所处的位置；
- 事件发生后到当前节点之间，已经发生了哪些股价、成交、估值或公司层面的变化；
- 当前价格里哪些可能已经被市场交易，哪些影响还只是未来推测。

`/re` 不得为事件研究新建独立链路；所有事件+行业研究默认都进入现有 `/rei` 行业链路。
