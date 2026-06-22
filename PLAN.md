# 蛋白序列/突变位点 专利规避工作流 — 开发计划

**日期**: 2026-06-22
**前身**: 抗体专利序列挖掘工具 v3.2 (query_patent.py)

---

## 一、目标

根据蛋白关键词匹配相关专利，提取专利中蛋白序列的突变位点，区分受保护与不受保护，提供一个根据蛋白关键词返回相应受保护序列/位点的 agent 接口。

设计为可复用工具：
- 可直接 Python 调用
- 可作为 skill 被调用
- 之后接入 macroflow

---

## 二、核心工作流

### Step 1：根据关键词提取专利序列 & 突变位点，区分保护状态

**输入**: 蛋白关键词（如 EGFR、TLR2）
**输出**: 该蛋白相关的受保护/不受保护序列和位点（可查询接口）

流程：
```
蛋白关键词 → Catalyst+ API 搜索相关专利 → 获取专利详情
→ 提取两类信息：
   1. 完整蛋白序列（复用现有 ST.26 / SEQ ID NO / 裸序列提取）
   2. 专利文本中明确描述的突变位点（如 "E484K"、"position 484 Glu→Lys"、"第484位谷氨酸替换为赖氨酸"）
→ 区分受保护 vs 不受保护：
   - 序列/突变出现在 claims 中 → 受保护
   - 序列/突变仅在 description 中 → 不受保护（仅提及）
   - 结合专利法律状态：granted 的受保护位点必须规避，pending 的需关注
→ 缓存到本地知识库（避免重复查询）
→ 提供 agent 接口：输入蛋白关键词 → 返回受保护的序列和位点列表
```

**关键技术点**：
- **长文本专利优化**：专利全文可能很长，突变描述分散在 claims/description 各处，需要优化长文本的提取策略（分段处理、避免截断遗漏）
- **提取准确性验证**：序列和突变位点提取后需要测试准确性，后续可能需要人工验证机制

知识库 JSON 结构：
```json
{
  "target": "EGFR",
  "build_time": "2026-06-22T10:00:00",
  "patents": [
    {
      "patent_id": "US2020123456A1",
      "title": "...",
      "status": "granted",
      "publication_date": "2020-03-15",
      "assignees": ["Company A"],
      "sequences": [
        {
          "seq_id": "SEQ_ID_NO_1",
          "sequence": "MTEYKLVVVGAVGVGKSALT...",
          "seq_type": "AA",
          "length": 189,
          "source": "ST.26",
          "location": "claims",
          "role": "目标蛋白变体",
          "context": "原文上下文片段..."
        }
      ],
      "mutations": [
        {
          "position": 484,
          "wild_type": "E",
          "mutant": "K",
          "notation": "E484K",
          "location": "claims",
          "protected": true,
          "context": "...substitution of glutamic acid at position 484 with lysine..."
        },
        {
          "position": 501,
          "wild_type": "N",
          "mutant": "Y",
          "notation": "N501Y",
          "location": "description",
          "protected": false,
          "context": "...in one embodiment, N501Y was also tested..."
        }
      ]
    }
  ]
}
```

关键设计点：
- **序列 + 突变位点双重提取**：
  - 完整序列：用于后续 alignment 比对
  - 突变位点：从专利文本中用正则/NLP提取，格式化为 `{position, wild_type, mutant, notation}`
- **受保护 vs 不受保护**（序列和突变位点都有）：
  - `location = "claims"` + `status = "granted"` → `protected: true`（必须规避）
  - `location = "description"` 或 `status = "pending"` → `protected: false`（可用但需注意）
- **缓存查询机制**：本地存储知识库，相同关键词不重复查询API，支持增量更新保持最新

### Step 2：根据输入序列或突变位点做专利风险筛查

**输入**: 用户的蛋白序列 或 突变位点列表
**输出**: 风险报告 JSON

本质：在 Step 1 的知识库上查询，只是输入形式不同。

两种输入形式：

#### 形式 A：输入完整蛋白序列
```
用户序列 → 与知识库中的专利序列做 pairwise alignment
→ 找到同源序列（确认是同一条蛋白）
→ 在对齐基础上，逐位点比较，找出差异位点（用户突变）
→ 检查用户的突变位点是否命中知识库中的受保护突变
→ 输出风险报告
```

#### 形式 B：直接输入突变位点
```
用户给出突变列表（如 ["E484K", "N501Y"]）
→ 在知识库中查找这些突变位点
→ 检查是否命中受保护的突变
→ 输出风险报告
```

**如果知识库中没有该蛋白的数据**：自动触发 Step 1 的建库流程（用蛋白关键词搜API），建完再查。

风险报告 JSON 结构：
```json
{
  "query_type": "sequence",
  "query_sequence": "MTEYKLVVLGAVGVGKSALT...",
  "protein_name": "EGFR",
  "screening_time": "2026-06-22T11:00:00",
  "hits": [
    {
      "patent_id": "US2020123456A1",
      "patent_status": "granted",
      "patent_seq_id": "SEQ_ID_NO_1",
      "patent_sequence": "MTEYKLVVVGAVGVGKSALT...",
      "identity": 0.97,
      "mutation_hits": [
        {
          "position": 484,
          "query_aa": "K",
          "wild_type": "E",
          "notation": "E484K",
          "patent_mutation_location": "claims",
          "protected": true,
          "risk_level": "high",
          "reason": "E484K命中已授权专利的claims，必须规避"
        },
        {
          "position": 501,
          "query_aa": "Y",
          "wild_type": "N",
          "notation": "N501Y",
          "patent_mutation_location": "description",
          "protected": false,
          "risk_level": "low",
          "reason": "N501Y仅在description提及，未被claims保护，可用"
        }
      ],
      "novel_mutations": [
        {
          "position": 8,
          "query_aa": "L",
          "patent_aa": "V",
          "notation": "V8L",
          "risk_level": "safe",
          "reason": "该位点突变未在任何专利中出现"
        }
      ],
      "overall_risk": "high"
    }
  ],
  "summary": {
    "total_patents_checked": 150,
    "high_risk_mutations": ["E484K"],
    "medium_risk_mutations": [],
    "low_risk_mutations": ["N501Y"],
    "safe_mutations": ["V8L"],
    "conclusion": "E484K命中已授权专利的claims保护，必须规避；N501Y仅提及未保护，可用"
  }
}
```

---

## 三、风险判定逻辑

| 专利状态 | 序列/突变位置 | protected | 风险等级 | 含义 |
|---------|-------------|-----------|---------|------|
| granted | claims | true | ⛔ high | 已获批+权利要求保护，必须规避 |
| granted | description | false | ⚠️ medium | 已获批但仅提及，可用需注意 |
| pending | claims | false | 🔶 medium | 审查中+权利要求保护，有风险 |
| pending | description | false | ✅ low | 审查中且仅提及，可用 |
| abandoned/expired/withdrawn | any | false | ✅ safe | 无风险 |

---

## 四、代码架构

```
patent-sequence-analysis-main/
├── PLAN.md                     # 本文档
├── README.md                   # 重写
├── CLAUDE.md
│
├── src/                        # 核心模块
│   ├── __init__.py
│   ├── api_client.py           # Catalyst+ API 调用（从 query_patent.py 抽出）
│   ├── sequence_extractor.py   # 序列提取（ST.26/SEQ ID NO/裸序列，优化长文本）
│   ├── mutation_extractor.py   # 突变位点提取（从专利文本中提取 E484K 等突变描述）
│   ├── kb_builder.py           # Step1: 知识库构建 + 缓存 + agent查询接口
│   ├── risk_screener.py        # Step2: 风险筛查（在知识库上查询）
│   ├── alignment.py            # 序列比对模块（封装 Biopython）
│   └── utils.py                # 通用工具函数
│
├── skill.py                    # skill 接口，供 macroflow 调用
├── run.py                      # 命令行入口
│
├── legacy/                     # 旧代码，保留做参考
│   ├── query_patent.py         # 原 v3.2 主脚本
│   ├── prototype/              # 原 Flask web 原型
│   └── scripts/                # 原辅助脚本
│
├── outputs/                    # 输出目录
└── knowledge_base/             # 知识库 JSON 缓存目录
```

### 模块职责

| 模块 | 职责 |
|------|------|
| `api_client.py` | 封装 Catalyst+ API（签名、分页、时间分片、详情获取） |
| `sequence_extractor.py` | 从专利详情 JSON 中提取完整序列，判断 location，优化长文本处理 |
| `mutation_extractor.py` | 从专利文本中提取突变位点描述（E484K等），判断 location |
| `kb_builder.py` | 组合 api_client + sequence_extractor + mutation_extractor，构建知识库，提供关键词查询接口，缓存机制 |
| `risk_screener.py` | 在知识库上做风险筛查（序列比对/突变查询），知识库无数据时自动触发建库 |
| `alignment.py` | 序列比对，返回 identity、对齐结果 |
| `skill.py` | 统一对外接口：`build_kb(...)` / `query_protected(...)` / `screen_risk(...)` |
| `run.py` | CLI 入口 |

---

## 五、调用方式

### 1. 直接 Python 调用
```python
from src.kb_builder import build_knowledge_base, query_protected_sites
from src.risk_screener import screen_risk

# Step 1: 构建知识库（有缓存，相同关键词不重复查）
kb = build_knowledge_base(target="EGFR")
# 保存到 knowledge_base/EGFR_20260622.json

# Step 1 的 agent 接口：查询某蛋白受保护的位点
protected = query_protected_sites(target="EGFR")
# 返回 [{notation: "E484K", patent_id: "...", location: "claims", status: "granted"}, ...]

# Step 2: 风险筛查（输入序列）
report = screen_risk(query_sequence="MTEYKLVVLGAVGVGKSALT...", target="EGFR")

# Step 2: 风险筛查（输入突变位点）
report = screen_risk(mutations=["E484K", "N501Y"], target="EGFR")
```

### 2. 命令行调用
```bash
# Step 1: 构建知识库
python run.py build-kb --target EGFR

# Step 1: 查询受保护位点
python run.py query --target EGFR

# Step 2: 风险筛查（序列）
python run.py screen --sequence "MTEYKLVVLGAVGVGKSALT..." --target EGFR

# Step 2: 风险筛查（突变位点）
python run.py screen --mutations E484K,N501Y --target EGFR
```

### 3. Skill 调用（供 macroflow）
```python
from skill import run_skill

# Step 1: 建库
result = run_skill({"action": "build_kb", "target": "EGFR"})

# Step 1: 查询受保护位点
result = run_skill({"action": "query_protected", "target": "EGFR"})

# Step 2: 风险筛查
result = run_skill({"action": "screen_risk", "query_sequence": "MTEY...", "target": "EGFR"})
result = run_skill({"action": "screen_risk", "mutations": ["E484K", "N501Y"], "target": "EGFR"})
```

---

## 六、复用 vs 新写

| 现有代码 | 处理方式 |
|---------|---------|
| API 签名、分页、时间分片逻辑 | 从 `legacy/query_patent.py` 抽出到 `api_client.py`，直接复用 |
| ST.26 提取、SEQ ID NO 提取、裸序列提取 | 从 `legacy/query_patent.py` 抽出到 `sequence_extractor.py`，复用+优化长文本 |
| LLM 调用逻辑 | 暂时保留，后续可用于辅助提取突变/验证 |
| 抗体特异的角色识别（CDR/VH/VL规则） | 保留但降低优先级，通用蛋白场景不依赖 |
| 三档 CSV 输出 | 不再使用，改为 JSON 知识库 + JSON 报告 |
| 专利状态→破专利建议映射 | 替换为受保护/不受保护的区分逻辑 |

**新写**：
- `mutation_extractor.py` — 从专利文本中提取突变位点（正则匹配多种格式）
- `kb_builder.py` — 知识库构建 + 缓存机制 + 关键词查询接口
- `risk_screener.py` — 风险筛查（序列比对 / 突变查询两种形式）
- `alignment.py` — 序列比对（基于 Biopython 或 parasail）
- `skill.py` — skill 接口
- `run.py` — CLI 入口
- `sequence_extractor.py` 中新增 claims vs description 位置判断 + 长文本优化

---

## 七、开发顺序

1. **重构抽取**：把 query_patent.py 中的 API 调用和序列提取逻辑抽到 src/ 下的独立模块
2. **新增 location 判断**：序列提取时区分 claims / description
3. **新增 mutation_extractor.py**：从专利文本中提取突变位点描述
4. **优化长文本处理**：专利全文分段处理，避免截断遗漏突变描述
5. **实现 kb_builder.py**：知识库构建 + 缓存机制 + 关键词查询受保护位点的 agent 接口
6. **测试序列提取准确性**：用实际专利验证序列和突变位点提取的准确率，记录需人工验证的条目
7. **实现 alignment.py**：序列比对模块
8. **实现 risk_screener.py**：风险筛查（序列/突变两种输入形式），知识库无数据时自动建库
9. **实现 skill.py + run.py**：对外接口
10. **端到端测试**：用实际靶点跑通全流程

---

## 八、依赖

- `requests` — API 调用（已有）
- `biopython` — 序列比对（新增，`Bio.pairwise2` 或 `Bio.Align`）
- 可选：`parasail` — 更快的序列比对库

