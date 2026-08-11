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
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ._policy import (
    FOLLOWUP_CLINICIAN,
    FOLLOWUP_WRITE_ALLOWED,
    NOTIFY_READ_ROLES,
    NOTIFY_WRITE_ROLES,
    atomic_write_json,
    enforce_read,
    enforce_write,
    get_caller,
    resolve_state_path,
)

STORE_FILENAME = "followup_store.json"
DATA_DIR_ENV = "A207_FOLLOWUP_DATA_DIR"
# 本包在权限矩阵中的登记名（enforce_* 查表键，唯一事实源在 a207_policy.matrix）
MCP_NAME = "CKDNutri-care-mcp"

# 放行集合唯一事实源在 a207_policy.matrix（P1-1）；此处仅保留本地别名，下文逻辑零改动。
# 写权限（schedule_followup 收口）：仅医生/营养师/编排层
_WRITE_ALLOWED = FOLLOWUP_WRITE_ALLOWED
# 临床角色可见原始医生备注；患者/家属角色仅可见摘要
_CLINICIAN = FOLLOWUP_CLINICIAN

# ---------------------------------------------------------------------------
# M10 通知引擎支持块（与随访存储隔离，避免 key 命名空间污染）
# ---------------------------------------------------------------------------
# 通知写权限：仅临床助手/风险管线（NOTIFY_WRITE_ROLES = {doctor, risk}）
_WRITE_ROLES = NOTIFY_WRITE_ROLES
# 通知读权限：临床/家庭/风险均可读自己患者的通知
_READ_ROLES = NOTIFY_READ_ROLES

_NOTIFY_STORE_FILENAME = "notification_store.json"
_NOTIFY_DATA_DIR_ENV = "A207_NOTIFICATION_DATA_DIR"


def _notify_store_path() -> Path:
    """通知写库路径：A207_NOTIFICATION_DATA_DIR override，否则落到 A207_DATA_DIR。"""
    override = os.environ.get(_NOTIFY_DATA_DIR_ENV)
    if override:
        return Path(override) / _NOTIFY_STORE_FILENAME
    return resolve_state_path(_NOTIFY_STORE_FILENAME)


def _notify_load() -> dict[str, Any]:
    try:
        with open(_notify_store_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _notify_save(store: dict[str, Any]) -> None:
    # OD-014：原子写，避免半写截断静默丢数据
    atomic_write_json(_notify_store_path(), store)


def _forbidden(role: str, action: str) -> dict[str, Any]:
    return {"ok": False, "error": "FORBIDDEN",
            "detail": f"caller={role} 无权执行 {action}（P3 权限矩阵）"}


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
# 基础间隔（天）：儿科共识 Rec17 —— G1-2 每 1-2 次/年(180d)，G3-4 每 ≥3-4 次/年(90d)，
# G5 每 >4 次/年(60d)，G5D 每月(30d，PRNT 2025)
_BASE_INTERVAL_DAYS = {
    "G1": 180, "G2": 180, "G3a": 90, "G3b": 90, "G4": 90, "G5": 60, "G5D": 30,
}
# 白蛋白尿升级：A2 缩短 30 天，A3 缩短 60 天（下限 14 天）
_ALBUMINURIA_REDUCTION = {"A1": 0, "A2": 30, "A3": 60}
_MIN_INTERVAL = 14


# ---------------------------------------------------------------------------
# 存储助手
# ---------------------------------------------------------------------------
def store_path() -> Path:
    """可写写库路径（P1-3 修复：不再写只读安装目录）。

    - 若设 A207_FOLLOWUP_DATA_DIR（开发/测试态），落到该目录；
    - 否则经 a207_policy.resolve_state_path 落到 A207_DATA_DIR 或系统临时目录。
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override) / STORE_FILENAME
    return resolve_state_path(STORE_FILENAME)


def _load_store() -> dict:
    try:
        with open(store_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_store(store: dict) -> None:
    # OD-014（P2-3）：原子写，避免半写截断静默丢数据
    atomic_write_json(store_path(), store)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _add_days(iso_date: str, days: int) -> str:
    return (date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()


def _short_id(prefix: str, patient_id: str) -> str:
    return f"{prefix}-{patient_id}-{uuid.uuid4().hex[:8]}"


def _visible_record(rec: dict, caller: str) -> dict:
    """患者/家属角色剔除原始医生备注（doctor_notes），仅留可见摘要。"""
    out = dict(rec)
    if caller not in _CLINICIAN:
        out.pop("doctor_notes", None)
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
    base = _BASE_INTERVAL_DAYS[ckd_stage]
    red = _ALBUMINURIA_REDUCTION.get(albuminuria_stage, 0)
    interval = max(_MIN_INTERVAL, base - red)
    return {
        "ok": True,
        "ckd_stage": ckd_stage,
        "albuminuria_stage": albuminuria_stage,
        "recommended_interval_days": interval,
        "basis": "KDIGO 2024 儿科随访频率（NICE NG203 Table 2 + 儿科共识 Rec17 + PRNT 2025）",
        "citation": _CITATION,
    }


# ---------------------------------------------------------------------------
# 2. 随访计划（写，医生/营养师）
# ---------------------------------------------------------------------------
def schedule_followup(patient_id: str, ckd_stage: str, albuminuria_stage: str,
                      visit_type: str, anchor_date: str,
                      plan_summary: str = "", note_to_clinician: str = "") -> dict[str, Any]:
    """创建随访计划（写，MX 收口：仅医生/营养师/编排层）。频率来自 KDIGO 2024 推荐。

    :param visit_type: outpatient/phone/online/dialysis/nutrition_counsel
    :param anchor_date: 计划基准日期 YYYY-MM-DD（通常为本次就诊日）
    :param caller: 缺省取部署注入身份（A207_CALLER），模型不可自证（P0-1）
    :param plan_summary: 随访计划摘要（所有角色可见）
    :param note_to_clinician: 仅供医生/营养的备注（患者/家属不可见）
    """
    caller = get_caller()
    if caller not in _WRITE_ALLOWED:
        return {"ok": False, "error": "FORBIDDEN",
                "detail": "随访计划仅医生/营养师/编排层可创建（MX 写权收口）"}
    rec = recommend_followup_interval(ckd_stage, albuminuria_stage)
    if not rec["ok"]:
        return rec
    interval = rec["recommended_interval_days"]
    plan = {
        "plan_id": _short_id("FP", patient_id),
        "cadence": {
            "interval_days": interval,
            "anchor_date": anchor_date,
            "next_due_date": _add_days(anchor_date, interval),
            "basis": rec["basis"],
            "citation": rec["citation"],
        },
        "visit_type": visit_type,
        "status": "active",
        "plan_summary": plan_summary,
        "note_to_clinician": note_to_clinician,
        "created_by": caller,
        "created_at": _now_iso(),
    }
    store = _load_store()
    p = store.setdefault(patient_id, {"records": [], "plans": [], "adherence": []})
    p["plans"].append(plan)
    _save_store(store)
    # 写操作由临床角色发起，返回完整计划
    return {"ok": True, "plan": plan}


# ---------------------------------------------------------------------------
# 3. 随访记录时间线（读）
# ---------------------------------------------------------------------------
def get_followup_records(patient_id: str) -> dict[str, Any]:
    """读取某患者随访记录与计划（读，所有角色可读）。

    权限：临床角色（医生/营养/编排/风险）看到完整记录（含医生备注）；
    患者/家属角色（parent_assistant/child_companion）仅见摘要——原始医生备注被剔除。
    身份缺省取部署注入值（A207_CALLER），模型不可自证（P0-1）。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    store = _load_store()
    p = store.get(patient_id)
    if not p:
        return {"ok": True, "patient_id": patient_id, "records": [], "plans": [],
                "message": "无随访数据", "visibility": "summary_only"}
    records = [_visible_record(r, caller) for r in p["records"]]
    plans = [_visible_plan(pl, caller) for pl in p["plans"]]
    return {
        "ok": True,
        "patient_id": patient_id,
        "records": records,
        "plans": plans,
        "visibility": "full" if caller in _CLINICIAN else "summary_only",
    }


def add_followup_record(patient_id: str, visit_date: str, visit_type: str, ckd_stage: str,
                        indicators_snapshot: dict, plan_summary: str, doctor_notes: str = "") -> dict[str, Any]:
    """追加一条随访记录（内部助手，供编排层/医生写入；不在 MCP 工具直接暴露，由 router 调 server 封装）。"""
    caller = get_caller()
    # OD-014：写路径统一走矩阵收口（与 schedule_followup 一致：仅医生/营养师/编排层）
    if caller not in _WRITE_ALLOWED:
        return {"ok": False, "error": "FORBIDDEN",
                "detail": "随访记录仅医生/营养师/编排层可写入（MX 写权收口）"}
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
    store = _load_store()
    p = store.setdefault(patient_id, {"records": [], "plans": [], "adherence": []})
    p["records"].append(rec)
    _save_store(store)
    return {"ok": True, "record": rec}


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
    composite = 100.0 * (weights[0] * diet_ratio + weights[1] * med_ratio + weights[2] * visit_ratio)
    level = "good" if composite >= 80 else "fair" if composite >= 50 else "poor"
    return {
        "ok": True,
        "composite_score": round(composite, 1),
        "level": level,
        "components": {
            "diet": round(diet_ratio * 100, 1),
            "medication": round(med_ratio * 100, 1),
            "visit": round(visit_ratio * 100, 1),
        },
        "weights": {"diet": weights[0], "medication": weights[1], "visit": weights[2]},
        "basis": "系统定义加权复合分（参考 MMAS-8 测量范式；儿科 CKD 复合评分无单一验证量表，待临床验证）",
    }


def get_adherence_score(patient_id: str, diet_ratio: float, med_ratio: float, visit_ratio: float,
                        weights: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)
                        ) -> dict[str, Any]:
    """计算并记录某患者依从性评分（M4 拥有依从性数据，落库快照）。

    身份缺省取部署注入值（A207_CALLER），模型不可自证（P0-1）。
    OD-014：本工具会落库（写操作），入口经 MX-3 收口——仅 doctor_assistant /
    nutritionist / orchestrator 可写；parent_assistant / child_companion 矩阵 M4=RL 只读，
    越权抛 PermissionDenied（此前漏校验，实测可越权写入依从性历史，污染后续报告）。
    """
    caller = get_caller()
    enforce_write(MCP_NAME, tool="get_adherence_score")
    res = calc_adherence_score(diet_ratio, med_ratio, visit_ratio, weights)
    if not res["ok"]:
        return res
    snap = {
        "date": _today(),
        "composite_score": res["composite_score"],
        "level": res["level"],
        "components": res["components"],
        "recorded_by": caller,
    }
    store = _load_store()
    p = store.setdefault(patient_id, {"records": [], "plans": [], "adherence": []})
    p["adherence"].append(snap)
    _save_store(store)
    res["patient_id"] = patient_id
    res["history"] = p["adherence"]
    return res


# ---------------------------------------------------------------------------
# 5. PEW 时间线 facade（ADR-007：存储归属 M3）
# ---------------------------------------------------------------------------
_PEW_ORDER = {"low": 0, "medium": 1, "high": 2}


def get_pew_timeline(patient_id: str, pew_history: list[dict] | None = None) -> dict[str, Any]:
    """PEW 时间线聚合 facade（ADR-007）。

    数据归属 M3（a207-nutrition-assessment-mcp-nfyy）：每次 assess_pew_risk 后由编排层调
    M3.record_pew_risk 落库，M3.get_pew_history 读取。M4 仅作 facade——本工具接受 M3
    返回的 pew_history（list of {date, score, level}）再并入统一随访时间线。零跨包 import。
    P1-2：本工具原名 get_pew_history，与 M3 同名接口易混淆双跳，已更名为 get_pew_timeline。
    """
    caller = get_caller()
    enforce_read(MCP_NAME)
    ph = pew_history or []
    trend = "no_data"
    if len(ph) >= 2:
        fo = _PEW_ORDER.get(ph[0].get("level", "low"), 0)
        lo = _PEW_ORDER.get(ph[-1].get("level", "low"), 0)
        trend = "worsening" if lo > fo else "improving" if lo < fo else "stable"
    return {
        "ok": True,
        "patient_id": patient_id,
        "source": "M3 (a207-nutrition-assessment-mcp-nfyy) — ADR-007 PEW 历史归属 M3",
        "count": len(ph),
        "points": ph,
        "trend": trend,
        "note": "M4 仅作 facade 聚合，PEW 历史存储由 M3 拥有；此工具接受 M3 get_pew_history 的输出再并入统一随访时间线。",
    }


# ---- M10: notification engine ----
def create_notification(patient_id: str, category: str, priority: str, title: str, body: str, due_at: str | None = None,
                        source_event: str | None = None) -> dict[str, Any]:
    """直接创建一条通知（写，仅编排/临床角色）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    if caller not in _WRITE_ROLES:
        return _forbidden(caller, "create_notification")
    store = _notify_load()
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
        "status": "unacked",
    }
    store[nid] = rec
    _notify_save(store)
    return {"ok": True, "notification": rec}


def build_event_notification(event_type: str, patient_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """将末端事件翻译为标准通知并写入（零跨包 import：事件数据由编排层经 payload 传入）。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    event_type: followup_due / risk_escalation / report_ready。
    payload 字段依据事件类型：followup_due->next_due_date(+可选 due_at)；
    risk_escalation->from_level,to_level,rule；report_ready->(无需额外字段)。
    """
    caller = get_caller()
    tpl = _EVENT_TEMPLATES.get(event_type)
    if tpl is None:
        return {"ok": False, "error": "INVALID_EVENT", "detail": f"未知事件类型: {event_type}"}
    try:
        body = tpl["body_tmpl"].format(patient_id=patient_id, **payload)
    except KeyError as exc:
        return {"ok": False, "error": "INVALID_PAYLOAD",
                "detail": f"事件 {event_type} 缺少字段: {exc}"}
    return create_notification(
        patient_id=patient_id, category=tpl["category"], priority=tpl["priority"],
        title=tpl["title"], body=body,
        due_at=payload.get("due_at"), source_event=_SOURCE_MAP[event_type])


def get_notifications(patient_id: str,
                      status: str = "all") -> dict[str, Any]:
    """读取某患者的通知列表（读，所有角色可读自己患者的通知）。status=all/unacked/acked。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    if caller not in _READ_ROLES:
        return _forbidden(caller, "get_notifications")
    store = _notify_load()
    items = [r for r in store.values()
             if r["patient_id"] == patient_id and (status == "all" or r["status"] == status)]
    items.sort(key=lambda r: r["created_at"], reverse=True)
    return {"ok": True, "patient_id": patient_id, "count": len(items), "notifications": items}


def ack_notification(notification_id: str) -> dict[str, Any]:
    """确认/已读一条通知（读角色均可）。幂等。

    身份来自部署注入的环境变量 A207_CALLER（P0-1：模型不可自证身份）。
    """
    caller = get_caller()
    if caller not in _READ_ROLES:
        return _forbidden(caller, "ack_notification")
    store = _notify_load()
    rec = store.get(notification_id)
    if rec is None:
        return {"ok": False, "error": "NOT_FOUND", "detail": f"通知 {notification_id} 不存在"}
    rec["status"] = "acked"
    _notify_save(store)
    return {"ok": True, "notification": rec}

# ================================================================
# 闭环状态机: update_notification_status (v2.3)
# ================================================================

def update_notification_status(notification_id: str, new_status: str,
                                resolution_note: str = "") -> dict[str, Any]:
    """推移风险闭环状态机。仅 CKD 临床助手。

    状态正向流转（与 ack/通知共用同一 status 字段）：
    unacked → acked → confirmed → resolved → closed。
    acked 由 ack_notification 设置；confirm/resolve/close 由本工具推进。
    """
    caller = get_caller()
    enforce_write('CKDNutri-care-mcp', tool='update_notification_status')
    ALLOWED = {"unacked", "acked", "confirmed", "resolved", "closed"}
    if new_status not in ALLOWED:
        return {"ok": False, "error": "INVALID_STATUS", "detail": f"status must be one of {ALLOWED}"}
    store = _notify_load()
    nid = notification_id.strip()
    rec = store.get(nid)
    if rec is None:
        return {"ok": False, "error": "NOT_FOUND", "detail": f"通知 {nid} 不存在"}
    current = rec.get("status", "unacked")
    ORDER = ["unacked", "acked", "confirmed", "resolved", "closed"]
    if new_status != current and ORDER.index(new_status) <= ORDER.index(current):
        return {"ok": False, "error": "INVALID_TRANSITION",
                "detail": f"Cannot go from {current} to {new_status}"}
    rec["status"] = new_status
    rec["status_updated_by"] = caller
    rec["status_updated_at"] = datetime.now().isoformat()
    if new_status == "resolved" and resolution_note:
        rec["resolution_note"] = resolution_note
    _notify_save(store)
    return {"ok": True, "notification": rec}
