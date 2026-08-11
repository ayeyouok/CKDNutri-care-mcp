"""身份注入（A1 核心）。

设计要点（对照架构复盘 P0-1）：
- 旧方案：caller 是 LLM 自己填的工具参数 → 模型可被提示词注入冒充医生。
- 新方案：caller 来自**部署时注入的环境变量 A207_CALLER**，模型碰不到。
  每个 Agent 用各自连接配置启动 MCP 服务端时写入该变量（如家长助手配置写死
  A207_CALLER=parent_assistant），服务端进程内读取，fail-closed：缺失即拒绝。

stdio 部署：启动子进程时由连接配置 setenv。
HTTP/SSE 部署（将来）：网关在会话建立时把身份写入请求上下文，服务端从上下文读取；
本模块保留 get_caller() 单一入口，将来只需在 HTTP 分支扩展，调用方代码不变。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

from .exceptions import CallerError, CallerUnknown

ENV_KEY = "A207_CALLER"


def get_caller() -> str:
    """返回当前进程注入的调用方身份。缺失或空 → 抛 CallerUnknown（fail-closed）。

    身份**只**来自环境变量 A207_CALLER（部署配置注入 / 测试用 as_caller 临时写入），
    单一通道、进程级共享。这样无论 a207_policy 以「全局包」还是「随包内置子模块
    _policy」的形式存在，调用方读取到的身份都一致——避免多副本模块状态漂移。
    """
    value = (os.environ.get(ENV_KEY) or "").strip()
    if not value:
        raise CallerUnknown(
            f"caller 未注入：环境变量 {ENV_KEY} 缺失或为空。"
            f"服务端必须由部署配置注入身份，模型不可自证身份（P0-1 修复）。"
        )
    return value


def set_caller(value: Optional[str]) -> None:
    """仅用于测试：覆盖 caller 来源（写入 A207_CALLER 环境变量）。生产代码不得调用。"""
    if value is None:
        os.environ.pop(ENV_KEY, None)
    else:
        os.environ[ENV_KEY] = value


@contextmanager
def as_caller(value: Optional[str]) -> Iterator[None]:
    """仅用于测试：在代码块内临时切换身份，退出时**一定**还原（异常也还原）。

    为什么需要它
    ------------
    真实部署里一个进程只有一个身份（部署配置写死），所以生产代码永远用不到这个。
    但端到端联调要模拟"医生录检验 → 风险引擎评估 → 通知发家长 → 孩子打卡"
    这样跨角色的完整链路，必须能逐步换身份，否则测不出真实的权限交接。

    手写 set_caller + try/finally 也能做，但漏写 finally 就会污染后续用例，
    而且那种污染很隐蔽——后面的用例莫名其妙有了不该有的权限，还看不出为什么。
    所以固化成上下文管理器，用法上不给人犯错的机会。

        with as_caller("doctor_assistant"):
            core.upsert_lab_result(...)
        with as_caller("parent_assistant"):
            core.get_labs(...)      # 自动换回受限视图

    身份写入 A207_CALLER 环境变量（与生产同一通道），因此 set_caller / as_caller
    对「全局 a207_policy」和「随包内置的 _policy 子模块」都生效——二者读到同一份
    进程级身份，不会因为模块被复制成多份而出现状态漂移。
    传 None 表示"模拟身份完全缺失"，用来测 fail-closed（会清掉该环境变量）。
    """
    prev = os.environ.get(ENV_KEY)
    if value is None:
        os.environ.pop(ENV_KEY, None)
    else:
        os.environ[ENV_KEY] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = prev
