# 公司研究产物复用设计

## 背景

公司研究链路已经有“先查已有产物、缺什么补什么”的原则，但该原则分散在文档和角色提示中，缺少一个统一、可执行、可审计的预检入口。结果是用户第二次询问同一股票时，仍可能重复触发财报采集、PDF 解析、digest、RAG 或财务分析，消耗时间和 token。

## 目标

新增公司研究状态审计器，并把它作为 `/rec` 公司研究链路的第 0 步：先盘点已有产物，再决定复用、跳过、补齐或刷新。默认只补 `missing`、`partial`、`stale`、`incompatible` 的层，不无脑重跑整条链路。

## 范围

本次只实现方案 B：

1. 新增 `research_orchestrator_scripts/audit_company_research_state.py`。
2. 输出结构化 `research_state`，包含产物路径、复用状态、跳过动作和下一步动作。
3. 更新 `/rec`、`/re`、`AGENTS.md` 和相关 custom agent 规则，使公司研究必须先审计再续跑。
4. 不重写完整 orchestrator，不替代 custom agents 执行研究职责。

## 产物层级

- L0 财报采集层：manifest、正式年报 PDF、摘要 PDF。
- L1 信息处理层：`content.json/md`、`llm_digest.json/md`、`digest_audit.json`、`rag_index/rag_chunks.jsonl`、`summary_comparison.json/md`。
- L2 财务证据草稿层：`analyst_report.json/md`、`evidence_check.json`、`analyst_audit.json`。
- L3 正式财务分析层：`formal_financial_analysis.json/md`。
- L4 估值层：`valuation_report.json/md`、`valuation_evidence_table.json`、`valuation_audit.json`，按 `as_of_date` 判断新鲜度。

## 状态语义

- `ready`：产物完整，可复用。
- `partial`：已有部分产物，只补缺失子产物。
- `missing`：没有可用产物，需要上游生成。
- `stale`：估值或行情类产物日期旧，只更新时效层。
- `incompatible`：已有产物与本次 `depth` 或 `focus` 不兼容，复用为底稿但需要补充分析。
- `blocked`：上游层未 ready，下游暂不应执行。
- `ambiguous`：目标无法唯一识别，需要补股票代码或年度。

## 默认复用规则

- PDF 和解析产物按同一份财报强复用，除非缺失、损坏或显式 `force_refresh=true`。
- 财务证据草稿按同一股票、年度、报告类型复用；如果 `depth` 升级或新增 `focus`，不重跑上游，只要求财务分析员做补充。
- 正式财务分析存在且兼容时直接复用。
- 估值按 `as_of_date` 判断；旧估值只能作为历史参考，不能当作当前估值。

## 调度要求

`/rec` 的第一步必须先调用：

```bash
python "research_orchestrator_scripts/audit_company_research_state.py" \
  --target <公司或代码> \
  --report-year <YYYY> \
  --report-type annual \
  --depth <quick|standard|deep> \
  --focus <focus> \
  --as-of-date <YYYY-MM-DD> \
  --write-state
```

之后主会话只根据 `next_actions` 委派对应 custom agents。最终输出必须列明 `reused_artifacts`、`new_artifacts`、`skipped_actions` 和仍需补齐的缺口。
