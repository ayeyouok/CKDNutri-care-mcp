"""CKDNutri-care-mcp：儿童 CKD 随访沟通域（P3，合并自 M4 随访 + M10 通知）。

职责（对应 PCP 契约 follow_up / notification 输出位）：
- 随访记录时间线（get_followup_records）：随访计划摘要可见，原始医生备注仅医生/营养可见
- 依从性评分（get_adherence_score）：透明加权复合分（饮食/用药/就诊），默认等权，落库快照
- PEW 时间线 facade（get_pew_timeline）：数据归属 M3（ADR-007），M4 仅聚合读取，零跨包 import
- 随访计划（schedule_followup）：仅临床助手写，频率按 KDIGO 2024 儿科推荐
- 通知引擎（create_notification / get_notifications / ack_notification / build_event_notification）：
  已读状态（status）与闭环工作流（workflow_status）分离；闭环严格一步流转
  unacked→confirmed→resolved→closed（+ escalated 旁路）

本包不知道患者是谁：患者标识、CKD 分期、化验快照一律由调用方显式传入。
除统一策略包 a207-policy（身份注入 / 权限矩阵 / 状态路径）外，无对其他 a207-* 包的 import。
"""
from __future__ import annotations

from importlib import metadata as _metadata

from .core import (
    ack_notification,
    add_followup_record,
    build_event_notification,
    calc_adherence_score,
    create_notification,
    escalate_notification,
    get_adherence_score,
    get_followup_records,
    get_notifications,
    get_pew_timeline,
    recommend_followup_interval,
    schedule_followup,
    update_notification_status,
)


def _pkg_version() -> str:
    """从安装元数据读取版本（P2-6 修复：与 pyproject.toml 单一事实源对齐，
    此前写死 "0.3.2" 导致与 pyproject 版本长期漂移）。未安装时回退 "0.0.0"。
    """
    try:
        return _metadata.version("CKDNutri-care-mcp")
    except _metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = _pkg_version()

__all__ = [
    "__version__",
    "ack_notification",
    "add_followup_record",
    "build_event_notification",
    "calc_adherence_score",
    "create_notification",
    "escalate_notification",
    "get_adherence_score",
    "get_followup_records",
    "get_notifications",
    "get_pew_timeline",
    "recommend_followup_interval",
    "schedule_followup",
    "update_notification_status",
]
