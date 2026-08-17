# -*- coding: utf-8 -*-
"""M4 核心逻辑（纯函数，无 fastmcp 依赖，可单测）。

内容：
1. 随访记录时间线（get_followup_records）：随访计划摘要可见，原始医生备注仅医生/营养可见
2. 依从性评分（get_adherence_score / calc_adherence_score）：透明加权复合分
3. PEW 时间线 facade（get_pew_timeline）：数据归属 M3（ADR-007），M4 仅聚合读取
4. 随访计划（schedule_followup）：频率按 KDIGO 2024 儿科推荐

身份与权限（Plan A）：caller 由部署环境注入（a207_policy.get_caller，env A207_CALLER），
模型不得自证身份（P0-1）；放行集合取自 a207_policy，本包不维护副本（P1-1）；
运行时写库经 resolve_state_path 落到可写目录，不写安装目录（P1-3）。

数据来源（权威标尺）：
- KDIGO 2024 CKD Guideline, Kidney Int 2024;105(4S):S117-S314（Francis et al. JAMA Pediatr 2024 儿科要点）
- NICE NG203 Table 2（2021 发布，2024 复核）
- 儿科共识 Rec17（Scielo 初级保健儿科 2024，引 Furth 2018 / KDIGO 2012）
- PRNT 2025 人体测量频率（Pediatr Nephrol 40:69-84）
- 依从性测量：MMAS-8 为 CKD 最常用工具（Springer Med 系统综述 2024）；儿科 CKD 复合依从性评分无单一经验证量表（pubmed 24814533），故采用系统定义加权复合分。
"""
from __future__ import annotations

import json
import math
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from a207_policy import (
    FOLLOWUP_CLINICIAN,
    PARENT_ROLE,
    PermissionDenied,
    enforce_read,
    enforce_write,
    get_caller,
    validate_patient_id,
    verify_guardian_token,
)

from .repository import get_repository

STORE_FILENAME = "followup_store.json"
DATA_DIR_ENV = "A207_FOLLOWUP_DATA_DIR"
# 本包在权限矩阵中的登记名（enforce_* 查表键，唯一事实源在 a207_policy.matrix）
MCP_NAME = "CKDNutri-care-mcp"

# 临床角色可见原始医生备注；患者/家属角色仅可见摘要（角色集合单一事实源在 a207_policy）
_CLINICIAN = FOLLOWUP_CLINICIAN

# ---------------------------------------------------------------------------
# M10 通知引擎支持块（与随访存储隔离，避免 key 命名空间污染）
# 权限判定统一走 _guard → enforce_*（2026-08-12 双轨制清理，见 _guard 定义）
# ---------------------------------------------------------------------------
_NOTIFY_STORE_FILENAME = "notification_store.json"
_NOTIFY_DATA_DIR_ENV = "A207_NOTIFICATION_DATA_DIR"

# 并发正确性（四审，2026-08-12）：
# - BUG-53 起：atomic_write_json 只防单次写半截断，不防两请求读-改-写丢更新；
#   所有写路径的 load→改→save 序列必须持 _STORE_LOCK（单进程内串行化）。
# - v2.4 起存储走 repository（JSON ↔ Tablestore 双后端）：_STORE_LOCK 定位降级为
#   **进程内优化**（减少乐观锁冲突重试），不再是正确性保证——Tablestore 后端
#   的写原子性由 repository 的 _rev 版本列 + 条件更新（乐观锁）保证（跨进程/多
#   worker 部署安全）；JSON 后端维持单进程语义（多进程 JSON 本身不支持，迁移
#   Tablestore 是正解，见 repository.py 模块 docstring）。
_STORE_LOCK = threading.Lock()

# 设计说明（2026-08-12 复核）：通知库按 notification_id 为根**扁平存储**是有意取舍——
# ack/update/escalate 均按 nid O(1) 直查（高频路径）；get_notifications 按 patient_id
# 过滤是 O(N) 全表遍历（低频读）。Tablestore 后端 notification_store 以 notification_id
# 为主键（行模型天然 O(1) 直查），get_notifications 用 GetRange 全表过滤——与 JSON
# 端语义一致，儿科单中心量级开销可忽略。若未来量级增长到万级通知，再评估
# patient_id 二级索引。
# 存储读写统一走 repository（JSON ↔ Tablestore 双后端，v2.4）：路径解析、损坏文件
# fail-closed、原子写、乐观锁全部收敛到 repository.py，core 不再直接操作文件。


def _forbidden(role: str, action: str) -> dict[str, Any]:
    return {"ok": False, "error": "FORBIDDEN",
            "detail": f"caller={role} 无权执行 {action}（P3 权限矩阵）"}


def _guard(mcp_name: str, tool: str, *, write: bool = False) -> dict[str, Any] | None:
    """统一权限守卫（2026-08-12 双轨制清理）：所有工具统一走 a207_policy.enforce_* 中枢，
    MX-3 写工具白名单与矩阵回查在此生效；越权转成既有 FORBIDDEN 信封（契约不变）。
    此前 schedule_followup / create_notification / ack_notification 用本地集合手动判断，
    绕过 enforce_* 的 MX-3 与矩阵回查——策略收紧时本地集合不会同步生效。
    """
    try:
        if write:
            enforce_write(mcp_name, tool)
        else:
            enforce_read(mcp_name, tool)
    except PermissionDenied as exc:
        return _forbidden(exc.caller, exc.reason)
    return None


def _guard_guardian(caller: str, patient_id: str, guardian_token: str | None,
                    tool: str) -> dict[str, Any] | None:
    """家长-患儿绑定核验（BUG-40 修复，2026-08-12）。

    P3 面向家长的读工具此前只做矩阵级 enforce_read（家长对 P3=READ/RL 放行），
    家长可传任意 patient_id 读取其他患儿随访摘要/通知/PEW 趋势/标记已读——跨患者
    隐私泄露。本辅助与 P1/P2 同口径：复用 a207_policy.verify_guardian_token
    （含过期校验，单一事实源，不在此维护副本）；家长必须携带与其患儿绑定的
    guardian_token 才能访问。
    """
    if caller != PARENT_ROLE:
        return None
    if not guardian_token:
        return {"ok": False, "error": "GUARDIAN_UNVERIFIED",
                "detail": f"caller=parent_assistant 调用 {tool} 必须携带 guardian_token"}
    if not verify_guardian_token(patient_id, guardian_token):
        return {"ok": False, "error": "FORBIDDEN",
                "detail": f"guardian_token 与 patient_id={patient_id} 不匹配或已过期"}
    return None


# 末端事件 -> 通知模板（零跨包 import：事件数据由编排层经 payload 传入）
_EVENT_TEMPLATES: dict[str, dict[str, str]] = {
    "followup_due": {
        "category": "followup_due",
        "priority": "high",
        "title": "随访到期提醒",
        "body_tmpl": "患者 {patient_id} 的随访计划将于 {next_due_date} 到期，请及时预约就诊。",
    },
    "risk_escalation": {
        "category": "risk_alert",
        "priority": "high",
        "title": "风险等级升高预警",
        "body_tmpl": "风险等级由 {from_level} 升至 {to_level}（依据 {rule}），请关注并复核。",
    },
    "report_ready": {
        "category": "report_ready",
        "priority": "medium",
        "title": "营养评估报告已生成",
        "body_tmpl": "患者 {patient_id} 的定期营养评估报告已生成，可前往查看完整报告。",
    },
}

_SOURCE_MAP = {
    "followup_due": "M4:next_due",
    "risk_escalation": "M8:risk_level",
    "report_ready": "M9:report",
}

# ---------------------------------------------------------------------------
# KDIGO 2024 儿科随访频率（recommend_followup_interval）
# ---------------------------------------------------------------------------
_CITATION = (
    "KDIGO 2024 (Kidney Int 2024;105(4S):S117-S314); "
    "NICE NG203 Table 2 (2021, rev 2024); "
    "儿科共识 Rec17 (Scielo 2024, 引 Furth 2018); "
    "PRNT 2025 人体测量 CKD5D 每月 (Pediatr Nephrol 40:69-84)"
)
# 基础间隔（天）：儿科共识 Rec17 —— G1-2 每 1-2 次/年(180d)，G3a 每 ≥3-4 次/年(90d)，
# G3b（eGFR 30-44，进展风险高于 G3a）每 ≥5-6 次/年(60d)，G4 每 ≥6-12 次/年(60d)，
# G5 每 >4 次/年(60d)，G5D 每月(30d，PRNT 2025)。
# BUG-50（2026-08-12）：G3b 从 90d 收窄到 60d——KDIGO 2024 建议 G3b 随访频于 G3a
# （原实现两档同 90d，eGFR 30-44 的进展风险未体现）。
# BUG-65（2026-08-12）：G4 从 90d 收窄到 60d——修复"G4 比 G3b 更重却随访更稀"的倒挂。
# 依据：KDIGO 2024 核心原则"风险越高监测越频"（G4 eGFR 15-29 进展风险高于 G3b 30-44），
# 且 NICE NG203 Table 2（本函数 citation 同引）G4 最低 2 次/年、G5 4 次/年——
# 原 G4=90d 与 G3b=60d 相比间隔倒置，会延误 G4 期患儿的进展监测。
_BASE_INTERVAL_DAYS = {
    "G1": 180, "G2": 180, "G3a": 90, "G3b": 60, "G4": 60, "G5": 60, "G5D": 30,
}
# 白蛋白尿升级：A2 缩短 30 天，A3 缩短 60 天（下限 14 天）
_ALBUMINURIA_REDUCTION = {"A1": 0, "A2": 30, "A3": 60}
_MIN_INTERVAL = 14
# M3（2026-08-16，第七轮审查）：visit_type 合法值（此前 add_followup_record/
# schedule_followup 均未校验，任意字符串落库；显式枚举 fail-closed）
_VISIT_TYPES = frozenset({"outpatient", "phone", "online", "dialysis", "nutrition_counsel"})
# L-3（2026-08-16，十一审）：随访记录绝对上限——防异常数据（远超正常随访量，
# 如每日多次写入累积数十年）单次返回撑爆 LLM 上下文；正常患儿（数十年随访）
# 也远低于此，limit=None 默认全量兼容，仅超上限截断。
_FOLLOWUP_RECORD_CAP = 10_000


# ---------------------------------------------------------------------------
# 存储助手
# ---------------------------------------------------------------------------
# 随访存储读写统一走 repository（v2.4 双后端）：按 patient_id 分片
# （load_followup/save_followup），路径解析、损坏文件 fail-closed（BUG-65/67）、
# 原子写、Tablestore 乐观锁全部收敛到 repository.py。写路径仍持 _STORE_LOCK
# （进程内优化，见上方并发正确性说明）。


def _now_iso() -> str:
    """带时区 UTC 时间戳（P1-8 修复 2026-08-13）——此前 datetime.now() naive 本地
    时间，与 gate.py 的 timezone.utc 比较/跨实例一致性冲突；统一 UTC+aware。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    # C2（2026-08-15）：统一 UTC 业务日——此前 date.today() 本地 naive，跨时区
    # 部署"今天/未来日期"判断漂移（未来日期拒绝、followup_due 到期判断全受影响）
    return datetime.now(timezone.utc).date().isoformat()


def _require_iso_date(value: Any, field: str = "visit_date",
                      allow_future: bool = True) -> str:
    """校验日期串为 YYYY-MM-DD 并返回规范化值（C/D，2026-08-12 三审）。

    - schedule_followup 的 visit_date 是计划基准日期（今日/过去均可，allow_future=True）；
    - add_followup_record 是"实际完成"的就诊记录，未来日期为脏数据（allow_future=False，
      对齐 P1 models.LabResultIn report_date 未来日期拒绝口径）。
    显式校验比 date.fromisoformat 的裸 ValueError 更友好（detail 直接说明格式契约），
    且 fail-closed：非法格式不进 next_due_date 计算，杜绝脏值静默穿透。
    """
    try:
        # 严格 %Y-%m-%d 精确匹配——date.fromisoformat 在 3.11+ 会接受 "20260801" 等
        # 紧凑格式与契约不符；strptime 精确格式对 "20260801"/"2026-8-1"/"2026/08/01"
        # 一律拒绝（fail-closed）。
        d = datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须为 YYYY-MM-DD 格式，收到：{value!r}")
    # L（2026-08-16，第七轮审查）：未来日期判断统一 UTC 业务日——此前 date.today()
    # 本地 naive，与全局 UTC 口径（_today()/_now_iso()）脱节，跨时区部署漂移。
    if not allow_future and d > datetime.now(timezone.utc).date():
        raise ValueError(
            f"{field} {d.isoformat()} 晚于当前 UTC 日期 "
            f"{datetime.now(timezone.utc).date().isoformat()}，"
            "疑似未来数据，拒绝写入（实际就诊记录不应来自未来）")
    return d.isoformat()


def _add_days(iso_date: str, days: int) -> str:
    return (date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()


def _short_id(prefix: str, patient_id: str) -> str:
    return f"{prefix}-{patient_id}-{uuid.uuid4().hex[:8]}"


# D1 修复（2026-08-14）：家长视角通知裁剪的医生内部字段（get_notifications / ack 共用）。
# resolution_note（处置备注）/escalation_reason（升级理由）/status_updated_by（医生身份）/
# escalated_by（升级操作者）不在 CLINICIAN_ONLY_FIELDS，须在此显式收口。
_NOTIF_CLINICIAN_ONLY = frozenset(
    {"resolution_note", "escalation_reason", "status_updated_by", "escalated_by"})


def _mask_notification(rec: dict, caller: str) -> dict:
    """非临床角色裁剪通知的医生内部字段（幂等：临床角色原样返回）。"""
    if caller in _CLINICIAN:
        return rec
    out = {k: v for k, v in rec.items() if k not in _NOTIF_CLINICIAN_ONLY}
    # L-2（2026-08-16，十一审）：升级审计列表 escalated_history[].by 直透医生身份
    # ——顶层 escalated_by 已剥，但历史条目里的 by（记录医生身份）漏网。家长看到
    # 每次升级的操作者身份（PII 级）。逐元素剥 by（保留 id/at/reason 供家长了解
    # 升级轨迹）。
    hist = out.get("escalated_history")
    if isinstance(hist, list):
        # F5（2026-08-17，十二审）：元素非 dict（脏数据如字符串）时 h.items() 抛
        # AttributeError 且不被 except 捕获 → 家长 get_notifications 整次读变
        # INTERNAL_ERROR（可用性故障）。非 dict 元素原样保留（不剥 by 也不 crash），
        # 脏数据不影响整次读取。
        out["escalated_history"] = [
            ({k2: v2 for k2, v2 in h.items() if k2 != "by"}
             if isinstance(h, dict) else h) for h in hist]
    elif isinstance(hist, str):
        try:
            parsed = json.loads(hist)
            if isinstance(parsed, list):
                out["escalated_history"] = [
                    ({k2: v2 for k2, v2 in h.items() if k2 != "by"}
                     if isinstance(h, dict) else h) for h in parsed]
        except (json.JSONDecodeError, TypeError):
            # F5/C 审计（2026-08-17）：JSON 损坏时**剥除整个字段**（fail-closed）——
            # 此前 pass 保留原样，若损坏串含 by 即泄露医生身份（fail-open 方向）。
            # 家长视角宁可少给不给漏；存储层读取已有 fail-closed 拦截（读时抛错），
            # 此处兜底家长展示路径。
            out.pop("escalated_history", None)
    return out


def _visible_record(rec: dict, caller: str) -> dict:
    """患者/家属角色剔除原始医生备注与医生身份，仅留可见摘要。

    L-2（2026-08-16，十一审）：此前只剥 doctor_notes，created_by（记录医生身份）
    漏网直透家长（PII 级）。现一并剥除；保留 created_at 供家长了解记录时间。
    """
    out = dict(rec)
    if caller not in _CLINICIAN:
        out.pop("doctor_notes", None)
        out.pop("created_by", None)
    return out


def _visible_plan(plan: dict, caller: str) -> dict:
    out = dict(plan)
    if caller not in _CLINICIAN:
        out.pop("note_to_clinician", None)
    return out


# ---------------------------------------------------------------------------
# 1. KDIGO 2024 随访频率推荐
# ---------------------------------------------------------------------------
def recommend_followup_interval(ckd_stage: str, albuminuria_stage: str = "A1") -> dict[str, Any]:
    """按 KDIGO 2024 儿科推荐返回随访间隔（天）。

    :param ckd_stage: G1/G2/G3a/G3b/G4/G5/G5D
    :param albuminuria_stage: A1/A2/A3（白蛋白尿越重，随访越频）
    :return: {recommended_interval_days, basis, citation}
    """
    if ckd_stage not in _BASE_INTERVAL_DAYS:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "ckd_stage 必须是 G1/G2/G3a/G3b/G4/G5/G5D"}
    # BUG-65（2026-08-12）：albuminuria_stage 显式校验合法值——此前仅 .get(...,0) 静默降级，
    # 传 "A4" 等非法值会被当作 A1 且不报错，掩盖配置错误（与 ckd_stage 的 fail-fast 同口径）。
    if albuminuria_stage not in _ALBUMINURIA_REDUCTION:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"albuminuria_stage 必须是 A1/A2/A3，收到：{albuminuria_stage!r}"}
    base = _BASE_INTERVAL_DAYS[ckd_stage]
    red = _ALBUMINURIA_REDUCTION[albuminuria_stage]
    interval = max(_MIN_INTERVAL, base - red)
    return {
        "ok": True,
        "data": {
            "ckd_stage": ckd_stage,
            "albuminuria_stage": albuminuria_stage,
            "recommended_interval_days": interval,
            "basis": "KDIGO 2024 儿科随访频率（NICE NG203 Table 2 + 儿科共识 Rec17 + PRNT 2025）",
            "citation": _CITATION,
        },
    }


# ---------------------------------------------------------------------------
# 2. 随访计划（写，医生/营养师）
# ---------------------------------------------------------------------------
def schedule_followup(patient_id: str, ckd_stage: str, albuminuria_stage: str,
                      visit_type: str, visit_date: str,
                      plan_summary: str = "", note_to_clinician: str = "") -> dict[str, Any]:
    """创建随访计划（写，MX 收口：仅医生/营养师/编排层）。频率来自 KDIGO 2024 推荐。

    :param visit_type: outpatient/phone/online/dialysis/nutrition_counsel
    :param visit_date: 计划基准日期 YYYY-MM-DD（通常为本次就诊日；BUG-65 统一命名，
        与 server 工具层及 add_followup_record 的 visit_date 一致，此前 core 叫 anchor_date）
    :param caller: 缺省取部署注入身份（A207_CALLER），模型不可自证（P0-1）
    :param plan_summary: 随访计划摘要（所有角色可见）
    :param note_to_clinician: 仅供医生/营养的备注（患者/家属不可见）
    """
    caller = get_caller()
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（a207_policy.validate_patient_id
    # ^P[0-9]{4,}$，与 P1 his 同口径）——畸形 id 不进存储/查询层。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}

    # 双轨制清理（2026-08-12）：统一走 enforce_write 中枢（MX-3 + 矩阵回查），
    # 不再本地判断 _WRITE_ALLOWED（策略收紧时本地集合不会同步生效）。
    denied = _guard(MCP_NAME, "schedule_followup", write=True)
    if denied:
        return denied
    # C（2026-08-12 三审）：visit_date 显式 YYYY-MM-DD 校验（计划基准日期，允许过去/今日）
    visit_date = _require_iso_date(visit_date, "visit_date")
    # M3（2026-08-16）：visit_type 枚举校验（与 add_followup_record 同口径）
    if visit_type not in _VISIT_TYPES:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"visit_type 必须是 {'/'.join(sorted(_VISIT_TYPES))} 之一，"
                          f"收到 {visit_type!r}"}
    rec = recommend_followup_interval(ckd_stage, albuminuria_stage)
    if not rec["ok"]:
        return rec
    interval = rec["data"]["recommended_interval_days"]
    plan = {
        "plan_id": _short_id("FP", patient_id),
        "cadence": {
            "interval_days": interval,
            "anchor_date": visit_date,
            "next_due_date": _add_days(visit_date, interval),
            "basis": rec["data"]["basis"],
            "citation": rec["data"]["citation"],
        },
        "visit_type": visit_type,
        "status": "active",
        "plan_summary": plan_summary,
        "note_to_clinician": note_to_clinician,
        "created_by": caller,
        "created_at": _now_iso(),
    }
    with _STORE_LOCK:
        p = get_repository().load_followup(patient_id)
        if p is None:
            p = {"records": [], "plans": [], "adherence": []}
        p.setdefault("records", [])
        p.setdefault("plans", [])
        p.setdefault("adherence", [])
        p["plans"].append(plan)
        get_repository().save_followup(patient_id, p)
    # 写操作由临床角色发起，返回完整计划
    return {"ok": True, "data": {"plan": plan}}


# ---------------------------------------------------------------------------
# 3. 随访记录时间线（读）
# ---------------------------------------------------------------------------
def get_followup_records(patient_id: str, guardian_token: str | None = None,
                         limit: int | None = None,
                         offset: int = 0) -> dict[str, Any]:
    """读取某患者随访记录与计划（读，所有角色可读）。

    权限：临床角色（医生/营养/编排/风险）看到完整记录（含医生备注）；
    患者/家属角色（parent_assistant/child_companion）仅见摘要——原始医生备注被剔除。
    BUG-40（2026-08-12）：家长读取必须携带 guardian_token 完成患儿绑定核验
    （此前家长可传任意 patient_id 跨患者读取随访摘要）。
    身份缺省取部署注入值（A207_CALLER），模型不可自证（P0-1）。

    L-3（2026-08-16，十一审）：**分页**——随访记录随年限无界增长，此前无 limit
    全量返回，LLM 上下文线性膨胀（多年随访记录直接撑爆上下文）。现支持
    limit/offset（按 visit_date 升序分页），limit=None 时返回全量（向后兼容），
    但超过 _FOLLOWUP_RECORD_CAP（10 万）仍截断防超限（记录数异常时保护）。
    """
    caller = get_caller()
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（a207_policy.validate_patient_id
    # ^P[0-9]{4,}$，与 P1 his 同口径）——畸形 id 不进存储/查询层。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}

    # 双轨制清理：统一走 enforce_read 中枢
    denied = _guard(MCP_NAME, "get_followup_records", write=False)
    if denied:
        return denied
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_followup_records")
    if denied:
        return denied
    if limit is not None and (limit < 0 or offset < 0):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "limit/offset 不能为负"}
    p = get_repository().load_followup(patient_id)
    if not p:
        return {"ok": True, "data": {
            "patient_id": patient_id, "records": [], "plans": [],
            "message": "无随访数据",
            # E（2026-08-12 三审）：无数据分支 visibility 按调用方角色返回——
            # 此前固定 "summary_only" 会让临床角色误以为视图被裁剪（实际只是无数据）。
            "visibility": "full" if caller in _CLINICIAN else "summary_only"}}
    # A（2026-08-12 三审）：p["records"]/p["plans"] 硬索引改 .get() 防御——
    # 早期版本/手工编辑的 store 可能缺 "plans" 键（setdefault 默认结构未写入时），
    # 硬索引 KeyError 且被 server _invalid 归 INTERNAL_ERROR，掩盖"脏数据"真相。
    records = [_visible_record(r, caller) for r in (p.get("records") or [])]
    plans = [_visible_plan(pl, caller) for pl in (p.get("plans") or [])]
    # P3 其余（2026-08-15）：随访记录按 visit_date 升序返回——此前原样透传存储顺序
    # （追加序），家长/临床视角时间线混乱，趋势性阅读依赖调用方自行排序。
    records = sorted(records, key=lambda r: str(r.get("visit_date") or ""))
    # L-3（2026-08-16）：分页截断——超限保护（记录数异常时防 LLM 上下文爆）：
    # limit=None 默认全量（向后兼容），显式 limit 按 offset 分页；_FOLLOWUP_RECORD_CAP
    # 为绝对上限（远超正常随访量，仅防御异常数据）。
    total = len(records)
    if limit is None and total > _FOLLOWUP_RECORD_CAP:
        records = records[:_FOLLOWUP_RECORD_CAP]
    elif limit is not None:
        records = records[offset:offset + limit]
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            "records": records,
            "plans": plans,
            "visibility": "full" if caller in _CLINICIAN else "summary_only",
            "total": total,
            "truncated": len(records) < total,
        },
    }


def _record_fingerprint(rec: dict[str, Any]) -> str:
    """八审（2026-08-16，M6 补全）：随访记录**内容指纹**——去重键从
    (visit_date, visit_type) 天粒度扩展为 (visit_date, visit_type, 内容指纹)。

    天粒度去重的缺陷：同类型同日的两次**真实访视**（如上午+下午各一次
    dialysis、两次 nutrition_counsel）会被误判 DUPLICATE 并强制"编辑覆盖"，
    真实丢数据。现判定改为：同日同类型**且内容完全一致**（indicators_snapshot/
    plan_summary/doctor_notes 相同）才视为真重试拒绝；内容不同即两次真实访视，
    放行追加。序列化排序保证字典键序无关（同内容不同键序不算差异）。
    """
    return json.dumps({
        "visit_type": rec.get("visit_type"),
        "indicators_snapshot": rec.get("indicators_snapshot") or {},
        "plan_summary": rec.get("plan_summary") or "",
        "doctor_notes": rec.get("doctor_notes") or "",
    }, sort_keys=True, ensure_ascii=False, default=str)


def _check_snapshot_jsonable(indicators_snapshot: dict[str, Any]) -> None:
    """八审（2026-08-16）：indicators_snapshot 内容须可 JSON 序列化——
    此前只验 isinstance dict，dict 内嵌 NaN/Inf/non-serializable 对象会在
    save_followup 的 json.dumps 处炸 INTERNAL_ERROR（且无明确入口提示）。
    显式预检 + 明确 detail，fail-closed（不可序列化拒绝落库，防脏数据写盘）。
    """
    try:
        json.dumps(indicators_snapshot, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"indicators_snapshot 含不可 JSON 序列化内容，拒绝落库（防脏数据写盘）: {exc}") from exc


def add_followup_record(patient_id: str, visit_date: str, visit_type: str, ckd_stage: str,
                        indicators_snapshot: dict, plan_summary: str, doctor_notes: str = "") -> dict[str, Any]:
    """追加一条随访记录（写，仅临床角色；server.add_followup_record_tool 直接暴露）。
    G（2026-08-12 三审）：docstring 修正——此前写"不在 MCP 工具直接暴露，由 router 调
    server 封装"，但 server.py 已注册 add_followup_record_tool，属文档-实现漂移。"""
    caller = get_caller()
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（a207_policy.validate_patient_id
    # ^P[0-9]{4,}$，与 P1 his 同口径）——畸形 id 不进存储/查询层。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}

    # 双轨制清理：统一走 enforce_write 中枢（MX-3 写工具白名单，与 schedule_followup 一致）
    denied = _guard(MCP_NAME, "add_followup_record", write=True)
    if denied:
        return denied
    # C/D（2026-08-12 三审）：visit_date 显式校验——随访记录是"实际完成"的就诊，
    # 未来日期为脏数据拒绝（对齐 P1 report_date 未来拒绝口径）。
    visit_date = _require_iso_date(visit_date, "visit_date", allow_future=False)
    # M3（2026-08-16，第七轮审查）：visit_type/ckd_stage 枚举校验——此前任意字符串
    # 静默落库（typo 如 "outpatinet" 产生脏数据，且后续按类型统计不可靠）。
    if visit_type not in _VISIT_TYPES:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"visit_type 必须是 {'/'.join(sorted(_VISIT_TYPES))} 之一，"
                          f"收到 {visit_type!r}"}
    if ckd_stage not in _BASE_INTERVAL_DAYS:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"ckd_stage 必须是 {'/'.join(_BASE_INTERVAL_DAYS)} 之一，"
                          f"收到 {ckd_stage!r}"}
    # 八审（2026-08-16）：indicators_snapshot 内容可序列化预检（fail-closed，防脏数据写盘）
    if indicators_snapshot is None or not isinstance(indicators_snapshot, dict):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "indicators_snapshot 必须为 dict"}
    try:
        _check_snapshot_jsonable(indicators_snapshot)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
    rec = {
        "record_id": _short_id("FR", patient_id),
        "visit_date": visit_date,
        "visit_type": visit_type,
        "ckd_stage": ckd_stage,
        "indicators_snapshot": indicators_snapshot,
        "plan_summary": plan_summary,
        "doctor_notes": doctor_notes,
        "created_by": caller,
        "created_at": _now_iso(),
    }
    with _STORE_LOCK:
        p = get_repository().load_followup(patient_id)
        if p is None:
            p = {"records": [], "plans": [], "adherence": []}
        p.setdefault("records", [])
        p.setdefault("plans", [])
        p.setdefault("adherence", [])
        # P3 其余（2026-08-15）：幂等——同患者同 visit_date 已存在记录拒绝重复追加
        # （随访是"实际完成"的就诊，重复提交多为重试/误操作；此前无检查会累积
        # 重复行，get_followup_records 时间线出现双份同日记录）。
        # M3（2026-08-16）：幂等键加 **visit_type**——同日不同访视（如上午门诊 +
        # 下午营养门诊）此前被误判重复拒绝；同类型同日才是重试。
        # 八审（2026-08-16，M6 补全）：幂等键再加**内容指纹**——(visit_date,
        # visit_type) 天粒度仍会误伤**同类型同日的两次真实访视**（如两次 dialysis /
        # 两次 nutrition_counsel）→ 强制编辑覆盖 = 真实丢数据。现内容完全一致
        # （重试/误操作）才判 DUPLICATE，内容不同放行（两次真实访视）。
        _fp_new = _record_fingerprint(rec)
        for existing in p.get("records") or []:
            if existing.get("visit_date") == visit_date \
                    and existing.get("visit_type") == visit_type \
                    and _record_fingerprint(existing) == _fp_new:
                return {"ok": False, "error": "DUPLICATE",
                        "detail": f"patient_id={patient_id} 在 {visit_date} "
                                  f"（{visit_type}）已有内容完全一致的随访记录"
                                  f"（{existing.get('record_id')}），拒绝重复追加；"
                                  "如需修正请编辑已有记录"}
        p["records"].append(rec)
        get_repository().save_followup(patient_id, p)
    # BUG-32（2026-08-12）：返回统一 {ok, data} 信封（与其他写操作一致）
    return {"ok": True, "data": {"record": rec}}


# ---------------------------------------------------------------------------
# 4. 依从性评分（透明加权复合分）
# ---------------------------------------------------------------------------
def calc_adherence_score(diet_ratio: float, med_ratio: float, visit_ratio: float,
                         weights: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)
                         ) -> dict[str, Any]:
    """计算依从性复合分（0-100）。

    公式：composite = 100 × (w_diet·diet_ratio + w_med·med_ratio + w_visit·visit_ratio)
    三个域比率（0-1）分别来自：饮食日记完成率（M3/M11）、用药按时率、随访到场率。
    等级：≥80 好 / 50-79 中 / <50 差。

    依据：MMAS-8 是 CKD 依从性测量最常用、经验证工具（Springer Med 系统综述 2024），
    但其为问卷式（0=高、1-2=中、3-8=低）。儿科 CKD 尚无单一经验证的"营养+用药+就诊"复合评分
    （pubmed 24814533 综述：单一工具不充分，应多方法组合）。故本复合分为**系统定义、待临床验证**，
    默认等权（1/3 各），可通过 weights 调整。
    """
    if not (0.0 <= diet_ratio <= 1.0 and 0.0 <= med_ratio <= 1.0 and 0.0 <= visit_ratio <= 1.0):
        return {"ok": False, "error": "INVALID_INPUT", "detail": "各比率须在 0-1 之间"}
    # BUG-65（2026-08-12）：weights 必须恰为 3 元素——此前仅校验和=1，传 (0.5, 0.5) 时
    # 和=1.0 通过校验但下方 weights[2] 抛 IndexError（未捕获崩溃）。
    if len(weights) != 3:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"weights 必须包含 3 个元素（diet/medication/visit 权重），收到 {len(weights)} 个"}
    # H（2026-08-12 三审）：元素级数值+有限性校验——server 层已做 isinstance+isfinite
    # 三层防线，但 core 为纯函数库可被编排层直调（绕过 server），补同口径防御：
    # bool 是 int 子类、NaN/Inf 穿透类型检查后 sum/乘加产生错误结果或写库 JSON 序列化失败。
    # 与同函数其他校验一致返回 INVALID_INPUT 信封（不抛异常，core 契约）。
    # P3-3（2026-08-15）：元素**非负**校验——此前 (-0.5, 1.5, 0) 和=1 通过校验，
    # 复合分可为负或超 100（实测 (0.2,0.8,0.5) 权重 → composite=110 判 good）。
    if not all(isinstance(w, (int, float)) and not isinstance(w, bool) and math.isfinite(w)
               and w >= 0 for w in weights):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "weights 所有元素必须为非负有限数值（int/float，不含 bool/NaN/Inf）"}
    # BUG-43（2026-08-12）：权重必须归一（和=1），否则 composite 可超 100（如全 0.5 → 满分 150）
    if abs(sum(weights) - 1.0) > 1e-6:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"weights 之和必须为 1（归一化权重），收到和={sum(weights):.3f}"}
    composite = 100.0 * (weights[0] * diet_ratio + weights[1] * med_ratio + weights[2] * visit_ratio)
    # P3 其余（2026-08-15）：等级判定与展示值同口径——此前判定用未舍入 composite、
    # 展示用 round(composite,1)，composite=79.96 时展示 80.0 但判 fair，用户看到的
    # "80.0 分 + fair"自相矛盾。统一以 round 后展示值为判定基准。
    composite_show = round(composite, 1)
    level = "good" if composite_show >= 80 else "fair" if composite_show >= 50 else "poor"
    return {
        "ok": True,
        "data": {
            "composite_score": composite_show,
            "level": level,
            "components": {
                "diet": round(diet_ratio * 100, 1),
                "medication": round(med_ratio * 100, 1),
                "visit": round(visit_ratio * 100, 1),
            },
            "weights": {"diet": weights[0], "medication": weights[1], "visit": weights[2]},
            "basis": "系统定义加权复合分（参考 MMAS-8 测量范式；儿科 CKD 复合评分无单一验证量表，待临床验证）",
        },
    }


def get_adherence_score(patient_id: str, diet_ratio: float, med_ratio: float, visit_ratio: float,
                        weights: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)
                        ) -> dict[str, Any]:
    """计算并记录某患者依从性评分（M4 拥有依从性数据，落库快照）。

    身份缺省取部署注入值（A207_CALLER），模型不可自证（P0-1）。
    OD-014：本工具会落库（写操作），入口经 MX-3 收口——WRITE_TOOL_POLICY 登记
    allowed={doctor_assistant}（2026-08-12 清理：注释移除已退役的 nutritionist /
    orchestrator / child_companion 角色名，避免误导）。
    """
    caller = get_caller()
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（a207_policy.validate_patient_id
    # ^P[0-9]{4,}$，与 P1 his 同口径）——畸形 id 不进存储/查询层。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}

    # BUG-65（2026-08-12）：统一走 _guard 中枢（与其他写工具同口径）——此前裸调
    # enforce_write，越权抛 PermissionDenied 依赖 server._invalid 兜底转 FORBIDDEN；
    # _guard 直接返回 FORBIDDEN 信封，行为一致且不依赖调用方异常捕获路径。
    denied = _guard(MCP_NAME, "get_adherence_score", write=True)
    if denied:
        return denied
    res = calc_adherence_score(diet_ratio, med_ratio, visit_ratio, weights)
    if not res["ok"]:
        return res
    snap = {
        "date": _today(),
        "composite_score": res["data"]["composite_score"],
        "level": res["data"]["level"],
        "components": res["data"]["components"],
        "recorded_by": caller,
    }
    with _STORE_LOCK:
        p = get_repository().load_followup(patient_id)
        if p is None:
            p = {"records": [], "plans": [], "adherence": []}
        p.setdefault("records", [])
        p.setdefault("plans", [])
        p.setdefault("adherence", [])
        # M-4（2026-08-16，十一审）：adherence 快照无界（每次评估 append 一条，无上限
        # → 单患者依从性历史线性增长，get_followup_records 全量返回撑上下文）。
        # 保留最近 _FOLLOWUP_RECORD_CAP 条（与随访记录同上限，远超正常评估量）。
        p["adherence"].append(snap)
        if len(p["adherence"]) > _FOLLOWUP_RECORD_CAP:
            p["adherence"] = p["adherence"][-_FOLLOWUP_RECORD_CAP:]
        get_repository().save_followup(patient_id, p)
    res["data"]["patient_id"] = patient_id
    res["data"]["history"] = p["adherence"]
    return res


# ---------------------------------------------------------------------------
# 5. PEW 时间线 facade（ADR-007：存储归属 M3）
# ---------------------------------------------------------------------------
_PEW_ORDER = {"low": 0, "medium": 1, "high": 2}


def _parse_pew_date(value: Any) -> Optional[datetime]:
    """解析 PEW 历史日期为 datetime；无效/缺失返回 None（BUG-67，对齐 content 包）。

    上游 M3 契约=ISO 升序，此处防御性解析——"/"、"." 分隔转 "-" 后 fromisoformat；
    无法解析（"Yesterday"、非零填充 "2023-6-1"）返回 None，由调用方剔除
    （fail-closed：无法可靠定位时间线的数据点不参与趋势计算）。

    P3-1（2026-08-15）：**先试原样 fromisoformat**（.replace 会破坏 ISO 微秒时间戳
    "2024-01-10T08:30:00.123456"→"…00-123456"），仅原样失败才做分隔符替换；
    返回统一 naive（tzinfo=None）——aware/naive 混排时 sort/比较抛 TypeError，
    上游若部分点带时区偏移、部分不带会整次读取崩溃（INTERNAL_ERROR）。
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("/", "-").replace(".", "-"))
        except ValueError:
            return None
    return dt.replace(tzinfo=None)


def get_pew_timeline(patient_id: str, guardian_token: str | None = None,
                     pew_history: list[dict] | None = None) -> dict[str, Any]:
    """PEW 时间线聚合 facade（ADR-007）。

    数据归属 M3（a207-nutrition-assessment-mcp-nfyy）：每次 assess_pew_risk 后由编排层调
    M3.record_pew_risk 落库，M3.get_pew_history 读取。M4 仅作 facade——本工具接受 M3
    返回的 pew_history（list of {date, score, level}）再并入统一随访时间线。零跨包 import。
    P1-2：本工具原名 get_pew_history，与 M3 同名接口易混淆双跳，已更名为 get_pew_timeline。
    BUG-40（2026-08-12）：家长读取必须携带 guardian_token（此前可跨患者读 PEW 趋势）。
    """
    caller = get_caller()
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（a207_policy.validate_patient_id
    # ^P[0-9]{4,}$，与 P1 his 同口径）——畸形 id 不进存储/查询层。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}

    denied = _guard(MCP_NAME, "get_pew_timeline", write=False)
    if denied:
        return denied
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_pew_timeline")
    if denied:
        return denied
    # BUG-66（2026-08-12）：家长/患儿等非临床、非编排角色拒绝外部传入的 pew_history——
    # 该参数是 facade 的权威数据注入通道（编排层从 M3.get_pew_history 拉取后传入聚合），
    # 家长若可自行构造 pew_history（含任意 trend 方向）即可伪造 PEW 恶化/改善趋势误导下游。
    # 潜在 2（2026-08-14）：orchestrator 角色已退役（CALLERS 仅 3 个），`caller !=
    # "orchestrator"` 为死代码——清理。doctor/risk_warning（_CLINICIAN）保留注入能力。
    if pew_history and caller not in _CLINICIAN:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": "家长/患儿视角不接受外部传入的 pew_history（防伪造 PEW 趋势）；"
                          "请通过编排层获取 M3 权威历史。"}
    ph = pew_history or []
    trend = "no_data"
    # BUG-65（2026-08-12）：趋势判定前显式按 date 升序排序 + 过滤非 dict 元素——
    # 上游 M3 get_pew_history 契约=升序，但若传倒序/乱序，ph[0]/ph[-1] 的"最早/最新"
    # 假设失效，趋势会完全反转（worsening↔improving）。非 dict/null 元素一并过滤
    # 避免 p.get() AttributeError（与 content 包 _pew_trend 同口径）。
    # BUG-65 复查：data["points"]/count 统一返回清洗+排序后的 pts（此前返回原始 ph，
    # 展示与趋势判定口径不一致——乱序输入下 points 显示乱序、count 含无效元素）。
    # BUG-67（2026-08-12）：剔除日期无效点 + 按解析后日期排序——纯字符串字典序对
    # "2024-01-10" vs "2024-1-1" 会排错（'-'< '1'，1月1日被排到1月10日之后），
    # 趋势判定完全反向。fail-closed：无法定位时间线的数据点不参与趋势（对齐 content 包 ⓫）。
    # BUG-67 后补（2026-08-12）：level 未知值同样剔除——_PEW_ORDER.get(level, 0) 会把
    # "unknown"/拼写错误静默映射为 0(low)，high→unknown 被误判"改善"掩盖恶化；与日期
    # 无效同理，无法可靠判定严重度的点不参与趋势（fail-closed，宁可不判不可误判）。
    dated = []
    for p in ph:
        if not isinstance(p, dict):
            continue
        dt = _parse_pew_date(p.get("date"))
        if dt is None:
            continue
        if str(p.get("level", "low")).strip().lower() not in _PEW_ORDER:
            continue
        dated.append((dt, p))
    dated.sort(key=lambda x: x[0])  # BUG-67：按解析后日期升序（points/趋势同口径）
    pts = [p for _, p in dated]
    if len(pts) >= 2:
        fo = _PEW_ORDER[str(pts[0].get("level", "low")).strip().lower()]
        lo = _PEW_ORDER[str(pts[-1].get("level", "low")).strip().lower()]
        trend = "worsening" if lo > fo else "improving" if lo < fo else "stable"
    return {
        "ok": True,
        "data": {
            "patient_id": patient_id,
            # M-2（2026-08-16，十一审）：架构语言不进家长上下文——此前 source 硬编码
            # "M3 (a207-nutrition-assessment-mcp-nfyy) — ADR-007"（模块编号+包名），
            # note 含 M3/M4 归属说明，家长/患儿视角暴露内部架构。改中性描述，
            # 归属语义保留在服务端 docstring。
            "source": "PEW 历史（营养评估）",
            "count": len(pts),
            "points": pts,
            "trend": trend,
            "note": "PEW 历史点按日期升序排列；趋势基于首末有效点的严重度对比。",
        },
    }


# ---- M10: notification engine ----
def create_notification(patient_id: str, category: str, priority: str, title: str, body: str, due_at: str | None = None,
                        source_event: str | None = None) -> dict[str, Any]:
    """直接创建一条通知（写，仅编排/临床角色）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    BUG-11 修复：补齐闭环字段 workflow_status / status_updated_by / status_updated_at
    （需求 §5.2：家长视角 get_notifications 需看到 workflow_status 字段）。
    """
    caller = get_caller()
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（a207_policy.validate_patient_id
    # ^P[0-9]{4,}$，与 P1 his 同口径）——畸形 id 不进存储/查询层。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}

    # 双轨制清理：统一走 enforce_write 中枢（MX-3：create_notification 已登记，doctor/risk）
    denied = _guard(MCP_NAME, "create_notification", write=True)
    if denied:
        return denied
    # 六审（2026-08-13）：priority 枚举校验（fail-closed）——LLM/编排层可能传
    # "urgent"/"critical" 等随意值，污染通知列表的优先级语义（过滤/排序/升级逻辑
    # 依赖 low/medium/high 三态）。非法值显式 INVALID_INPUT，不静默落库。
    if priority not in ("low", "medium", "high"):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"priority 必须是 low / medium / high，收到：{priority!r}"}
    # C-B7 修复（2026-08-14）：category 枚举校验（fail-closed，与 priority 对称）——
    # 此前 category 无校验，任意串落库会污染分类语义（家长端按 category 归类展示）。
    # 合法集合与 _EVENT_TEMPLATES 对齐：followup_due / risk_alert / report_ready。
    if category not in ("followup_due", "risk_alert", "report_ready"):
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"category 必须是 followup_due / risk_alert / report_ready，"
                          f"收到：{category!r}"}
    # P1-8 修复（2026-08-13）：due_at 格式校验（fail-closed）——此前 due_at 原样
    # 落库不校验，畸形值（"明天"、随机串）污染通知到期语义且无法排序。
    # 合法：YYYY-MM-DD 日期 或 ISO 8601 datetime（可含时区）。非空即校验。
    if due_at is not None and due_at != "":
        _due_ok = False
        _due_raw = str(due_at)
        # C-S2 修复（2026-08-14）：去掉 [:10] 截断——此前 "2026-08-01garbage"[:10]
        # = "2026-08-01" 通过校验且**原样落库**（fail-closed 被截断绕过，脏串污染
        # 到期排序）。现在完整解析：YYYY-MM-DD 或 ISO 8601 datetime；多余字符一律拒绝。
        try:
            date.fromisoformat(_due_raw)
            _due_ok = True
        except ValueError:
            pass
        if not _due_ok:
            try:
                datetime.fromisoformat(_due_raw)
                _due_ok = True
            except ValueError:
                pass
        if not _due_ok:
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": f"due_at 必须为 YYYY-MM-DD 或 ISO 8601 时间串，收到：{due_at!r}"}
    with _STORE_LOCK:
        # P3 其余（2026-08-15）：同事件去重——同患者同类别同 **source_event** 的未关闭
        # 通知不重复创建（HAIP 定时触发/编排重试会重复入队；此前无检查每次触发都
        # 新增一行，通知列表被重复项淹没，家长收到多条同事件提醒）。
        # **仅事件驱动通知去重**（source_event 非空）：手动创建（无 source_event）
        # 是临床主动行为，不拦（避免临床多次建通知被误拒）。
        if source_event:
            # F-5（2026-08-15）：去重键加 **due_at**——随访重排场景：同类别同 source_event
            # （如 followup_due 事件类型固定映射）但**新的到期日**应视为"新一次提醒"，
            # 旧去重键不含 due_at 会把重排后的新提醒当重复阻断（家长收不到新 followup_due）。
            # 含 due_at 后：同事件同到期日仍去重（防 HAIP 重试刷屏），到期日变化放行。
            dup_key = (category, str(source_event).strip(), due_at or "")
            # H3（2026-08-16，第七轮审查）：confirmed 也应拦——此前
            # not in ("closed", "confirmed") 把已确认通知排除在去重外，
            # 编排/HAIP 对同事件重试会再创建重复通知（架空去重，疑似笔误）。
            # 去重目的：同事件不重复入队；confirmed 表示事件已处理，重试
            # 仍应拒绝（事件未变）。仅 closed 放行（可重新开事件）。
            for existing in get_repository().all_notifications():
                if existing.get("patient_id") == patient_id \
                        and (existing.get("category"), existing.get("source_event") or "",
                             existing.get("due_at") or "") == dup_key \
                        and existing.get("workflow_status") not in ("closed",):
                    return {"ok": False, "error": "DUPLICATE",
                            "detail": f"该事件（{category}/{source_event}/due {due_at or '无'}）"
                                      f"已有未关闭通知 {existing.get('id')}，拒绝重复创建；"
                                      "可对该通知执行 ack/escalate 处理"}
        nid = "N" + uuid.uuid4().hex[:12].upper()
        rec = {
            "id": nid,
            "patient_id": patient_id,
            "category": category,
            "priority": priority,
            "title": title,
            "body": body,
            "created_at": _now_iso(),
            "due_at": due_at,
            "source_event": source_event,
            # 已读状态（ack_notification 置 acked，幂等）
            "status": "unacked",
            # 闭环工作流状态（update_notification_status 严格一步推进，BUG-10/11/12）
            "workflow_status": "unacked",
            # BUG-46：escalated 独立布尔（escalate_notification 置 true），与 workflow_status 正交
            "escalated": False,
            "status_updated_by": None,
            "status_updated_at": None,
        }
        get_repository().save_notification(nid, rec)
    return {"ok": True, "data": {"notification": rec}}


def build_event_notification(event_type: str, patient_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """将末端事件翻译为标准通知并写入（零跨包 import：事件数据由编排层经 payload 传入）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    event_type: followup_due / risk_escalation / report_ready。
    payload 字段依据事件类型：followup_due->next_due_date(+可选 due_at)；
    risk_escalation->from_level,to_level,rule；report_ready->(无需额外字段)。
    """
    caller = get_caller()
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（a207_policy.validate_patient_id
    # ^P[0-9]{4,}$，与 P1 his 同口径）——畸形 id 不进存储/查询层。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}

    # S10 修复（2026-08-13）：工具入口显式鉴权——此前本函数是 server 暴露的工具，
    # 鉴权**隐式依赖**下游 create_notification 的 enforce_write（若下游重构/换实现
    # 即开越权口）。集中式原则：入口自己 enforce，下游再 enforce 属防御纵深不冗余。
    denied = _guard(MCP_NAME, "build_event_notification", write=True)
    if denied:
        return denied
    tpl = _EVENT_TEMPLATES.get(event_type)
    if tpl is None:
        return {"ok": False, "error": "INVALID_EVENT", "detail": f"未知事件类型: {event_type}"}
    try:
        # BUG-65（2026-08-12）：payload 可能含 patient_id 键（编排层常见）——直接
        # .format(patient_id=patient_id, **payload) 会抛 TypeError: got multiple values
        # for keyword argument 'patient_id'。先合并为单一 dict，显式 patient_id 覆盖
        # payload 中的同键（两者同值，覆盖无副作用）。
        fmt_args = dict(payload)
        fmt_args["patient_id"] = patient_id
        body = tpl["body_tmpl"].format(**fmt_args)
    except KeyError as exc:
        return {"ok": False, "error": "INVALID_PAYLOAD",
                "detail": f"事件 {event_type} 缺少字段: {exc}"}
    except (ValueError, TypeError) as exc:
        # N5 修复（2026-08-13）：畸形 payload（值类型与模板占位符不匹配、非法格式
        # 说明符等）此前抛 500（INTERNAL_ERROR）；统一转 INVALID_PAYLOAD 信封。
        return {"ok": False, "error": "INVALID_PAYLOAD",
                "detail": f"事件 {event_type} 模板填充失败（payload 值非法）: {exc}"}
    return create_notification(
        patient_id=patient_id, category=tpl["category"], priority=tpl["priority"],
        title=tpl["title"], body=body,
        due_at=payload.get("due_at"), source_event=_SOURCE_MAP[event_type])


def get_notifications(patient_id: str,
                      status: str = "all",
                      workflow_status: str = "all",
                      escalated: bool | None = None,
                      guardian_token: str | None = None,
                      page: int | None = None,
                      page_size: int = 50) -> dict[str, Any]:
    """读取某患者的通知列表（读，所有角色可读自己患者的通知）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    - status: 已读状态过滤 all/unacked/acked（ack_notification 置 acked）
    - workflow_status: 闭环状态过滤 all/unacked/confirmed/resolved/closed（BUG-25 修复，
      需求 §5.2 家长视角需见 workflow_status 字段；医生可按"未关闭工单"快速过滤）
    - escalated: BUG-46 独立布尔过滤（True=仅已升级 / False=仅未升级 / None=不过滤）
    - page/page_size（P2 修复 2026-08-13）：分页——此前全量返回，通知多时灌爆 LLM
      上下文。page 缺省 None=不分页（保持兼容）；page_size 默认 50、上限 200 钳制。
    BUG-37（2026-08-12）：status / workflow_status 参数校验合法值，非法值返回 INVALID_INPUT
    （此前静默返回空列表，typo 难排查）。
    BUG-40（2026-08-12）：家长读取必须携带 guardian_token（此前可跨患者读通知列表）。
    返回条目含 workflow_status / escalated / status_updated_by / status_updated_at 闭环字段。
    """
    caller = get_caller()
    # N1 修复（2026-08-13）：统一 patient_id 契约校验（a207_policy.validate_patient_id
    # ^P[0-9]{4,}$，与 P1 his 同口径）——畸形 id 不进存储/查询层。
    try:
        patient_id = validate_patient_id(patient_id)
    except ValueError as exc:
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}

    denied = _guard(MCP_NAME, "get_notifications", write=False)
    if denied:
        return denied
    denied = _guard_guardian(caller, patient_id, guardian_token, "get_notifications")
    if denied:
        return denied
    # BUG-65（2026-08-12）：MCP 客户端显式传 JSON null（Python None）时，Optional 默认值
    # "all" 不生效——None 会落入下方合法性校验直接 INVALID_INPUT。入口统一规范化，
    # None/空串等价于不过滤（all），与缺省行为一致。
    status = status or "all"
    workflow_status = workflow_status or "all"
    _VALID_STATUS = {"all", "unacked", "acked"}
    _VALID_WORKFLOW = {"all", "unacked", "confirmed", "resolved", "closed"}
    if status not in _VALID_STATUS:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"status 必须是 {sorted(_VALID_STATUS)} 之一，收到：{status!r}"}
    if workflow_status not in _VALID_WORKFLOW:
        return {"ok": False, "error": "INVALID_INPUT",
                "detail": f"workflow_status 必须是 {sorted(_VALID_WORKFLOW)} 之一，收到：{workflow_status!r}"}
    items = [
        r for r in get_repository().all_notifications()
        # B（2026-08-12 三审）：r["patient_id"]/r["status"] 硬索引改 .get() 统一——
        # 与下方 workflow_status/escalated 的 .get 风格一致，旧版本/手工写入缺键
        # 记录不再 KeyError（此前混入缺键记录时整次读取崩溃）。
        if r.get("patient_id") == patient_id
        and (status == "all" or r.get("status") == status)
        and (workflow_status == "all" or r.get("workflow_status", "unacked") == workflow_status)
        and (escalated is None or bool(r.get("escalated", False)) == escalated)
    ]
    # BUG-65（2026-08-12）：safe get 排序键——硬取 r["created_at"] 时，旧版本数据（早期
    # 版本未写入该字段）会抛 KeyError。缺省 "" 经 reverse=True 降序后排**末尾**
    # （最旧位置，列表头部为最新），避免崩溃；注释修正：空串实为"最旧排末尾"而非"排最前"。
    items.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    # D1 修复（2026-08-14）：家长视角裁剪医生内部字段——此前 items 直接返回仓库原始记录，
    # resolution_note（处置备注）/escalation_reason（升级理由）/status_updated_by（医生身份）
    # 对家长可见，与 get_followup_records 的 _visible_record 裁剪同包两套口径；且这三键
    # 不在 CLINICIAN_ONLY_FIELDS（matrix.py），无递归兜底。非临床角色统一剥离。
    if caller not in _CLINICIAN:
        items = [_mask_notification(r, caller) for r in items]
    # P2 修复（2026-08-13）：分页（page=None 保持全量兼容；page_size 上限 200 钳制）
    total = len(items)
    if page is not None:
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": "page 必须为 ≥1 的整数"}
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": "page_size 必须为 ≥1 的整数"}
        page_size = min(page_size, 200)
        start = (page - 1) * page_size
        page_items = items[start:start + page_size]
        return {"ok": True, "data": {
            "patient_id": patient_id, "count": len(page_items),
            "total": total, "page": page, "page_size": page_size,
            "has_more": start + page_size < total, "notifications": page_items}}
    return {"ok": True, "data": {
        "patient_id": patient_id, "count": total, "notifications": items}}


def ack_notification(notification_id: str, guardian_token: str | None = None) -> dict[str, Any]:
    """确认通知已读（幂等）。仅置 status=acked，**不改变** workflow_status（BUG-12）。

    需求 §5.2：已读确认与闭环工作流状态分离——ack 是"家长/医生已读"，闭环流转由
    update_notification_status 推进（unacked→confirmed→resolved→closed）。

    BUG-28 说明（2026-08-12）：ack 走**读权闸门（write=False）是有意的设计意图**——
    所有拥有 P3 读权的角色（含家长）都可标记自己患者的通知已读；ack 不产生新的
    业务状态、幂等、无 MX-3 收口需求，故仍按读操作闸门放行。
    A1-1（2026-08-16，十审）：WRITE_TOOL_POLICY 的 ack_notification 登记已删除——
    该登记是装饰性的（本函数从不走 enforce_write），会让人误以为受 MX-3 写权收口；
    真实强制 = 读闸门 + 家长 guardian_token 绑定（下方 :1029）。
    BUG-40（2026-08-12）：家长 ack 必须携带 guardian_token 且与该通知所属患者绑定
    （此前家长传任意 notification_id 可标记任意患者通知已读）。
    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    denied = _guard(MCP_NAME, "ack_notification", write=False)
    if denied:
        return denied
    with _STORE_LOCK:
        # C-B7 修复（2026-08-14）：notification_id 显式 strip + None/空拒绝——
        # 此前未 strip（" abc " 查不到归 NOT_FOUND，语义误导）；None 穿透可能让
        # Tablestore 端用非法主键构造请求（JSON 端 store.get(None) 返回 None 掩盖）。
        if notification_id is None or not str(notification_id).strip():
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": "notification_id 不能为空"}
        notification_id = str(notification_id).strip()
        rec = get_repository().load_notification(notification_id)
        if rec is None:
            return {"ok": False, "error": "NOT_FOUND", "detail": f"通知 {notification_id} 不存在"}
        # 家长需校验与其患儿的绑定关系（先取通知所属 patient_id 再核验）
        # P2 修复（2026-08-13）：rec["patient_id"] 硬索引改 .get()——旧版本/手工写入
        # 缺该键的记录此前直接 KeyError（server _invalid 归 INTERNAL_ERROR，掩盖脏数据）。
        _pid = rec.get("patient_id")
        if _pid is None:
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": f"通知 {notification_id} 缺少 patient_id（脏数据），拒绝 ack"}
        denied = _guard_guardian(caller, _pid, guardian_token, "ack_notification")
        if denied:
            return denied
        rec["status"] = "acked"
        # C-S1 修复（2026-08-14）：只传变更字段子集（防并发标量 lost update）
        get_repository().save_notification(notification_id, {"status": "acked"})
    # D1 修复（2026-08-14）：家长 ack 返回同源裁剪（与 get_notifications 同口径）——
    # 家长可 ack（读权闸门设计），返回的 rec 若含 resolution_note/escalation_reason/
    # status_updated_by/escalated_by 即泄露医生内部字段。
    return {"ok": True, "data": {"notification": _mask_notification(rec, caller)}}

# ================================================================
# 闭环状态机: update_notification_status (v2.3 / 2026-08-12 重构)
# ================================================================

# 需求 §5.1：unacked → confirmed → resolved → closed（严格一步流转，禁止跳级）。
# BUG-46（2026-08-12）：escalated 从 workflow_status 的"第六态"改为**独立布尔字段**
# （需求 §5.2：escalated 与 workflow_status 正交——通知可在 unacked/confirmed 状态下被
# HAIP 或临床升级，升级后 workflow_status 仍保留原值、escalated=true；此前 escalated 是
# 一个状态值，升级后丢失"升级前是否已确认"信息）。升级动作由 escalate_notification 设置。
_WORKFLOW_ORDER = ["unacked", "confirmed", "resolved", "closed"]
_WORKFLOW_ALLOWED = frozenset(_WORKFLOW_ORDER)
_WORKFLOW_TRANSITIONS: dict[str, frozenset[str]] = {
    "unacked": frozenset({"confirmed"}),   # confirmed=医生确认
    "confirmed": frozenset({"resolved"}),
    "resolved": frozenset({"closed"}),
    "closed": frozenset(),                 # 终态
}


def update_notification_status(notification_id: str, new_status: str,
                                resolution_note: str = "") -> dict[str, Any]:
    """推移风险闭环状态机。仅 CKD 临床助手（MX-3 收口，BUG-04 修复）。

    需求 §5.1/§5.2：
    - workflow_status 严格一步流转：unacked → confirmed → resolved → closed（禁止跳级）。
    - BUG-46：escalated 是**独立布尔字段**（escalate_notification 设置），不在本状态机内——
      升级与 workflow_status 正交，通知可在 confirmed 下被升级，升级后仍为 confirmed。
    - resolved 必须携带 resolution_note（BUG-09 修复，缺则返回 INVALID_INPUT）。
    - 与 ack_notification 解耦：ack 只置已读 status，本工具只推进 workflow_status（BUG-12）。
    """
    caller = get_caller()
    # BUG-04 修复：update_notification_status 已登记 WRITE_TOOL_POLICY（allowed={doctor}），
    # enforce_write 走工具白名单 + 矩阵回查，risk_warning 管线身份不再可推移人工闭环。
    denied = _guard(MCP_NAME, "update_notification_status", write=True)
    if denied:
        return denied
    if new_status not in _WORKFLOW_ALLOWED:
        return {"ok": False, "error": "INVALID_STATUS",
                "detail": f"status 必须是 {sorted(_WORKFLOW_ALLOWED)} 之一"}
    # 潜在 3（2026-08-14）：notification_id None 显式拒绝——此前 None.strip()
    # 抛 AttributeError 被 server _invalid 归 INTERNAL_ERROR（500 类），误导排障。
    if not notification_id or not str(notification_id).strip():
        return {"ok": False, "error": "INVALID_INPUT", "detail": "notification_id 不能为空"}
    with _STORE_LOCK:
        nid = str(notification_id).strip()
        rec = get_repository().load_notification(nid)
        if rec is None:
            return {"ok": False, "error": "NOT_FOUND", "detail": f"通知 {nid} 不存在"}
        current = rec.get("workflow_status", "unacked")
        if new_status == current:
            # BUG-65（2026-08-12）：幂等返回前，允许"已 resolved 且携带新备注"时更新备注——
            # 此前 new_status==current 直接返回，医生补充/修正 resolution_note 会被静默忽略。
            note = (resolution_note or "").strip()
            if current == "resolved" and note and rec.get("resolution_note") != note:
                rec["resolution_note"] = note
                rec["status_updated_by"] = caller
                rec["status_updated_at"] = _now_iso()
                # C-S1：变更字段子集（防并发标量 lost update）
                get_repository().save_notification(nid, {
                    "resolution_note": note, "status_updated_by": caller,
                    "status_updated_at": rec["status_updated_at"]})
            return {"ok": True, "data": {"notification": rec}}  # 幂等
        allowed_next = _WORKFLOW_TRANSITIONS.get(current, frozenset())
        if new_status not in allowed_next:
            return {"ok": False, "error": "INVALID_TRANSITION",
                    "detail": f"workflow_status 不允许从 {current} 直接转到 {new_status}"
                              f"（严格一步流转，需求 §5.1）"}
        if new_status == "resolved" and not (resolution_note or "").strip():
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": "resolved 必须携带 resolution_note（需求 §5.2）"}
        rec["workflow_status"] = new_status
        rec["status_updated_by"] = caller
        # BUG-65（2026-08-12）：统一 _now_iso()（秒级）——此前裸调 datetime.now().isoformat()
        # 带微秒，与 create_notification 的 _now_iso() 格式不一致，字典序排序/字符串比较会错位。
        rec["status_updated_at"] = _now_iso()
        # C-S1 修复（2026-08-14）：只传变更字段子集（防并发标量 lost update）
        patch = {"workflow_status": new_status, "status_updated_by": caller,
                 "status_updated_at": rec["status_updated_at"]}
        if new_status == "resolved":
            rec["resolution_note"] = resolution_note.strip()
            patch["resolution_note"] = resolution_note.strip()
        get_repository().save_notification(nid, patch)
    return {"ok": True, "data": {"notification": rec}}


def escalate_notification(notification_id: str, reason: str = "") -> dict[str, Any]:
    """标记通知升级（HAIP 24h 未确认自动升级 / 临床主动升级）。仅 CKD 临床助手。

    BUG-46（2026-08-12）：escalated 是**独立布尔字段**，与 workflow_status 正交——
    通知可在 unacked 或 confirmed 状态下被升级，升级后 workflow_status 保持原值、
    escalated=true（此前 escalated 是 workflow_status 的一个状态值，升级会丢失
    "升级前是否已确认"信息）。已关闭（closed）的工单不可再升级。
    """
    caller = get_caller()
    denied = _guard(MCP_NAME, "escalate_notification", write=True)
    if denied:
        return denied
    # 潜在 3（2026-08-14）：None 显式拒绝（对齐 update_notification_status）
    if not notification_id or not str(notification_id).strip():
        return {"ok": False, "error": "INVALID_INPUT", "detail": "notification_id 不能为空"}
    with _STORE_LOCK:
        nid = str(notification_id).strip()
        rec = get_repository().load_notification(nid)
        if rec is None:
            return {"ok": False, "error": "NOT_FOUND", "detail": f"通知 {nid} 不存在"}
        if rec.get("workflow_status") == "closed":
            return {"ok": False, "error": "INVALID_INPUT",
                    "detail": "已关闭工单不可再升级"}
        rec["escalated"] = True
        rec["escalated_by"] = caller
        # BUG-65（2026-08-12）：统一 _now_iso()（秒级），与 create_notification 等一致
        rec["escalated_at"] = _now_iso()
        # P3 其余（2026-08-15）：升级审计追加——此前重复 escalate 覆盖 escalated_at/
        # escalation_reason（最后一次升级淹没历史），审计丢失。现追加 escalated_history
        # 列表（元素带唯一 id，存储层按 id 去重合并，并发安全），保留每次升级轨迹。
        # 注意：存储读回该列是 **JSON 字符串**（序列化契约），需先解析为 list 再追加。
        entry = {"id": uuid.uuid4().hex[:8], "by": caller, "at": rec["escalated_at"]}
        if reason.strip():
            entry["reason"] = reason.strip()
        raw_hist = rec.get("escalated_history")
        if isinstance(raw_hist, str):
            try:
                history = json.loads(raw_hist)
            except (json.JSONDecodeError, TypeError):
                history = []
        elif isinstance(raw_hist, list):
            history = list(raw_hist)
        else:
            history = []
        history.append(entry)
        rec["escalated_history"] = history
        # C-S1 修复（2026-08-14）：只传**变更字段子集**——此前传完整 rec 快照
        # （load→改→save），并发/乐观锁重试时标量 new 覆盖会回写旧 escalated 值
        # 覆盖并发写者（S5 只救了列表字段）。部分更新后 _merge_row/JSON 合并只
        # 覆盖本次字段，并发者的 escalated 等保留。
        patch = {"escalated": True, "escalated_by": caller,
                 "escalated_at": rec["escalated_at"],
                 "escalated_history": json.dumps(history, ensure_ascii=False)}
        if reason.strip():
            rec["escalation_reason"] = reason.strip()
            patch["escalation_reason"] = reason.strip()
        get_repository().save_notification(nid, patch)
    return {"ok": True, "data": {"notification": rec}}