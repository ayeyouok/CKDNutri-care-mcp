"""可写状态路径解析（A3 核心）。

旧方案：状态文件写在 os.path.dirname(__file__)（即 site-packages 安装目录），
容器/云沙箱里通常是只读的 → 写入抛异常（架构复盘 P1-3，与 M2 数据缺失败同源）。

新方案：统一经本函数解析可写路径：
- 若设环境变量 A207_DATA_DIR，落到该目录；
- 否则落到系统临时目录下的 a207_state/（总是可写）。
各包把原先写死的 STORE 常量改成 `resolve_state_path("xxx_store.json")` 即可。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

ENV_KEY = "A207_DATA_DIR"


def resolve_state_path(filename: str, *, base: str | None = None) -> Path:
    """返回可写的 filename 绝对路径，并确保父目录存在。

    :param filename: 状态文件名，如 "followup_store.json"
    :param base: 测试可传入临时基目录覆盖环境变量
    """
    if base:
        root = Path(base)
    else:
        override = os.environ.get(ENV_KEY)
        if override:
            root = Path(override)
        else:
            # 未设环境变量：落到系统临时目录，保证可写（避免只读安装目录崩溃）
            import tempfile
            root = Path(tempfile.gettempdir()) / "a207_state"
    root.mkdir(parents=True, exist_ok=True)
    return root / filename


def atomic_write_json(path, data, *, encoding: str = "utf-8") -> None:
    """原子写 JSON：先写同目录临时文件，再 os.replace 换入，避免半写截断。

    OD-014（P2-3）：各包原 `open(path,"w") + json.dump` 直接覆盖写，进程在写一半时
    被 kill / 磁盘满会留下截断文件，下次 _load_store 静默读成空壳（丢数据）。
    改为：写 `<path>.<rand>.tmp` → flush+fsync → os.replace 原子替换（POSIX 与
    Windows 均保证同卷 replace 原子性）。调用方无感知，返回值 None。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
