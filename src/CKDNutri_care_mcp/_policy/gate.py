"""权限执行点（确定性，非 LLM 可选）。

- check_permission：保留 M13 兼容的返回形态（dict），供路由层查表。
- enforce_read / enforce_write：供各 MCP 工具在入口处调用，放行返回 access 字符串，
  拒绝抛 PermissionDenied（确定性，模型无法绕过 —— 解决 P0-2 门禁虚设的根因：
  校验不再依赖 LLM「自觉」调用某个 gate 工具，而是每个工具内强制）。
- detect_write_tool / is_write_action：MX-3 写工具识别。
"""

from __future__ import annotations

import re

from .caller import get_caller
from .exceptions import CallerError, PermissionDenied
from .matrix import (
    ACCESS_RW,
    CALLERS,
    KNOWLEDGE_PROFILE,
    NUTRITION_ASSESSMENT_CLINICAL_ROLES,
    NUTRITION_ASSESSMENT_CLINICAL_TOOLS,
    NUTRITION_ASSESSMENT_DATA_ROLES,
    NUTRITION_ASSESSMENT_DATA_TOOLS,
    NUTRITION_ASSESSMENT_WRITE_ALLOWED,
    PERMISSION_MATRIX,
    WRITE_TOOL_POLICY,
    WRITE_TOOL_ALIASES,
    normalize_mcp,
    resolve_access,
)

_ALIASES = WRITE_TOOL_ALIASES  # 别名引用，保持 detect_write_tool 可读

_NOTIFY_RE = re.compile(r"notify_[a-z_]+")
_UNDERSCORE_PREFIXES = tuple(p for p in (
    "write", "create", "update", "delete", "insert",
    "upsert_", "notify_", "log_", "award_", "push_", "trigger_",
    "close_", "schedule_", "save_",
) if p.endswith("_"))

# OD-010 选项 A（已落地）：M3 是「计算+数据」混合包，工具级 ACL（enforce_nutrition_tool）
# 在矩阵（MCP 粒度）之下做细分授权：
#   - upsert_food_diary（写日记）：仅家长/患儿，比矩阵 M3×家长=RL 更宽 → 豁免矩阵 R/W 校验
#   - record_pew_risk（落 PEW 历史）：仅临床角色，比矩阵 M3×临床=READ 更宽 → 豁免矩阵 R/W 校验
# 这两个写工具是「工具级白名单治理」的有意例外，红队 B7b 亦据此跳过（见 tests/red_team_probe.py）。
_MATRIX_EXEMPT_WRITE_TOOLS: frozenset[str] = frozenset(
    {"upsert_food_diary", "record_pew_risk"})


def detect_write_tool(text: str) -> str | None:
    """从 intent/action 文本中识别已登记的写工具，识别不到返回 None。"""
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    for tool in sorted(WRITE_TOOL_POLICY, key=len, reverse=True):
        if tool in lowered:
            return tool
    for alias, tool in sorted(_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if alias in lowered:
            return tool
    if _NOTIFY_RE.search(lowered):
        return "notify_physician"
    return None


def is_write_action(action: str) -> bool:
    lowered = (action or "").strip().lower()
    if any(lowered.startswith(prefix) for prefix in (
        "write", "create", "update", "delete", "insert")):
        return True
    if any(prefix in lowered for prefix in _UNDERSCORE_PREFIXES):
        return True
    return any(hint in lowered for hint in ("写入", "写回", "新增一条", "保存到", "提交写"))


def check_permission(caller: str, mcp: str, action: str = "read") -> dict:
    """M13 兼容查表：返回 {allow, access, resolved_action, reason, ...}。"""
    caller_id = (caller or "").strip()
    mcp_id = normalize_mcp(mcp)
    action_id = (action or "read").strip()

    if caller_id not in CALLERS:
        return _perm(False, "-", "read", f"caller={caller_id or '(空)'} 未在权限矩阵登记")
    if mcp_id not in PERMISSION_MATRIX:
        return _perm(False, "-", "read", f"mcp={mcp_id or '(空)'} 不在 5 个已登记 MCP 内")

    access = PERMISSION_MATRIX[mcp_id][caller_id]
    tool = detect_write_tool(action_id)
    if tool:
        return _check_write_tool(caller_id, mcp_id, access, tool)

    is_write = is_write_action(action_id)
    allow = resolve_access(access, is_write)
    kind = "write" if is_write else "read"
    verdict = "放行" if allow else "拒绝"
    return _perm(allow, access, kind, f"矩阵 {mcp_id} x {caller_id} = {access}，{kind} 请求{verdict}")


def _check_write_tool(caller_id: str, mcp_id: str, access: str, tool: str) -> dict:
    policy = WRITE_TOOL_POLICY.get(tool)
    if policy is None:
        # 未登记的写工具名：回到读写二分
        allow = resolve_access(access, True)
        return _perm(allow, access, "write",
                     f"未登记写工具 {tool}，按矩阵 {mcp_id} x {caller_id} = {access} 判定")
    owner_mcp = str(policy["mcp"])
    allowed: frozenset[str] = policy["allowed"]
    if mcp_id != owner_mcp:
        return _perm(False, access, "write",
                     f"写工具 {tool} 属于 {owner_mcp}，与 mcp={mcp_id} 不一致")
    if caller_id not in allowed:
        return _perm(False, access, "write",
                     f"MX-3 写权收口：{tool} 仅允许 {'/'.join(sorted(allowed))}，"
                     f"当前 caller={caller_id}")
    return _perm(True, access, "write", f"MX-3 放行：{tool} 由 {caller_id} 执行",
                 tool=tool, requires_confirmation=bool(policy["requires_confirmation"]))


def _perm(allow: bool, access: str, resolved_action: str, reason: str, *,
          tool: str | None = None, requires_confirmation: bool = False) -> dict:
    return {
        "allow": allow,
        "access": access,
        "resolved_action": resolved_action,
        "reason": reason,
        "write_tool": tool,
        "requires_confirmation": requires_confirmation,
        "limited_view": access == "RL",
    }


def enforce_read(mcp_name: str, tool: str | None = None) -> str:
    """入口守卫：caller 是否可读 mcp_name。放行返回 access（R/RL/RW），拒绝抛 PermissionDenied。
    tool 参数预留用于未来细粒度读取工具级控制。
    """
    return _enforce(mcp_name, "read")


def enforce_write(mcp_name: str, tool: str | None = None) -> str:
    """入口守卫：caller 是否可写 mcp_name（tool 为写工具名时走 MX-3）。"""
    return _enforce(mcp_name, tool or "write")


def enforce_nutrition_tool(caller: str, tool: str) -> str:
    """M3 工具级授权（OD-010 选项 A）：数据面写仅家长/患儿，数据面读 + 计算面仅临床角色。

    M3 是「计算 + 数据」混合包。矩阵对 M3 仅做 MCP 粒度授权（家长=R/W、临床=READ、
    risk=-），无法表达"同包内某些工具家长能写、某些只能临床角色算"。
    故在此做工具级细分（单一事实源在 matrix 模块）：
    - 数据面·写 upsert_food_diary：仅家长/患儿（与 MX-3 WRITE_TOOL_POLICY /
      core._WRITE_ALLOWED_CALLERS 一致，单一事实源）
    - 数据面·读 get_food_diary_summary：家长/患儿/临床角色（临床需读日记做评估）
    - 计算面·临床判读/落库（calc_prnt_targets / assess_intake_vs_target / assess_pew_risk /
      calc_growth_zscore / record_pew_risk / get_pew_history）：仅 doctor（临床角色）
    未登记工具 fail-closed 拒绝。caller 为已校验身份字符串（由调用方经 get_caller() 取得）。
    """
    if caller not in CALLERS:
        raise PermissionDenied(
            caller, "CKDNutri-nutrition-mcp", tool, "caller 未登记")
    # 数据面·写：饮食日记写入仅家长/患儿（与 MX-3 写权收口一致，单一事实源）
    if tool == "upsert_food_diary":
        if caller in NUTRITION_ASSESSMENT_WRITE_ALLOWED:
            return "RL"
        raise PermissionDenied(
            caller, "CKDNutri-nutrition-mcp", tool,
            "饮食日记写入仅限家长/患儿")
    # 数据面·读：日记摘要家长/患儿/临床角色可读
    if tool in NUTRITION_ASSESSMENT_DATA_TOOLS:
        if caller in NUTRITION_ASSESSMENT_DATA_ROLES:
            return "RL"
        raise PermissionDenied(
            caller, "CKDNutri-nutrition-mcp", tool,
            "日记摘要仅限家长/患儿/临床角色")
    # 计算面·临床判读/落库：仅临床角色
    if tool in NUTRITION_ASSESSMENT_CLINICAL_TOOLS:
        if caller in NUTRITION_ASSESSMENT_CLINICAL_ROLES:
            return PERMISSION_MATRIX["CKDNutri-nutrition-mcp"][caller]
        raise PermissionDenied(
            caller, "CKDNutri-nutrition-mcp", tool,
            "计算面工具仅限临床角色(doctor)")
    raise PermissionDenied(
        caller, "CKDNutri-nutrition-mcp", tool, f"未登记工具: {tool}")


def _enforce(mcp_name: str, action: str) -> str:
    caller = get_caller()
    if caller not in CALLERS:
        raise PermissionDenied(caller, mcp_name, action, f"caller 未登记: {caller}")
    mcp = normalize_mcp(mcp_name)
    if mcp not in PERMISSION_MATRIX:
        raise PermissionDenied(caller, mcp_name, action, "mcp 未登记")
    access = PERMISSION_MATRIX[mcp][caller]

    wt = detect_write_tool(action) if action != "read" else None
    if wt:
        policy = WRITE_TOOL_POLICY.get(wt)
        if policy is None:
            if not resolve_access(access, True):
                raise PermissionDenied(caller, mcp, action, f"access={access} 不允许写")
        elif mcp != policy["mcp"] or caller not in policy["allowed"]:
            raise PermissionDenied(caller, mcp, action, f"MX-3 写权受限: {wt}")
        else:
            # OD-011：登记写工具额外回查矩阵 R/W（OD-010 待决豁免除外），确保矩阵为唯一事实源。
            # 例：notify_* 仅 risk_warning=R/W 可写；push_to_emr 仅 doctor=R/W（即便 nutritionist 同有 R/W 仍按工具白名单收口）。
            if wt not in _MATRIX_EXEMPT_WRITE_TOOLS:
                owner = policy["mcp"]
                if PERMISSION_MATRIX[owner][caller] != ACCESS_RW:
                    raise PermissionDenied(
                        caller, owner, action,
                        f"矩阵 {owner} x {caller} = {PERMISSION_MATRIX[owner][caller]}，无 R/W，拒绝 {wt}")
    else:
        is_write = action != "read"
        if not resolve_access(access, is_write):
            raise PermissionDenied(caller, mcp, action,
                                   f"access={access} 不允许 {action}")
    return access


def knowledge_profile(caller: str) -> str:
    """M12 按 caller 切语料 profile。"""
    return KNOWLEDGE_PROFILE.get((caller or "").strip(), "none")
