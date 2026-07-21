---
name: rei
description: Run the project's industry or sector research workflow, starting from industry-level evidence and using anchor companies only as validation when needed.
argument-hint: "target=<industry or sector> [anchor_companies=code/company list] [fiscal_year=YYYY] [depth=quick|standard|deep] [focus=supply_demand|policy|price|competition|geopolitics|hot_news|...] [deliverable_type=framework_only|evidence_pack|investment_research|theme_event_study|company_screening] [event_name=event] [event_type=type] [event_window=window] [impact_variables=price|supply|demand|profit|logistics|capex|competition|sentiment] [baseline_period=pre-event baseline] [geography_scope=region] [pricing_variable=price variable] [counterfactual=no-event baseline]"
---

# /rei

## English Edition Output Requirement

All coordinator messages, task titles, summaries, agent handoffs, generated artifacts, and the final industry research report must be in English. Chinese policy titles, company names, source quotations, and Chinese-market search queries may remain Chinese only as source data, with English explanation. Stable schema keys, enum values, file names, stock codes, URLs, dates, and evidence locators must remain unchanged.

行业或板块研究入口。这个 skill 的职责是把行业研究任务按固定链路分派给对应角色：先补行业层证据和研究框架，再按需用锚点公司做验证，最后交给 `industry-researcher` 输出正式行业判断。主会话只做调度、回流和汇总。

## 执行原则

- 主会话只负责：识别行业主题、盘点行业层证据、决定是否需要锚点公司、分派行业链路与公司链路、整合结果。
- 行业研究默认先走“行业证据优先”链路：先收集政策、价格、统计、供需、库存、开工率、装机、进出口、贸易和事件等行业层证据，先形成行业框架，再决定如何使用锚点公司。
- 锚点公司是验证层样本，用于校验产业链位置、业务敞口、盈利弹性和反例；不得把公司财报或公司分析当作行业结论的默认起点。
- 如果用户未提供锚点公司，不要一上来就补公司链路；只有在行业框架初步形成且确有必要时，才筛选 1-3 家相关 A 股公司做验证，除非用户明确只要研究框架。
- 具体职能必须优先委派给项目内 custom agents，而不是由主会话临时做行业分析，或退化成 generic subagent。
- 只要任务落在已定义角色边界内，不得使用 generic / general-purpose / Explore 代替 custom agents；只有角色边界无法覆盖时才允许降级。
- 下列脚本是对应 custom agents 的工具，不是主会话的默认动作：
  - 行业层证据与输入包组装 → `industry-info-collector`
  - 公司研究补齐 → `/rec` 链路中的 `information-collector` / `information-processor` / `financial-analyst`
  - `run_industry_collection.py` → `industry-info-collector`
- 如果用户没有特别说明，默认 `deliverable_type=investment_research`，即目标是“可供 PM/IC 讨论的买方行业研究”，而不是默认只给框架摘要。
- 正式行业判断由 `industry-researcher` custom agent 承担；主会话只汇总其结论、验证样本差异、缺口和下一步。

## 参数

- `target`：行业名、板块名或主题名。
- `anchor_companies`：可选的 1-3 家验证型锚点公司，支持股票代码或公司名。
- `fiscal_year`：财报年度；未指定时先查锚点公司可用的最近完整财报年度。
- `depth`：`quick`、`standard`、`deep`，默认 `standard`。
- `focus`：可选重点，如 `supply_demand`、`policy`、`price`、`competition`、`geopolitics`、`hot_news`、`localization`。
- `as_of_date`：行业输入包生成日期；未指定时使用当前日期。
- `deliverable_type`：
  - `framework_only`：只要行业框架；
  - `evidence_pack`：只要证据包和变量表；
  - `investment_research`：需要形成可讨论的买方行业判断；
  - `theme_event_study`：重点研究政策、战争、热点、供应冲击、技术范式迁移、前沿科技突破、资本开支主线等事件或外部驱动影响；
  - `company_screening`：从行业出发筛选或比较公司。
- `event_name`：事件名，例如战争、集采、出口管制、资本开支主线、技术突破。
- `event_type`：事件类型，例如 `war_geopolitics`、`policy_regulation`、`trade_restriction`、`supply_disruption`、`demand_shock`、`logistics_disruption`、`technology_paradigm_shift`、`frontier_technology_breakthrough`、`capex_cycle`、`social_hotspot`。
- `event_window`：事件观察窗口，例如未来 3 个月、6-12 个月或特定日期区间。
- `impact_variables`：希望重点判断的变量，例如 `price`、`supply`、`demand`、`profit`、`logistics`、`capex`、`competition`、`sentiment`。
- `baseline_period`：事件前基线窗口，用于比较“有事件”和“无事件时本应如何”。
- `geography_scope`：事件影响的区域范围，例如全球、中国进口链、北美云厂商。
- `pricing_variable`：若用户关心定价、ASP、费率、价差或中标价，显式指定目标价格变量。
- `counterfactual`：无事件时的基线假设，用于构造反事实比较。

## 无锚点公司时

如果用户只给行业名，没有给锚点公司：

1. 主会话先把任务交给 `industry-info-collector`，优先补行业层证据与研究框架。
2. 先明确：行业定义、产业链拆分、关键变量、近期政策/价格/供需/库存/开工率/装机/贸易/事件缺口。
3. 只有在需要把行业判断映射到 A 股公司时，才筛选 1-3 家可作为验证样本的公司，并说明选择理由和局限。
4. 如果用户明确要求研究“事件对高相关公司股价的影响”，则高相关公司不再是可选验证样本，而是必需验证样本；除非用户已经指定公司，否则主会话或 `industry-info-collector` 必须筛选 1-2 家高相关 A 股公司。
5. 除非用户明确只要研究框架，否则在行业框架初步形成后，可按需把这些样本分派到公司研究链路，补业务敞口、财务弹性、股价反应和反例验证。
5. 如果行业层证据尚未成形，只能输出行业框架、当前缺口和待验证变量，不能用公司链路半成品倒推出完整行业结论。
6. 如果当前确实无法识别合适的 A 股验证样本，必须明确说明原因、候选方向和当前缺口，而不是跳过行业层研究或直接硬写结论。

## 锚点公司选择逻辑

当需要为行业研究选择 A 股锚点公司时，主会话或 `industry-info-collector` 不得只按市场热度选择，必须说明选择理由和局限。优先选择能形成横向验证的 1-3 家公司：

1. 至少覆盖以下维度中的两个：
   - 产业链环节差异；
   - 行业地位差异；
   - 业务纯度差异；
   - 盈利弹性差异；
   - 风险敞口差异。
2. 对每家锚点公司必须说明：
   - 选择理由；
   - 对目标行业的真实收入、毛利、产能、订单、AUM、客户或资本开支敞口；
   - 能验证的行业问题；
   - 不能代表行业整体的局限。
3. 如果只选择 1 家锚点公司，必须说明为什么 1 家足够；除非另有充分独立行业层数据支持，否则最终行业结论原则上不得标为高置信 `completed`。
4. 如果用户指定的锚点公司行业敞口较弱，必须把它标记为“主题映射样本”或“低纯度样本”，不得默认作为行业代表公司。

## 行业证据成熟度 Gate

`industry-info-collector` 返回后，主会话不得直接把结果包装为正式行业研究，除非输入包至少满足以下条件：

### 对 `investment_research` / `company_screening`

- 行业边界和细分环节已定义；
- 至少覆盖以下 5 类证据中的 3 类，并尽量量化：
  - 需求或业务活动；
  - 供给、产能、库存、开工率或其他供给侧代理变量；
  - 价格、价差、费率、利润率或其他盈利代理变量；
  - 竞争格局、份额、排名或同行结构；
  - 政策、监管、贸易、战争或重大事件；
- 至少包含 3 个核心量化变量，且带时点和来源；
- 明确列出缺失变量及其对结论的影响。

### 对 `theme_event_study`

- 至少包含关键事件时间线；
- 至少包含事件前基线或“无事件时本应如何”的反事实描述；
- 至少说明事件影响供给、需求、物流、价格、成本、资本开支、竞争、订单、交付、利润或情绪中的哪个环节；
- 若用户关心价格、价差、ASP、费率、中标价、毛利率或利润，必须至少说明对应的定价/利润形成机制；
- 至少给出 1 个可跟踪证伪指标；
- 必须明确写出当前是“已观察到的传导”“预期中的传导”还是“仅题材映射”。

如果不满足以上条件，当前轮次状态必须保持为 `partial` 或 `blocked`，只能先输出“行业框架 + 缺口 + 补证清单”，不得要求 `industry-researcher` 硬写高置信完整行业结论。

但这里的 `partial` / `blocked` 只是链路中的中间状态，不得直接作为最终用户交付。除非命中终止条件，否则主会话必须继续调度补证，并在后续轮次里把新增证据回灌到最终报告。

## 强结论许可与自动降级规则

`/rei` 是行业研究链路的总闸门。这里的“自动降级”指的是：一旦发现关键证据不够，就必须先下调结论强度，不能继续往下硬写成高置信正式行业结论。

自动降级只影响结论强度，不影响最终交付形态。主会话必须继续补证，直到：
- 证据补到足以形成高置信完整研究；
- 或达到终止条件，改为交付完整但低置信 / 带缺口的研究报告。

只要命中以下任一情况，`/rei` 就不得要求 `industry-researcher` 输出高置信正式行业结论：

1. `can_support_full_research=false`；
2. `quantitative_variable_table` 少于 3 个核心量化变量；
3. `package_quality_gate.industry_evidence_coverage=低`；
4. `company_evidence_ratio=高` 且行业层证据偏弱；
5. `gaps` 里明确缺少需求、供给、价格/盈利、竞争格局中的关键项；
6. 只有题材样本、低纯度样本或和行业关系不够直接的锚点公司；
7. 只有单点数据，没有历史比较，却想下“企稳”“拐点”“反转”“景气改善”这类强结论。

当 collector 未过关时，`/rei` 的动作不再是“写个 partial 就结束”，而是按下面顺序续跑：
- 先继续调用 `industry-info-collector` 补行业层证据；
- 若缺口落在公司敞口、订单、产能、盈利传导、客户认证、股价反应或反例验证，再按需触发 `/rec` 公司链路；
- 补证后重新检查 Gate，并决定是否进入下一轮；
- 只有在命中终止条件后，才允许停止补证并生成最终完整报告。

如果因为流程需要仍然调用 `industry-researcher`，主会话也必须把任务写成二选一：
- 当前轮次：只允许输出“部分研究”“框架草稿”或“待验证假设”，并明确缺口、建议上游和需更新章节；
- 终止轮次：允许输出完整但低置信 / 带缺口的研究报告，但不得把未证实内容写成高置信结论。

下列材料不能单独视为行业正式结论的主证据：
- 单家公司财报表述；
- 单家公司订单或涨价说法；
- 单条新闻；
- 二手观点转述；
- 题材热度；
- 没有样本框的渠道纪要；
- 无法映射到行业总量的弱代理变量。

它们只能作为：
- 辅助线索；
- 待验证假设；
- 题材映射证据。

## 降级后的处理协议

一旦行业链路被降级，不代表停在原地。`/rei` 必须先继续补证，再决定最终报告的置信度；不允许把空的 `partial`、单独的补证清单或观察清单直接当作最终交付。

主会话必须遵循下面的强制续跑协议：

1. `继续补证`：这是默认动作。优先让 `industry-info-collector` 按 `gaps` 补行业层证据；若缺口指向公司验证层，则继续调度 `/rec` 链路。
2. `用替代证据给临时判断`：只有在已执行至少一轮补证后，且直接数据仍不可得时，才允许使用；同时必须说明当前判断靠哪些旁证撑起来、哪些强结论仍然不能说。
3. `转成观察清单`：只有在命中终止条件、短期内确实拿不到关键数据时，才允许作为完整报告中的一个章节，而不是替代整份报告。

`/rei` 不允许只返回一个空的 `partial` 就结束。只要结果被降级，就必须：
- 明确当前处于第几轮补证；
- 明确下一轮由哪个角色补哪个缺口；
- 在终止条件触发前继续执行；
- 在终止条件触发后交付完整报告，并单列低置信结论、未证实假设和不可证实部分。

终止条件如下：
- 每个主要证据缺口最多补证 2 轮；
- `industry-info-collector` 最多追加 2 轮；
- 公司验证链路最多追加 1 轮，除非用户明确要求继续；
- 连续两轮无法拿到关键来源，或新增证据不再改变核心判断；
- 剩余缺口只能依赖私有、付费、未公开或当前环境不可得的数据；
- 用户任务具有时点性，需要在当前轮次完成交付。

如果主会话选择了第 2 条路或命中终止条件，就必须明确把最终内容分成四层：
- 已经被证据确认的部分；
- 基于替代证据的低置信判断；
- 目前只是待验证假设的部分；
- 现在还不能说或无法证实的部分。

## 标准执行链路

### 1. 主会话先委派 `industry-info-collector`

`industry-info-collector` 先检查是否已有行业层输入包或可复用的本地行业资料，优先收集和组装：

- 行业边界、细分环节、地域范围和统计口径；
- 政策/监管文件；
- 价格与行情快照；
- 行业公开统计（产量、出货、库存、装机、进出口、开工率等）；
- 需求、供给、价格/盈利、竞争格局、政策/事件五类核心变量；
- 贸易、地缘、热点事件时间线；
- 若为 `theme_event_study`，还必须优先组装 `event_study_request` 对应的事件定义、事件前基线、反事实、传导链、价格/利润机制、观察指标和证伪指标；
- 可用时再接入锚点公司的公司研究产物作为验证材料。

如果行业层关键证据缺失，它必须先返回行业框架、缺口、核心变量和建议补证方向，而不是把公司财报当作默认主证据。

它可以按需调用：

```bash
python "industry_info_collector_scripts/run_industry_collection.py" --stock-code <code> --company-name <name> --fiscal-year <year> --as-of-date <YYYY-MM-DD> --industry-name <industry>
```

如果当前脚本更偏公司输入包，但本次任务是纯行业研究，`industry-info-collector` 仍应优先组装行业层输入包，不得因为缺少锚点公司就把行业研究退化为公司研究。

### 2. 主会话检查成熟度 Gate

主会话拿到 `industry-info-collector` 的返回后，先检查：

- `package_quality_gate`
- `quantitative_variable_table`
- `gaps`
- `can_support_full_research`

如果行业层证据仍不够，不能把当前轮次结果直接当成最终交付。主会话必须继续补证，至少再执行一轮上游收集；只有在命中终止条件后，才允许停止补证并转为“完整但低置信 / 带缺口”的最终报告。无论哪种情况，都不能直接让 `industry-researcher` 用空洞定性语言补齐缺失部分。

### 3. 主会话按需把验证样本分派到公司研究链路

当行业框架初步形成后，如果需要验证产业链位置、业务敞口、盈利弹性、股价反应或反例，主会话再检查每家锚点公司是否已有：

- `analyst_report.json`
- `content.json`
- `llm_digest.json`
- `rag_index/rag_chunks.jsonl`
- `summary_comparison.json`

如果缺失，主会话不要自己补公司证据，而是把该公司任务分派到 `/rec` 链路，由：

- `information-collector`
- `information-processor`
- `financial-analyst`

依次补齐。

这一层的目的，是用公司研究验证和细化行业判断，而不是让公司研究取代行业研究本身。若公司验证材料无法转成收入敞口、利润弹性、产能/订单/AUM/客户等横向可比字段，只能作为背景参考，不能作为强验证样本。

若用户关心事件对公司股价的影响，还必须尽量补齐以下两层：
- 事件发生前到 `as_of_date` 之间，已发生的股价、成交额、估值、公司公告、订单或经营变量变化；
- `as_of_date` 之后，哪些影响只是未来推测，哪些变量会决定价格是否继续反应。

### 4. 主会话按需再次委派 `industry-info-collector`

当行业层证据仍有缺口、公司验证材料刚补齐、或上一轮补证已改变核心判断时，主会话必须按需再次委派 `industry-info-collector` 更新或完善：

- `industry_input_package.json` / `industry_input_package.md`
- `evidence_table.json`
- `collection_audit.json`

输入包中必须区分：

- `industry_level_evidence`
- `company_validation_evidence`
- `market_theme_evidence`
- `unsupported_assumptions`

若为 `theme_event_study`，输入包还必须尽量包含：

- `event_study.event_metadata`
- `event_study.baseline_and_counterfactual`
- `event_study.event_timeline`
- `event_study.transmission_chain`
- `event_study.pricing_mechanism`（若价格/利润是重点）
- `event_study.observed_vs_expected_impacts`
- `event_study.falsification_indicators`
- `event_study.event_specific_gaps`

### 5. 主会话委派 `industry-researcher`

当一个或多个 `industry_input_package.json` 可用后，正式行业判断由 `industry-researcher` custom agent 承担。若已命中终止条件但仍存在关键缺口，主会话也必须委派 `industry-researcher` 产出一份完整但低置信 / 带缺口的最终报告，而不是停在框架草稿。传给它：

- 行业名称、`deliverable_type`、研究重点和必答问题；
- 行业层关键证据路径（政策、价格、统计、事件、供需信号等）；
- 若为 `theme_event_study`，显式传入 `event_study_request`，其中至少包含 `event_name`、`event_type`、`impact_variables`、`baseline_period`、`event_window`、`geography_scope`，以及按需提供的 `pricing_variable`、`counterfactual`；
- 锚点公司列表（如有）；
- 每家公司的 `industry_input_package.json/md`；
- `evidence_table.json`；
- `collection_audit.json`；
- 相关公司财务研究摘要路径（如有）。

要求 `industry-researcher` 输出：

- 行业定义与产业链位置；
- 核心量化指标表；
- 纵向比较：历史位置、同比/环比、边际变化；
- 景气阶段与驱动因素；
- 供需平衡和关键约束；
- 价格/利润传导链；
- 竞争格局与横向比较；
- 锚点公司的行业位置、真实业务敞口、受益路径和局限；
- 哪些是基本面传导，哪些只是主题映射或情绪映射；
- 若用户关心高相关公司股价影响，还必须区分：事件发生前后到当前节点的已兑现事实，以及当前节点之后的未来推测；
- 核心假设、跟踪指标和证伪条件；
- 情景分析、置信度和缺口。

### 6. 主会话汇总结果

主会话最后的汇总必须结论前置：先给用户能直接对照的行业判断，再给支撑证据，最后才是可靠性与状态字段。禁止把状态字段、调用清单或补数清单堆在开头。

**第一层：结论区（置顶）**

- 行业结论：行业边界、景气、关键事件、供需、价格/利润、竞争、公司位置；证据不足时给低置信临时判断并说明靠什么旁证支撑，不许只给框架；
- `top_3_variables`：最影响当前判断的三个变量；
- `top_3_tracking_indicators`：后续最该跟踪的三个指标；
- `what_would_change_the_view`：哪些证据会改变当前判断；
- 若用户关心高相关公司股价影响，还必须交代：哪些变化在 `as_of_date` 前已经发生、哪些可能已经被市场交易、哪些还只是 `as_of_date` 之后的未来影响。

**第二层：研究正文**

- 锚点公司：列公司、代表性、纯度和验证状态；
- 缺口：缺哪些行业数据或公司证据，以及这些缺口会让判断偏乐观还是偏悲观。

**第三层：可靠性与状态（置于最后，作为限定语）**

- 目标：行业、板块或主题；
- `deliverable_type`；
- 已调用角色：`industry-info-collector`、`industry-researcher`，以及按需调用的公司链路角色；
- 复用产物：关键路径；
- 新生成产物：关键路径；
- `research_status`：完整研究、部分研究、框架草稿或证据不足；
- `delivery_status`：高置信完整报告、低置信完整报告或带缺口完整报告；
- `quantification_status`：已量化变量数量、核心缺失变量；
- `comparability_status`：是否完成纵向和横向比较；
- `actionability_status`：可执行、仅观察、需补证或不可执行；
- 置信度：高/中/低及原因；
- `next_best_step`：如果结果被降级，明确写出接下来是继续补证、用替代证据给临时判断，还是转成观察清单。
- 下一步：是否需要更多行业证据、更多验证样本、估值分析或反方审查。

无论 `research_status` 是完整研究、部分研究还是框架草稿，只要本轮已经命中终止条件，主会话都必须把上述信息组织成一份**结论前置的完整报告**交付给用户，而不能只返回框架、缺口或待办清单。可靠性可以低，结论不能缺。

## 战争、政策、热点或主题研究

如果用户明确要求研究战争、地缘冲突、政策变化、市场热点新闻或主题催化对行业的影响，或者目标行业本身就是资源品、工业气体、能源、军工、航运、半导体材料等地缘敏感方向：

- 主会话必须把相关内容写进 `focus`、当前状态和缺口，不得只给静态产业链框架。
- 主会话只负责把重点和缺口传给 `industry-researcher`，不直接代写这部分正文。
- `industry-researcher` 必须说明传导链条：
  1. 最近关键事件时间线，哪些是新增变量，哪些只是旧叙事重复；
  2. 事件前行业基线和“无事件时本应如何”的反事实；
  3. 事件如何影响供给、需求、物流、价格、资本开支、竞争、订单、交付、成本、利润或下游扩产节奏；
  4. 若用户关心价格、ASP、费率、中标价、毛利率或利润，必须说明对应定价或利润形成机制；
  5. 哪些锚点公司有真实业务敞口，敞口在收入、毛利、产能、气源、订单或资本开支哪个层面体现；
  6. 哪些只是主题映射或市场情绪映射，不能直接外推到盈利；
  7. 哪些变量可以在后续跟踪中证伪。
- 如果事件已经成为当前景气判断的一等变量，但仍缺少价格、库存、开工率、订单、利润或其他可观察落地变量，必须把结论降级为“事件驱动假设待验证”，不能伪装成已兑现的基本面结论。
