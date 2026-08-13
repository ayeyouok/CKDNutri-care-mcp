"""P3 随访沟通域 MCP 服务：随访 + 通知 + DAG + 闭环工单。

合并自 M4 (a207-followup-mcp) + M10 (a207-notification-mcp)。
v2.3 新增：trigger_event_notification（DAG）+ update_notification_status（闭环状态机）。
"""
from __future__ import annotations

import inspect
import json
import logging
import math

from functools import wraps
from typing import Any, Literal, Optional

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

# 2026-08-12（系统性审查）：标准 logging 提升可观测性 + 异常分级归类（与 assessment
# server 同口径）——① ValueError（core 层业务/参数校验）归 INVALID_INPUT 且 detail 保留；
# ② 内部数据错误（文件/JSON/RuntimeError）归 INTERNAL_ERROR；③ 未知系统异常
# （TypeError/KeyError/AttributeError/ZeroDivisionError 等内部 Code Bug）归
# INTERNAL_ERROR 且 detail **脱敏**，完整 StackTrace 仅留服务端日志（此前默认兜底
# INVALID_INPUT 会把内部 bug 误报为 400 客户端错误、且无任何日志可追溯）。
logger = logging.getLogger("CKDNutri-care-mcp")

# ---- FastMCP 类型别名（强约束 JSON Schema，提升 LLM Function Calling 准确率）----
# 值域与 core 实证对齐：
#   CKDStage ← core._BASE_INTERVAL_DAYS 键（含 G5D 透析期）；
#   AlbuminuriaStage ← core._ALBUMINURIA_REDUCTION 键；
#   VisitType ← core.schedule_followup docstring 明确五类（outpatient/phone/online/
#     dialysis/nutrition_counsel）——注意 LLM 易幻觉 routine/first_visit 等非标准值。
CKDStage = Literal["G1", "G2", "G3a", "G3b", "G4", "G5", "G5D"]
AlbuminuriaStage = Literal["A1", "A2", "A3"]
VisitType = Literal["outpatient", "phone", "online", "dialysis", "nutrition_counsel"]
NotificationStatus = Literal["all", "unacked", "acked"]
WorkflowStatus = Literal["all", "unacked", "confirmed", "resolved", "closed"]
NewWorkflowStatus = Literal["unacked", "confirmed", "resolved", "closed"]
EventType = Literal["followup_due", "risk_escalation", "report_ready"]

# 凭据敏感键模式（子串匹配 + 大小写不敏感）——键名含任一模式即整体掩码
# 2026-08-12（五审）：`auth` 简单子串会误杀 author/authority/authentic 等正常业务
# 字段（如 author_id、authority 等级）；细化为 "authorization"（凭据载体词）与
# "auth_"（凭据字段前缀），author 等不再误伤。
SENSITIVE_KEY_PATTERNS = ("token", "secret", "password", "authorization", "credential", "auth_")


def _sanitize_value(key: str, val: Any, depth: int = 0) -> Any:
    """递归脱敏 + 日志防轰炸截断（2026-08-12 系统性审查，P1）。

    ① 递归遍历 dict/list——嵌套凭据（如 {"auth": {"token": "xxx"}}）不再漏网
    （此前单层 `k in (...)` 只匹配顶层键）；② 敏感键模式子串匹配 + 大小写不敏感；
    ③ 深度 >3 或容器元素 >20 截断为摘要——防巨型 payload（indicators_snapshot /
    payload 数 KB）整体刷入日志引发磁盘 I/O 剧增。
    2026-08-12（P1 四修）：深度熔断提升至函数体**最顶部**——此前位于 dict/list 分支
    之后，深层容器在到达熔断前已持续递归（容器类型熔断失效），循环引用（如
    a["self"]=a）将无限递归直至 RecursionError 崩溃；提前熔断后任何深度 >3 的
    容器/值一律返回 "<MaxDepthReached>"，不再递归。
    """
    if depth > 3:
        return "<MaxDepthReached>"
    key_lower = str(key).lower()
    if any(p in key_lower for p in SENSITIVE_KEY_PATTERNS):
        return "***"
    if isinstance(val, dict):
        if len(val) > 20:
            return f"<Dict len={len(val)}>"
        return {k: _sanitize_value(str(k), v, depth + 1) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        if len(val) > 20:
            return f"<{type(val).__name__} len={len(val)}>"
        return [_sanitize_value(key, item, depth + 1) for item in val]
    return val


def _sanitize_bound_args(bound_args: Optional[dict[str, Any]]) -> dict[str, Any]:
    """把已按形参名归一化的参数字典整体递归脱敏（日志专用，不进客户端 detail）。"""
    if not bound_args:
        return {}
    return {k: _sanitize_value(k, v) for k, v in bound_args.items()}


def _invalid(exc: Exception, tool: str = "unknown",
             bound_args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    # 2026-08-12（系统性审查，P1）：bound_args 为**形参名绑定的完整字典**（装饰器经
    # inspect.signature.bind 归一化，含默认值）——位置参数与关键字参数统一收口，从根源
    # 杜绝位置参数绕过脱敏把 guardian_token 明文写入日志；脱敏后仅用于服务端日志，
    # 返回信封不含任何调用参数。
    safe_args = _sanitize_bound_args(bound_args)
    if isinstance(exc, CallerError):
        # BUG-54（2026-08-12）：越权/身份未解析统一返回 FORBIDDEN 信封（与本包 _guard /
        # clinical-data _guard_access 同格式），不再向上抛导致 500；PermissionDenied 带
        # caller/action/reason，CallerUnknown 缺字段时降级文案。此前 get_adherence_score 等
        # 裸调 enforce_* 的工具有越权即 500 崩溃。
        # 2026-08-12（六审）：reason 三重保底——属性为空串 "" 时 getattr 默认值不生效
        # （属性存在），此前会输出空括号 "（）"；None or str(exc) or 兜底文案。
        # 2026-08-12（七审）：caller/action 亦做 or 保底——属性被显式置 None 时
        # getattr 默认值同样不生效，会输出 "caller=None 无权 None"。
        logger.warning("随访服务鉴权拒绝: tool=%s args=%s exc=%s", tool, safe_args, exc)
        caller = getattr(exc, "caller", None) or "?"
        action = getattr(exc, "action", None) or "access"
        reason = getattr(exc, "reason", None) or str(exc) or "无明确原因"
        return {"ok": False, "error": "FORBIDDEN",
                "detail": f"caller={caller} 无权 {action}（{reason}）"}
    # BUG-52（2026-08-12）：内部数据错误归 INTERNAL_ERROR，避免误归 INVALID_INPUT
    # BUG-65（2026-08-12）：RuntimeError 补入——_load_store/_notify_load 遇 JSON 损坏时
    # 抛 RuntimeError 包装（fail-closed 防静默清空），此前 _invalid 未覆盖，损坏会被误归
    # INVALID_INPUT（400），违背"数据文件损坏=服务端内部错误"的归类初衷。
    # 2026-08-12（系统性审查，P1）：detail **脱敏**——OSError/FileNotFoundError 的 str
    # 含服务端绝对路径（如 /var/app/data/stores/patient_xxx.json），原样返回泄露内部
    # 文件系统结构；完整异常（含路径）仅留服务端日志（logger.warning）。
    if isinstance(exc, (FileNotFoundError, OSError, json.JSONDecodeError, RuntimeError)):
        logger.warning("随访服务内部数据错误: tool=%s args=%s exc=%s",
                       tool, safe_args, exc)
        return {"ok": False, "error": "INTERNAL_ERROR",
                "detail": "内部数据错误（error_code=CARE_DATA），详情见服务端日志"}
    if isinstance(exc, ValueError):
        # core 层业务/参数校验异常（值域、范围、格式等）——detail 对调用方有明确语义，保留；
        # info 级记录参数校验拦截（预期业务拒绝，供调用模式分析，非故障告警）。
        logger.info("随访服务参数校验拦截: tool=%s args=%s exc=%s", tool, safe_args, exc)
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
    # 未知系统异常 = 内部 Code Bug——归 INTERNAL_ERROR（编排层不应重试/误判入参问题），
    # detail 脱敏（不泄露内部实现），完整堆栈仅服务端日志。
    logger.error("随访服务未预期异常（内部 bug，error_code=CARE_UNKNOWN）: tool=%s args=%s",
                 tool, safe_args, exc_info=exc)
    return {"ok": False, "error": "INTERNAL_ERROR",
            "detail": "随访服务内部错误（error_code=CARE_UNKNOWN），请查服务端日志"}


def handle_mcp_exceptions(fn):
    """DRY：统一 MCP 工具异常处理模板（2026-08-12 系统性审查）。

    消除 10 个工具重复的 `try: return core(...) except Exception as exc: return _invalid(exc)`。
    functools.wraps 保留原函数 __name__/__doc__/__wrapped__——FastMCP 依赖 docstring
    生成工具描述、inspect.signature 跟随 __wrapped__ 提取参数 schema，均不受影响。
    2026-08-12（P1 三修）：异常时经 `sig.bind(*args, **kwargs)` + apply_defaults 把
    **位置参数与关键字参数统一绑定为形参名字典**（含默认值）——位置参数不再以元组
    原样进日志（此前 args=%r 会把 guardian_token 明文写入日志，高危）。
    2026-08-12（P1 四修）：bind 失败兜底按 `sig.parameters` 形参名**索引映射**位置
    参数——此前回退 {"raw_args": args} 时元素 key 为 "raw_args"（不含敏感子串），
    guardian_token 等位置参数明文落日志（脱敏失效盲区）；现逐个映射回形参名
    （guardian_token → 命中敏感模式掩码），超量位置参数用 arg_{idx} 兜底名。
    """
    sig = inspect.signature(fn)

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            try:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                bound_dict = dict(bound.arguments)
            except Exception:  # 参数非法（过多/缺失/未知键）：按形参名索引映射，保留 Key 上下文
                param_names = list(sig.parameters.keys())
                bound_dict = {}
                for idx, arg in enumerate(args):
                    p_name = param_names[idx] if idx < len(param_names) else f"arg_{idx}"
                    bound_dict[p_name] = arg
                bound_dict.update(kwargs)
            return _invalid(exc, tool=fn.__name__, bound_args=bound_dict)
    return wrapper


def main():
    mcp.run()


# ---- M4: 随访 ----

@mcp.tool
@handle_mcp_exceptions
def schedule_followup_tool(
    patient_id: str,
    ckd_stage: CKDStage,
    albuminuria_stage: AlbuminuriaStage,
    visit_type: VisitType,
    visit_date: str,
    plan_summary: str = "",
    note_to_clinician: str = "",
) -> dict[str, Any]:
    """创建随访计划（按 CKD 分期自动算 next_due）。CKD 临床助手独占。

    ckd_stage: G1/G2/G3a/G3b/G4/G5/G5D（KDIGO 2024 儿童分期）；
    albuminuria_stage: A1/A2/A3；visit_type: outpatient/phone/online/dialysis/
    nutrition_counsel（LLM 勿用 routine/first_visit 等非标准值）。"""
    return schedule_followup(
        patient_id=patient_id,
        ckd_stage=ckd_stage,
        albuminuria_stage=albuminuria_stage,
        visit_type=visit_type,
        visit_date=visit_date,
        plan_summary=plan_summary,
        note_to_clinician=note_to_clinician,
    )


@mcp.tool
@handle_mcp_exceptions
def get_followup_records_tool(patient_id: str, guardian_token: Optional[str] = None) -> dict[str, Any]:
    """查随访历史（含计划 + 下次到期日）。按身份视图裁剪；家长需携带 guardian_token。"""
    return get_followup_records(patient_id=patient_id, guardian_token=guardian_token)


@mcp.tool
@handle_mcp_exceptions
def add_followup_record_tool(
    patient_id: str,
    visit_date: str,
    visit_type: VisitType,
    ckd_stage: CKDStage,
    indicators_snapshot: Optional[dict[str, Any]] = None,
    plan_summary: str = "",
    doctor_notes: str = "",
) -> dict[str, Any]:
    """记录一次实际完成的就诊随访（写，仅 CKD 临床助手）。

    visit_type: outpatient/phone/online/dialysis/nutrition_counsel；
    ckd_stage: G1/G2/G3a/G3b/G4/G5/G5D；
    indicators_snapshot: 本次就诊检验指标快照（如 {egfr, k_mmol_L, ...}），供后续评估对照。
    """
    # 2026-08-12（BUG-65/P1/五/七审）：core.add_followup_record 权限矩阵已登记后补暴露；
    # indicators_snapshot 显式 isinstance 校验（防非 dict 穿透 `or {}` 短路进 core 崩溃）。
    if indicators_snapshot is not None and not isinstance(indicators_snapshot, dict):
        raise ValueError(
            f"indicators_snapshot 必须为 dict，实际为 {type(indicators_snapshot).__name__}")
    return add_followup_record(
        patient_id=patient_id,
        visit_date=visit_date,
        visit_type=visit_type,
        ckd_stage=ckd_stage,
        indicators_snapshot=indicators_snapshot or {},
        plan_summary=plan_summary,
        doctor_notes=doctor_notes,
    )


@mcp.tool
@handle_mcp_exceptions
def get_adherence_score_tool(
    patient_id: str,
    diet_ratio: float,
    med_ratio: float,
    visit_ratio: float,
    weights: Optional[list[float]] = None,
) -> dict[str, Any]:
    """计算并落库依从性评分。仅 CKD 临床助手可写。

    weights: 自定义权重列表 [diet, medication, visit]（三项之和应为 1），默认等权 1/3；
    仅传数值（int/float），不得含 bool/NaN/Inf。
    """
    # 2026-08-12（六/七/八审）：weights 三层防线——① 外层 isinstance（防 str 被 tuple()
    # 拆字符）；② 元素级数值断言（防 [1.0,"0.5"]/[None]/[True]，bool 是 int 子类）；
    # ③ math.isfinite（防 NaN/Inf——isinstance 校验对 float('nan') 无效，Inf 进 core
    # 乘加可抛 OverflowError、NaN 写库 JSON 序列化失败）。客户端错误统一 ValueError →
    # INVALID_INPUT，不触发 ERROR 告警；内部 bug 异常仍 INTERNAL_ERROR（不全局吞异常）。
    if weights is not None:
        if not isinstance(weights, (list, tuple)):
            raise ValueError(
                f"weights 必须为 list/tuple（数值权重），实际为 {type(weights).__name__}")
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                   and math.isfinite(x) for x in weights):
            raise ValueError("weights 列表所有元素必须为有限数值（int/float，不含 bool/NaN/Inf）")
    kw = {"weights": tuple(weights)} if weights else {}
    return get_adherence_score(patient_id=patient_id, diet_ratio=diet_ratio,
                               med_ratio=med_ratio, visit_ratio=visit_ratio, **kw)


@mcp.tool
@handle_mcp_exceptions
def get_pew_timeline_tool(
    patient_id: str,
    guardian_token: Optional[str] = None,
    pew_history: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """PEW 历史并入统一随访时间线（facade）。家长需携带 guardian_token。

    pew_history: PEW 评估记录对象数组（每个元素为 {date, level, ...} 对象）。
    """
    # 2026-08-12（P1/七/八审）：pew_history 两层防线——① 外层 isinstance（防 str/数字
    # 穿透）；② 元素级 dict 断言（防 ["2026-08-12"] 等非 dict 元素进 core 时间线解析抛
    # TypeError/AttributeError）。
    if pew_history is not None:
        if not isinstance(pew_history, (list, tuple)):
            raise ValueError(
                f"pew_history 必须为 list/tuple，实际为 {type(pew_history).__name__}")
        if not all(isinstance(x, dict) for x in pew_history):
            raise ValueError("pew_history 列表中所有元素必须为 dict 对象")
    return get_pew_timeline(patient_id=patient_id, guardian_token=guardian_token,
                            pew_history=pew_history)


# ---- M10: 通知 ----

@mcp.tool
@handle_mcp_exceptions
def get_notifications_tool(
    patient_id: str,
    status: NotificationStatus = "all",
    workflow_status: WorkflowStatus = "all",
    escalated: Optional[bool] = None,
    guardian_token: Optional[str] = None,
    page: Optional[int] = None,
    page_size: int = 50,
) -> dict[str, Any]:
    """查通知列表。status 按已读（all/unacked/acked）；workflow_status 按闭环状态
    （all/unacked/confirmed/resolved/closed）；escalated 按升级布尔过滤
    （升级与 workflow_status 正交）；家长需携带 guardian_token。
    page/page_size（P2 修复 2026-08-13）：可选分页——page=None 返回全量（兼容），
    传 page 则分页（page_size 默认 50、上限 200 钳制），避免通知多时灌爆上下文。
    """
    # 2026-08-12（BUG-25/46 + 系统性审查/五审）：workflow_status 过滤 BUG-25 新增、
    # escalated 独立布尔 BUG-46；status/workflow_status 去 Optional（防 null 穿透）并
    # 统一使用头部别名（防双处漂移）。
    return get_notifications(patient_id=patient_id, status=status, workflow_status=workflow_status,
                             escalated=escalated, guardian_token=guardian_token,
                             page=page, page_size=page_size)


@mcp.tool
@handle_mcp_exceptions
def ack_notification_tool(notification_id: str, guardian_token: Optional[str] = None) -> dict[str, Any]:
    """确认通知（幂等）。家长需携带 guardian_token 且与通知所属患者绑定。"""
    return ack_notification(notification_id=notification_id, guardian_token=guardian_token)


# ---- DAG: 事件→模板→落库→推送 一键完成 (v2.3) ----

@mcp.tool
@handle_mcp_exceptions
def trigger_event_notification_tool(
    event_type: EventType,
    patient_id: str,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """事件触发的通知创建：事件类型→模板填充→落库→推送 一键完成（DAG）。

    event_type: followup_due / risk_escalation / report_ready。
    payload 按事件类型填字段（须为对象 dict）：
      - followup_due: {next_due_date(, due_at)}
      - risk_escalation: {from_level, to_level, rule}
      - report_ready: {}（无需额外字段）
    """
    # 2026-08-12（v0.3.1 修复 + 五/七审）：build_event_notification 完成模板填充+落库
    # （已含 create_notification），不二次写库；payload 显式 isinstance 校验（防字符串
    # 穿透 `or {}` 短路进 core 抛 AttributeError）。
    if payload is not None and not isinstance(payload, dict):
        raise ValueError(f"payload 必须为 dict，实际为 {type(payload).__name__}")
    return build_event_notification(event_type=event_type, patient_id=patient_id,
                                    payload=payload or {})


# ---- 闭环：风险工单状态机 (v2.3) ----

@mcp.tool
@handle_mcp_exceptions
def update_notification_status_tool(
    notification_id: str,
    new_status: NewWorkflowStatus,
    resolution_note: str = "",
) -> dict[str, Any]:
    """推移风险闭环状态机。仅 CKD 临床助手。

    new_status: unacked → confirmed → resolved → closed（严格一步流转，禁止跳级）；
    目标为 resolved 时必须携带 resolution_note。已读确认请用 ack_notification_tool
    （只置 status=acked，不影响 workflow_status）。
    """
    # 2026-08-12（BUG-46 + 五审）：升级是独立布尔（escalate_notification_tool 设置
    # escalated），与 workflow_status 正交；new_status 统一头部别名 NewWorkflowStatus。
    return update_notification_status(notification_id=notification_id,
                                      new_status=new_status, resolution_note=resolution_note)


@mcp.tool
@handle_mcp_exceptions
def escalate_notification_tool(notification_id: str, reason: str = "") -> dict[str, Any]:
    """标记通知升级（HAIP 24h 未确认自动升级 / 临床主动升级）。仅 CKD 临床助手。

    升级后 workflow_status 保持原值（unacked/confirmed 皆可被升级），不丢失升级前状态。
    """
    # 2026-08-12（BUG-46）：escalated 是独立布尔字段，与 workflow_status 正交。
    return escalate_notification(notification_id=notification_id, reason=reason)


if __name__ == "__main__":
    main()
