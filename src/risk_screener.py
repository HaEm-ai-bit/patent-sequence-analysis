"""
风险筛查模块 (Step 2)

在知识库上做风险筛查，两种输入形式：
- 形式 A：输入完整蛋白序列 → 对齐 → 比对突变 → 检查是否命中受保护突变
- 形式 B：直接输入突变位点列表 → 在知识库中查找 → 检查是否受保护

如果知识库中没有该蛋白数据，自动触发 Step 1 的建库流程。
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .alignment import align_sequences, find_mutations
from .kb_builder import build_knowledge_base, get_risk_level, query_protected_sites
from .utils import AA_ALPHABET


def screen_risk(
    target: str,
    query_sequence: str | None = None,
    mutations: list[str] | None = None,
    kb: dict | None = None,
    kb_dir=None,
    **build_kwargs,
) -> dict:
    """
    风险筛查主入口。

    两种输入形式：
    - query_sequence: 用户的完整蛋白序列
    - mutations: 突变位点列表（如 ["E484K", "N501Y"]）

    至少提供一种输入。

    Args:
        target: 蛋白关键词（如 "EGFR"）
        query_sequence: 用户的蛋白序列（可选）
        mutations: 突变位点列表（可选）
        kb: 已有知识库（可选，不传则自动构建或加载缓存）
        kb_dir: 知识库目录
        **build_kwargs: 传给 build_knowledge_base() 的参数

    Returns:
        风险报告 dict，结构见 PLAN.md
    """
    if not query_sequence and not mutations:
        raise ValueError("必须提供 query_sequence 或 mutations 至少一种输入")

    # 1. 获取知识库（无则自动构建）
    if kb is None:
        kb = build_knowledge_base(target=target, kb_dir=kb_dir, **build_kwargs)

    # 2. 根据输入形式选择筛查方式
    if query_sequence:
        report = _screen_by_sequence(target, query_sequence, kb)
    else:
        report = _screen_by_mutations(target, mutations, kb)

    # 3. 添加元信息
    report["screening_time"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    report["target"] = target

    return report


def _screen_by_sequence(target: str, query_sequence: str, kb: dict) -> dict:
    """
    形式 A：输入完整蛋白序列，做序列比对 + 突变风险筛查。
    """
    # 收集知识库中所有的 AA 序列
    patent_sequences = []
    for patent in kb.get("patents", []):
        patent_id = patent.get("patent_id", "")
        patent_status = patent.get("status", "")
        for seq in patent.get("sequences", []):
            if seq.get("seq_type") == "AA" and seq.get("sequence"):
                patent_sequences.append({
                    "patent_id": patent_id,
                    "patent_status": patent_status,
                    "seq_id": seq.get("seq_id"),
                    "patent_sequence": seq["sequence"],
                    "source": seq.get("source"),
                    "location": seq.get("location"),
                    "protected": seq.get("protected", False),
                    "role": seq.get("role"),
                })

    hits = []
    for pseq in patent_sequences:
        # 序列比对
        aln_result = align_sequences(query_sequence, pseq["patent_sequence"])

        if not aln_result.is_homologous():
            continue  # 非同源，跳过

        # 逐位点比较，找出差异
        diffs = find_mutations(query_sequence, pseq["patent_sequence"], aln_result)

        # 检查差异位点是否命中知识库中的受保护突变
        mutation_hits = []
        novel_mutations = []

        # 构建该专利的突变索引
        patent_mutations = _get_patent_mutations(kb, pseq["patent_id"])

        for diff in diffs:
            matched_patent_mut = _find_matching_mutation(
                diff.position, diff.target_aa, diff.query_aa, patent_mutations
            )

            if matched_patent_mut:
                risk_level = get_risk_level(
                    matched_patent_mut.get("location", ""),
                    pseq["patent_status"],
                )
                reason = _generate_risk_reason(
                    diff.notation, pseq["patent_status"],
                    matched_patent_mut.get("location", ""),
                    risk_level,
                )
                mutation_hits.append({
                    "position": diff.position,
                    "query_aa": diff.query_aa,
                    "wild_type": diff.target_aa,
                    "notation": diff.notation,
                    "patent_mutation_location": matched_patent_mut.get("location"),
                    "protected": matched_patent_mut.get("protected", False),
                    "risk_level": risk_level,
                    "reason": reason,
                })
            else:
                # 该位点突变未在任何专利中出现 → 新突变，安全
                novel_mutations.append({
                    "position": diff.position,
                    "query_aa": diff.query_aa,
                    "patent_aa": diff.target_aa,
                    "notation": diff.notation,
                    "risk_level": "safe",
                    "reason": "该位点突变未在任何专利中出现",
                })

        if mutation_hits or novel_mutations:
            overall_risk = _determine_overall_risk(mutation_hits)
            hits.append({
                "patent_id": pseq["patent_id"],
                "patent_status": pseq["patent_status"],
                "patent_seq_id": pseq["seq_id"],
                "patent_sequence": pseq["patent_sequence"],
                "identity": round(aln_result.identity, 4),
                "mutation_hits": mutation_hits,
                "novel_mutations": novel_mutations,
                "overall_risk": overall_risk,
            })

    # 生成总结
    summary = _generate_summary(hits, kb)

    return {
        "query_type": "sequence",
        "query_sequence": query_sequence[:100] + ("..." if len(query_sequence) > 100 else ""),
        "hits": hits,
        "summary": summary,
    }


def _screen_by_mutations(target: str, mutations: list[str], kb: dict) -> dict:
    """
    形式 B：直接输入突变位点列表，在知识库中查找。
    """
    # 解析用户输入的突变
    user_mutations = _parse_mutation_list(mutations)

    hits = []
    for patent in kb.get("patents", []):
        patent_id = patent.get("patent_id", "")
        patent_status = patent.get("status", "")

        mutation_hits = []

        for user_mut in user_mutations:
            # 在该专利的突变中查找匹配
            for kb_mut in patent.get("mutations", []):
                if _mutations_match(user_mut, kb_mut):
                    risk_level = get_risk_level(
                        kb_mut.get("location", ""),
                        patent_status,
                    )
                    reason = _generate_risk_reason(
                        kb_mut.get("notation", ""),
                        patent_status,
                        kb_mut.get("location", ""),
                        risk_level,
                    )
                    mutation_hits.append({
                        "position": kb_mut.get("position"),
                        "query_aa": user_mut.get("mutant", ""),
                        "wild_type": kb_mut.get("wild_type", ""),
                        "notation": kb_mut.get("notation", ""),
                        "patent_mutation_location": kb_mut.get("location"),
                        "protected": kb_mut.get("protected", False),
                        "risk_level": risk_level,
                        "reason": reason,
                    })

        if mutation_hits:
            overall_risk = _determine_overall_risk(mutation_hits)
            hits.append({
                "patent_id": patent_id,
                "patent_status": patent_status,
                "mutation_hits": mutation_hits,
                "novel_mutations": [],
                "overall_risk": overall_risk,
            })

    # 生成总结
    summary = _generate_summary(hits, kb)

    return {
        "query_type": "mutations",
        "query_mutations": mutations,
        "hits": hits,
        "summary": summary,
    }


# ========== 辅助函数 ==========

def _parse_mutation_list(mutations: list[str]) -> list[dict]:
    """
    解析突变位点列表，格式如 ["E484K", "N501Y"]。
    """
    pattern = re.compile(r'^([ACDEFGHIKLMNPQRSTVWY])(\d+)([ACDEFGHIKLMNPQRSTVWY])$')
    result = []
    for m_str in mutations:
        m = pattern.match(m_str.strip().upper())
        if m:
            result.append({
                "wild_type": m.group(1),
                "position": int(m.group(2)),
                "mutant": m.group(3),
                "notation": m_str.strip().upper(),
            })
    return result


def _mutations_match(user_mut: dict, kb_mut: dict) -> bool:
    """
    判断用户突变是否与知识库中的突变匹配。
    只匹配位置和野生型，突变型不需要一致（因为用户可能有不同的突变）。
    """
    user_pos = user_mut.get("position")
    kb_pos = kb_mut.get("position")
    if user_pos is None or kb_pos is None:
        return False
    return user_pos == kb_pos


def _get_patent_mutations(kb: dict, patent_id: str) -> list[dict]:
    """获取某个专利的所有突变。"""
    for patent in kb.get("patents", []):
        if patent.get("patent_id") == patent_id:
            return patent.get("mutations", [])
    return []


def _find_matching_mutation(position: int, target_aa: str, query_aa: str, patent_mutations: list[dict]) -> dict | None:
    """在专利突变列表中查找匹配的突变（位置匹配）。"""
    for mut in patent_mutations:
        if mut.get("position") == position:
            return mut
    return None


def _generate_risk_reason(notation: str, patent_status: str, location: str, risk_level: str) -> str:
    """生成风险原因描述。"""
    if risk_level == "high":
        return f"{notation}命中已授权专利的claims，必须规避"
    elif risk_level == "medium":
        if patent_status == "granted":
            return f"{notation}仅在已授权专利的description中提及，未被claims保护，需注意"
        else:
            return f"{notation}在审查中专利的claims中，有授权风险"
    elif risk_level == "low":
        return f"{notation}在审查中专利的description中提及，可用"
    elif risk_level == "safe":
        return f"{notation}所在专利已失效，无风险"
    return f"{notation}风险未知"


def _determine_overall_risk(mutation_hits: list[dict]) -> str:
    """根据所有突变命中情况判断总体风险等级。"""
    if not mutation_hits:
        return "safe"

    risk_levels = {hit.get("risk_level", "low") for hit in mutation_hits}

    if "high" in risk_levels:
        return "high"
    if "medium" in risk_levels:
        return "medium"
    if "low" in risk_levels:
        return "low"
    return "safe"


def _generate_summary(hits: list[dict], kb: dict) -> dict:
    """生成风险报告总结。"""
    all_mutation_hits = []
    all_novel_mutations = []

    for hit in hits:
        all_mutation_hits.extend(hit.get("mutation_hits", []))
        all_novel_mutations.extend(hit.get("novel_mutations", []))

    high_risk = [m["notation"] for m in all_mutation_hits if m.get("risk_level") == "high"]
    medium_risk = [m["notation"] for m in all_mutation_hits if m.get("risk_level") == "medium"]
    low_risk = [m["notation"] for m in all_mutation_hits if m.get("risk_level") == "low"]
    safe = [m["notation"] for m in all_novel_mutations]

    # 生成结论
    conclusions = []
    if high_risk:
        conclusions.append(f"{', '.join(high_risk)}命中已授权专利的claims保护，必须规避")
    if medium_risk:
        conclusions.append(f"{', '.join(medium_risk)}需关注（审查中或在description中提及）")
    if low_risk:
        conclusions.append(f"{', '.join(low_risk)}可用但需注意")
    if safe:
        conclusions.append(f"{', '.join(safe)}为安全突变")
    if not conclusions:
        conclusions.append("未发现风险突变")

    return {
        "total_patents_checked": kb.get("total_patents_searched", 0),
        "patents_with_hits": len(hits),
        "high_risk_mutations": sorted(set(high_risk)),
        "medium_risk_mutations": sorted(set(medium_risk)),
        "low_risk_mutations": sorted(set(low_risk)),
        "safe_mutations": sorted(set(safe)),
        "conclusion": "；".join(conclusions),
    }
