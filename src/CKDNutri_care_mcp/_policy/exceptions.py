"""权限/身份相关异常类（集中定义，单一事实源）。

为什么集中 + 回退复用全局类
---------------------------
a207-policy 会以「全局包」和「随包内置的 _policy 子模块」两种形态并存
（见 deploy/sync_policy.py）。异常类一旦被复制成两份，就是两个不同的类对象，
测试里 `from a207_policy import CallerUnknown` 抓住的是全局那份，而内置 _policy
抛的是另一份 → `except` 按类身份匹配会漏抓。

解法：本模块先定义本地类，再尝试把自身类名「重定向」到全局 a207_policy 的同名类
（取不到再保留本地）。这样只要全局 a207_policy 可导入，两种形态抛出的异常就是
同一个类对象，测试捕获与部署运行都一致；若全局包完全不存在（隔离部署），则退化为
本地自洽副本。
"""

from __future__ import annotations


class CallerError(Exception):
    """权限/身份相关错误的基类。"""


class CallerUnknown(CallerError):
    """caller 未注入（环境变量缺失或为空）→ fail-closed 拒绝。"""


class PermissionDenied(CallerError):
    """权限不足，确定性拒绝（模型无法绕过）。携带 caller/mcp/action/reason 供工具层转成既有 forbidden 形态。"""

    def __init__(self, caller: str, mcp: str, action: str, reason: str):
        self.caller = caller
        self.mcp = mcp
        self.action = action
        self.reason = reason
        super().__init__(f"DENIED {caller} -> {mcp}:{action} : {reason}")


try:
    import a207_policy as _global_policy  # 全局 a207_policy（若存在）

    CallerError = _global_policy.CallerError
    CallerUnknown = _global_policy.CallerUnknown
    PermissionDenied = _global_policy.PermissionDenied
except Exception:
    # 全局包不可用：保留本地副本（隔离部署时自洽）
    pass
