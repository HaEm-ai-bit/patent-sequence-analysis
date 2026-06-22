"""
蛋白序列/突变位点专利规避工作流 - 核心模块
"""

from .kb_builder import build_knowledge_base, query_protected_sites
from .risk_screener import screen_risk

__all__ = [
    "build_knowledge_base",
    "query_protected_sites",
    "screen_risk",
]
