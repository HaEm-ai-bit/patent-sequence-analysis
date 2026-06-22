"""
突变位点提取模块

从专利文本中提取突变位点描述，并判断 location（claims / description）。

支持的突变格式：
- 标准格式：E484K, N501Y, D614G
- 位置+氨基酸：position 484 Glu→Lys, position 484 Glu to Lys
- 中文格式：第484位谷氨酸替换为赖氨酸, 484位E替换为K
- 三字母格式：Glu484Lys, Glu484→Lys
- 数字+替换：484E→K, 484E/K

新写模块，旧代码中无对应功能。
"""

import re
from dataclasses import dataclass, field

from .utils import AA_THREE_TO_ONE, flatten_field


# ========== 氨基酸单字母 ↔ 三字母映射 ==========

# 反向映射：三字母 → 单字母（已有 AA_THREE_TO_ONE）
# 单字母 → 三字母（新增）
ONE_TO_THREE = {v: k for k, v in AA_THREE_TO_ONE.items() if k != 'Xaa'}

# 氨基酸中文名映射
AA_CHINESE = {
    '丙': 'A', '精': 'R', '天冬酰胺': 'N', '天冬氨酸': 'D', '半胱': 'C',
    '谷氨酰胺': 'Q', '谷氨酸': 'E', '甘': 'G', '组': 'H', '异亮': 'I',
    '亮': 'L', '赖': 'K', '甲硫': 'M', '苯丙': 'F', '脯': 'P',
    '丝': 'S', '苏': 'T', '色': 'W', '酪': 'Y', '缬': 'V',
}

# 用于匹配三字母氨基酸名称的正则部分
_THREE_LETTER_AA = '|'.join(sorted(AA_THREE_TO_ONE.keys(), key=len, reverse=True))


# ========== 突变位点数据结构 ==========

@dataclass
class MutationInfo:
    """
    提取的一条突变位点信息。

    Attributes:
        position: 突变位点位置（1-based）
        wild_type: 野生型氨基酸（单字母）
        mutant: 突变型氨基酸（单字母）
        notation: 标准格式标记（如 "E484K"）
        location: 出现位置 "claims" / "description" / "unknown"
        context: 原文上下文片段
    """
    position: int
    wild_type: str
    mutant: str
    notation: str = ""
    location: str = "unknown"
    context: str = ""

    def __post_init__(self):
        if not self.notation:
            self.notation = f"{self.wild_type}{self.position}{self.mutant}"

    def to_dict(self) -> dict:
        return {
            "position": self.position,
            "wild_type": self.wild_type,
            "mutant": self.mutant,
            "notation": self.notation,
            "location": self.location,
            "protected": self.location == "claims",
            "context": self.context[:300] if self.context else "",
        }

    def __repr__(self):
        return f"MutationInfo({self.notation}/{self.location})"


# ========== 突变格式正则 ==========

# 格式1: E484K (单字母+数字+单字母，最常见)
_MUT_SINGLE = re.compile(
    r'\b([ACDEFGHIKLMNPQRSTVWY])(\d{1,5})([ACDEFGHIKLMNPQRSTVWY])\b'
)

# 格式2: Glu484Lys (三字母+数字+三字母)
_MUT_THREE = re.compile(
    rf'\b({_THREE_LETTER_AA})(\d{{1,5}})({_THREE_LETTER_AA})\b'
)

# 格式3: position 484 Glu→Lys / position 484 Glu to Lys / position 484 Glu/Lys
_MUT_POSITION_EN = re.compile(
    rf'(?:position|pos\.?|residue)\s+(\d{{1,5}})\s+({_THREE_LETTER_AA}|[ACDEFGHIKLMNPQRSTVWY])'
    rf'\s*(?:→|->|to|/|replaced\s+by|substituted\s+by|substituted\s+with|mutated\s+to)\s*'
    rf'({_THREE_LETTER_AA}|[ACDEFGHIKLMNPQRSTVWY])',
    re.IGNORECASE,
)

# 格式4: 484E→K / 484E/K
_MUT_NUM_AA = re.compile(
    r'\b(\d{1,5})([ACDEFGHIKLMNPQRSTVWY])\s*(?:→|->|/)\s*([ACDEFGHIKLMNPQRSTVWY])\b'
)

# 格式5: 中文格式 第484位谷氨酸替换为赖氨酸 / 484位E替换为K
_MUT_CHINESE = re.compile(
    r'第?(\d{1,5})位\s*(?:'
    rf'([谷甘丙缬亮异亮苯丙酪色丝苏天冬酰胺天冬氨酸半胱组赖精脯甲硫甲硫氨酸]|'
    rf'[ACDEFGHIKLMNPQRSTVWY])'
    r')\s*(?:替换|取代|代替|突变|变)\s*为\s*'
    rf'(?:([谷甘丙缬亮异亮苯丙酪色丝苏天冬酰胺天冬氨酸半胱组赖精脯甲硫甲硫氨酸]|'
    rf'[ACDEFGHIKLMNPQRSTVWY]))'
)

# 格式6: substitution of X at position N with Y / replacement of X at N by Y
_MUT_SUBSTITUTION = re.compile(
    rf'(?:substitution|replacement|mutation)\s+(?:of\s+)?'
    rf'({_THREE_LETTER_AA}|[ACDEFGHIKLMNPQRSTVWY])\s+(?:at\s+)?(?:position|pos\.?|residue)?\s*(\d{{1,5}})'
    rf'\s*(?:with|by|to)\s*'
    rf'({_THREE_LETTER_AA}|[ACDEFGHIKLMNPQRSTVWY])',
    re.IGNORECASE,
)

# 排除：看起来像突变但实际不是的模式（如 "E3ligase", "K2Pchannel"）
# 注意：只排除紧邻突变的假阳性词，不影响远处的正常词汇
_FALSE_POSITIVE = re.compile(
    r'(?:ligase|channel|kinase|domain|family|complex|subunit|receptor|factor|enzyme|pathway)',
    re.IGNORECASE,
)


# ========== 三字母/中文转单字母 ==========

def _aa_to_single(aa: str) -> str | None:
    """
    将氨基酸表示转为单字母码。
    支持：单字母、三字母、中文缩写。
    """
    aa = aa.strip()
    if len(aa) == 1 and aa in AA_THREE_TO_ONE.values():
        return aa.upper()
    if aa in AA_THREE_TO_ONE:
        return AA_THREE_TO_ONE[aa]
    if aa in AA_CHINESE:
        return AA_CHINESE[aa]
    # 尝试匹配中文名
    for cn, single in AA_CHINESE.items():
        if cn in aa:
            return single
    return None


# ========== 从文本提取突变 ==========

def extract_mutations_from_text(text: str) -> list[MutationInfo]:
    """
    从文本中提取所有突变位点描述。

    Returns:
        List of MutationInfo objects (未设置 location，由调用方设置)
    """
    if not text:
        return []

    text = text or ""
    # 长文本优化：分段处理避免截断遗漏
    if len(text) > 50000:
        text = text[:30000] + "\n" + text[-20000:]

    seen_notations: set[str] = set()
    results: list[MutationInfo] = []

    def _add_mutation(position: int, wt: str | None, mt: str | None, ctx_start: int, ctx_end: int):
        """添加突变到结果列表，自动去重。"""
        if wt is None or mt is None:
            return
        wt = wt.upper()
        mt = mt.upper()
        if wt == mt:
            return  # 无意义突变（野生型=突变型）
        notation = f"{wt}{position}{mt}"
        if notation in seen_notations:
            return
        # 排除假阳性：检查突变标记紧后面是否跟着假阳性词
        # 例如 E3ligase → E3 不是突变，而是 E3 泛素连接酶
        right_context = text[m.end(): min(len(text), m.end() + 20)]
        if _FALSE_POSITIVE.match(right_context):
            return
        seen_notations.add(notation)
        # 提取上下文
        context_window = text[max(0, ctx_start - 80): min(len(text), ctx_end + 80)]
        results.append(MutationInfo(
            position=position,
            wild_type=wt,
            mutant=mt,
            notation=notation,
            context=re.sub(r'\s+', ' ', context_window).strip(),
        ))

    # 格式1: E484K
    for m in _MUT_SINGLE.finditer(text):
        wt = m.group(1)
        pos = int(m.group(2))
        mt = m.group(3)
        _add_mutation(pos, wt, mt, m.start(), m.end())

    # 格式2: Glu484Lys
    for m in _MUT_THREE.finditer(text):
        wt = _aa_to_single(m.group(1))
        pos = int(m.group(2))
        mt = _aa_to_single(m.group(3))
        _add_mutation(pos, wt, mt, m.start(), m.end())

    # 格式3: position 484 Glu→Lys
    for m in _MUT_POSITION_EN.finditer(text):
        pos = int(m.group(1))
        wt = _aa_to_single(m.group(2))
        mt = _aa_to_single(m.group(3))
        _add_mutation(pos, wt, mt, m.start(), m.end())

    # 格式4: 484E→K
    for m in _MUT_NUM_AA.finditer(text):
        pos = int(m.group(1))
        wt = m.group(2)
        mt = m.group(3)
        _add_mutation(pos, wt, mt, m.start(), m.end())

    # 格式5: 中文格式
    for m in _MUT_CHINESE.finditer(text):
        pos = int(m.group(1))
        wt = _aa_to_single(m.group(2))
        mt = _aa_to_single(m.group(3))
        _add_mutation(pos, wt, mt, m.start(), m.end())

    # 格式6: substitution of X at position N with Y
    for m in _MUT_SUBSTITUTION.finditer(text):
        wt = _aa_to_single(m.group(1))
        pos = int(m.group(2))
        mt = _aa_to_single(m.group(3))
        _add_mutation(pos, wt, mt, m.start(), m.end())

    return results


# ========== 判断突变的 location ==========

def determine_mutation_location(notation: str, claims_text: str, desc_text: str) -> str:
    """
    判断突变位点出现在 claims 还是 description 中。

    Args:
        notation: 突变标记（如 "E484K"）
        claims_text: 权利要求文本
        desc_text: 说明书文本

    Returns:
        "claims" / "description" / "unknown"
    """
    if notation in claims_text:
        return "claims"
    if notation in desc_text:
        return "description"
    # 有些专利中突变以其他格式出现在 claims 中，如 "484E→K"
    # 尝试宽松匹配：只检查位置号
    pos = re.search(r'\d+', notation)
    if pos:
        pos_str = pos.group()
        if pos_str in claims_text:
            return "claims"
        if pos_str in desc_text:
            return "description"
    return "unknown"


# ========== 主入口：从专利记录提取所有突变 ==========

def extract_mutations_from_patent(record: dict) -> list[MutationInfo]:
    """
    从单个专利详情中提取所有突变位点，并判断每条突变的 location。

    扫描顺序：claims → descriptions

    Args:
        record: 专利详情 dict

    Returns:
        List of MutationInfo objects
    """
    if not isinstance(record, dict):
        return []

    claims_text = flatten_field(record.get("claims"))
    desc_text = flatten_field(record.get("descriptions"))

    seen_notations: set[str] = set()
    results: list[MutationInfo] = []

    # 1. 从 claims 中提取
    for mut in extract_mutations_from_text(claims_text):
        if mut.notation not in seen_notations:
            seen_notations.add(mut.notation)
            mut.location = "claims"
            results.append(mut)

    # 2. 从 descriptions 中提取
    for mut in extract_mutations_from_text(desc_text):
        if mut.notation not in seen_notations:
            seen_notations.add(mut.notation)
            # 判断 location
            mut.location = determine_mutation_location(mut.notation, claims_text, desc_text)
            results.append(mut)

    return results
