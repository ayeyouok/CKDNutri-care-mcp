# -*- coding: utf-8 -*-
"""pydantic 请求/响应模型：约束 M4 工具的入参与出参形状。

说明：core.py 内部使用纯参数（便于单测），server.py 用本文件模型做入口校验。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CallerId = Literal[
    "orchestrator",
    "doctor_assistant",
    "nutritionist",
    "parent_assistant",
    "child_companion",
    "risk_warning",
]

CkdStage = Literal["G1", "G2", "G3a", "G3b", "G4", "G5", "G5D"]
AlbuminuriaStage = Literal["A1", "A2", "A3"]
VisitType = Literal["outpatient", "phone", "online", "dialysis", "nutrition_counsel"]


class ScheduleFollowupRequest(BaseModel):
    patient_id: str = Field(pattern=r"^P[0-9]{4,}$", description="患者标识（与 PCP 一致）")
    ckd_stage: CkdStage = Field(description="CKD 分期（决定 KDIGO 2024 推荐随访频率）")
    albuminuria_stage: AlbuminuriaStage = "A1"
    visit_type: VisitType = "outpatient"
    anchor_date: str = Field(description="计划基准日期 YYYY-MM-DD（通常为本次就诊日）")
    # P0-1：身份由部署环境注入（A207_CALLER），不再作为工具入参暴露给模型。
    caller: CallerId | None = Field(default=None, description="保留字段，工具层不再接收")
    plan_summary: str = Field(description="随访计划摘要（所有角色可见）")
    note_to_clinician: str = Field(default="", description="仅供医生/营养的备注（患者/家属不可见）")


class GetFollowupRequest(BaseModel):
    patient_id: str = Field(pattern=r"^P[0-9]{4,}$")
    # P0-1：同上，身份不再随入参传入。
    caller: CallerId | None = None


class AdherenceRequest(BaseModel):
    patient_id: str = Field(pattern=r"^P[0-9]{4,}$")
    diet_ratio: float = Field(ge=0.0, le=1.0, description="饮食日记完成率 0-1（来自 M3/M11）")
    med_ratio: float = Field(ge=0.0, le=1.0, description="用药按时率 0-1")
    visit_ratio: float = Field(ge=0.0, le=1.0, description="随访到场率 0-1")
    # P0-1：同上，身份不再随入参传入。
    caller: CallerId | None = None


class PewTimelineFacadeRequest(BaseModel):
    patient_id: str = Field(pattern=r"^P[0-9]{4,}$")
    pew_history: list[dict] = Field(default=[], description="来自 M3 get_pew_history 的输出（ADR-007）")
