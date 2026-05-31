"""
按 CSV 文件中的专利号调用 /patent/pass/url 接口获取 PDF 链接，
然后下载到 outputs/pdfs/ 目录下
文件名格式：{target}_{专利号}_{最高相关度}.pdf
"""

import csv
import hashlib
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://relay.catalystplus.cn:7443"
URL_API = "/patent/pass/url"

ACCESS_KEY = "CPS6Unoe4qnoCjPaSwSmAHqXG6NilfpZ"
ACCESS_SECRET = "t05997TiO7Fg6o2DvKtoJVCZRH7eNSsh"

OUTPUTS_DIR = Path("outputs")
PDF_DIR = OUTPUTS_DIR / "pdfs"
PDF_DIR.mkdir(parents=True, exist_ok=True)

RELEVANCE_RANK = {"高": 3, "中": 2, "低": 1, "暂未查到序列": 0, "无": 0, "": 0}


def generate_digester() -> str:
    tz = ZoneInfo("Asia/Shanghai")
    ts = datetime.now(tz).strftime("%Y%m%d%H%M")
    raw = ACCESS_KEY + ACCESS_SECRET[:10] + ts
    return hashlib.sha512(raw.encode()).hexdigest()


def get_pdf_urls(patent_ids: list[str], retries: int = 3) -> dict[str, str]:
    """批量获取专利 PDF 链接，返回 {patent_id: url}"""
    for attempt in range(retries):
        digester = generate_digester()
        payload = {
            "accessKey": ACCESS_KEY,
            "digester": digester,
            "patentIds": patent_ids,
        }
        try:
            resp = requests.post(
                BASE_URL + URL_API,
                json=payload,
                verify=False,
                timeout=30,
            )
            data = resp.json()
            if data.get("code") != 200:
                print(f"  [警告] 接口返回: {data.get('code')} {data.get('message')}")
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                return {}
            result = data.get("data") or {}
            if isinstance(result, dict):
                parsed = {}
                for path_key, url in result.items():
                    filename = path_key.split("/")[-1]
                    patent_id = filename.rsplit(".", 1)[0]
                    parsed[patent_id] = url
                return parsed
            if isinstance(result, list):
                return {
                    item["patentId"]: item.get("url", "")
                    for item in result
                    if isinstance(item, dict)
                }
            return {}
        except Exception as e:
            print(f"  [错误] 请求失败 (尝试 {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(2)
    return {}


def collect_patents_from_csvs() -> dict[str, tuple[str, str]]:
    """
    读取 outputs/ 下所有 CSV，收集每个专利的靶点和最高相关度。
    返回 {patent_id: (target, best_relevance_label)}
    """
    patent_map: dict[str, tuple[str, str]] = {}

    for csv_file in sorted(OUTPUTS_DIR.glob("*_sequences_*.csv")):
        print(f"读取: {csv_file.name}")
        with open(csv_file, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                pid    = row.get("patent_id", "").strip()
                target = row.get("target", "").strip()
                if not pid or not target:
                    continue
                rel = row.get("break_relevance", "").strip()
                has_seq = bool(row.get("sequence", "").strip())
                label = rel if (has_seq and rel in ("高", "中", "低")) else "暂未查到序列"

                if pid not in patent_map:
                    patent_map[pid] = (target, label)
                else:
                    old_target, old_label = patent_map[pid]
                    if RELEVANCE_RANK.get(label, 0) > RELEVANCE_RANK.get(old_label, 0):
                        patent_map[pid] = (target, label)

    return patent_map


def download_pdf(url: str, dest: Path) -> bool:
    """下载单个 PDF 文件"""
    if not url:
        return False
    try:
        resp = requests.get(url, verify=False, timeout=60, stream=True)
        if resp.status_code == 200:
            dest.write_bytes(resp.content)
            return True
        print(f"  [错误] HTTP {resp.status_code}")
        return False
    except Exception as e:
        print(f"  [错误] 下载失败: {e}")
        return False


def main():
    print("=== 专利 PDF 下载工具 ===\n")

    patent_map = collect_patents_from_csvs()
    if not patent_map:
        print("[错误] 未找到任何 CSV 文件或专利号")
        return

    print(f"\n共 {len(patent_map)} 个唯一专利号")

    # 过滤已下载的（检查 {target}_{pid}_{label}.pdf 是否存在）
    to_download: dict[str, tuple[str, str]] = {}
    for pid, (target, label) in patent_map.items():
        dest = PDF_DIR / f"{target}_{pid}_{label}.pdf"
        if dest.exists():
            pass  # 已存在，跳过
        else:
            to_download[pid] = (target, label)

    already = len(patent_map) - len(to_download)
    print(f"已存在: {already}  需要下载: {len(to_download)}")

    if not to_download:
        print("\n所有 PDF 均已下载完毕。")
        return

    # 分批获取 URL 并立即下载（每批 5 个，防止 300s 预签名 URL 过期）
    ids = list(to_download.keys())
    batch_size = 5
    success, fail, no_url = 0, 0, 0

    print(f"\n开始下载 → {PDF_DIR}/  (共 {len(ids)} 个，每批 {batch_size} 个)")
    for i in range(0, len(ids), batch_size):
        batch = ids[i: i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(ids) + batch_size - 1) // batch_size
        print(f"\n[批次 {batch_num}/{total_batches}] 获取 URL: {batch}")

        url_map = get_pdf_urls(batch)
        print(f"  返回 {len(url_map)} 条 URL")

        for pid in batch:
            target, label = to_download[pid]
            dest = PDF_DIR / f"{target}_{pid}_{label}.pdf"
            url = url_map.get(pid, "")

            if not url:
                print(f"  [无链接] {pid}")
                no_url += 1
                continue

            print(f"  下载: {dest.name}")
            if download_pdf(url, dest):
                size_kb = dest.stat().st_size // 1024
                print(f"    OK ({size_kb} KB)")
                success += 1
            else:
                fail += 1

    print(f"\n=== 完成 ===")
    print(f"  成功: {success}  失败: {fail}  无链接: {no_url}")
    print(f"  文件保存在: {PDF_DIR.resolve()}")


if __name__ == "__main__":
    main()
