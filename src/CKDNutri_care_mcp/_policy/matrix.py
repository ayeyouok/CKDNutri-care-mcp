"""权限矩阵 / 写权策略 / 各包放行集合 —— A207 唯一事实源（Plan A 收口位置）。

v2.3 阶段 0（2026-08-11）：角色从 6 瘦身为 2 + 1 管线身份。
  - Agent 身份（2）：doctor_assistant（CKD 临床助手）、parent_assistant（CKD 家庭助手）
  - 管线身份（1）：risk_warning（仅用于 notify 写权与自动化管线，非 Agent）
  - 退役：orchestrator / nutritionist / child_companion（3 角色）
  - 退役包：M11（a207-gamification-mcp）→ 打卡并入 M3 upsert_food_diary
  - 网关退出 MCP 统计：M13（a207-router-mcp）→ P0 危急词回归 HTTP 中间件（原型暂保留 MCP 形态）
"""

from __future__ import annotations

# ---------------------------------------------------------------- 角色与 MCP

CALLERS: tuple[str, ...] = (
    "doctor_assistant",
    "parent_assistant",
    "risk_warning",             # 管线身份（非 Agent，仅 WRITE_TOOL_POLICY notify_* / 风险管线）
)

MCP_REGISTRY: dict[str, str] = {
    "CKDNutri-clinical-data-mcp": "P1",
    "CKDNutri-nutrition-mcp": "P2",
    "CKDNutri-care-mcp": "P3",
    "CKDNutri-assessment-mcp": "P4",
    "CKDNutri-content-mcp": "P5",
}

CLINICAL_CALC_MCP = "CKDNutri-assessment-mcp"

# 包名别名归一（OD-002）：登记表沿用早期命名，实际交付目录/PyPI 包名已变更。
# 任何曾经出现过的 a207-* 废弃包名都在此全量归一，避免上游（旧版路由/编排/调用方）
# 传入旧名时 normalize_mcp 原样返回 → 上层校验 PERMISSION_MATRIX 误报「mcp 未登记」。
# 注意：a207-router-mcp / a207-gateway-mcp 是网关/中间件、不是数据域 MCP，
#       故意不在此表 → 它们正确落回「未登记」。
MCP_ALIASES: dict[str, str] = {
    # --- P1 临床数据（HIS + LIS 合并）---
    "a207-clinical-data-mcp": "CKDNutri-clinical-data-mcp",
    "a207-his-mcp": "CKDNutri-clinical-data-mcp",
    "a207-lis-mcp": "CKDNutri-clinical-data-mcp",
    # --- P2 营养域（营养评估 + 食物养分 + 食谱 + 游戏化并入）---
    "a207-nutrition-mcp": "CKDNutri-nutrition-mcp",
    "a207-nutrition-assessment-mcp": "CKDNutri-nutrition-mcp",
    "a207-nutrition-assessment-mcp-nfyy": "CKDNutri-nutrition-mcp",
    "a207-nutrition-calc-mcp": "CKDNutri-nutrition-mcp",
    "a207-meal-plan-mcp": "CKDNutri-nutrition-mcp",
    "a207-gamification-mcp": "CKDNutri-nutrition-mcp",   # M11 打卡并入 M3 upsert_food_diary
    # --- P3 随访沟通（随访 + 通知合并）---
    "a207-care-mcp": "CKDNutri-care-mcp",
    "a207-followup-mcp": "CKDNutri-care-mcp",
    "a207-notification-mcp": "CKDNutri-care-mcp",
    "a207-notify-mcp": "CKDNutri-care-mcp",
    # --- P4 决策计算（临床计算 + 风险引擎合并）---
    "a207-decision-mcp": "CKDNutri-assessment-mcp",
    "a207-clinical-calc-mcp": "CKDNutri-assessment-mcp",
    "a207-ckd-clinical-calc-mcp": "CKDNutri-assessment-mcp",
    "a207-risk-rules-mcp": "CKDNutri-assessment-mcp",
    # --- P5 内容输出（报告 + 知识库合并）---
    "a207-content-mcp": "CKDNutri-content-mcp",
    "a207-report-mcp": "CKDNutri-content-mcp",
    "a207-knowledge-mcp": "CKDNutri-content-mcp",
}


def normalize_mcp(name: str) -> str:
    """把实际发行/目录包名归一为登记表内的键；未知名原样返回（由上层判未登记）。"""
    key = (name or "").strip()
    return MCP_ALIASES.get(key, key)


# MX-1：分期类问题不由家庭助手判定，改读 M1 已确诊分期
MX1_BLOCKED_CALLERS: frozenset[str] = frozenset({"parent_assistant"})

# ---------------------------------------------------------------- MX-1 字段可见性边界
CLINICIAN_ONLY_FIELDS: frozenset[str] = frozenset({
    "note_to_clinician", "clinician_note", "doctor_note", "doctor_notes", "internal_note", "note_to_doctor",
    "emr_status", "push_to_emr", "physician_confirmed",
    "scr_umol_L", "egfr_ml_min", "k_mmol_L", "p_mmol_L", "ca_mmol_L", "na_mmol_L",
    "cl_mmol_L", "bun_mmol_L", "albumin_g_L", "prealbumin_mg_L", "hb_g_L",
    "ipth_pg_mL", "vitd25oh_nmol_L", "ua_umol_L", "urine_protein_g_24h", "upcr_mg_mmol",
    "critical_value", "critical_flag",
    "prior_level", "level_correction",
    "nutrition_ceiling", "stage_confirmed_by", "z_score_height",
})

# 上述字段「绝不可见」的角色（家庭助手）。M9 报告层据此做受限脱敏（OD-013）。
CLINICIAN_ONLY_HIDDEN_FROM: frozenset[str] = frozenset({"parent_assistant"})

# ---------------------------------------------------------------- 权限矩阵（v2.3 阶段 0：3 角色 × 5 MCP，CKDNutri P1–P5）

ACCESS_NONE = "-"
ACCESS_READ = "R"
ACCESS_LIMITED = "RL"
ACCESS_RW = "R/W"

_READ_OK: frozenset[str] = frozenset({ACCESS_READ, ACCESS_LIMITED, ACCESS_RW})

PERMISSION_MATRIX: dict[str, dict[str, str]] = {
    "CKDNutri-clinical-data-mcp": {
        # doctor=R/W：临床助手可读写患儿档案与化验
        # parent=RL：家庭助手仅受限视图（脱敏）
        # risk=R：风险引擎读分期/化验
        "doctor_assistant": ACCESS_RW,
        "parent_assistant": ACCESS_LIMITED,
        "risk_warning": ACCESS_READ,
    },
    "CKDNutri-nutrition-mcp": {
        # doctor=R：临床角色读营养数据 + 跑计算面工具（CLINICAL_ROLES 单独收口）
        # parent=R/W：写饮食日记(upsert_food_diary) 且需回读日记摘要 —— 与 WRITE_TOOL_POLICY 一致
        # risk=-：风险引擎不读营养域
        "doctor_assistant": ACCESS_READ,
        "parent_assistant": ACCESS_RW,
        "risk_warning": ACCESS_NONE,
    },
    "CKDNutri-care-mcp": {
        # doctor=R/W：临床助手可创建/确认通知
        # parent=R：家庭助手读通知（含闭环状态查看）
        # risk=R/W：管线身份写通知（notify_* 系列由 WRITE_TOOL_POLICY 钳制为 {risk_warning}）
        "doctor_assistant": ACCESS_RW,
        "parent_assistant": ACCESS_READ,
        "risk_warning": ACCESS_RW,
    },
    "CKDNutri-assessment-mcp": {
        # doctor=R：读 eGFR / 分期
        # parent=-：分期类问题由家庭助手读 M1 已确诊分期（MX-1），不暴露评估域
        # risk=R：风险引擎读分期
        "doctor_assistant": ACCESS_READ,
        "parent_assistant": ACCESS_NONE,
        "risk_warning": ACCESS_READ,
    },
    "CKDNutri-content-mcp": {
        # doctor=R/W：报告生成 + push_to_emr 需 R/W 回查
        # parent=RL：受限报告视图
        # risk=R：风险引擎读报告上下文
        "doctor_assistant": ACCESS_RW,
        "parent_assistant": ACCESS_LIMITED,
        "risk_warning": ACCESS_READ,
    },
}

# M12 按角色切语料 profile（v2.3 阶段 0：删 nutritionist/child_companion）
KNOWLEDGE_PROFILE: dict[str, str] = {
    "doctor_assistant": "full",
    "risk_warning": "full",
    "parent_assistant": "plain_language",
}

# ---------------------------------------------------------------- MX-3 写权（v2.3 阶段 0：删 M11 两条 + 清理退役角色）

WRITE_TOOL_POLICY: dict[str, dict[str, object]] = {
    "upsert_lab_result": {
        "mcp": "CKDNutri-clinical-data-mcp",
        "allowed": frozenset({"doctor_assistant"}),
        "requires_confirmation": False,
        "note": "写入后触发 HAIP Workflow → P4 assess_clinical_status 重评",
    },
    "upsert_food_diary": {
        "mcp": "CKDNutri-nutrition-mcp",
        "allowed": frozenset({"parent_assistant"}),    # v2.3: 仅家庭助手，删 child_companion
        "requires_confirmation": False,
        "note": "打卡落点，供 sum_diet_intake 与食谱参考依从性",
    },
    # M11 log_meal_checkin / award_badge 退役
    "push_to_emr": {
        "mcp": "CKDNutri-content-mcp",
        "allowed": frozenset({"doctor_assistant"}),
        "requires_confirmation": True,
        "note": "需调用方另传 physician_confirmed=true，人在回路",
    },
    "notify_physician": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"risk_warning"}),         # 仅管线身份
        "requires_confirmation": False,
        "note": "推送前须过 24h 同规则去重",
    },
    "notify_parent": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"risk_warning"}),
        "requires_confirmation": False,
        "note": "推送前须过 24h 同规则去重",
    },
    "trigger_warning_event": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"risk_warning"}),
        "requires_confirmation": False,
        "note": "等级须由本轮新数据重评得出，禁止沿用历史等级",
    },
    "close_warning": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"risk_warning"}),
        "requires_confirmation": False,
        "note": "关闭工单需留判定链路",
    },
    "get_adherence_score": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"doctor_assistant"}),     # v2.3: 删 nutritionist / orchestrator
        "requires_confirmation": False,
        "note": "OD-014：依从性评分落库（写），仅临床助手可写；家庭助手 M4=RL 只读",
    },
}

WRITE_TOOL_ALIASES: dict[str, str] = {
    "写回病历": "push_to_emr", "写入病历": "push_to_emr", "推送病历": "push_to_emr",
    "录入化验": "upsert_lab_result", "写入化验": "upsert_lab_result",
    "新增生化": "upsert_lab_result", "新增检验": "upsert_lab_result",
    "记录饮食日记": "upsert_food_diary", "录入饮食": "upsert_food_diary",
    "写入饮食日记": "upsert_food_diary",
    # 打卡/勋章别名退役（M11 退役）
    "推送医生": "notify_physician", "通知医生": "notify_physician",
    "通知家长": "notify_parent", "发预警": "trigger_warning_event",
    "触发预警": "trigger_warning_event", "关闭预警": "close_warning",
}

WRITE_ACTION_PREFIXES: tuple[str, ...] = (
    "write", "create", "update", "delete", "insert",
    "upsert_", "notify_", "push_", "trigger_",
    "close_", "schedule_", "save_",
)

CN_WRITE_HINTS: tuple[str, ...] = ("写入", "写回", "新增一条", "保存到", "提交写")


def resolve_access(access: str, is_write: bool) -> bool:
    """把矩阵格子翻译成布尔放行结果。"""
    if is_write:
        return access == ACCESS_RW
    return access in _READ_OK


# ---------------------------------------------------------------- OD-011：从矩阵派生本地集合

def _matrix_readers(mcp: str) -> frozenset[str]:
    """从权限矩阵派生：对该 mcp 有读权限（R/RL/RW）的 caller 集合。"""
    real_mcp = normalize_mcp(mcp)
    return frozenset(c for c, a in PERMISSION_MATRIX[real_mcp].items() if a in _READ_OK)


def _matrix_writers(mcp: str) -> frozenset[str]:
    """从权限矩阵派生：对该 mcp 有写权限（R/W）的 caller 集合。

    OD-011 收口点：各包本地写白名单的唯一事实源就是矩阵，不允许再手写一份更宽的集合。
    内部先 normalize_mcp 防御：若传入未归一化的旧包名（如 a207-care-mcp）也安全落回矩阵键。
    """
    real_mcp = normalize_mcp(mcp)
    return frozenset(c for c, a in PERMISSION_MATRIX[real_mcp].items() if a == ACCESS_RW)


# ================================================================
# 各包本地放行集合（v2.3 阶段 0：从 3 角色矩阵重新派生）
# ================================================================

# --- M1 HIS ---
HIS_FULL_VIEW: frozenset[str] = frozenset({"doctor_assistant", "risk_warning"})
HIS_LIMITED: frozenset[str] = frozenset({"parent_assistant"})
HIS_READ: frozenset[str] = HIS_FULL_VIEW | HIS_LIMITED
HIS_BLOCKED: frozenset[str] = frozenset()        # 无被完全封锁的角色
HIS_COHORT: frozenset[str] = frozenset({"doctor_assistant", "risk_warning"})
HIS_ALLOWED_FILTER_KEYS: frozenset[str] = frozenset(
    {"age_band", "ckd_stage", "dialysis", "sex", "primary_disease",
     "has_allergies", "min_age_years", "max_age_years"})

# F3（v2.3）：P1 HIS 家长视图专有敏感聚合块，登记到单一事实源，避免与 CLINICIAN_ONLY_FIELDS 双轨漂移。
# 这些块家长绝不可见：
#   biochemistry      —— 原始化验面板（内含 scr_umol_L / k_mmol_L 等 CLINICIAN_ONLY_FIELDS 叶子）
#   food_diary_5d     —— 5 日饮食日记（原始摄入）
#   dialysis_detail   —— 透析明细
#   medical_record_no —— 病案号（PII）
#   bsa_m2            —— 体表面积（临床计算指标，家长视图不展示）
# 注：z_score_height / stage_confirmed_by 等叶子级敏感键已由 CLINICIAN_ONLY_FIELDS 覆盖，无需在此重复。
P1_PARENT_HIDDEN_FIELDS: frozenset[str] = frozenset({
    "biochemistry", "food_diary_5d", "dialysis_detail", "medical_record_no", "bsa_m2",
})

# --- M2 LIS ---
LIS_READ_FULL: frozenset[str] = frozenset({"doctor_assistant", "risk_warning"})
LIS_READ_LIMITED: frozenset[str] = frozenset({"parent_assistant"})
LIS_CRITICAL_CHANNEL: frozenset[str] = frozenset({"risk_warning", "doctor_assistant"})
LIS_WRITE_ALLOWED: frozenset[str] = _matrix_writers("CKDNutri-clinical-data-mcp")   # {doctor}

# --- M4 随访 ---
FOLLOWUP_WRITE_ALLOWED: frozenset[str] = _matrix_writers("CKDNutri-care-mcp")  # {doctor}
FOLLOWUP_CLINICIAN: frozenset[str] = frozenset({"doctor_assistant", "risk_warning"})

# --- M3 营养评估 工具级 ACL ---
# OD-011 收口：写白名单唯一事实源=矩阵，不允许手写更宽集合（与 LIS/FOLLOWUP 一致）。
# 矩阵 nutrition×parent=R/W ⇒ 这里自然得出 {parent_assistant}。
NUTRITION_ASSESSMENT_WRITE_ALLOWED: frozenset[str] = _matrix_writers("CKDNutri-nutrition-mcp")
NUTRITION_ASSESSMENT_DATA_TOOLS: frozenset[str] = frozenset({
    "upsert_food_diary",
    "get_food_diary_summary",
})
NUTRITION_ASSESSMENT_DATA_ROLES: frozenset[str] = frozenset({
    "parent_assistant",
    "doctor_assistant",
})
NUTRITION_ASSESSMENT_CLINICAL_TOOLS: frozenset[str] = frozenset({
    "calc_prnt_targets", "assess_intake_vs_target", "assess_pew_risk",
    "calc_growth_zscore", "record_pew_risk", "get_pew_history",
})
NUTRITION_ASSESSMENT_CLINICAL_ROLES: frozenset[str] = frozenset({"doctor_assistant"})

# --- M10 通知 ---
NOTIFY_WRITE_ROLES: frozenset[str] = _matrix_writers("CKDNutri-care-mcp")   # {doctor, risk}
NOTIFY_READ_ROLES: frozenset[str] = _matrix_readers("CKDNutri-care-mcp")    # {doctor, parent, risk}
# M11 游戏化 —— v2.3 阶段 0 退役，相关集合全部移除

# v2.3 阶段 0 兼容空值（M11/router 本地代码仍 import 这些符号，M11 退役后清理）
# 设为空以确保：① import 不报错 ② enforce 逻辑拒绝所有调用（退役行为一致）
# GAMIFICATION_MCP 现指向 M11 并入后的新家（M3 / P2）；GAMIFICATION_ALLOWED 仍为空集合，
# 故任何引用它的调用方仍被 fail-closed 拒绝，符号漂移已纠正、无安全回归。
GAMIFICATION_MCP = "CKDNutri-nutrition-mcp"
GAMIFICATION_ALLOWED: frozenset[str] = frozenset()
CHILD_FORBIDDEN_MCPS: frozenset[str] = frozenset()
