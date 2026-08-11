# -*- coding: utf-8 -*-
"""a207-followup-mcp：儿童 CKD 随访系统（M4）。

职责（对应 PCP 契约 follow_up 输出位）：
- 随访记录时间线（get_followup_records）：随访计划摘要可见，原始医生备注仅医生/营养可见
- 依从性评分（get_adherence_score）：透明加权复合分（饮食/用药/就诊），默认等权
- PEW 时间线 facade（get_pew_timeline）：数据归属 M3（ADR-007），M4 仅聚合读取，零跨包 import
- 随访计划（schedule_followup）：医生/营养师写，频率按 KDIGO 2024 儿科推荐

本包不知道患者是谁：患者标识、CKD 分期、化验快照一律由调用方显式传入。
除统一策略包 a207-policy（身份注入 / 权限矩阵 / 状态路径）外，无对其他 a207-* 包的 import。
"""
from .core import (
    calc_adherence_score,
    get_adherence_score,
    get_followup_records,
    get_pew_timeline,
    recommend_followup_interval,
    schedule_followup,
)

__version__ = "0.2.2"

__all__ = [
    "__version__",
    "get_followup_records",
    "get_adherence_score",
    "get_pew_timeline",
    "schedule_followup",
    "recommend_followup_interval",
    "calc_adherence_score",
]
