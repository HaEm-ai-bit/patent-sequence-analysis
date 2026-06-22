# archive/ — 旧版代码与数据存档

本目录保留了 v3.2 "抗体专利序列挖掘工具" 的完整代码和历史输出，供参考借鉴。

**当前项目已升级为"蛋白序列/突变位点专利规避工作流"，新代码在 `src/` 下。**

---

## 目录说明

| 目录/文件 | 说明 |
|----------|------|
| `legacy/query_patent.py` | 原 v3.2 主脚本（约1460行），包含 API 调用、序列提取、LLM验证、CSV输出全部逻辑 |
| `legacy/prototype/` | 原 Flask Web 原型（app.py + templates），包装 query_patent.py 的命令行调用 |
| `legacy/scripts/` | 原辅助脚本：`merge_and_clean.py`（合并CSV去重）、`download_pdfs.py`（下载专利PDF） |
| `outputs/` | 历史运行输出（TLR2/CD318 的 CSV + JSON），2026-06-02 生成 |

## 可借鉴的部分

- `legacy/query_patent.py` 中的 **API签名、分页、时间分片** 逻辑 → 已抽取到 `src/api_client.py`
- `legacy/query_patent.py` 中的 **ST.26/SEQ ID NO/裸序列提取** 逻辑 → 已抽取到 `src/sequence_extractor.py`
- `legacy/query_patent.py` 中的 **LLM调用和验证** 逻辑 → 后续可用于辅助判断
- `legacy/scripts/` 的工具脚本思路 → 后续可按需迁移
