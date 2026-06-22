# 蛋白序列/突变位点 专利规避工作流

根据蛋白关键词匹配相关专利，提取专利中蛋白序列的突变位点，区分受保护与不受保护，提供风险筛查与规避建议。

---

## 核心功能

### Step 1：知识库构建

根据蛋白关键词（如 EGFR、TLR2）搜索相关专利，提取两类信息并构建本地知识库：

- **完整蛋白序列**：ST.26 序列表 / SEQ ID NO 引用 / 裸序列提取
- **突变位点**：从专利文本中提取突变描述（E484K、Glu484Lys 等多种格式）

每条序列和突变都标注了：
- `location`：出现在 claims 还是 description 中
- `protected`：是否受专利保护（granted + claims = 受保护）
- 风险等级：high / medium / low / safe

知识库自动缓存到本地，相同关键词不重复查询 API。

### Step 2：风险筛查

两种输入形式：

| 输入形式 | 流程 |
|---------|------|
| **输入完整蛋白序列** | 序列比对 → 确认同源 → 逐位点比较 → 检查突变是否命中受保护位点 → 输出风险报告 |
| **输入突变位点列表** | 直接在知识库中查找 → 检查是否受保护 → 输出风险报告 |

如果知识库中没有该蛋白的数据，自动触发 Step 1 建库。

### 风险判定矩阵

| 专利状态 | 序列/突变位置 | protected | 风险等级 | 含义 |
|---------|-------------|-----------|---------|------|
| granted | claims | ✅ true | ⛔ high | 已获批+权利要求保护，**必须规避** |
| granted | description | false | ⚠️ medium | 已获批但仅提及，可用需注意 |
| pending | claims | false | 🔶 medium | 审查中+权利要求保护，有风险 |
| pending | description | false | ✅ low | 审查中且仅提及，可用 |
| abandoned/expired/withdrawn | any | false | ✅ safe | 无风险 |

---

## 快速开始

### 安装依赖

```bash
pip install requests biopython
```

### 配置 API 凭证

设置环境变量：

```bash
export CATALYST_ACCESS_KEY="your_access_key"
export CATALYST_ACCESS_SECRET="your_access_secret"
```

### 命令行使用

```bash
# Step 1: 构建知识库
python run.py build-kb --target EGFR

# Step 1: 查询受保护位点
python run.py query --target EGFR

# Step 2: 风险筛查（输入序列）
python run.py screen --sequence "MTEYKLVVLGAVGVGKSALT..." --target EGFR

# Step 2: 风险筛查（输入突变位点）
python run.py screen --mutations E484K,N501Y --target EGFR

# 强制重建知识库（忽略缓存）
python run.py build-kb --target EGFR --force

# 保存结果到文件
python run.py screen --mutations E484K --target EGFR -o report.json
```

### Python 直接调用

```python
from src.kb_builder import build_knowledge_base, query_protected_sites
from src.risk_screener import screen_risk

# Step 1: 构建知识库（有缓存，相同关键词不重复查）
kb = build_knowledge_base(target="EGFR")

# Step 1 的 agent 接口：查询某蛋白受保护的位点
protected = query_protected_sites(target="EGFR")

# Step 2: 风险筛查（输入序列）
report = screen_risk(query_sequence="MTEYKLVVLGAVGVGKSALT...", target="EGFR")

# Step 2: 风险筛查（输入突变位点）
report = screen_risk(mutations=["E484K", "N501Y"], target="EGFR")
```

### Skill 调用（供 macroflow 集成）

```python
from skill import run_skill

# 构建知识库
result = run_skill({"action": "build_kb", "target": "EGFR"})

# 查询受保护位点
result = run_skill({"action": "query_protected", "target": "EGFR"})

# 风险筛查
result = run_skill({"action": "screen_risk", "query_sequence": "MTEY...", "target": "EGFR"})
result = run_skill({"action": "screen_risk", "mutations": ["E484K", "N501Y"], "target": "EGFR"})
```

---

## 项目结构

```
patent-sequence-analysis-main/
├── README.md                   # 本文档
├── PLAN.md                     # 开发计划
├── CLAUDE.md                   # 项目配置
│
├── src/                        # 核心模块
│   ├── __init__.py             # 公共 API 导出
│   ├── api_client.py           # Catalyst+ API 客户端（签名、搜索、详情）
│   ├── sequence_extractor.py   # 序列提取（ST.26/SEQ ID NO/裸序列 + location 判断）
│   ├── mutation_extractor.py   # 突变位点提取（6种格式 + location 判断）
│   ├── kb_builder.py           # Step1: 知识库构建 + 缓存 + agent 查询接口
│   ├── risk_screener.py        # Step2: 风险筛查（序列/突变两种输入）
│   ├── alignment.py            # 序列比对（Biopython pairwise alignment）
│   └── utils.py                # 通用工具函数
│
├── skill.py                    # Skill 接口，供 macroflow 调用
├── run.py                      # 命令行入口
│
├── knowledge_base/             # 知识库 JSON 缓存目录
└── archive/                    # 旧版 v3.2 代码与历史数据（保留参考）
```

### 模块职责

| 模块 | 职责 |
|------|------|
| `api_client.py` | 封装 Catalyst+ API（签名、分页、时间分片、详情获取） |
| `sequence_extractor.py` | 从专利详情 JSON 中提取完整序列，判断 location（claims/description） |
| `mutation_extractor.py` | 从专利文本中提取突变位点描述（E484K 等多种格式），判断 location |
| `kb_builder.py` | 组合 api_client + sequence_extractor + mutation_extractor，构建知识库，提供查询接口 |
| `risk_screener.py` | 在知识库上做风险筛查（序列比对 / 突变查询），知识库无数据时自动触发建库 |
| `alignment.py` | 序列比对，返回 identity、对齐结果、差异位点 |
| `skill.py` | 统一对外接口：`build_kb` / `query_protected` / `screen_risk` |

---

## 突变位点提取格式

支持从专利文本中提取以下格式的突变描述：

| 格式 | 示例 | 匹配正则 |
|------|------|---------|
| 标准单字母 | `E484K` | `[A-Z]\d+[A-Z]` |
| 三字母码 | `Glu484Lys` | 三字母+数字+三字母 |
| position 描述 | `position 484 Glu→Lys` | position + 数字 + AA + 箭头 + AA |
| 数字开头 | `484E→K` / `484E/K` | 数字+AA+分隔符+AA |
| 中文格式 | `第484位谷氨酸替换为赖氨酸` | 中文数字+位+AA+替换为+AA |
| substitution 句式 | `substitution of Glu at position 484 with Lys` | substitution...of...at...with |

---

## 知识库 JSON 结构

```json
{
  "target": "EGFR",
  "build_time": "2026-06-22T10:00:00",
  "total_patents_searched": 200,
  "patents_with_data": 15,
  "patents": [
    {
      "patent_id": "US2020123456A1",
      "title": "Anti-EGFR antibody...",
      "status": "granted",
      "publication_date": "2020-03-15",
      "assignees": ["Company A"],
      "sequences": [
        {
          "seq_id": "1",
          "sequence": "MTEYKLVVVGAVGVGKSALT...",
          "seq_type": "AA",
          "length": 170,
          "source": "ST.26",
          "location": "claims",
          "protected": true,
          "role": "目标蛋白变体"
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
          "context": "substitution of Glu at position 484 with Lys"
        }
      ]
    }
  ]
}
```

---

## 风险报告结构

```json
{
  "query_type": "mutations",
  "target": "EGFR",
  "screening_time": "2026-06-22T11:00:00",
  "hits": [
    {
      "patent_id": "US2020123456A1",
      "patent_status": "granted",
      "overall_risk": "high",
      "mutation_hits": [
        {
          "notation": "E484K",
          "risk_level": "high",
          "reason": "E484K命中已授权专利的claims，必须规避"
        }
      ]
    }
  ],
  "summary": {
    "high_risk_mutations": ["E484K"],
    "medium_risk_mutations": [],
    "low_risk_mutations": ["N501Y"],
    "safe_mutations": [],
    "conclusion": "E484K命中已授权专利的claims保护，必须规避"
  }
}
```

---

## 依赖

- `requests` — API 调用
- `biopython` — 序列比对（BLOSUM62 替换矩阵 + pairwise alignment）

---

## 版本历史

| 版本 | 说明 |
|------|------|
| v1.0 | 初始版本：关键词搜索 + 序列提取 |
| v2.0 | 时间分片、ST.26 提取、增强识别 |
| v3.2 | LLM 验证、裸序列提取、三档 CSV 输出 |
| **v4.0** | **重构为专利规避工作流：知识库 + 突变提取 + 风险筛查 + Skill 接口** |

---

## License

Internal use only.
