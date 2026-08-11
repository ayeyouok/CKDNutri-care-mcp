"""P3 随访沟通域 MCP 服务：随访 + 通知 + DAG + 闭环工单。

合并自 M4 (a207-followup-mcp) + M10 (a207-notification-mcp)。
v2.3 新增：trigger_event_notification（DAG）+ update_notification_status（闭环状态机）。
"""
from __future__ import annotations

from typing import Any, Optional

from fastmcp import FastMCP

from a207_policy import CallerError

from .core import (
    ack_notification,
    build_event_notification,
    create_notification,
    get_adherence_score,
    get_followup_records,
    get_notifications,
    get_pew_timeline,
    schedule_followup,
    update_notification_status,
)

mcp = FastMCP("CKDNutri-care-mcp")


def _invalid(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, CallerError):
        raise
    return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}


def main():
    mcp.run()


# ---- M4: 随访 ----

@mcp.tool
def schedule_followup_tool(
    patient_id: str,
    ckd_stage: str,
    albuminuria_stage: str,
    visit_type: str,
    visit_date: str,
    plan_summary: str = "",
    note_to_clinician: str = "",
) -> dict[str, Any]:
    """创建随访计划（按 CKD 分期自动算 next_due）。CKD 临床助手独占。"""
    try:
        return schedule_followup(
            patient_id, ckd_stage, albuminuria_stage, visit_type,
            visit_date, plan_summary=plan_summary, note_to_clinician=note_to_clinician,
        )
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_followup_records_tool(patient_id: str) -> dict[str, Any]:
    """查随访历史（含计划 + 下次到期日）。按身份视图裁剪。"""
    try:
        return get_followup_records(patient_id)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_adherence_score_tool(
    patient_id: str,
    diet_ratio: float,
    med_ratio: float,
    visit_ratio: float,
) -> dict[str, Any]:
    """计算并落库依从性评分。仅 CKD 临床助手可写。"""
    try:
        return get_adherence_score(patient_id, diet_ratio, med_ratio, visit_ratio)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_pew_timeline_tool(
    patient_id: str,
    pew_history: Optional[list] = None,
) -> dict[str, Any]:
    """PEW 历史并入统一随访时间线（facade）。"""
    try:
        return get_pew_timeline(patient_id, pew_history=pew_history)
    except Exception as exc:
        return _invalid(exc)


# ---- M10: 通知 ----

@mcp.tool
def get_notifications_tool(
    patient_id: str,
    status: Optional[str] = "all",
) -> dict[str, Any]:
    """查通知列表（可按 status/ack 过滤，含 workflow_status 闭环字段）。"""
    try:
        return get_notifications(patient_id, status=status)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def ack_notification_tool(notification_id: str) -> dict[str, Any]:
    """确认通知（幂等）。"""
    try:
        return ack_notification(notification_id)
    except Exception as exc:
        return _invalid(exc)


# ---- DAG: 事件→模板→落库→推送 一键完成 (v2.3) ----

@mcp.tool
def trigger_event_notification_tool(
    event_type: str,
    patient_id: str,
    payload: Optional[dict] = None,
) -> dict[str, Any]:
    """事件触发的通知创建：事件类型→模板填充→落库→推送 一键完成（DAG）。

    event_type: followup_due / risk_escalation / report_ready
    payload 按事件类型填字段：
      - followup_due: {next_due_date(, due_at)}
      - risk_escalation: {from_level, to_level, rule}
      - report_ready: {}（无需额外字段）
    内部由 build_event_notification 完成模板填充 + 落库（已含 create_notification），
    本工具直接返回其结果，不再二次写库（修复 v0.3.1 重复写库 + 非法 kwarg 崩溃）。
    """
    try:
        return build_event_notification(event_type, patient_id, payload or {})
    except Exception as exc:
        return _invalid(exc)


# ---- 闭环：风险工单状态机 (v2.3) ----

@mcp.tool
def update_notification_status_tool(
    notification_id: str,
    new_status: str,
    resolution_note: str = "",
) -> dict[str, Any]:
    """推移风险闭环状态机。仅 CKD 临床助手。

    workflow_status: unacked → confirmed → resolved → closed
    须在 confirmed 后才能设为 resolved（需 resolution_note）；
    24h 内未确认的由 HAIP Workflow 自动升级（escalated）。
    """
    try:
        return update_notification_status(notification_id, new_status, resolution_note)
    except Exception as exc:
        return _invalid(exc)


if __name__ == "__main__":
    main()
