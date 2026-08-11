"""a207-policy —— A207 统一身份注入 / 权限矩阵 / 状态路径策略包。

Plan A 单一事实源。13 个 MCP 统一依赖本包：
- caller 注入（env A207_CALLER，fail-closed）→ get_caller / set_caller
- 权限矩阵 + MX-3 写权 + 各包放行集合 → matrix 模块
- 确定性执行 → enforce_read / enforce_write / check_permission / knowledge_profile
- 可写状态路径 → resolve_state_path

调用方（各 MCP 工具）应在函数入口：caller = get_caller(); access = enforce_read("a207-xxx-mcp")
取代原先「caller 是 LLM 自己填的参数」的旧模式（P0-1 修复）。
"""

from __future__ import annotations

from .caller import as_caller, get_caller, set_caller
from .exceptions import CallerError, CallerUnknown, PermissionDenied
from .gate import (
    _MATRIX_EXEMPT_WRITE_TOOLS,
    check_permission,
    detect_write_tool,
    enforce_nutrition_tool,
    enforce_read,
    enforce_write,
    is_write_action,
    knowledge_profile,
)
from .matrix import (
    ACCESS_LIMITED,
    ACCESS_NONE,
    ACCESS_RW,
    ACCESS_READ,
    CALLERS,
    CHILD_FORBIDDEN_MCPS,
    CLINICAL_CALC_MCP,
    CLINICIAN_ONLY_FIELDS,
    CLINICIAN_ONLY_HIDDEN_FROM,
    GAMIFICATION_MCP,
    FOLLOWUP_CLINICIAN,
    FOLLOWUP_WRITE_ALLOWED,
    GAMIFICATION_ALLOWED,
    HIS_ALLOWED_FILTER_KEYS,
    HIS_BLOCKED,
    HIS_COHORT,
    HIS_FULL_VIEW,
    HIS_LIMITED,
    HIS_READ,
    KNOWLEDGE_PROFILE,
    LIS_CRITICAL_CHANNEL,
    LIS_READ_FULL,
    LIS_READ_LIMITED,
    LIS_WRITE_ALLOWED,
    P1_PARENT_HIDDEN_FIELDS,
    MCP_ALIASES,
    MCP_REGISTRY,
    MX1_BLOCKED_CALLERS,
    NOTIFY_READ_ROLES,
    NOTIFY_WRITE_ROLES,
    NUTRITION_ASSESSMENT_CLINICAL_ROLES,
    NUTRITION_ASSESSMENT_CLINICAL_TOOLS,
    NUTRITION_ASSESSMENT_DATA_ROLES,
    NUTRITION_ASSESSMENT_DATA_TOOLS,
    NUTRITION_ASSESSMENT_WRITE_ALLOWED,
    PERMISSION_MATRIX,
    WRITE_TOOL_ALIASES,
    WRITE_TOOL_POLICY,
    normalize_mcp,
    resolve_access,
)
from .state import atomic_write_json, resolve_state_path

__all__ = [
    "CallerError", "CallerUnknown", "as_caller", "get_caller", "set_caller",
    "PermissionDenied", "check_permission", "detect_write_tool",
    "enforce_nutrition_tool", "enforce_read", "enforce_write", "is_write_action",
    "knowledge_profile", "_MATRIX_EXEMPT_WRITE_TOOLS",
    "ACCESS_NONE", "ACCESS_READ", "ACCESS_LIMITED", "ACCESS_RW",
    "CALLERS", "CHILD_FORBIDDEN_MCPS", "CLINICAL_CALC_MCP", "CLINICIAN_ONLY_FIELDS",
    "CLINICIAN_ONLY_HIDDEN_FROM",
    "GAMIFICATION_MCP",
    "FOLLOWUP_CLINICIAN", "FOLLOWUP_WRITE_ALLOWED", "GAMIFICATION_ALLOWED",
    "HIS_ALLOWED_FILTER_KEYS", "HIS_BLOCKED", "HIS_COHORT", "HIS_FULL_VIEW",
    "HIS_LIMITED", "HIS_READ", "KNOWLEDGE_PROFILE",
    "LIS_CRITICAL_CHANNEL", "LIS_READ_FULL", "LIS_READ_LIMITED", "LIS_WRITE_ALLOWED",
    "P1_PARENT_HIDDEN_FIELDS",
    "MCP_ALIASES", "MCP_REGISTRY", "MX1_BLOCKED_CALLERS",
    "NOTIFY_READ_ROLES", "NOTIFY_WRITE_ROLES", "NUTRITION_ASSESSMENT_WRITE_ALLOWED",
    "NUTRITION_ASSESSMENT_DATA_TOOLS", "NUTRITION_ASSESSMENT_DATA_ROLES",
    "NUTRITION_ASSESSMENT_CLINICAL_TOOLS", "NUTRITION_ASSESSMENT_CLINICAL_ROLES",
    "PERMISSION_MATRIX", "WRITE_TOOL_ALIASES", "WRITE_TOOL_POLICY",
    "normalize_mcp", "resolve_access", "resolve_state_path", "atomic_write_json",
]
