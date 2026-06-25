"""
通用工具函数
"""

import json
import re
from pathlib import Path


# ========== 氨基酸三字母码到单字母码映射 ==========
AA_THREE_TO_ONE = {
    'Ala': 'A', 'Arg': 'R', 'Asn': 'N', 'Asp': 'D', 'Cys': 'C',
    'Gln': 'Q', 'Glu': 'E', 'Gly': 'G', 'His': 'H', 'Ile': 'I',
    'Leu': 'L', 'Lys': 'K', 'Met': 'M', 'Phe': 'F', 'Pro': 'P',
    'Ser': 'S', 'Thr': 'T', 'Trp': 'W', 'Tyr': 'Y', 'Val': 'V',
    'Xaa': 'X', 'Sec': 'U', 'Pyl': 'O'
}

# 单字母氨基酸字母表
AA_ALPHABET = set("ACDEFGHIKLMNPQRSTVWY")

# 核酸字母表
NT_ALPHABET = set("ACGTU")


def convert_three_letter_to_one(three_letter_seq: str) -> str:
    """
    将三字母氨基酸码序列转换为单字母码。
    例如: "GlySerIlePhe" -> "GSIF"
    处理包含位置编号的格式，如：
    "Gly Ser Ile Phe Ser Gly Ser Ala
     1   5   10  15"
    """
    # 移除所有数字和空白字符，只保留字母
    seq = re.sub(r'[0-9\s]+', '', three_letter_seq)

    # 按三字母码分割（首字母大写，后两个小写）
    result = []
    i = 0
    while i < len(seq):
        # 尝试匹配三字母码
        if i + 3 <= len(seq):
            three = seq[i:i + 3]
            if three in AA_THREE_TO_ONE:
                result.append(AA_THREE_TO_ONE[three])
                i += 3
                continue
        # 如果不匹配，跳过一个字符
        i += 1

    return ''.join(result)


def flatten_field(value) -> str:
    """
    把 claims/descriptions 等可能是 list[dict] 的字段拍平为纯文本。
    支持格式：
    - str → 直接返回
    - list[dict] → 拼接每个 dict 的 enName/zhName
    - dict → 返回 enName/zhName
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("enName") or item.get("zhName") or "")
        return "\n".join(parts)
    if isinstance(value, dict):
        return value.get("enName") or value.get("zhName") or ""
    return ""


def save_json(path: Path, data: dict):
    """保存 JSON 到文件，自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> dict | None:
    """从文件加载 JSON，文件不存在或解析失败返回 None。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def short_text(text: str, max_len: int = 200) -> str:
    """截断长文本，添加省略号。"""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def choose_title(record: dict) -> str:
    """从专利记录中选取标题（优先中文）。"""
    return (record.get("zhName") or "").strip() or (record.get("enName") or "").strip()


def choose_abstract(record: dict) -> str:
    """从专利记录中选取摘要（优先中文）。"""
    return (record.get("zhAbstract") or "").strip() or (record.get("enAbstract") or "").strip()


def chunked(items: list, size: int) -> list[list]:
    """将列表切分为固定大小的批次。"""
    return [items[i:i + size] for i in range(0, len(items), size)]


def normalize_patent_status(status: str) -> str:
    """
    将专利状态字符串标准化为统一关键词。
    返回: "granted" | "pending" | "abandoned" | "expired" | "withdrawn" | "unknown"
    """
    if not status:
        return "unknown"
    s = status.lower().strip()
    if "grant" in s or "授权" in s or s == "active":
        return "granted"
    if "pend" in s or "审查" in s or "申请" in s:
        return "pending"
    if "abandon" in s or "放弃" in s:
        return "abandoned"
    if "expir" in s or "到期" in s or "失效" in s or "laps" in s:
        return "expired"
    if "withdraw" in s or "撤回" in s:
        return "withdrawn"
    return "unknown"
