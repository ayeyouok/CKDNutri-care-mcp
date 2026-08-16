# -*- coding: utf-8 -*-
"""P3 随访沟通域 DAO 抽象层（v0.6：默认 Tablestore + json 开发模式）。

设计目标（对齐 P1 clinical-data repository.py 模式）：
- 数据访问契约与存储实现解耦：core.py 只面向本层接口编程，存储后端可切换
  （默认阿里云表格存储 Tablestore ↔ 本地 JSON 开发模式），业务逻辑零改动。
- 本层只做「数据存取 + 并发原子性」，不做权限/脱敏/业务计算。

后端选择（环境变量，与 P1/P2 同语义）：
- 缺省 / A207_STORAGE_BACKEND=tablestore：阿里云表格存储（生产，需配 A207_OTS_*，
  缺参 fail-fast，不静默回退）
- A207_STORAGE_BACKEND=json：本地 JSON 文件（本地开发/测试）

Tablestore 连接参数（由部署环境注入，与 A207_CALLER 同模式，不入代码）：
- A207_OTS_ENDPOINT / A207_OTS_INSTANCE_NAME / A207_OTS_ACCESS_KEY_ID /
  A207_OTS_ACCESS_KEY_SECRET

表结构（v2.4）：
- followup_store     主键 patient_id；属性列 = records/plans/adherence（嵌套 JSON 化）+
                      _rev 版本列（乐观锁）
- notification_store 主键 notification_id；属性列 = 通知字段 + _rev 版本列

并发正确性（2026-08-12 四审，回应"多 worker 部署下 _STORE_LOCK 只护单进程"）：
- 进程内 _STORE_LOCK 保留为**优化**（减少版本冲突），不再是正确性保证；
- Tablestore 后端写路径用 **乐观锁**（_rev 版本列 + 条件更新 Condition +
  冲突重试）：save_followup / save_notification 读 _rev → 条件写 _rev+1 →
  条件不满足（并发覆盖）抛 OTSClientError → 重试（最多 3 次）→ 仍冲突抛
  RuntimeError（fail-closed，不静默丢更新）。
- LocalJson 后端维持现状语义：_STORE_LOCK 进程内串行化（单进程部署无回归；
  多进程 JSON 后端本身不支持——迁移 Tablestore 是正解）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from a207_policy import atomic_write_json, resolve_state_path
from a207_policy.storage import TablestoreBase, ensure_json_backend_allowed  # 2026-08-15：共享 Tablestore 基础设施

logger = logging.getLogger("CKDNutri-care-mcp.repository")

# ---- 存储文件名/目录 env（与 core 既有契约一致，测试依赖）----
FOLLOWUP_STORE_FILENAME = "followup_store.json"
NOTIFICATION_STORE_FILENAME = "notification_store.json"
FOLLOWUP_DATA_DIR_ENV = "A207_FOLLOWUP_DATA_DIR"
NOTIFICATION_DATA_DIR_ENV = "A207_NOTIFICATION_DATA_DIR"

# Tablestore 连接参数（与 P1 repository.py 同约定）
OTS_ENDPOINT_ENV = "A207_OTS_ENDPOINT"
OTS_INSTANCE_ENV = "A207_OTS_INSTANCE_NAME"
OTS_AK_ID_ENV = "A207_OTS_ACCESS_KEY_ID"
OTS_AK_SECRET_ENV = "A207_OTS_ACCESS_KEY_SECRET"
STORAGE_BACKEND_ENV = "A207_STORAGE_BACKEND"

# Tablestore 表名
TABLE_FOLLOWUP = "followup_store"
TABLE_NOTIFICATION = "notification_store"

# 乐观锁版本列名（Tablestore 后端专用；JSON 后端无此列）
_REV_COL = "_rev"
# 乐观锁冲突重试次数
_MAX_RETRY = 3

# 进程内锁：两后端共用（JSON 串行化 RMW；Tablestore 减少版本冲突）
_FILE_LOCK = threading.Lock()

# S5 修复（2026-08-13）：乐观锁冲突重试时**合并业务字段**而非整行覆盖——
# 此前冲突后仅用旧 attrs 重试覆盖，高并发下后写者覆盖先写者的部分字段
# （如通知状态机 unacked→confirmed→resolved 多步流转的中间状态丢失）。
# 合并规则见 _merge_row（列表按元素 id 去重合并、标量 new 优先）。


# repository 实例缓存（按 backend 缓存，切换 env 后重建；Tablestore 避免每请求重握手）
# N6 修复（2026-08-13）：加 _REPO_CACHE_LOCK——当前同步路径 double-check 竞态危害低
# （重复构建 TablestoreRepository 惰性建连幂等），但 async 化后多协程并发首调会
# 重复建连/重复校验 OTS 参数，持锁串行化。
_REPO_CACHE: dict[str, CareRepository] = {}
_REPO_CACHE_LOCK = threading.Lock()

# 随访默认结构（与 core 历史 setdefault 语义一致）
DEFAULT_FOLLOWUP = {"records": [], "plans": [], "adherence": []}


@runtime_checkable
class CareRepository(Protocol):
    """P3 数据访问契约（随访 + 通知）。"""

    # ---- 随访（按 patient_id 分片）----
    def load_followup(self, patient_id: str) -> dict[str, Any] | None:
        """读取患者随访数据（records/plans/adherence）；无记录返回 None。"""
        ...

    def save_followup(self, patient_id: str, data: dict[str, Any]) -> None:
        """整体保存患者随访数据（并发安全：Tablestore 乐观锁 / JSON 进程内锁）。"""
        ...

    # ---- 通知（按 notification_id 单行）----
    def load_notification(self, notification_id: str) -> dict[str, Any] | None:
        """按 id 读取单条通知；不存在返回 None。"""
        ...

    def save_notification(self, notification_id: str, rec: dict[str, Any]) -> None:
        """整体保存单条通知（并发安全同上）。"""
        ...

    def all_notifications(self) -> list[dict[str, Any]]:
        """全量通知（供按 patient_id 过滤；Tablestore 用 GetRange）。"""
        ...


# ---------------------------------------------------------------------------
# 本地 JSON 文件后端
# ---------------------------------------------------------------------------
def _followup_json_path() -> Path:
    override = os.environ.get(FOLLOWUP_DATA_DIR_ENV)
    if override:
        return Path(override) / FOLLOWUP_STORE_FILENAME
    return resolve_state_path(FOLLOWUP_STORE_FILENAME)


def _notification_json_path() -> Path:
    override = os.environ.get(NOTIFICATION_DATA_DIR_ENV)
    if override:
        return Path(override) / NOTIFICATION_STORE_FILENAME
    return resolve_state_path(NOTIFICATION_STORE_FILENAME)


def _read_json_file(path: Path, name: str) -> dict[str, Any]:
    """读 JSON 文件；缺失返回 {}；损坏/非 dict 抛 RuntimeError（fail-closed，
    对齐 core 既有 _load_store/_notify_load 的 BUG-65/67 语义）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{name} {path.name} JSON 损坏，拒绝加载（防止静默清空），"
            f"请检查磁盘/恢复备份: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(
            f"{name} {path.name} 读取失败，拒绝加载: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{name} {path.name} 数据类型错误：期望 dict，实际为 {type(data).__name__}，"
            f"拒绝加载（防止静默清空）")
    return data


class LocalJsonRepository:
    """本地 JSON 文件后端（现状实现，行为与 core 既有读写完全一致）。"""

    # ---- 随访 ----
    def load_followup(self, patient_id: str) -> dict[str, Any] | None:
        store = _read_json_file(_followup_json_path(), "随访库")
        p = store.get(patient_id)
        if not isinstance(p, dict):
            return None
        return p

    def save_followup(self, patient_id: str, data: dict[str, Any]) -> None:
        # C-S1 修复（2026-08-14）：部分更新语义（与 Tablestore 端 _merge_row 对齐）——
        # 此前整体替换 store[pid]=data，core 若传完整快照会在并发/重试时覆盖并发者
        # 的新增标量字段。现在以存储行为底、data 字段覆盖（data 应为本次变更子集）。
        with _FILE_LOCK:
            store = _read_json_file(_followup_json_path(), "随访库")
            store[patient_id] = {**store.get(patient_id, {}), **data}
            atomic_write_json(_followup_json_path(), store)

    # ---- 通知 ----
    def load_notification(self, notification_id: str) -> dict[str, Any] | None:
        store = _read_json_file(_notification_json_path(), "通知库")
        rec = store.get(notification_id)
        return rec if isinstance(rec, dict) else None

    def save_notification(self, notification_id: str, rec: dict[str, Any]) -> None:
        # C-S1 修复（2026-08-14）：部分更新语义（与 Tablestore 端 _merge_row 对齐）——
        # 此前整体替换 store[nid]=rec，core 传完整快照（load→改→save）时并发/重试
        # 会覆盖并发者的 escalated 等标量字段（S5 只救了列表字段）。
        with _FILE_LOCK:
            store = _read_json_file(_notification_json_path(), "通知库")
            store[notification_id] = {**store.get(notification_id, {}), **rec}
            atomic_write_json(_notification_json_path(), store)

    def all_notifications(self) -> list[dict[str, Any]]:
        store = _read_json_file(_notification_json_path(), "通知库")
        return [r for r in store.values() if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# 阿里云表格存储（Tablestore）后端
# ---------------------------------------------------------------------------
class TablestoreRepository(TablestoreBase):
    """Tablestore 后端（连接参数由 A207_OTS_* 环境变量注入，SDK 延迟导入）。

    2026-08-15：连接/乐观锁/GetRange/建表等基础设施收敛到
    a207_policy.storage.TablestoreBase（消除三包 ~750 行复制），本类仅留业务方法。
    并发正确性：写路径用 **_rev 版本列 + 条件更新（乐观锁）**——读当前 _rev，
    PutRow 携带 Condition(_rev == 当前值)，条件不满足（被并发写者覆盖）时 SDK
    抛 OTSClientError，重试（最多 _MAX_RETRY 次）；仍冲突则抛 RuntimeError
    （fail-closed：不静默丢更新，提示运维）。
    """

    @staticmethod
    def _pk(patient_id: str) -> list[tuple[str, str]]:
        return [("patient_id", patient_id)]

    @staticmethod
    def _pk_nid(notification_id: str) -> list[tuple[str, str]]:
        return [("notification_id", notification_id)]


    # ---- 随访 ----

    @staticmethod
    def _deserialize_followup(attrs: dict[str, Any]) -> dict[str, Any]:
        # C-B3 修复（2026-08-14）：损坏 JSON 一律抛错（fail-closed，与 JSON 端
        # _read_json_file 同口径）——此前 except JSONDecodeError 静默置 []，读到的
        # 空列表经 save 全量覆盖写回 → 患儿随访数据（记录/计划/依从性）**永久丢失**
        # 且无任何告警。损坏即显式失败，交由上层定位（人工修复或降级），绝不静默。
        out: dict[str, Any] = {}
        for key in ("records", "plans", "adherence"):
            raw = attrs.get(key)
            if isinstance(raw, str):
                try:
                    out[key] = json.loads(raw)
                except json.JSONDecodeError as exc:
                    # P3-2（2026-08-15）：数据损坏是**服务端存储问题**，抛 RuntimeError
                    # （归 INTERNAL_ERROR + 脱敏）——此前 ValueError 被 server 转
                    # INVALID_INPUT（客户端错误码），误导调用方以为是入参错误，
                    # 与 P4 #8 规则库损坏同口径修正。
                    raise RuntimeError(
                        f"随访数据列 {key} 损坏（非法 JSON）：拒绝静默清空，"
                        "请人工修复 Tablestore 该行数据") from exc
            elif isinstance(raw, list):
                out[key] = raw
            else:
                out[key] = []
        return out

    @staticmethod
    def _serialize_followup(data: dict[str, Any]) -> dict[str, Any]:
        # M4（2026-08-16，第七轮审查）：保留**全部键**——此前只序列化
        # records/plans/adherence 三键，core 若在行上存其他字段（未来扩展）会被
        # 丢弃，与 JSON 后端合并式写入语义不一致（潜在数据丢失）。list/dict 类
        # JSON 序列化，标量直存。
        attrs: dict[str, Any] = {}
        for key, val in data.items():
            if isinstance(val, (list, dict)):
                attrs[key] = json.dumps(val, ensure_ascii=False)
            else:
                attrs[key] = val
        return attrs

    def load_followup(self, patient_id: str) -> dict[str, Any] | None:
        row = self._get_row(TABLE_FOLLOWUP, self._pk(patient_id))
        if row is None:
            return None
        return self._deserialize_followup(row)

    def save_followup(self, patient_id: str, data: dict[str, Any]) -> None:
        attrs = self._serialize_followup(data)
        self._save_row_locked(TABLE_FOLLOWUP, self._pk(patient_id), attrs)

    # ---- 通知 ----

    @staticmethod
    def _deserialize_notification(attrs: dict[str, Any]) -> dict[str, Any]:
        rec = dict(attrs)
        rec.pop(_REV_COL, None)  # 版本列是内部实现，不对外暴露
        return rec

    @staticmethod
    def _serialize_notification(rec: dict[str, Any]) -> dict[str, Any]:
        attrs = {k: v for k, v in rec.items() if v is not None}
        attrs.pop(_REV_COL, None)
        return attrs

    def load_notification(self, notification_id: str) -> dict[str, Any] | None:
        row = self._get_row(TABLE_NOTIFICATION, self._pk_nid(notification_id))
        if row is None:
            return None
        return self._deserialize_notification(row)

    def save_notification(self, notification_id: str, rec: dict[str, Any]) -> None:
        attrs = self._serialize_notification(rec)
        self._save_row_locked(
            TABLE_NOTIFICATION, self._pk_nid(notification_id), attrs)

    def all_notifications(self) -> list[dict[str, Any]]:
        rows = self._range_all(TABLE_NOTIFICATION, ["notification_id"])
        return [self._deserialize_notification(item["attrs"]) for item in rows]


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def ensure_tablestore_tables() -> None:
    """创建/校验 Tablestore 表（幂等，仅建缺失表；2026-08-15 收敛到 storage.ensure_tables）。"""
    TablestoreBase().ensure_tables({
        TABLE_FOLLOWUP: [("patient_id", "STRING")],
        TABLE_NOTIFICATION: [("notification_id", "STRING")],
    })



def get_repository() -> CareRepository:
    """按环境变量选择存储后端：缺省 tablestore（生产，缺 OTS 参数 fail-fast）；
    显式 A207_STORAGE_BACKEND=json 用本地 JSON（开发模式）。实例按 backend 缓存。"""
    backend = os.environ.get(STORAGE_BACKEND_ENV, "tablestore").strip().lower()
    if backend == "json":
        # 生产护栏（2026-08-15）：json 后端仅限显式确认（A207_ACCEPT_DEV_STORAGE=1）
        ensure_json_backend_allowed()  # 未确认即抛 RuntimeError（fail-closed）
    repo = _REPO_CACHE.get(backend)
    if repo is None:
        with _REPO_CACHE_LOCK:  # N6：double-check 防并发首调重复构建
            repo = _REPO_CACHE.get(backend)
            if repo is None:
                repo = (TablestoreRepository() if backend != "json"
                        else LocalJsonRepository())
                _REPO_CACHE[backend] = repo
    return repo


__all__ = [
    "CareRepository",
    "LocalJsonRepository",
    "TablestoreRepository",
    "ensure_tablestore_tables",
    "get_repository",
    "TABLE_FOLLOWUP",
    "TABLE_NOTIFICATION",
    "FOLLOWUP_STORE_FILENAME",
    "NOTIFICATION_STORE_FILENAME",
    "FOLLOWUP_DATA_DIR_ENV",
    "NOTIFICATION_DATA_DIR_ENV",
    "STORAGE_BACKEND_ENV",
]
