"""
序列比对模块

基于 Biopython 的 pairwise alignment，用于：
- 判断两条序列是否同源（同一蛋白）
- 计算序列一致性 (identity)
- 在对齐基础上逐位点比较，找出差异位点

新写模块。
"""

from dataclasses import dataclass

from Bio.Align import PairwiseAligner, substitution_matrices


# ========== 比对结果数据结构 ==========

@dataclass
class AlignmentResult:
    """
    序列比对结果。

    Attributes:
        identity: 序列一致性（0~1），相同残基占比
        score: 比对得分
        aligned_query: 对齐后的 query 序列（含 gap）
        aligned_target: 对齐后的 target 序列（含 gap）
        query_length: query 原始长度
        target_length: target 原始长度
    """
    identity: float
    score: float
    aligned_query: str
    aligned_target: str
    query_length: int
    target_length: int

    def is_homologous(self, threshold: float = 0.7) -> bool:
        """判断是否同源（同一蛋白），默认阈值 70% 一致性。"""
        return self.identity >= threshold

    def to_dict(self) -> dict:
        return {
            "identity": round(self.identity, 4),
            "score": round(self.score, 2),
            "aligned_query": self.aligned_query,
            "aligned_target": self.aligned_target,
            "query_length": self.query_length,
            "target_length": self.target_length,
        }


@dataclass
class MutationDifference:
    """
    两条序列对齐后的差异位点。

    Attributes:
        position: 在 target（专利序列）上的位置（1-based）
        query_aa: query 序列在该位置的氨基酸
        target_aa: target（专利序列）在该位置的氨基酸
        notation: 标准格式标记（如 "E484K" 表示 target 为 E，query 为 K）
        is_gap: 是否为 gap（插入/缺失）
    """
    position: int
    query_aa: str
    target_aa: str
    notation: str = ""
    is_gap: bool = False

    def __post_init__(self):
        if not self.notation and not self.is_gap:
            self.notation = f"{self.target_aa}{self.position}{self.query_aa}"

    def to_dict(self) -> dict:
        return {
            "position": self.position,
            "query_aa": self.query_aa,
            "target_aa": self.target_aa,
            "notation": self.notation,
            "is_gap": self.is_gap,
        }


# ========== 核心比对函数 ==========

# 全局比对器（使用 BLOSUM62 替换矩阵）
_aligner = PairwiseAligner()
_aligner.mode = "global"
_aligner.substitution_matrix = substitution_matrices.load("BLOSUM62")
_aligner.open_gap_score = -10
_aligner.extend_gap_score = -0.5


def align_sequences(query: str, target: str) -> AlignmentResult:
    """
    对两条氨基酸序列做全局 pairwise alignment。

    Args:
        query: 用户输入的序列
        target: 专利中的参考序列

    Returns:
        AlignmentResult
    """
    if not query or not target:
        return AlignmentResult(
            identity=0.0, score=0.0,
            aligned_query="", aligned_target="",
            query_length=len(query), target_length=len(target),
        )

    alignments = _aligner.align(query, target)

    if not alignments:
        return AlignmentResult(
            identity=0.0, score=0.0,
            aligned_query=query, aligned_target=target,
            query_length=len(query), target_length=len(target),
        )

    # 取最优比对
    best = alignments[0]

    # 提取对齐后的序列字符串
    aligned_str = str(best).split('\n')
    # Biopython 格式:
    # line 0: target
    # line 1: | . .
    # line 2: query
    # 但实际上新版本的格式可能不同，用 aligned 属性更可靠

    # 使用 aligned 属性获取对齐坐标
    aligned_target_str, aligned_query_str = _format_alignment(best, query, target)

    # 计算 identity
    matches = sum(1 for a, b in zip(aligned_query_str, aligned_target_str)
                  if a == b and a != '-' and b != '-')
    aligned_len = sum(1 for a, b in zip(aligned_query_str, aligned_target_str)
                      if a != '-' and b != '-')
    identity = matches / aligned_len if aligned_len > 0 else 0.0

    return AlignmentResult(
        identity=identity,
        score=best.score,
        aligned_query=aligned_query_str,
        aligned_target=aligned_target_str,
        query_length=len(query),
        target_length=len(target),
    )


def _format_alignment(alignment, query: str, target: str) -> tuple[str, str]:
    """
    从 Biopython Alignment 对象中提取对齐后的序列字符串。
    """
    try:
        # Biopython 1.80+ 的 Alignment 对象
        aligned_seqs = alignment.aligned
        target_coords = aligned_seqs[0]  # list of (start, end) for target
        query_coords = aligned_seqs[1]   # list of (start, end) for query

        # 构建对齐字符串
        aligned_target_parts = []
        aligned_query_parts = []

        prev_t_end = 0
        prev_q_end = 0

        for (t_start, t_end), (q_start, q_end) in zip(target_coords, query_coords):
            # 处理 gap
            t_gap = t_start - prev_t_end
            q_gap = q_start - prev_q_end

            if t_gap > 0 and q_gap > 0:
                # 两侧都有未对齐的残基（不太可能，但处理一下）
                aligned_target_parts.append(target[prev_t_end:t_start])
                aligned_query_parts.append('-' * t_gap if q_gap <= t_gap else query[prev_q_end:q_start])
            elif t_gap > 0:
                # target 有 gap（query 中是插入）
                aligned_target_parts.append(target[prev_t_end:t_start])
                aligned_query_parts.append('-' * t_gap)
            elif q_gap > 0:
                # query 有 gap（target 中是插入）
                aligned_target_parts.append('-' * q_gap)
                aligned_query_parts.append(query[prev_q_end:q_start])

            # 对齐区域
            t_len = t_end - t_start
            q_len = q_end - q_start
            aligned_target_parts.append(target[t_start:t_end])
            aligned_query_parts.append(query[q_start:q_end])

            prev_t_end = t_end
            prev_q_end = q_end

        # 处理尾部未对齐区域
        t_remaining = len(target) - prev_t_end
        q_remaining = len(query) - prev_q_end

        if t_remaining > 0:
            aligned_target_parts.append(target[prev_t_end:])
            aligned_query_parts.append('-' * t_remaining)
        if q_remaining > 0:
            aligned_target_parts.append('-' * q_remaining)
            aligned_query_parts.append(query[prev_q_end:])

        return ''.join(aligned_target_parts), ''.join(aligned_query_parts)

    except Exception:
        # 降级方案：直接拼接
        return target, query


# ========== 差异位点提取 ==========

def find_mutations(
    query: str,
    target: str,
    alignment_result: AlignmentResult | None = None,
) -> list[MutationDifference]:
    """
    在对齐基础上逐位点比较，找出差异位点（query 相对于 target 的突变）。

    前提：query 和 target 必须是同一条蛋白（identity >= 70%）。

    Args:
        query: 用户输入序列
        target: 专利参考序列
        alignment_result: 已有的比对结果（可选，不传则重新比对）

    Returns:
        List of MutationDifference
    """
    if alignment_result is None:
        alignment_result = align_sequences(query, target)

    if not alignment_result.is_homologous():
        # 非同源序列，无法比对突变
        return []

    differences = []
    target_pos = 0  # 1-based position in target

    aligned_q = alignment_result.aligned_query
    aligned_t = alignment_result.aligned_target

    for q_aa, t_aa in zip(aligned_q, aligned_t):
        if t_aa != '-':
            target_pos += 1

        if q_aa == '-' or t_aa == '-':
            # gap
            if q_aa != t_aa:
                differences.append(MutationDifference(
                    position=target_pos if t_aa != '-' else target_pos + 1,
                    query_aa=q_aa,
                    target_aa=t_aa,
                    is_gap=True,
                ))
        elif q_aa != t_aa:
            # 替换
            differences.append(MutationDifference(
                position=target_pos,
                query_aa=q_aa,
                target_aa=t_aa,
            ))

    return differences
