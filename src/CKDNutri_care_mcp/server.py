"""P3 随访沟通域 MCP 服务：随访 + 通知 + DAG + 闭环工单。

合并自 M4 (a207-followup-mcp) + M10 (a207-notification-mcp)。
v2.3 新增：trigger_event_notification（DAG）+ update_notification_status（闭环状态机）。
"""
from __future__ import annotations

import json

from typing import Any, Optional

from fastmcp import FastMCP

from a207_policy import CallerError

from .core import (
    ack_notification,
    add_followup_record,
    build_event_notification,
    escalate_notification,
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
        # BUG-54（2026-08-12）：越权/身份未解析统一返回 FORBIDDEN 信封（与本包 _guard /
        # clinical-data _guard_access 同格式），不再向上抛导致 500；PermissionDenied 带
        # caller/action/reason，CallerUnknown 缺字段时降级文案。此前 get_adherence_score 等
        # 裸调 enforce_* 的工具有越权即 500 崩溃。
        return {"ok": False, "error": "FORBIDDEN",
                "detail": f"caller={getattr(exc, 'caller', '?')} 无权 {getattr(exc, 'action', 'access')}"
                          f"（{getattr(exc, 'reason', str(exc))}）"}
    # BUG-52（2026-08-12）：内部数据错误归 INTERNAL_ERROR，避免误归 INVALID_INPUT
    # BUG-65（2026-08-12）：RuntimeError 补入——_load_store/_notify_load 遇 JSON 损坏时
    # 抛 RuntimeError 包装（fail-closed 防静默清空），此前 _invalid 未覆盖，损坏会被误归
    # INVALID_INPUT（400），违背"数据文件损坏=服务端内部错误"的归类初衷。
    if isinstance(exc, (FileNotFoundError, OSError, json.JSONDecodeError, RuntimeError)):
        return {"ok": False, "error": "INTERNAL_ERROR",
                "detail": f"内部数据错误：{exc}"}
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
def get_followup_records_tool(patient_id: str, guardian_token: Optional[str] = None) -> dict[str, Any]:
    """查随访历史（含计划 + 下次到期日）。按身份视图裁剪；家长需携带 guardian_token。"""
    try:
        return get_followup_records(patient_id, guardian_token=guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def add_followup_record_tool(
    patient_id: str,
    visit_date: str,
    visit_type: str,
    ckd_stage: str,
    indicators_snapshot: Optional[dict] = None,
    plan_summary: str = "",
    doctor_notes: str = "",
) -> dict[str, Any]:
    """记录一次实际完成的就诊随访（写，仅 CKD 临床助手；MX-3 收口）。

    BUG-65（2026-08-12）：core.add_followup_record 已实现且权限矩阵已登记
    （WRITE_TOOL_POLICY 白名单 + 矩阵回查），但此前未暴露为 MCP 工具——外部客户端
    只能创建随访计划（schedule_followup_tool）却无法写入已完成的随访记录。本工具补齐暴露。
    indicators_snapshot: 本次就诊检验指标快照（如 {egfr, k_mmol_L, ...}），供后续评估对照。"""
    try:
        return add_followup_record(
            patient_id, visit_date, visit_type, ckd_stage,
            indicators_snapshot or {}, plan_summary, doctor_notes,
        )
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_adherence_score_tool(
    patient_id: str,
    diet_ratio: float,
    med_ratio: float,
    visit_ratio: float,
    weights: Optional[list] = None,
) -> dict[str, Any]:
    """计算并落库依从性评分。仅 CKD 临床助手可写。

    BUG-65（2026-08-12）：补 weights 透传——core.calc_adherence_score 支持自定义权重
    （diet/medication/visit，默认等权 1/3），此前工具层遗漏该参数，客户端无法自定义权重。
    MCP 层用 list 承载（tuple 非 JSON 原生类型，FastMCP schema 更友好），透传前转 tuple。"""
    try:
        kw = {"weights": tuple(weights)} if weights is not None else {}
        return get_adherence_score(patient_id, diet_ratio, med_ratio, visit_ratio, **kw)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def get_pew_timeline_tool(
    patient_id: str,
    guardian_token: Optional[str] = None,
    pew_history: Optional[list] = None,
) -> dict[str, Any]:
    """PEW 历史并入统一随访时间线（facade）。家长需携带 guardian_token。"""
    try:
        return get_pew_timeline(patient_id, guardian_token=guardian_token, pew_history=pew_history)
    except Exception as exc:
        return _invalid(exc)


# ---- M10: 通知 ----

@mcp.tool
def get_notifications_tool(
    patient_id: str,
    status: Optional[str] = "all",
    workflow_status: Optional[str] = "all",
    escalated: Optional[bool] = None,
    guardian_token: Optional[str] = None,
) -> dict[str, Any]:
    """查通知列表。status 按已读（all/unacked/acked）；workflow_status 按闭环状态
    （all/unacked/confirmed/resolved/closed，BUG-25 新增）；escalated 按升级布尔过滤
    （BUG-46：escalated 独立于 workflow_status）。家长需携带 guardian_token。"""
    try:
        return get_notifications(patient_id, status=status, workflow_status=workflow_status,
                                 escalated=escalated, guardian_token=guardian_token)
    except Exception as exc:
        return _invalid(exc)


@mcp.tool
def ack_notification_tool(notification_id: str, guardian_token: Optional[str] = None) -> dict[str, Any]:
    """确认通知（幂等）。家长需携带 guardian_token 且与通知所属患者绑定。"""
    try:
        return ack_notification(notification_id, guardian_token=guardian_token)
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
    """推移风险闭环状态机。仅 CKD 临床助手（MX-3 收口）。

    workflow_status: unacked → confirmed → resolved → closed（严格一步流转，禁止跳级）；
    resolved 必须携带 resolution_note（需求 §5.2）。
    BUG-46：升级是**独立布尔**（escalate_notification_tool 设置 escalated），与
    workflow_status 正交——通知可在 unacked/confirmed 下被升级，不丢失升级前状态。
    已读确认请用 ack_notification_tool（只置 status=acked，不影响 workflow_status）。
    """
    try:
        return update_notification_status(notification_id, new_status, resolution_note)
    except Exception as exc:
        return _invalid(exc)

@mcp.tool
def escalate_notification_tool(notification_id: str, reason: str = "") -> dict[str, Any]:
    """标记通知升级（HAIP 24h 未确认自动升级 / 临床主动升级）。仅 CKD 临床助手。

    escalated 是独立布尔字段（BUG-46）：升级后 workflow_status 保持原值
    （unacked/confirmed 皆可被升级），不丢失"升级前是否已确认"信息。"""
    try:
        return escalate_notification(notification_id, reason)
    except Exception as exc:
        return _invalid(exc)


if __name__ == "__main__":
    main()
