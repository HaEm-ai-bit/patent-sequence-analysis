"""
Skill 接口，供 macroflow 调用

提供三个 action：
- build_kb: 构建知识库
- query_protected: 查询受保护序列/位点
- screen_risk: 风险筛查
"""

from src.kb_builder import build_knowledge_base, query_protected_sites
from src.risk_screener import screen_risk


def run_skill(params: dict) -> dict:
    """
    统一对外接口，供 macroflow 调用。

    Args:
        params: 包含 action 和相关参数的 dict

    支持的 action：
    - build_kb: {"action": "build_kb", "target": "EGFR", ...}
    - query_protected: {"action": "query_protected", "target": "EGFR"}
    - screen_risk: {"action": "screen_risk", "target": "EGFR", "query_sequence": "MTEY...", ...}
                   或 {"action": "screen_risk", "target": "EGFR", "mutations": ["E484K", "N501Y"]}
    """
    action = params.get("action")
    if not action:
        return {"error": "缺少 action 参数", "supported_actions": ["build_kb", "query_protected", "screen_risk"]}

    target = params.get("target")
    if not target:
        return {"error": "缺少 target 参数（蛋白关键词）"}

    try:
        if action == "build_kb":
            kb = build_knowledge_base(
                target=target,
                time_start=params.get("time_start", "1900-01-01"),
                time_end=params.get("time_end"),
                max_pages=params.get("max_pages", 5),
                force_rebuild=params.get("force_rebuild", False),
            )
            return {
                "status": "success",
                "target": target,
                "total_patents": kb.get("total_patents_searched", 0),
                "patents_with_data": kb.get("patents_with_data", 0),
                "build_time": kb.get("build_time"),
            }

        elif action == "query_protected":
            result = query_protected_sites(target=target)
            return {
                "status": "success",
                **result,
            }

        elif action == "screen_risk":
            query_sequence = params.get("query_sequence")
            mutations = params.get("mutations")

            if not query_sequence and not mutations:
                return {"error": "screen_risk 需要提供 query_sequence 或 mutations 参数"}

            report = screen_risk(
                target=target,
                query_sequence=query_sequence,
                mutations=mutations,
            )
            return {
                "status": "success",
                **report,
            }

        else:
            return {"error": f"未知 action: {action}", "supported_actions": ["build_kb", "query_protected", "screen_risk"]}

    except Exception as e:
        return {"status": "error", "action": action, "error": str(e)}
