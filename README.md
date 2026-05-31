# 专利序列挖掘工具

从 Catalyst+ API 提取指定靶点的生物序列数据，用于专利无效化分析。

---

## 核心功能

1. **时间分片查询** — 自动突破 API 100 条/页限制，按时间段切割重查
2. **ST.26 序列提取** — 从标准化序列表（`<223>` 标签）精确提取，准确率 100%
3. **LLM 辅助验证** — 对无标签序列调用 qwen-turbo 判断角色和相关性
4. **裸序列提取** — 从专利正文提取未格式化的氨基酸序列，经 LLM 验证后分级入档
5. **破专利建议** — 自动生成风险等级（高/中/低）和行动建议
6. **三档 CSV 输出** — 完整版 / 高可信度版 / LLM 验证版

---

## 快速开始

```bash
# 安装依赖
pip install requests

# 查询单个靶点（TLR2，全量）
python query_patent.py --targets TLR2 --max-pages 5

# 查询多个靶点
python query_patent.py --targets TLR2 CD318 --max-pages 10

# 指定时间范围
python query_patent.py --targets TLR2 --start 2020-01-01 --end 2025-12-31
```

### 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--targets` | — | 靶点名称，支持多个（空格分隔） |
| `--max-pages` | 5 | 每关键词最大查询页数 |
| `--output-dir` | outputs/ | 结果输出目录 |
| `--start` | 1900-01-01 | 查询起始日期 |
| `--end` | 今天 | 查询截止日期 |
| `--no-llm` | — | 禁用 LLM 验证（仅规则提取） |

---

## 项目结构

```
zhuanliAPI/
├── query_patent.py          # 主程序（单一入口）
├── README.md
├── scripts/
│   ├── merge_and_clean.py   # 合并多次查询的 CSV，去重
│   └── download_pdfs.py     # 按专利号批量下载 PDF
└── outputs/                 # 运行结果（每次运行生成带时间戳的文件）
    ├── <target>_patent_sequences_<ts>.csv               # 完整版
    ├── <target>_patent_sequences_<ts>_high_confidence.csv  # 高可信度版
    ├── <target>_patent_sequences_<ts>_llm_verified.csv  # LLM验证版
    └── patent_antibody_result_<ts>.json                 # 原始 API 数据
```

---

## 序列提取逻辑

```
专利详情
  ├── 1. ST.26 序列表（descriptions 字段）
  │       └── 有 <223> 标签 → 直接识别角色，confidence = 高（ST.26序列表）
  │
  ├── 2. SEQ ID NO 引用（claims/abstracts/descriptions）
  │       └── "SEQ ID NO:X SEQUENCE" 格式 → confidence = 中（SEQ ID NO引用）
  │
  └── 3. 裸 AA 序列（新增）
          └── 连续氨基酸字符串（>=12aa），需含稀有氨基酸(W/Y/H/F/Q)
              且出现在 antibody/VH/VL/CDR/polypeptide 等关键词附近
              → confidence = 低（裸序列）

↓ LLM 验证（针对无角色信息的序列）

验证策略：
  - ST.26 无 <223>           → LLM 验证
  - ST.26 有 <223> 但未识别  → LLM 验证
  - SEQ ID NO 序列           → LLM 验证
  - 裸序列                   → LLM 验证

LLM 验证结果分流：
  - 非裸序列 + LLM 高置信  → confidence = 中（LLM验证-高置信）→ 所有 CSV
  - 非裸序列 + LLM 中置信  → confidence = 中（LLM验证）       → 完整 + llm_verified
  - 裸序列 + LLM 高置信    → confidence = 中（LLM验证-高置信）→ 所有 CSV
  - 裸序列 + LLM 中/低置信 → confidence = 低（LLM验证-低置信）→ 仅完整 CSV
```

---

## 输出 CSV 字段

| 字段 | 说明 |
|------|------|
| target | 靶点名称 |
| patent_id | 专利号 |
| sequence | 序列字符串 |
| seq_role | 序列角色（CDR3 / VH / VL / siRNA / 引物…） |
| break_relevance | 破专利相关性：高 / 中 / 低 |
| break_guide | 具体破专利行动建议 |
| confidence_level | 可信度等级（见下表） |
| final_recommendation | 综合结论（含风险标识） |
| llm_verified | LLM 验证状态：是 / 低置信 / 失败 / （空） |
| llm_confidence | LLM 置信度 |
| llm_reasoning | LLM 判断依据（50字以内） |
| seq_type | AA（氨基酸）/ NT（核酸） |
| seq_context | 序列在原文中的上下文 |
| feature_desc | ST.26 的 `<223>` 标签内容 |
| status_label | 专利状态：有效 / 审查中 / 已放弃 / 已到期 |
| action_note | 整体破专利建议（按专利状态） |

### 可信度等级

| 等级 | 含义 | 进入哪些 CSV |
|------|------|-------------|
| 高（ST.26序列表） | ST.26 标准格式，有 `<223>` 标签 | 全部 |
| 高（ST.26+LLM验证） | ST.26 无标签，经 LLM 高置信验证 | 全部 |
| 中（LLM验证-高置信） | LLM 高置信验证通过 | 全部 |
| 中（LLM验证） | LLM 中置信验证通过 | 完整 + llm_verified |
| 中（SEQ ID NO引用） | 有 SEQ ID NO 引用，等待验证 | 完整 |
| 低（LLM验证-低置信） | 裸序列，LLM 中/低置信 | 仅完整 |
| 低（裸序列） | 裸序列，LLM 失败或未验证 | 仅完整 |

---

## 配置

在 `query_patent.py` 顶部修改：

```python
# 靶点关键词映射（同一蛋白的多个名称）
TARGET_KEYWORD_GROUPS = {
    "TLR2": ["TLR2"],
    "CD318": ["CD318", "CDCP1"],
}

# 各靶点查询起始时间（减少不必要的早期查询）
TARGET_START_OVERRIDE = {
    "TLR2": "2015-01-01",
    "CD318": "2003-01-01",
}

# LLM 配置
LLM_API_URL = "https://api.gpugeek.com/v1/chat/completions"
LLM_API_KEY = os.getenv("LLM_API_KEY", "your_key_here")
LLM_MODEL   = "Vendor3/qwen-turbo"
```

---

## 辅助工具

```bash
# 合并多次查询结果，去重
python scripts/merge_and_clean.py outputs/TLR2_*.csv outputs/CD318_*.csv -o merged.csv

# 按专利号批量下载 PDF
python scripts/download_pdfs.py outputs/TLR2_patent_sequences_*.csv
```

---

## 注意事项

- 需要 VPN 访问 Catalyst+ API
- API 单次最多返回 100 条，脚本自动时间分片
- LLM 验证需要配置有效的 API Key（`LLM_API_KEY` 环境变量）
- 每次查询成本约 $0.20（qwen-turbo，按专利分组调用）

---

## 版本历史

| 版本 | 说明 |
|------|------|
| v1.0 | 初始版本 |
| v2.0 | 时间分片、ST.26提取、增强识别 |
| v2.2 | 可信度分类、结论列、双CSV输出 |
| v3.1 | LLM 验证（SEQ ID NO + ST.26无标签），三档CSV |
| **v3.2** | **裸序列提取（关键词上下文过滤 + LLM置信度分流）** |
