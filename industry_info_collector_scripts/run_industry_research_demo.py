"""行业研究员通用样例验证脚本。

该脚本不是正式行业研究员 Agent，而是用信息收集员2生成的输入包做一次
可复现的规则化演示，验证输入包是否足以支持行业研究员提出正确问题、识别限制、
并向下游 Agent 输出不越界的研究指令。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="运行行业研究员样例验证")
    parser.add_argument("--package", required=True, help="industry_input_package.json 路径")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    """读取 JSON 文件。"""

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def now_iso() -> str:
    """返回北京时间 ISO 时间。"""

    return datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()


def build_demo_report(package: dict[str, Any]) -> dict[str, Any]:
    """基于输入包生成通用行业研究样例报告。

    Args:
        package: 信息收集员2生成的行业研究输入包。

    Returns:
        通用样例行业研究报告。
    """

    company = package["company"]
    info = package["information_package"]
    classification = info.get("industry_classification", {})
    competitors = info.get("competitors", [])
    industry_data = info.get("industry_data", {})
    financial = info.get("financial_summary", {})
    market_data = info.get("market_data", {})
    public_stats = industry_data.get("public_stats", [])
    industry_signals = industry_data.get("industry_signals", [])
    policies = info.get("policy_and_regulation", [])
    business_segments = info.get("business_segments", [])
    limitations = package.get("limitations", [])
    risks = build_generic_risks(public_stats, industry_signals, policies, market_data, business_segments)
    market_values_available = market_snapshot_has_values(market_data)
    confidence = "medium" if public_stats and market_values_available else "low"
    return {
        "schema_version": "1.1",
        "generated_at": now_iso(),
        "demo_type": "generic_industry_researcher_flow_validation",
        "company": company,
        "executive_summary": {
            "one_sentence_conclusion": f"{company['name']} 的初始行业归属为 {classification.get('primary_industry', '未知行业')} / {classification.get('secondary_industry', '未知细分行业')}；当前输入包可支持行业研究员建立研究框架、识别证据缺口和向下游提出补数要求，但是否能形成高置信行业结论取决于行业统计、行业信号、政策和行情估值数据是否完整。",
            "industry_view": "preliminary_framework_only",
            "confidence": confidence,
            "no_trade_decision": True,
        },
        "industry_classification_check": {
            "primary_industry": classification.get("primary_industry"),
            "secondary_industry": classification.get("secondary_industry"),
            "basis": classification.get("classification_basis", []),
            "assessment": "行业归属可作为初始研究口径；若仍为未知行业，行业研究员必须先补充行业分类、业务分部和利润来源。",
        },
        "financial_to_industry_questions": build_financial_questions(financial, industry_data, market_data),
        "peer_candidates_check": {
            "peers": competitors,
            "assessment": "同行候选如存在，只能作为初步竞争格局线索；同行年报、同行财务分析和最终估值可比公司筛选由其他 Agent 继续完成。" if competitors else "本包未提供同行候选；这不阻塞行业输入包生成，但行业研究员需要从原信息收集员和财务分析员获取同行资料。",
        },
        "data_coverage_check": {
            "company_events_count": len(info.get("company_events", [])),
            "policy_record_count": len(policies),
            "public_stat_count": len(public_stats),
            "industry_signal_count": len(industry_signals),
            "market_data_status": market_data.get("collection_status"),
            "business_segment_count": len(business_segments),
        },
        "industry_metrics_to_collect_next": build_next_metrics(industry_data, package.get("recommended_next_collection", [])),
        "risks": risks,
        "downstream_instructions": build_downstream_instructions(market_data, public_stats, industry_signals, competitors),
        "limitations": limitations + [
            "本样例报告由规则化脚本生成，只用于验证信息链路，不替代正式 LLM 行业研究员。",
            "该脚本不会输出买入、卖出、仓位或目标价。",
        ],
        "quality_check": {
            "real_industry_identified": bool(classification.get("primary_industry")) and classification.get("primary_industry") != "未知行业",
            "peer_candidates_available": len(competitors) >= 3,
            "risks_count": len(risks),
            "no_buy_sell_position_target_price": True,
            "market_data_available": market_values_available,
            "market_adapter_connected": market_data.get("collection_status") == "available_from_local_file",
            "ready_for_llm_industry_researcher": True,
        },
    }


def market_snapshot_has_values(market_data: dict[str, Any]) -> bool:
    """判断行情估值快照是否包含至少一个核心数值。"""

    if market_data.get("collection_status") != "available_from_local_file":
        return False
    price = market_data.get("price_snapshot") or {}
    valuation = market_data.get("valuation_snapshot") or {}
    return any(
        value not in {None, ""}
        for value in [
            price.get("price"),
            valuation.get("market_cap"),
            valuation.get("pe_ttm"),
            valuation.get("pb"),
            valuation.get("ps_ttm"),
            valuation.get("ev_ebitda"),
            valuation.get("dividend_yield"),
        ]
    )


def build_financial_questions(financial: dict[str, Any], industry_data: dict[str, Any], market_data: dict[str, Any]) -> list[dict[str, str]]:
    """根据财务摘要和行业数据覆盖生成通用追问。"""

    revenue_yoy = ((financial.get("revenue") or {}).get("yoy") or {}).get("value", "缺失")
    cash_yoy = ((financial.get("operating_cash_flow") or {}).get("yoy") or {}).get("value", "缺失")
    questions = [
        {
            "fact": f"营业收入同比 {revenue_yoy}，经营现金流同比 {cash_yoy}。",
            "industry_question": "这些变化是否与行业需求、价格、库存、供给、渠道或政策变化一致？",
        }
    ]
    if not industry_data.get("public_stats"):
        questions.append({"fact": "缺少行业公开统计数据。", "industry_question": "无法量化公司增速相对行业的强弱，需要补充行业规模、销量、价格或渗透率数据。"})
    if market_data.get("collection_status") != "available_from_local_file":
        questions.append({"fact": "缺少行情估值快照。", "industry_question": "估值分析员无法判断市场是否已经反映行业预期，需要补充价格、市值和估值倍数。"})
    return questions


def build_generic_risks(
    public_stats: list[dict[str, Any]],
    industry_signals: list[dict[str, Any]],
    policies: list[dict[str, Any]],
    market_data: dict[str, Any],
    business_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """根据输入包覆盖度生成通用反方风险。"""

    risks: list[dict[str, Any]] = []
    unfavorable = [signal for signal in industry_signals if signal.get("direction") in {"down", "mixed", "unknown"}]
    for signal in unfavorable[:2]:
        risks.append(
            {
                "risk": f"行业信号不利或不确定：{signal.get('metric_name')}",
                "why_it_matters": signal.get("summary") or "行业信号方向不明确，可能影响行业景气和公司经营假设。",
                "early_indicators": [signal.get("metric_name"), signal.get("signal_type")],
            }
        )
    if policies:
        risks.append(
            {
                "risk": "政策或监管变化风险",
                "why_it_matters": "政策监管可能影响需求、供给、成本、价格、准入或合规要求，行业研究员需要回到政策原文判断影响方向。",
                "early_indicators": [policy.get("title") for policy in policies[:3]],
            }
        )
    if not public_stats:
        risks.append(
            {
                "risk": "缺少行业公开统计导致周期位置无法量化",
                "why_it_matters": "没有行业规模、销量、价格、库存或渗透率数据时，无法判断公司变化是行业共性还是个体 Alpha。",
                "early_indicators": ["行业规模", "销量/订单", "价格/库存", "渗透率"],
            }
        )
    if market_data.get("collection_status") != "available_from_local_file":
        risks.append(
            {
                "risk": "缺少市场估值快照",
                "why_it_matters": "没有价格、市值和估值倍数时，不能判断市场是否已经透支或低估行业逻辑。",
                "early_indicators": ["股价", "市值", "PE/PB/PS", "股息率"],
            }
        )
    if business_segments and business_segments[0].get("segment_name") == "未结构化提取":
        risks.append(
            {
                "risk": "业务分部未结构化",
                "why_it_matters": "若无法拆分业务暴露，行业研究员难以判断公司真正受哪个行业变量驱动。",
                "early_indicators": ["分产品收入", "分行业收入", "分地区收入", "分部毛利率"],
            }
        )
    return risks[:5]


def build_next_metrics(industry_data: dict[str, Any], recommendations: list[str]) -> list[dict[str, Any]]:
    """整理下一步需要补采或验证的行业指标。"""

    metrics = []
    for item in industry_data.get("industry_metrics", []):
        metrics.append({"metric_name": item.get("metric_name"), "why_needed": item.get("description"), "current_status": "seed_pointer"})
    for item in industry_data.get("public_stats", []):
        metrics.append({"metric_name": item.get("metric_name"), "why_needed": "已提供行业公开统计，可用于行业位置和周期判断。", "current_status": "available"})
    for item in industry_data.get("industry_signals", []):
        metrics.append({"metric_name": item.get("metric_name"), "why_needed": item.get("summary"), "current_status": "available"})
    if not metrics:
        metrics.extend({"metric_name": recommendation, "why_needed": "当前输入包缺口补采建议。", "current_status": "missing"} for recommendation in recommendations)
    return metrics


def build_downstream_instructions(
    market_data: dict[str, Any],
    public_stats: list[dict[str, Any]],
    industry_signals: list[dict[str, Any]],
    competitors: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """生成通用下游 Agent 指令。"""

    valuation = []
    if market_snapshot_has_values(market_data):
        valuation.append("可使用本包中的行情估值快照作为估值分析输入，但仍需核对来源口径和日期。")
    elif market_data.get("collection_status") == "available_from_local_file":
        valuation.append("行情估值适配器已接入，但核心数值为空；这只能说明接口格式可用，估值分析员仍需等待真实价格、市值和估值倍数。")
    else:
        valuation.append("当前输入包没有可用行情估值快照，估值分析员应等待用户稳定来源导出的价格、市值和估值倍数。")
    if competitors:
        valuation.append("同行候选只作为初步线索，最终估值可比公司需结合业务结构、规模、利润率和估值口径筛选。")
    else:
        valuation.append("当前未提供同行候选，估值分析员需从同行财报分析链路获取可比公司池。")
    thesis = [
        "投资假设生成员应区分公司财务事实、行业统计事实和行业信号，避免把单公司表现直接外推为行业趋势。",
        "若行业公开统计或行业信号不足，应把关键假设写成待验证条件，而不是强结论。",
    ]
    risk = [
        "风控 Agent 应监控政策、需求、供给、价格、库存、渠道、技术和估值变化中已有数据覆盖的部分。",
        "若行业信号方向恶化或统计数据与公司表现背离，应触发行业观点复核。",
    ]
    if public_stats:
        thesis.append("已有行业公开统计，可用于验证公司增速和行业增速是否一致。")
    if industry_signals:
        risk.append("已有行业信号记录，应对不利或不确定信号设置持续跟踪。")
    return {"to_valuation_agent": valuation, "to_investment_thesis_agent": thesis, "to_risk_agent": risk}


def render_markdown(report: dict[str, Any]) -> str:
    """渲染样例报告 Markdown。"""

    company = report["company"]
    lines = [
        f"# {company['name']} 行业研究员样例验证",
        "",
        "## 1. 一句话结论",
        report["executive_summary"]["one_sentence_conclusion"],
        "",
        "## 2. 行业归属检查",
        f"- 主要行业：{report['industry_classification_check']['primary_industry']}",
        f"- 细分行业：{report['industry_classification_check']['secondary_industry']}",
        "- 依据：",
    ]
    lines.extend([f"  - {item}" for item in report["industry_classification_check"].get("basis", [])] or ["  - 暂无，需要补充。"])
    lines.extend(["", "## 3. 财务表现需要追问的行业问题"])
    for item in report["financial_to_industry_questions"]:
        lines.append(f"- 事实：{item['fact']}")
        lines.append(f"  - 行业问题：{item['industry_question']}")
    lines.extend(["", "## 4. 数据覆盖检查"])
    for key, value in report["data_coverage_check"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## 5. 同行候选检查", report["peer_candidates_check"]["assessment"]])
    for peer in report["peer_candidates_check"].get("peers", []):
        lines.append(f"- {peer.get('stock_code')} {peer.get('company_name')}：{peer.get('reason')}")
    lines.extend(["", "## 6. 下一步必须补采或验证的指标"])
    for item in report["industry_metrics_to_collect_next"]:
        lines.append(f"- {item.get('metric_name')}：{item.get('why_needed')}（状态：{item.get('current_status')}）")
    lines.extend(["", "## 7. 反方风险"])
    for risk in report["risks"]:
        indicators = [str(item) for item in risk.get("early_indicators", []) if item]
        lines.append(f"- {risk['risk']}：{risk['why_it_matters']} 跟踪指标：{', '.join(indicators)}")
    lines.extend(["", "## 8. 对下游 Agent 的指令", "### 估值分析员"])
    lines.extend([f"- {item}" for item in report["downstream_instructions"]["to_valuation_agent"]])
    lines.append("### 投资假设生成员")
    lines.extend([f"- {item}" for item in report["downstream_instructions"]["to_investment_thesis_agent"]])
    lines.append("### 风控 Agent")
    lines.extend([f"- {item}" for item in report["downstream_instructions"]["to_risk_agent"]])
    lines.extend(["", "## 9. 限制条件"])
    lines.extend([f"- {item}" for item in report["limitations"]])
    return "\n".join(lines) + "\n"


def main() -> None:
    """运行行业研究样例并写入同目录。"""

    args = parse_args()
    package_path = Path(args.package)
    package = load_json(package_path)
    report = build_demo_report(package)
    json_path = package_path.with_name("industry_research_demo.json")
    md_path = package_path.with_name("industry_research_demo.md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"json_path": str(json_path), "markdown_path": str(md_path), "quality_check": report["quality_check"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
