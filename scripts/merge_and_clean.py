"""
合并新旧 CSV + 清理 PDF 目录
- CSV: 按 (target, patent_id) 去重，保留最高相关度行，序列行全保留
- PDF: 删除旧格式文件（无 target_ 前缀）、重命名、按相关度过滤
"""
import csv, pathlib, re, shutil, sys
sys.stdout.reconfigure(encoding="utf-8")

OUTPUTS   = pathlib.Path("outputs")
PDF_DIR   = OUTPUTS / "pdfs"
RANK      = {"高": 3, "中": 2, "低": 1, "暂未查到序列": 0, "无": 0, "": 0}

# ── 1. 读取所有 CSV，按靶点合并 ────────────────────────────────────────────
# {target: {patent_id: {meta_row, seq_rows[]}}}
patent_meta: dict[str, dict[str, dict]] = {}   # patent_id → 最优 meta 行（无序列行）
patent_seqs: dict[str, list[dict]] = {}         # patent_id → 所有序列行

all_csvs = sorted(OUTPUTS.glob("*_sequences_*.csv"))
print(f"找到 {len(all_csvs)} 个 CSV 文件:")
for f in all_csvs: print(f"  {f.name}")

rows_by_target: dict[str, list[dict]] = {}

for csv_file in all_csvs:
    with open(csv_file, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            pid    = row.get("patent_id", "").strip()
            target = row.get("target", "").strip()
            seq    = row.get("sequence", "").strip()
            rel    = row.get("break_relevance", "").strip()
            if not pid or not target:
                continue
            key = (target, pid)
            if seq:
                patent_seqs.setdefault(key, [])
                # 同一序列去重
                existing_seqs = [r["sequence"] for r in patent_seqs[key]]
                if seq not in existing_seqs:
                    patent_seqs[key].append(dict(row))
            else:
                # 无序列的代表行——保留最高相关度版本
                if key not in patent_meta:
                    patent_meta[key] = dict(row)
                else:
                    old_rel = patent_meta[key].get("break_relevance", "")
                    if RANK.get(rel, 0) > RANK.get(old_rel, 0):
                        patent_meta[key] = dict(row)

# 构建最终行列表（按靶点）
for (target, pid), seqs in patent_seqs.items():
    rows_by_target.setdefault(target, []).extend(seqs)

for (target, pid), meta in patent_meta.items():
    # 只有当该专利完全没有序列行时才加入无序列行
    if not patent_seqs.get((target, pid)):
        rows_by_target.setdefault(target, []).append(meta)

# ── 2. 统计并决定是否过滤 ──────────────────────────────────────────────────
print("\n=== 合并后统计 ===")
final_rows: dict[str, list[dict]] = {}

for target, rows in sorted(rows_by_target.items()):
    has_seq = [r for r in rows if r.get("sequence", "").strip()]
    no_seq  = [r for r in rows if not r.get("sequence", "").strip()]
    high_mid = [r for r in has_seq if r.get("break_relevance","") in ("高","中")]
    low_rows = [r for r in has_seq if r.get("break_relevance","") not in ("高","中")]

    print(f"\n{target}:")
    print(f"  有序列行: {len(has_seq)}  其中高/中: {len(high_mid)}  低: {len(low_rows)}")
    print(f"  无序列行: {len(no_seq)}")
    print(f"  合计行数: {len(rows)}")

    # 策略：有序列行保留全部；无序列行仅保留高/中相关度
    # （无序列行的相关度在旧脚本里被设为"暂未查到序列"或空，没有独立高中低）
    # 实际上这些行对应专利状态不同，都保留，只是在PDF层面过滤
    final_rows[target] = rows
    print(f"  → 最终保留: {len(rows)} 行")

# ── 3. 写出最终 CSV（用最新时间戳，清理旧文件）─────────────────────────────
from datetime import datetime
from zoneinfo import ZoneInfo
ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")

FIELDNAMES = [
    "target", "patent_id", "sequence",
    "seq_role", "break_relevance", "break_guide", "confidence_level", "final_recommendation",
    "llm_verified", "llm_confidence", "llm_raw_response",
    "seq_type", "seq_context",
    "status_label", "action_note", "patent_status", "publication_date",
    "title", "assignees", "abstract_brief", "claims_brief",
]

saved_csvs = []
for target, rows in sorted(final_rows.items()):
    # 排序：高→中→低→无序列，每组内按 patent_id
    def sort_key(r):
        rel = r.get("break_relevance", "")
        has = 1 if r.get("sequence", "").strip() else 0
        return (-has, -RANK.get(rel, 0), r.get("patent_id", ""))
    rows.sort(key=sort_key)

    out_path = OUTPUTS / f"{target}_patent_sequences_{ts}.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    saved_csvs.append(out_path)
    print(f"\n已写出: {out_path.name}  ({len(rows)} 行)")

# 删除旧 CSV（跳过被 IDE 锁定的文件）
old_csvs = [f for f in all_csvs if f not in saved_csvs]
for f in old_csvs:
    try:
        f.unlink()
        print(f"删除旧 CSV: {f.name}")
    except PermissionError:
        print(f"[跳过] 文件被占用，请手动关闭后删除: {f.name}")

# ── 4. 清理 PDF 目录 ───────────────────────────────────────────────────────
print("\n=== 清理 PDF 目录 ===")

# 建立 patent_id → (target, best_relevance) 映射（来自最新 CSV）
pdf_label: dict[str, tuple[str,str]] = {}
for target, rows in final_rows.items():
    seen = {}
    for r in rows:
        pid = r.get("patent_id","").strip()
        rel = r.get("break_relevance","").strip()
        has = bool(r.get("sequence","").strip())
        label = rel if (has and rel in ("高","中","低")) else "暂未查到序列"
        if pid not in seen or RANK.get(label,0) > RANK.get(seen[pid][1],0):
            seen[pid] = (target, label)
    for pid, info in seen.items():
        if pid not in pdf_label or RANK.get(info[1],0) > RANK.get(pdf_label[pid][1],0):
            pdf_label[pid] = info

# 期望的正确文件名
def expected_name(pid, target, label):
    return f"{target}_{pid}_{label}.pdf"

all_pdfs = list(PDF_DIR.glob("*.pdf"))
correct = set()
to_delete = []

for pdf in all_pdfs:
    stem = pdf.stem  # e.g. "CD318_CN117264055A_高"
    parts = stem.split("_", 1)  # ["CD318", "CN117264055A_高"]
    if len(parts) == 2 and parts[0] in ("TLR2","CD318"):
        # 已有 target 前缀
        pid_label = parts[1].rsplit("_", 1)
        if len(pid_label) == 2:
            pid, label = pid_label
            info = pdf_label.get(pid)
            if info:
                exp = expected_name(pid, info[0], info[1])
                if pdf.name == exp:
                    correct.add(pdf)
                elif (PDF_DIR / exp).exists():
                    to_delete.append(pdf)  # 正确版本已存在，删除此旧版
                else:
                    # 需要重命名
                    pdf.rename(PDF_DIR / exp)
                    correct.add(PDF_DIR / exp)
                    print(f"  重命名: {pdf.name} → {exp}")
            else:
                # 不在当前 CSV 中的专利 PDF，暂时保留
                correct.add(pdf)
    else:
        # 旧格式（无 target 前缀）
        pid_label = stem.rsplit("_", 1)
        if len(pid_label) == 2:
            pid, label = pid_label
            info = pdf_label.get(pid)
            if info:
                exp = expected_name(pid, info[0], info[1])
                if (PDF_DIR / exp).exists():
                    to_delete.append(pdf)  # 新版已存在
                else:
                    pdf.rename(PDF_DIR / exp)
                    correct.add(PDF_DIR / exp)
                    print(f"  重命名(旧→新): {pdf.name} → {exp}")
            else:
                to_delete.append(pdf)  # 找不到来源

for pdf in to_delete:
    if pdf.exists():
        pdf.unlink()
        print(f"  删除重复: {pdf.name}")

# ── 5. 统计最终 PDF ────────────────────────────────────────────────────────
print("\n=== 最终 PDF 统计 ===")
final_pdfs = sorted(PDF_DIR.glob("*.pdf"))
by_rel = {"高":[],"中":[],"低":[],"暂未查到序列":[],"其他":[]}
for f in final_pdfs:
    stem = f.stem
    for label in ("高","中","低","暂未查到序列"):
        if stem.endswith("_" + label):
            by_rel[label].append(f.name)
            break
    else:
        by_rel["其他"].append(f.name)

for rel, files in by_rel.items():
    if files:
        print(f"  {rel}: {len(files)} 个")

print(f"  合计: {len(final_pdfs)} 个 PDF")
print(f"\n完成！")
for p in saved_csvs:
    print(f"  CSV: {p.name}")
print(f"  PDF: {PDF_DIR}/  ({len(final_pdfs)} 个文件)")
