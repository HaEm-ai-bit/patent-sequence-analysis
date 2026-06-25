import argparse
import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import urllib3

# 禁用 HTTPS 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://relay.catalystplus.cn:7443"
SUMMARY_API = "/patent/pass/advanced"
DETAIL_API = "/patent/pass/ids/detail"

# 建议改为环境变量读取，避免把凭证硬编码进代码库
ACCESS_KEY = "your_access_key_here"
ACCESS_SECRET = "your_access_secret_here"

# ============ LLM Agent 配置 ============
LLM_API_URL = "https://api.gpugeek.com/v1/chat/completions"
LLM_API_KEY = os.getenv("LLM_API_KEY", "40prl5b41lhrz551000dh6lht1wr1pp650qt70t4")
LLM_MODEL = "Vendor3/qwen-turbo"
LLM_MAX_TOKENS = 1024
LLM_TIMEOUT = 30

DEFAULT_KEYWORDS = ["TLR2", "CD318"]
DEFAULT_TIME_START = "1900-01-01"
DEFAULT_TIME_END = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

AA_ALPHABET = set("ACDEFGHIKLMNPQRSTVWY")
NT_ALPHABET = set("ACGTU")

# ========== 氨基酸三字母码到单字母码映射 ==========
AA_THREE_TO_ONE = {
    'Ala': 'A', 'Arg': 'R', 'Asn': 'N', 'Asp': 'D', 'Cys': 'C',
    'Gln': 'Q', 'Glu': 'E', 'Gly': 'G', 'His': 'H', 'Ile': 'I',
    'Leu': 'L', 'Lys': 'K', 'Met': 'M', 'Phe': 'F', 'Pro': 'P',
    'Ser': 'S', 'Thr': 'T', 'Trp': 'W', 'Tyr': 'Y', 'Val': 'V',
    'Xaa': 'X', 'Sec': 'U', 'Pyl': 'O'
}

# ---------- 精准序列正则 ----------
# 核酸：5'-XXXXXX-3' 或 3'-XXXXXX-5' 格式（RNA/DNA 均支持）
_NT_BODY = r"[ACGTURYMKSWHBVDN\s\-]{6,200}"
NT_PRIME_PATTERN = re.compile(
    r"[35][′']\s*[-]?\s*(" + _NT_BODY + r")\s*[-]?\s*[35][′']",
    re.IGNORECASE,
)
# SEQ ID NO: n 后面跟随的序列——不用 IGNORECASE，捕获组要求纯大写
# 间隔最多 8 个非字母符号（冒号/括号/数字/空白），避免跳到下一句话
SEQ_ID_PATTERN = re.compile(
    r"(?:SEQ|seq|Seq)\s+(?:ID|id)\s+(?:NO|no|No)[.:\s]*\d+[^a-zA-Z]{0,8}([A-Z]{5,200})"
)
# 英文常见词后缀——捕获到此类后缀结尾的全大写单词直接跳过
_EN_SUFFIX = re.compile(r"(?:TION|MENT|NESS|ENCE|ANCE|IVELY|INGLY|OUSLY|IVELY|EDLY|ERLY|IALLY|ALLY|ULLY|FULLY|WARD|WARDS|WISE|SHIP|HOOD|OLOGY|MENT)$")
# 氨基酸：连续大写字母，仅含标准20种氨基酸字符，长度>=8，但排除纯核酸字母
_AA_STRICT = re.compile(r"\b([ACDEFGHIKLMNPQRSTVWY]{12,150})\b")
# 裸AA提取：上下文必须含序列相关关键词才提取（降低误报）
_BARE_AA_CTX_KW = re.compile(
    r"polypeptide|peptide|amino.acid|antibod|VH\b|VL\b|CDR|variable.region|"
    r"heavy.chain|light.chain|antigen.binding|epitope|protein.sequen|融合蛋白|多肽|氨基酸序列|抗体",
    re.IGNORECASE
)

# ========== ST.26 序列表提取正则 ==========
# 匹配 <400> tag 后的序列ID和序列内容（三字母码格式）
# 序列内容会跨多行，包含位置编号，需要提取所有氨基酸名称
# 从 <400> 匹配到下一个 <210> 或 <110> 标签
ST26_SEQ_PATTERN = re.compile(
    r'<400>\s*(\d+)(.*?)(?=<210>|<110>|$)',
    re.DOTALL
)

# ST.26 特征描述标签（用于识别序列角色）
ST26_FEATURE_PATTERN = re.compile(
    r'<223>\s*(.*?)(?=<[0-9]|$)',
    re.DOTALL
)


# ============ LLM Agent 函数 ============

def call_llm(prompt: str, retries: int = 3) -> dict | None:
    """
    调用LLM API，返回解析后的JSON dict。
    失败时返回None（降级到原有推断逻辑）。
    """
    if not LLM_API_KEY:
        return None

    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                LLM_API_URL,
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": LLM_MAX_TOKENS
                },
                timeout=LLM_TIMEOUT,
                verify=False
            )

            if response.status_code == 429:  # 限流
                wait = 60 * attempt
                print(f"    [LLM限流] 等待 {wait}s 后重试 ({attempt}/{retries})...")
                time.sleep(wait)
                continue

            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                print(f"    [LLM错误] {last_error}")
                time.sleep(1.2 * attempt)
                continue

            result = response.json()
            raw_text = result["choices"][0]["message"]["content"]
            return _parse_llm_json(raw_text)

        except requests.Timeout:
            print(f"    [LLM超时] 重试 {attempt}/{retries}...")
            time.sleep(2 * attempt)

        except Exception as e:
            last_error = str(e)
            print(f"    [LLM异常] {e}")
            break

    print(f"    [LLM失败] {last_error}")
    return None


def _parse_llm_json(text: str) -> dict | None:
    """
    处理LLM返回的非标准JSON格式：
    - 纯JSON
    - ```json ... ``` 代码块
    - 混有解释文字的JSON
    """
    text = text.strip()

    # 情况1：提取 ```json 代码块
    code_block = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', text)
    if code_block:
        text = code_block.group(1)

    # 情况2：提取最外层 {...}
    brace_match = re.search(r'\{[\s\S]+\}', text)
    if brace_match:
        text = brace_match.group(0)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 情况3：尝试修复常见问题（末尾逗号、单引号）
        text = re.sub(r',\s*([}\]])', r'\1', text)  # 删除尾逗号
        text = text.replace("'", '"')                # 单引号改双引号
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


def build_llm_prompt_for_patent(
    patent_id: str,
    patent_json: dict,
    sequences: list[dict],
    target: str
) -> str:
    """
    为单个专利构建批量验证Prompt（增强版）。

    Args:
        patent_id: 专利号
        patent_json: 从JSON文件中提取的该专利的完整信息
        sequences: 同一专利的所有需要LLM验证的序列
        target: 靶点名
    """
    # 构建序列列表
    seq_list_text = ""
    for i, s in enumerate(sequences):
        seq_preview = s['sequence'][:100] + ('...' if len(s['sequence']) > 100 else '')
        seq_list_text += (
            f"\n序列{i+1}: {seq_preview}\n"
            f"  序列类型: {s.get('seq_type', 'unknown')}\n"
            f"  上下文: {s['seq_context'][:200]}\n"
            f"  当前推断角色: {s['seq_role']}\n"
        )

    # 从patent_json提取关键信息（限制长度避免超过LLM上下文）
    patent_title = patent_json.get('enName') or patent_json.get('zhName', '')

    # 提取claims（处理list格式）
    claims_raw = patent_json.get('claims', [])
    if isinstance(claims_raw, list) and len(claims_raw) > 0:
        claims_text = claims_raw[0].get('enName', '') or claims_raw[0].get('zhName', '')
    else:
        claims_text = str(claims_raw)
    claims_text = claims_text[:2000] if len(claims_text) > 2000 else claims_text

    # 提取descriptions（处理list格式）
    desc_raw = patent_json.get('descriptions', [])
    if isinstance(desc_raw, list) and len(desc_raw) > 0:
        desc_text = desc_raw[0].get('enName', '') or desc_raw[0].get('zhName', '')
    else:
        desc_text = str(desc_raw)
    desc_text = desc_text[:1500] if len(desc_text) > 1500 else desc_text

    # 提取摘要
    abstract = patent_json.get('enAbstract') or patent_json.get('zhAbstract', '')
    abstract = abstract[:500] if len(abstract) > 500 else abstract

    return f"""你是专利序列分析专家。请分析以下专利中每条序列的真实用途，并以JSON格式返回结果。

专利号: {patent_id}
标题: {patent_title}
靶点: {target}

摘要:
{abstract}

权利要求（前2000字）:
{claims_text}

描述（前1500字）:
{desc_text}

需要验证的序列列表:{seq_list_text}

分析要点：
1. 结合权利要求、描述和摘要，判断每条序列在专利中的真实用途
2. 如果序列上下文中有Feature标签，优先参考标签信息
3. 判断序列对破专利的价值（CDR/VH/VL等核心序列价值高，引物/信号肽价值低）

请对每条序列返回：
1. role: 序列角色（CDR3/CDR2/CDR1/VH/VL/VHH/引物/靶点蛋白/linker/signal peptide/其他）
2. relevance: 对破专利的相关性（高/中/低）
3. guide: 具体的破专利建议（中文，50字以内）
4. confidence: 你的判断置信度（高/中/低）
5. reasoning: 简要说明判断依据（50字以内）

返回格式：
{{
  "sequences": [
    {{
      "seq_index": 1,
      "role": "...",
      "relevance": "...",
      "guide": "...",
      "confidence": "...",
      "reasoning": "..."
    }}
  ]
}}"""


def convert_three_letter_to_one(three_letter_seq: str) -> str:
    """
    将三字母氨基酸码序列转换为单字母码
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
            three = seq[i:i+3]
            if three in AA_THREE_TO_ONE:
                result.append(AA_THREE_TO_ONE[three])
                i += 3
                continue
        # 如果不匹配，跳过一个字符
        i += 1

    return ''.join(result)


def extract_st26_feature_description(text: str, seq_id: str) -> str | None:
    """
    从ST.26格式中提取序列的特征描述（<223>标签）
    用于更准确地识别序列角色
    """
    # 查找对应SEQ ID NO的<210>标签
    seq_block_pattern = re.compile(
        rf'<210>\s*{seq_id}(.*?)(?=<210>|$)',
        re.DOTALL
    )
    match = seq_block_pattern.search(text)
    if not match:
        return None

    seq_block = match.group(1)

    # 提取<223>标签内容
    feature_match = ST26_FEATURE_PATTERN.search(seq_block)
    if feature_match:
        description = feature_match.group(1).strip()
        # 清理多余的空白和标签
        description = re.sub(r'\s+', ' ', description)
        description = re.sub(r'<[^>]+>', '', description)
        return description[:200]  # 限制长度

    return None


def extract_st26_sequences(text: str) -> list[tuple[str, str, str, str | None]]:
    """
    从ST.26格式的序列表中提取序列（增强版）
    返回 list of (sequence, seq_type, context_snippet, feature_description)
    feature_description 是从<223>标签提取的序列角色描述（100%准确）
    """
    results = []

    # 查找序列表部分
    seq_listing_match = re.search(r'[Ss]equence\s+[Ll]isting', text)
    if not seq_listing_match:
        return results

    # 只处理序列表部分（从Sequence listing开始到文本结束或下一个大章节）
    seq_section = text[seq_listing_match.start():]

    # 提取所有 <400> 标记的序列
    matches = ST26_SEQ_PATTERN.findall(seq_section)

    for seq_id, seq_text in matches:
        # 转换三字母码为单字母码
        one_letter = convert_three_letter_to_one(seq_text)

        if len(one_letter) >= 5:  # 至少5个氨基酸
            # 判断序列类型
            seq_type = 'AA' if set(one_letter) <= AA_ALPHABET else 'NT'

            # 提取特征描述
            feature_desc = extract_st26_feature_description(seq_section, seq_id)

            # 构建上下文（包含SEQ ID NO和特征描述）
            context_parts = [f"SEQ ID NO: {seq_id} (from sequence listing)"]
            if feature_desc:
                context_parts.append(f"Feature: {feature_desc}")
            context_parts.append(f"Seq: {one_letter[:50]}")
            context = " | ".join(context_parts)

            results.append((one_letter, seq_type, context, feature_desc))

    return results


def generate_digester(access_key: str, access_secret: str) -> str:
    """
    动态摘要规则（你提供的图中规则）:
    data = access_key + access_secret[:10] + 当前上海时间(yyyyMMddHHmm)
    digester = sha512(data).hexdigest()
    """
    current_minutes = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d%H%M")
    data = access_key + access_secret[:10] + current_minutes
    return hashlib.sha512(data.encode("utf-8")).hexdigest()


def signed_post(api_path: str, payload: dict, timeout: int = 30) -> dict:
    last_error = None
    for attempt in range(1, 4):
        try:
            # 每次重试都重新生成 digester，避免跨分钟过期
            body = {
                "accessKey": ACCESS_KEY,
                "digester": generate_digester(ACCESS_KEY, ACCESS_SECRET),
                **payload,
            }
            response = requests.post(
                BASE_URL + api_path,
                headers={"Content-Type": "application/json"},
                json=body,
                verify=False,
                timeout=timeout,
            )
            response.encoding = "utf-8"
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"http_status": response.status_code, "raw": response.text}
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(1.2 * attempt)
    return {"error": "request_failed", "api_path": api_path, "message": last_error}


def split_time_range(start_date: str, end_date: str, months_per_chunk: int = 6) -> list[tuple[str, str]]:
    """
    将时间范围切分为多个小区间，用于突破API单次查询100条限制

    Args:
        start_date: 开始日期 "YYYY-MM-DD"
        end_date: 结束日期 "YYYY-MM-DD"
        months_per_chunk: 每个时间片的月数（默认6个月）

    Returns:
        List of (start, end) tuples
    """
    from datetime import datetime, timedelta

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    chunks = []
    current = start

    while current < end:
        # 计算下一个时间点（近似月数转天数）
        next_date = current + timedelta(days=months_per_chunk * 30)
        if next_date > end:
            next_date = end

        chunks.append((
            current.strftime("%Y-%m-%d"),
            next_date.strftime("%Y-%m-%d")
        ))

        current = next_date + timedelta(days=1)  # 避免重复

    return chunks


def query_summary_by_keyword(
    keyword: str, time_start: str, time_end: str, max_pages: int = 5, page_size: int = 100
) -> dict:
    """
    分页拉取全量摘要，支持时间分片突破100条限制

    策略：
    1. 先尝试直接分页查询
    2. 如果返回100条（可能被截断），则切分时间范围重新查询
    3. 合并去重所有结果
    """
    all_items: list[dict] = []

    # 第一轮：尝试分页查询
    for page in range(1, max_pages + 1):
        payload = {
            "keywords": [keyword],
            "timeRange": {"start": time_start, "end": time_end},
            "pageSize": page_size,
            "pageNum": page,
        }
        result = signed_post(SUMMARY_API, payload)
        items = result.get("data") or []
        if not isinstance(items, list):
            break
        all_items.extend(items)
        print(f"      Page {page}/{max_pages} -> {len(items)} items (total {len(all_items)} items)")
        if len(items) < page_size:
            break  # 已是最后一页
        time.sleep(0.5)

    # 如果累计仍然是100的整数倍，可能存在更多数据，尝试时间分片
    if len(all_items) > 0 and len(all_items) % 100 == 0 and max_pages >= 5:
        print(f"      [WARNING] Detected possible data truncation, starting time-slicing query...")

        # 计算合适的时间片大小
        from datetime import datetime
        start_dt = datetime.strptime(time_start, "%Y-%m-%d")
        end_dt = datetime.strptime(time_end, "%Y-%m-%d")
        total_days = (end_dt - start_dt).days

        # 根据总天数动态调整时间片大小
        if total_days > 3650:  # 超过10年
            months_per_chunk = 6
        elif total_days > 1825:  # 超过5年
            months_per_chunk = 3
        else:
            months_per_chunk = 2

        time_chunks = split_time_range(time_start, time_end, months_per_chunk)
        print(f"      将时间范围切分为 {len(time_chunks)} 个片段（每片约{months_per_chunk}个月）")

        # 使用字典去重（按patentId）
        all_items_by_id = {item.get("patentId"): item for item in all_items if isinstance(item, dict) and item.get("patentId")}

        for idx, (chunk_start, chunk_end) in enumerate(time_chunks, 1):
            payload = {
                "keywords": [keyword],
                "timeRange": {"start": chunk_start, "end": chunk_end},
                "pageSize": page_size,
                "pageNum": 1,
            }
            result = signed_post(SUMMARY_API, payload)
            items = result.get("data") or []

            if isinstance(items, list):
                new_count = 0
                for item in items:
                    if isinstance(item, dict):
                        patent_id = item.get("patentId")
                        if patent_id and patent_id not in all_items_by_id:
                            all_items_by_id[patent_id] = item
                            new_count += 1

                print(f"        Chunk {idx}/{len(time_chunks)} ({chunk_start}~{chunk_end}) -> {len(items)} items, new: {new_count}")

            time.sleep(0.3)

        all_items = list(all_items_by_id.values())
        print(f"      [OK] Time-slicing query completed, total {len(all_items)} unique patents")

    return {"code": 200, "message": "success", "data": all_items}


def collect_patent_ids(summary_result: dict) -> list[str]:
    data = summary_result.get("data")
    if not isinstance(data, list):
        return []
    patent_ids = []
    for item in data:
        if isinstance(item, dict):
            patent_id = item.get("patentId")
            if patent_id:
                patent_ids.append(patent_id)
    return sorted(set(patent_ids))


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def query_detail_by_patent_ids(
    patent_ids: list[str], detail_api: str, batch_size: int = 20
) -> list[dict]:
    all_details = []
    for batch in chunked(patent_ids, batch_size):
        detail_result = signed_post(detail_api, {"patentIds": batch})
        all_details.append({"request_patent_ids": batch, "response": detail_result})
    return all_details


def _flatten_field(value) -> str:
    """把 claims/descriptions 等可能是 list[dict] 的字段拍平为纯文本。"""
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


def _extract_sequences_with_context(text: str) -> list[tuple[str, str, str]]:
    """
    返回 list of (sequence, seq_type, context_snippet)。
    seq_type: 'NT' | 'AA'
    context_snippet: 序列前后约 120 字符，帮助同事判断这段序列的用途。
    """
    text = text or ""
    if len(text) > 40000:
        text = text[:24000] + "\n" + text[-16000:]

    results: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def _ctx(m_or_start, m_or_end=None, span=120):
        start = m_or_start if isinstance(m_or_start, int) else m_or_start.start()
        end = m_or_end if isinstance(m_or_end, int) else m_or_start.end()
        snippet = text[max(0, start - span): min(len(text), end + span)]
        return re.sub(r"\s+", " ", snippet).strip()

    # 1. 5'/3' 核酸序列
    for m in NT_PRIME_PATTERN.finditer(text):
        normalized = re.sub(r"[\s\-]", "", m.group(1)).upper()
        if len(normalized) >= 6 and normalized not in seen:
            seen.add(normalized)
            results.append((normalized, "NT", _ctx(m)))

    # 2. SEQ ID NO 后的序列（最可靠，优先处理）
    for m in SEQ_ID_PATTERN.finditer(text):
        normalized = m.group(1)          # 已是大写（pattern 捕获 [A-Z]）
        chars = set(normalized)
        if normalized in seen:
            continue
        # 跳过明显是英文词的：以常见后缀结尾，或长度<=9且全部字母都是常见英文字母
        if _EN_SUFFIX.search(normalized):
            continue
        if chars.issubset(NT_ALPHABET) and len(normalized) >= 6:
            seen.add(normalized)
            results.append((normalized, "NT", _ctx(m)))
        elif chars.issubset(AA_ALPHABET) and len(normalized) >= 8 and len(chars) >= 3:
            if not chars.issubset(NT_ALPHABET):
                seen.add(normalized)
                results.append((normalized, "AA", _ctx(m)))

    # Rule 3: 裸AA片段（上下文关键词过滤，降低误报率）
    # 只提取出现在序列相关关键词附近的连续AA序列
    _RARE_AA = set("WYHFQ")  # 这些氨基酸字母在英文词中罕见，有则更可能是真实序列
    for m in _AA_STRICT.finditer(text):
        normalized = m.group(1)
        if normalized in seen:
            continue
        if _EN_SUFFIX.search(normalized):
            continue
        chars = set(normalized)
        if chars.issubset(NT_ALPHABET):  # 纯核酸字母，跳过
            continue
        if len(chars) < 4:  # 字符种类太少
            continue
        if not (chars & _RARE_AA):  # 不含任何稀有氨基酸，可能是英文词
            continue
        # 检查上下文：序列前后300字符内必须有序列相关关键词
        ctx_start = max(0, m.start() - 300)
        ctx_end = min(len(text), m.end() + 300)
        context_window = text[ctx_start:ctx_end]
        if not _BARE_AA_CTX_KW.search(context_window):
            continue
        seen.add(normalized)
        results.append((normalized, "AA", _ctx(m)))

    return results


def extract_sequences_from_patent_record(record: dict) -> list[tuple[str, str, str, str | None, str]]:
    """
    从单个专利详情中提取所有序列，返回 list of (sequence, seq_type, context, feature_desc, source)。
    扫描顺序：权利要求 > 摘要 > 说明书（越靠前越核心）。
    增强：从ST.26格式的序列表中提取三字母码序列。

    返回值：
    - sequence: 序列字符串
    - seq_type: AA或NT
    - context: 上下文
    - feature_desc: ST.26的<223>标签描述（仅ST.26来源有值）
    - source: 序列来源标识（"ST.26" / "SEQ_ID_NO" / "bare"）
    """
    if not isinstance(record, dict):
        return []

    seen: set[str] = set()
    results: list[tuple[str, str, str, str | None, str]] = []

    # 1. ST.26序列表提取（优先级最高，100%准确）
    desc_text = _flatten_field(record.get("descriptions"))
    for seq, stype, ctx, feature_desc in extract_st26_sequences(desc_text):
        if seq not in seen:
            seen.add(seq)
            results.append((seq, stype, ctx, feature_desc, "ST.26"))

    # 2. 传统提取：从claims, abstracts, descriptions中提取
    for key in ["claims", "enAbstract", "zhAbstract", "descriptions"]:
        text = _flatten_field(record.get(key))
        for seq, stype, ctx in _extract_sequences_with_context(text):
            if seq not in seen:
                seen.add(seq)
                # 判断是否有SEQ ID NO引用
                source = "SEQ_ID_NO" if "SEQ ID NO" in ctx else "bare"
                results.append((seq, stype, ctx, None, source))

    return results


# 旧接口保留，供 main() 里的全局统计使用
def extract_sequences_from_obj(obj) -> dict:
    if isinstance(obj, dict):
        all_seqs = extract_sequences_from_patent_record(obj)
    elif isinstance(obj, (list, str)):
        text = _flatten_field(obj) if not isinstance(obj, str) else obj
        all_seqs = [(s, t, c, None, "bare") for s, t, c in _extract_sequences_with_context(text)]
    else:
        all_seqs = []
    return {
        "aa_sequences": sorted({s for s, t, _, _, _ in all_seqs if t == "AA"}),
        "nt_sequences": sorted({s for s, t, _, _, _ in all_seqs if t == "NT"}),
    }


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def flatten_detail_records(details: list[dict]) -> dict:
    patent_map = {}
    for batch in details:
        response = batch.get("response", {})
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, list):
            continue
        for item in data:
            if isinstance(item, dict) and item.get("patentId"):
                patent_map[item["patentId"]] = item
    return patent_map


def short_text(text: str, max_len: int = 200) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def choose_title(record: dict) -> str:
    return (record.get("zhName") or "").strip() or (record.get("enName") or "").strip()


def choose_abstract(record: dict) -> str:
    return (record.get("zhAbstract") or "").strip() or (record.get("enAbstract") or "").strip()


# 专利状态 → 破专利行动建议
_STATUS_ACTION = {
    "granted":              ("有效",   "*** 有效专利 — 需规避权利要求范围，或收集现有技术举证无效"),
    "pending":              ("审查中", "**  申请中 — 可监控权利要求变化，适时提出异议/第三方意见"),
    "abandoned":            ("已放弃", "*   已放弃 — 序列无侵权风险，可自由参考"),
    "expired":              ("已到期", "*   已到期 — 序列已进入公有领域，可自由使用"),
    "lapsed":               ("已失效", "*   已失效 — 同上"),
    "withdrawn":            ("已撤回", "*   已撤回 — 无侵权风险"),
}

def _action_note(status: str) -> tuple[str, str]:
    """返回 (状态标签, 建议文字)"""
    s = (status or "").lower()
    for key, val in _STATUS_ACTION.items():
        if key in s:
            return val
    return ("未知", "[!] 状态未知，请人工核查专利法律状态")


def _annotate(seq: str, seq_type: str, context: str, target: str, source: str = "bare") -> tuple[str, str, str, str]:
    """
    根据序列上下文自动推断：
      seq_role        — 这条序列是什么（CDR/VH/VL/siRNA/PCR引物/靶点蛋白…）
      break_relevance — 对破专利的相关度：高 / 中 / 低
      break_guide     — 具体行动建议
      confidence_level — 序列可信度等级

    增强版：优先利用ST.26的Feature标签进行精确识别

    Args:
        seq: 序列字符串
        seq_type: AA或NT
        context: 上下文
        target: 靶点名称
        source: 序列来源（"ST.26" / "SEQ_ID_NO" / "bare"）

    Returns:
        (seq_role, break_relevance, break_guide, confidence_level)
    """
    ctx = (context or "").lower()
    tgt = target.lower()

    # 确定可信度等级
    if source == "ST.26":
        confidence = "高（ST.26序列表）"
    elif source == "SEQ_ID_NO":
        confidence = "中（SEQ ID NO引用）"
    else:
        confidence = "低（裸序列）"

    # ========== 优先检查ST.26特征描述（最准确）==========
    if "feature:" in ctx:
        # 提取特征描述部分
        feature_match = re.search(r'feature:\s*([^|]+)', ctx, re.IGNORECASE)
        if feature_match:
            feature = feature_match.group(1).strip().lower()

            # 根据特征描述精确识别
            if "cdr" in feature:
                if "cdr3" in feature or "cdr-h3" in feature or "cdr-l3" in feature or "hcdr3" in feature or "lcdr3" in feature:
                    return ("CDR3（互补决定区3）", "高", "CDR3是抗体特异性核心，必须在IMGT/PDB中全面检索先有技术", confidence)
                elif "cdr2" in feature or "cdr-h2" in feature or "cdr-l2" in feature or "hcdr2" in feature or "lcdr2" in feature:
                    return ("CDR2（互补决定区2）", "高", "CDR2影响抗体亲和力，需检索现有技术中是否有相同序列", confidence)
                elif "cdr1" in feature or "cdr-h1" in feature or "cdr-l1" in feature or "hcdr1" in feature or "lcdr1" in feature:
                    return ("CDR1（互补决定区1）", "高", "CDR1是抗体特异性关键区域，需在IMGT数据库中检索是否有先有技术", confidence)
                else:
                    return ("CDR序列（互补决定区）", "高", "CDR是抗体专利保护核心，需比对IMGT数据库确认是否有先有技术", confidence)

            if ("variable" in feature or "vhh" in feature or "vh" in feature) and ("heavy" in feature or "chain" in feature):
                return ("VH重链可变区", "高", "用BLAST比对IMGT/PDB，若存在更早相同/高度相似序列可举证无效", confidence)

            if ("variable" in feature or "vl" in feature) and ("light" in feature or "kappa" in feature or "lambda" in feature):
                return ("VL轻链可变区", "高", "轻链+重链组合构成完整抗体，需连同重链一并评估保护范围", confidence)

            if "signal" in feature or "leader" in feature or "secretion" in feature:
                return ("信号肽序列", "低", "信号肽通常不是专利保护重点，可忽略", confidence)

            if "linker" in feature or "connector" in feature:
                return ("Linker连接肽", "中", "Linker序列可能影响抗体构象，需确认是否为专利保护范围", confidence)

            if "framework" in feature or "fr1" in feature or "fr2" in feature or "fr3" in feature or "fr4" in feature:
                return ("Framework区（框架区）", "中", "Framework区相对保守，但仍需检索是否有先有技术", confidence)

            if "constant" in feature:
                return ("恒定区序列", "低", "恒定区通常不是专利保护重点，除非有特殊修饰", confidence)


    # ========== 传统上下文关键词识别 ==========
    if re.search(r"sirna|si rna|small.interfering", ctx):
        if re.search(r"sense.strand|positive.sense|正义链", ctx):
            role = "siRNA正义链"
        elif re.search(r"anti.?sense|反义", ctx):
            role = "siRNA反义链"
        else:
            role = "siRNA序列"
        relevance = "高" if tgt in ctx else "中"
        guide = "检索同靶点更早的siRNA文献/专利（PubMed/CNKI），若有先有技术可质疑新颖性；确认与我方siRNA靶向位点是否重叠"

    elif re.search(r"lna|antisense oligonucleotide|反义寡核苷酸", ctx):
        role = "LNA/反义寡核苷酸"
        relevance = "高"
        guide = "检索早期antisense/LNA相关文献，关注靶点pre-mRNA剪接位点是否有先有技术"

    elif re.search(r"\bprimer\b|pcr.amplif|amplif.+primer", ctx):
        role = "PCR引物（实验工具）"
        relevance = "低"
        guide = "PCR引物为实验操作序列，与专利核心保护范围关联弱，无直接侵权风险，可忽略"

    # 代谢/内参基因引物：出现代谢基因名称，与靶点无关
    elif seq_type == "NT" and re.search(
        r"\bpepck\b|\bmdh2\b|\bstat3\b|\bsrebf\b|\bhmg.coa\b|\bipp1\b|\busf1\b"
        r"|\b18s\b|\bgapdh\b|\bactin\b|\bhprt\b|\brnf13\b|\bnm_\d+",
        ctx,
    ):
        role = "代谢/内参基因引物（与靶点无关）"
        relevance = "低"
        guide = f"这些是用于肝癌细胞代谢研究的内参引物，与{target}抗体/靶点序列无关，不影响破专利策略，可忽略"

    elif re.search(r"cdr3|hcdr3|lcdr3|third complementarity", ctx):
        role = "CDR3（互补决定区3，抗体特异性最强）"
        relevance = "高"
        guide = "CDR3是抗体特异性核心，必须设计不同CDR3规避；同时检索PDB/IMGT中相同或高相似CDR3的早期报道以举证无效"
    elif re.search(r"cdr2|hcdr2|lcdr2", ctx):
        role = "CDR2（互补决定区2）"
        relevance = "高"
        guide = "检索IMGT中相似CDR2以寻找先有技术；设计新抗体须连同CDR3一并规避"
    elif re.search(r"cdr1|hcdr1|lcdr1", ctx):
        role = "CDR1（互补决定区1）"
        relevance = "高"
        guide = "检索IMGT/PDB中相似CDR1；与CDR2/CDR3联合评估保护范围，单独CDR1通常不足以颠覆专利"
    elif re.search(r"\bcdr\b|complementarity.{0,30}determin", ctx):
        role = "CDR序列（互补决定区）"
        relevance = "高"
        guide = "CDR是抗体专利保护核心，需比对IMGT数据库确认是否有先有技术，设计新抗体须全面规避"

    elif re.search(r"\bvhh\b|nanobody|single.domain.antibody|单域抗体|纳米抗体", ctx):
        role = "纳米抗体序列（VHH单域）"
        relevance = "高"
        guide = f"检索更早的抗{target} VHH文献；或自行开发CDR不同的VHH进行规避"

    elif re.search(r"heavy.{0,15}chain|heavy.{0,15}variable|\bvh\b|重链", ctx):
        role = "VH重链可变区"
        relevance = "高"
        guide = "用BLAST比对该VH与IMGT/PDB，若存在更早相同/高度相似序列可举证无效；设计新抗体须使用不同重链序列"
    elif re.search(r"light.{0,15}chain|light.{0,15}variable|\bvl\b|轻链", ctx):
        role = "VL轻链可变区"
        relevance = "高"
        guide = "轻链+重链组合构成完整抗体，若轻链有先有技术则保护范围受限；设计时须连同重链一并更换"

    elif re.search(r"amino acid sequence of.{0,30}" + re.escape(tgt) + r"|靶.{0,5}蛋白.{0,10}序列", ctx):
        role = f"{target}靶点蛋白序列"
        relevance = "中"
        guide = "靶点本身序列通常无法单独被专利保护，可用于确认抗体靶向的表位位置"

    elif re.search(r"polypeptide.{0,20}ligand|peptide.{0,20}ligand|多肽配体", ctx):
        role = "多肽配体序列"
        relevance = "高" if tgt in ctx else "中"
        guide = "检索更早的同靶点结合多肽文献，若有先有技术则该专利新颖性存疑"

    # ── 根据序列本身的特征推断（当上下文无描述性文字时）──────────────────
    # 抗体重链可变区：EVQL / QVQL / QESG 开头的长 AA（>40 AA）
    elif seq_type == "AA" and re.match(r"^(EVQL|QVQL|DVQL|EIEQ|QLES)", seq.upper()) and len(seq) > 40:
        role = "VH重链可变区（由序列特征推断）"
        relevance = "高"
        guide = "用BLAST比对IMGT/PDB，若存在更早相同/高度相似序列可举证无效；设计新抗体须使用不同重链序列"

    # 抗体轻链可变区：DIVL / DIQM / EIVL / SYEL 开头的长 AA（>40 AA）
    elif seq_type == "AA" and re.match(r"^(DIVL|DIVT|DIQM|EIVL|SYEL|SCSS|QSVL|QSALT)", seq.upper()) and len(seq) > 40:
        role = "VL轻链可变区（由序列特征推断）"
        relevance = "高"
        guide = "轻链+重链组合构成完整抗体，若轻链有先有技术则保护范围受限；设计时须连同重链一并更换"

    # 短 AA（≤20 AA）且为纯 AA 字符：大概率是 CDR 片段
    elif seq_type == "AA" and len(seq) <= 20 and re.match(r"^[ACDEFGHIKLMNPQRSTVWY]+$", seq.upper()):
        role = "CDR片段（短肽，由序列长度推断）"
        relevance = "高"
        guide = "短肽序列通常对应 CDR1/CDR2/CDR3，需人工对照权利要求确认是哪段 CDR，再检索 IMGT 中是否有先有技术"

    elif seq_type == "NT":
        role = "核苷酸序列（用途未明，需人工核查）"
        relevance = "低" if tgt not in ctx else "中"
        guide = "需人工确认该序列在专利中的具体用途（如克隆载体/报告基因/内参引物），再评估相关性"

    else:
        role = "氨基酸序列（用途未明，需人工核查）"
        relevance = "中"
        guide = "建议用BLAST（NCBI/IMGT）比对，确认序列来源及其在公开数据库中的最早出现时间"

    return role, relevance, guide, confidence


def generate_final_recommendation(
    confidence: str,
    relevance: str,
    status_label: str,
    seq_role: str,
    sequence: str
) -> str:
    """
    综合所有信息，生成最终的破专利建议（结论列）

    Args:
        confidence: 可信度等级
        relevance: 相关性（高/中/低）
        status_label: 专利状态标签
        seq_role: 序列角色
        sequence: 序列字符串

    Returns:
        明确的破专利建议
    """
    # 如果没有序列，返回空
    if not sequence:
        return ""

    # 低可信度序列需要人工核查
    if "低" in confidence:
        return "⚠️ 序列准确性存疑，需人工核查后再制定策略"

    # 专利已失效，可自由使用
    if status_label in ["已放弃", "已到期", "已撤回"]:
        return "✅ 专利已失效，可自由使用"

    # 高相关性序列
    if relevance == "高":
        if "CDR3" in seq_role:
            return "🔴 高风险：CDR3序列是核心保护对象，必须设计不同CDR或寻找先有技术"
        elif "CDR" in seq_role:
            return "🔴 高风险：CDR序列受专利保护，必须设计不同序列或举证无效"
        elif "VH" in seq_role or "VL" in seq_role or "重链" in seq_role or "轻链" in seq_role:
            return "🔴 高风险：可变区序列受保护，需设计不同序列或举证无效"
        elif "siRNA" in seq_role or "LNA" in seq_role or "反义" in seq_role:
            return "🔴 高风险：核酸药物序列受保护，需设计不同靶向位点或举证无效"
        elif "多肽配体" in seq_role or "纳米抗体" in seq_role:
            return "🔴 高风险：结合分子受保护，需设计不同序列或举证无效"
        else:
            return "🔴 高风险：该序列对破专利高度相关，建议BLAST比对寻找先有技术"

    # 中相关性序列
    if relevance == "中":
        return "🟡 中风险：建议BLAST比对寻找先有技术，或设计替代序列"

    # 低相关性序列
    if relevance == "低":
        return "🟢 低风险：该序列对破专利价值有限，可关注其他序列"

    return "需进一步分析"


def _apply_llm_verification(rows: list[dict], target: str, patent_json_map: dict, use_llm: bool = True) -> None:
    """
    对需要LLM验证的序列使用LLM验证，直接修改rows（in-place）。

    验证策略：
    - 有<223>标签 + 成功识别 → 不用Agent
    - 有<223>标签 + 未识别 → 用Agent
    - 无<223>标签（SEQ ID NO或裸序列）→ 用Agent

    Args:
        rows: CSV行列表（每行是一个dict）
        target: 靶点名
        patent_json_map: 专利ID到完整JSON的映射 {patent_id: patent_json_dict}
        use_llm: 是否启用LLM（False时跳过）
    """
    if not use_llm or not LLM_API_KEY:
        print("  [跳过LLM验证] 未配置API密钥或使用 --no-llm")
        return

    # 1. 筛选需要LLM验证的序列
    need_llm_rows = []
    for r in rows:
        if not r.get("sequence"):
            continue

        confidence = r.get("confidence_level", "")
        seq_role = r.get("seq_role", "")
        feature_desc = r.get("feature_desc", "")

        # 策略1: ST.26序列但无<223>标签（feature_desc为空）
        if "高（ST.26序列表）" in confidence and not feature_desc:
            need_llm_rows.append(r)
        # 策略2: 有<223>标签但未识别（seq_role包含"用途未明"）
        elif "高（ST.26序列表）" in confidence and "用途未明" in seq_role:
            need_llm_rows.append(r)
        # 策略3: 中可信度（SEQ ID NO）
        elif "中（SEQ ID NO引用）" in confidence:
            need_llm_rows.append(r)
        # 策略4: 低可信度（裸序列）
        elif "低（裸序列）" in confidence:
            need_llm_rows.append(r)

    if not need_llm_rows:
        print("  [跳过LLM验证] 无需验证的序列")
        return

    print(f"  [LLM验证] 发现 {len(need_llm_rows)} 条需验证序列")

    # 2. 按patent_id分组
    from collections import defaultdict
    patent_groups = defaultdict(list)
    for r in need_llm_rows:
        patent_groups[r["patent_id"]].append(r)

    print(f"  [LLM验证] 涉及 {len(patent_groups)} 个专利")

    # 3. 批量调用LLM
    verified_count = 0
    failed_count = 0

    for patent_id, seqs in patent_groups.items():
        # 从patent_json_map获取该专利的完整JSON
        patent_json = patent_json_map.get(patent_id, {})

        if not patent_json:
            print(f"    [警告] 未找到专利 {patent_id} 的JSON数据，跳过")
            for row in seqs:
                row["llm_verified"] = "失败（无JSON数据）"
                row["llm_confidence"] = ""
                row["llm_reasoning"] = ""
                row["llm_raw_response"] = ""
            failed_count += len(seqs)
            continue

        # 构建增强版prompt（使用完整JSON）
        prompt = build_llm_prompt_for_patent(
            patent_id=patent_id,
            patent_json=patent_json,
            sequences=seqs,
            target=target
        )

        # 调用LLM
        result = call_llm(prompt)

        if result and "sequences" in result:
            # 解析结果并更新rows
            for llm_seq in result["sequences"]:
                idx = llm_seq.get("seq_index", 0) - 1  # 1-based → 0-based
                if 0 <= idx < len(seqs):
                    row = seqs[idx]
                    row["seq_role"] = llm_seq.get("role", row["seq_role"])
                    row["break_relevance"] = llm_seq.get("relevance", row["break_relevance"])
                    row["break_guide"] = llm_seq.get("guide", row["break_guide"])
                    row["llm_confidence"] = llm_seq.get("confidence", "中")
                    row["llm_reasoning"] = llm_seq.get("reasoning", "")
                    row["llm_raw_response"] = json.dumps(llm_seq, ensure_ascii=False)

                    # 更新confidence_level，裸序列按LLM置信度分流
                    is_bare = "低（裸序列）" in row["confidence_level"]
                    llm_conf = llm_seq.get("confidence", "中")

                    if is_bare:
                        if llm_conf == "高":
                            # 裸序列LLM高置信 → 进入高可信度和llm_verified CSV
                            row["confidence_level"] = "中（LLM验证-高置信）"
                            row["llm_verified"] = "是"
                            verified_count += 1
                        else:
                            # 裸序列LLM中/低置信 → 只进入完整CSV
                            row["confidence_level"] = "低（LLM验证-低置信）"
                            row["llm_verified"] = "低置信"
                            verified_count += 1
                    else:
                        # 非裸序列：原有逻辑
                        row["llm_verified"] = "是"
                        if llm_conf == "高":
                            if "高（ST.26序列表）" in row["confidence_level"]:
                                row["confidence_level"] = "高（ST.26+LLM验证）"
                            else:
                                row["confidence_level"] = "中（LLM验证-高置信）"
                        else:
                            if "高（ST.26序列表）" in row["confidence_level"]:
                                row["confidence_level"] = "高（ST.26+LLM验证）"
                            else:
                                row["confidence_level"] = "中（LLM验证）"
                        verified_count += 1

                    # 重新生成final_recommendation
                    row["final_recommendation"] = generate_final_recommendation(
                        row["confidence_level"],
                        row["break_relevance"],
                        row["status_label"],
                        row["seq_role"],
                        row["sequence"]
                    )
        else:
            # LLM失败，保持原有推断
            for row in seqs:
                row["llm_verified"] = "失败"
                row["llm_confidence"] = ""
                row["llm_reasoning"] = ""
                row["llm_raw_response"] = ""
            failed_count += len(seqs)

        time.sleep(0.5)  # 避免限流

    print(f"  [LLM验证完成] 成功: {verified_count}, 失败: {failed_count}")


CSV_FIELDS = [
    # ── 核心三列 ────────────────────────────────────────────────────
    "target",           # 靶点
    "patent_id",        # 专利号
    "sequence",         # 单条序列（每条占一行）
    # ── 序列解读（最关键，让同事一眼看懂）──────────────────────────
    "seq_role",         # 序列是什么：CDR3 / VH / siRNA正义链 / PCR引物…
    "break_relevance",  # 对破专利的相关度：高 / 中 / 低
    "break_guide",      # 针对这条序列的具体破专利行动建议
    "confidence_level", # 序列可信度：高（ST.26序列表）/ 中（SEQ ID NO引用）/ 低（裸序列）
    "final_recommendation", # 综合结论：明确的破专利建议
    # ── LLM验证字段（新增）──────────────────────────────────────────
    "llm_verified",     # 是否LLM验证：是/否/失败
    "llm_confidence",   # LLM置信度：高/中/低
    "llm_reasoning",    # LLM判断依据（50字以内）
    "llm_raw_response", # LLM原始JSON响应
    # ── 序列基本属性 ────────────────────────────────────────────────
    "seq_type",         # AA（氨基酸）或 NT（核酸）
    "seq_context",      # 序列在专利原文中的上下文（原始文本，供核对）
    "feature_desc",     # ST.26的<223>标签内容（仅ST.26序列有值）
    # ── 专利状态（第二重要）────────────────────────────────────────
    "status_label",     # 有效 / 审查中 / 已放弃 / 已到期
    "action_note",      # 整体破专利建议（按专利状态）
    "patent_status",    # 原始状态字段
    "publication_date", # 公开日（越早越多现有技术可引用）
    # ── 专利背景信息 ────────────────────────────────────────────────
    "title",
    "assignees",
    "abstract_brief",
    "claims_brief",
]


def export_keyword_csvs(
    out_dir: Path, run_ts: str, keywords: list[str], all_summaries: dict, detail_map: dict, use_llm: bool = True
) -> list[Path]:
    csv_paths = []

    # 构建patent_json_map（专利ID到完整JSON的映射）
    patent_json_map = {}
    for patent_id, detail_item in detail_map.items():
        if isinstance(detail_item, dict):
            patent_json_map[patent_id] = detail_item

    for keyword in keywords:
        summary_data = all_summaries.get(keyword, {}).get("data", [])
        if not isinstance(summary_data, list):
            summary_data = []

        rows = []
        for summary_item in summary_data:
            if not isinstance(summary_item, dict):
                continue
            patent_id = summary_item.get("patentId", "")
            detail_item = detail_map.get(patent_id, {})
            source = detail_item if detail_item else summary_item

            status = source.get("status", "")
            status_label, note = _action_note(status)
            title = choose_title(source)
            assignees = " | ".join(source.get("assignees", []) or [])
            pub_date = source.get("publicationDate", "")
            abstract = short_text(choose_abstract(source))

            # 权利要求摘要
            claims_raw = _flatten_field(source.get("claims"))
            claims_brief = short_text(claims_raw, max_len=300)

            # 提取序列（每条一行）
            seqs = extract_sequences_from_patent_record(source)

            if seqs:
                for seq, stype, ctx, feature_desc, src in seqs:
                    role, relevance, guide, confidence = _annotate(seq, stype, ctx, keyword, src)
                    final_rec = generate_final_recommendation(confidence, relevance, status_label, role, seq)
                    rows.append({
                        "target":           keyword,
                        "patent_id":        patent_id,
                        "sequence":         seq,
                        "seq_role":         role,
                        "break_relevance":  relevance,
                        "break_guide":      guide,
                        "confidence_level": confidence,
                        "final_recommendation": final_rec,
                        "seq_type":         stype,
                        "seq_context":      short_text(ctx, max_len=150),
                        "feature_desc":     feature_desc or "",  # 保存<223>标签内容
                        "status_label":     status_label,
                        "action_note":      note,
                        "patent_status":    status,
                        "publication_date": pub_date,
                        "title":            title,
                        "assignees":        assignees,
                        "abstract_brief":   abstract,
                        "claims_brief":     claims_brief,
                    })
            else:
                # 无提取到序列的专利也保留一行，便于同事全局查看
                rows.append({
                    "target":           keyword,
                    "patent_id":        patent_id,
                    "sequence":         "",
                    "seq_role":         "",
                    "break_relevance":  "",
                    "break_guide":      "",
                    "confidence_level": "",
                    "final_recommendation": "",
                    "seq_type":         "",
                    "seq_context":      "",
                    "status_label":     status_label,
                    "action_note":      note,
                    "patent_status":    status,
                    "publication_date": pub_date,
                    "title":            title,
                    "assignees":        assignees,
                    "abstract_brief":   abstract,
                    "claims_brief":     claims_brief,
                })

        # 排序：有效专利优先，有效期内按公开日倒序；放弃/到期排后
        def _sort_key(r):
            urgency = 0 if "有效" in r["status_label"] else (1 if "审查" in r["status_label"] else 2)
            has_seq = 0 if r["sequence"] else 1
            return (urgency, has_seq, r["publication_date"])

        rows.sort(key=_sort_key)

        # ⭐ LLM验证步骤（使用完整JSON）
        _apply_llm_verification(rows, keyword, patent_json_map, use_llm=use_llm)

        safe_kw = re.sub(r"[^A-Za-z0-9_-]+", "_", keyword)

        # 输出完整CSV（包含所有序列）
        csv_file_all = out_dir / f"{safe_kw}_patent_sequences_{run_ts}.csv"
        csv_file_all.parent.mkdir(parents=True, exist_ok=True)
        with csv_file_all.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        csv_paths.append(csv_file_all)

        # 输出高可信度CSV（只包含ST.26序列和LLM验证的高置信序列）
        high_confidence_rows = [
            r for r in rows
            if "高（ST.26序列表）" in r.get("confidence_level", "")
            or "高（ST.26+LLM验证）" in r.get("confidence_level", "")
            or "中（LLM验证-高置信）" in r.get("confidence_level", "")
        ]
        if high_confidence_rows:
            csv_file_high = out_dir / f"{safe_kw}_patent_sequences_{run_ts}_high_confidence.csv"
            with csv_file_high.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerows(high_confidence_rows)
            csv_paths.append(csv_file_high)

        # 输出LLM验证CSV（包含所有LLM验证成功的序列）
        llm_verified_rows = [r for r in rows if r.get("llm_verified") == "是"]
        if llm_verified_rows:
            csv_file_llm = out_dir / f"{safe_kw}_patent_sequences_{run_ts}_llm_verified.csv"
            with csv_file_llm.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerows(llm_verified_rows)
            csv_paths.append(csv_file_llm)

    return csv_paths


# ── 目标 → 关键词组映射 ────────────────────────────────────────────────────
# 每个靶点可对应多个同义词关键词，结果自动合并去重后生成一张 CSV
TARGET_KEYWORD_GROUPS: dict[str, list[str]] = {
    "TLR2":  ["TLR2"],           # 单关键词，限制 2015+ 和 max_pages
    "CD318": ["CD318", "CDCP1"], # CD318 别名 CDCP1，合并去重
}
# 根据实际数据分布优化起始年份，避免查询无数据的历史时期
TARGET_START_OVERRIDE: dict[str, str] = {
    "TLR2": "2015-01-01",    # TLR2数据从2015年开始
    "CD318": "2003-01-01",   # CD318/CDCP1数据从2003年开始
    "CDCP1": "2003-01-01",   # 同CD318
}


def main():
    parser = argparse.ArgumentParser(
        description="关键词摘要 -> 专利号详情 -> 抽取抗体/核酸序列（支持分页+多关键词合并）"
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        default=list(TARGET_KEYWORD_GROUPS.keys()),
        help="靶点列表（默认 TLR2 CD318）",
    )
    parser.add_argument("--start", default=DEFAULT_TIME_START, help="时间范围起始日期")
    parser.add_argument("--end", default=DEFAULT_TIME_END, help="时间范围结束日期")
    parser.add_argument(
        "--max-pages", type=int, default=5,
        help="每个关键词最多取多少页（每页100条，默认5页=500条）",
    )
    parser.add_argument(
        "--detail-api",
        default=DETAIL_API,
        help="专利详情接口路径（默认 /patent/pass/ids/detail）",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="输出目录（默认 outputs）",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="禁用LLM验证（仅使用规则推断）"
    )
    parser.add_argument(
        "--llm-dry-run",
        action="store_true",
        help="预估LLM成本但不实际调用"
    )
    args = parser.parse_args()

    # 判断是否使用LLM
    use_llm = not args.no_llm and bool(LLM_API_KEY)

    if args.llm_dry_run:
        print("\n[LLM成本预估模式] 将跳过实际LLM调用")
        use_llm = False

    run_ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir)

    # all_summaries: {target: {"data": [合并所有关键词的摘要items]}}
    all_summaries: dict[str, dict] = {}
    all_patent_ids: set[str] = set()

    for target in args.targets:
        keywords = TARGET_KEYWORD_GROUPS.get(target, [target])
        start = TARGET_START_OVERRIDE.get(target, args.start)
        target_items: list[dict] = []
        seen_ids: set[str] = set()

        print(f"\n[1/3] 查询靶点 [{target}] 关键词: {keywords}  时间: {start} ~ {args.end}  max_pages={args.max_pages}")
        for kw in keywords:
            print(f"  关键词: {kw}")
            summary = query_summary_by_keyword(kw, start, args.end, max_pages=args.max_pages)
            for item in (summary.get("data") or []):
                if isinstance(item, dict):
                    pid = item.get("patentId")
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        target_items.append(item)
            time.sleep(0.3)

        all_summaries[target] = {"code": 200, "message": "success", "data": target_items}
        all_patent_ids.update(seen_ids)
        print(f"  靶点 {target} 合并去重后专利数: {len(seen_ids)}")

    merged_patent_ids = sorted(all_patent_ids)
    print(f"\n[2/3] 所有靶点合并去重后专利总数: {len(merged_patent_ids)}")

    details = []
    if merged_patent_ids:
        print(f"      开始拉取详情（共 {len(merged_patent_ids)} 个专利，每批20个）...")
        details = query_detail_by_patent_ids(merged_patent_ids, args.detail_api)
        print(f"[3/3] 详情接口调用批次: {len(details)}")
    else:
        print("[3/3] 无专利号可查，跳过详情接口")

    detail_map = flatten_detail_records(details)

    # 全局序列统计
    def _tally(records):
        aa, nt = set(), set()
        for item in records:
            for seq, stype, _, _, _ in extract_sequences_from_patent_record(item):
                (aa if stype == "AA" else nt).add(seq)
        return sorted(aa), sorted(nt)

    summary_items = [
        item
        for p in all_summaries.values()
        if isinstance(p, dict)
        for item in p.get("data", [])
        if isinstance(item, dict)
    ]
    aa_s, nt_s = _tally(summary_items)
    aa_d, nt_d = _tally(detail_map.values())

    extracted = {
        "aa_sequences": sorted(set(aa_s + aa_d)),
        "nt_sequences": sorted(set(nt_s + nt_d)),
    }
    print(
        f"提取到候选序列: 蛋白/抗体 {len(extracted['aa_sequences'])} 条, "
        f"核酸 {len(extracted['nt_sequences'])} 条"
    )

    output = {
        "meta": {
            "run_time_shanghai": run_ts,
            "base_url": BASE_URL,
            "summary_api": SUMMARY_API,
            "detail_api": args.detail_api,
            "targets": args.targets,
            "target_keyword_groups": TARGET_KEYWORD_GROUPS,
            "max_pages_per_keyword": args.max_pages,
            "time_range": {"start": args.start, "end": args.end},
            "merged_patent_count": len(merged_patent_ids),
        },
        "patent_ids": merged_patent_ids,
        "summaries_by_target": all_summaries,
        "details": details,
        "extracted_sequences": extracted,
    }

    out_file = out_dir / f"patent_antibody_result_{run_ts}.json"
    save_json(out_file, output)
    # 按靶点（target）生成 CSV，而非关键词
    csv_files = export_keyword_csvs(out_dir, run_ts, args.targets, all_summaries, detail_map, use_llm=use_llm)

    print(f"\n结果已保存: {out_file.resolve()}")
    for csv_file in csv_files:
        print(f"CSV 已生成: {csv_file.resolve()}")
    detail_no_permission = any(
        isinstance(item, dict)
        and isinstance(item.get("response"), dict)
        and item["response"].get("code") == 1005
        for item in details
    )
    if detail_no_permission:
        print("提示: 详情返回 code=1005，当前 key 对详情接口无权限。")


if __name__ == "__main__":
    main()
