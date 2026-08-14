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


def _item_key(item: Any) -> tuple:
    """列表元素去重键：dict 优先取业务 id 键，否则按 JSON 序列化全等。"""
    if isinstance(item, dict):
        for k in ("record_id", "plan_id", "notification_id", "id", "entry_id"):
            if item.get(k) is not None:
                return ("id", k, item[k])
    return ("json", json.dumps(item, ensure_ascii=False, sort_keys=True))


def _merge_lists(cur: list, new: list) -> list:
    """列表按元素 id 去重合并（new 优先覆盖同 id，追加新元素）。"""
    result = list(cur)
    for item in new:
        key = _item_key(item)
        replaced = False
        for index, existing in enumerate(result):
            if _item_key(existing) == key:
                result[index] = item  # 同 id 以 new 为准（后写者意图）
                replaced = True
                break
        if not replaced:
            result.append(item)
    return result


def _merge_row(current: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """冲突重试合并：以**最新行**为底，new 非 None 字段覆盖；JSON 列表字段去重合并。

    current 为存储层读回的原始属性列（列表字段是 JSON 字符串），new 为本次欲写的
    序列化列。合并后既保留并发写者的新增（列表合并），又体现本次修改（标量覆盖）。
    """
    merged = dict(current)
    for key, value in new.items():
        cur_value = merged.get(key)
        # 两端都是 JSON 数组字符串 → 反序列化按 id 去重合并
        if isinstance(cur_value, str) and isinstance(value, str):
            try:
                cur_list = json.loads(cur_value)
                new_list = json.loads(value)
                if isinstance(cur_list, list) and isinstance(new_list, list):
                    merged[key] = json.dumps(_merge_lists(cur_list, new_list),
                                             ensure_ascii=False)
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
        merged[key] = value
    return merged

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
class TablestoreRepository:
    """Tablestore 后端（连接参数由 A207_OTS_* 环境变量注入，SDK 延迟导入）。

    并发正确性：写路径用 **_rev 版本列 + 条件更新（乐观锁）**——读当前 _rev，
    PutRow 携带 Condition(_rev == 当前值)，条件不满足（被并发写者覆盖）时 SDK
    抛 OTSClientError，重试（最多 _MAX_RETRY 次）；仍冲突则抛 RuntimeError
    （fail-closed：不静默丢更新，提示运维）。
    """

    def __init__(self, client: Any | None = None) -> None:
        """client 仅供测试注入内存 Fake（生产不传，走 A207_OTS_* 环境变量）。"""
        if client is not None:
            self._client = client
            return
        self.endpoint = os.environ.get(OTS_ENDPOINT_ENV)
        self.instance = os.environ.get(OTS_INSTANCE_ENV)
        self.ak_id = os.environ.get(OTS_AK_ID_ENV)
        self.ak_secret = os.environ.get(OTS_AK_SECRET_ENV)
        missing = [name for name, val in (
            (OTS_ENDPOINT_ENV, self.endpoint),
            (OTS_INSTANCE_ENV, self.instance),
            (OTS_AK_ID_ENV, self.ak_id),
            (OTS_AK_SECRET_ENV, self.ak_secret),
        ) if not val]
        if missing:
            raise RuntimeError(
                f"Tablestore 后端缺少连接参数：{', '.join(missing)}。"
                f"请注入 A207_OTS_* 环境变量（生产默认后端，勿静默回退 JSON）。")
        self._client = None  # 惰性建连

    def _get_client(self):
        if self._client is None:
            import tablestore  # 延迟导入：JSON 后端无需依赖 SDK

            self._client = tablestore.OTSClient(
                self.endpoint, self.ak_id, self.ak_secret, self.instance)
        return self._client

    # ---- 基础读写 ----

    @staticmethod
    def _pk(patient_id: str) -> list[tuple[str, str]]:
        return [("patient_id", patient_id)]

    @staticmethod
    def _pk_nid(notification_id: str) -> list[tuple[str, str]]:
        return [("notification_id", notification_id)]

    def _get_row(self, table: str, pk: list[tuple[str, str]]) -> dict[str, Any] | None:
        try:
            _, row, _ = self._get_client().get_row(table, pk)
        except Exception as exc:
            # 五审（2026-08-13）：fail-closed——存储故障（网络/超时/鉴权）抛 RuntimeError
            # （→ INTERNAL_ERROR），**不得静默当"行不存在"**（此前宽 except 会把 Tablestore
            # 抖动误判为"无随访数据"，医疗数据可信度受损）。行不存在（row is None）不抛。
            logger.error("Tablestore get_row 失败: table=%s pk=%s exc=%s", table, pk, exc)
            raise RuntimeError(
                f"Tablestore 读取失败（table={table}），详情见服务端日志") from exc
        if row is None:
            return None
        # attribute_columns 为 (name, value, timestamp) 三元组，仅取 name/value
        return {name: value for name, value, _ in row.attribute_columns}

    def _put_row_conditioned(self, table: str, pk: list[tuple[str, str]],
                             attrs: dict[str, Any], rev: int,
                             expect_exists: bool) -> None:
        """条件写：_rev 必须等于 rev（乐观锁）。条件不满足抛 OTSClientError。"""
        # 五审（2026-08-13）🔴：此前 import SingleColumnValueCondition——该符号在
        # tablestore 6.x SDK **不存在**（正确类名 SingleColumnCondition），生产切
        # Tablestore 写随访/通知会 ImportError 崩溃。已修正并补 Fake 回归。
        from tablestore import (ComparatorType, Condition, Row,
                                RowExistenceExpectation, SingleColumnCondition)

        expectation = (RowExistenceExpectation.EXPECT_EXIST if expect_exists
                       else RowExistenceExpectation.EXPECT_NOT_EXIST)
        col_cond = None
        if expect_exists:
            col_cond = SingleColumnCondition(
                _REV_COL, ComparatorType.EQUAL, rev)
        condition = Condition(expectation, col_cond)
        clean = {k: v for k, v in attrs.items() if v is not None}
        row = Row(pk, list(clean.items()))
        self._get_client().put_row(table, row, condition)

    def _save_row_with_optimistic_lock(
            self, table: str, pk: list[tuple[str, str]],
            attrs: dict[str, Any]) -> None:
        """乐观锁写入：读 _rev → 条件写 _rev+1 → 冲突重试。

        S5 修复（2026-08-13）：冲突重试时用 _merge_row **重新读取并合并**最新行与
        本次 attrs（此前整行覆盖旧 attrs → 高并发下后写覆盖先写的部分字段，lost update）。
        列表字段（records/plans/adherence）按元素 id 去重合并，标量 new 优先。
        """
        from tablestore import OTSClientError

        last_err: Exception | None = None
        for _ in range(_MAX_RETRY):
            current = self._get_row(table, pk)
            rev = int(current.get(_REV_COL, 0)) if current else 0
            next_attrs = dict(attrs)
            if current:
                next_attrs = _merge_row(current, next_attrs)  # S5：合并并发修改
            next_attrs[_REV_COL] = rev + 1
            try:
                self._put_row_conditioned(
                    table, pk, next_attrs, rev, expect_exists=current is not None)
                return
            except OTSClientError as exc:
                # C-B4 修复（2026-08-14）：仅**条件检查失败**（乐观锁冲突）重试——
                # 此前把所有 OTSClientError（鉴权失败/参数非法/表不存在等 SDK 错误）
                # 一律当并发冲突重试 3 次后报"存储并发写冲突"，把配置/环境错误误导
                # 成高并发问题。非冲突错误立即抛（归 INTERNAL_ERROR 定位真实根因）。
                code = str(getattr(exc, "code", "") or "")
                msg = str(getattr(exc, "message", "") or "")
                is_conflict = ("ConditionCheck" in code or "ConditionCheck" in msg
                               or "not match" in msg.lower())
                if not is_conflict:
                    raise
                last_err = exc  # 条件不满足 → 并发写冲突，重试
        raise RuntimeError(
            f"存储并发写冲突（{table} pk={pk}），重试 {_MAX_RETRY} 次仍失败，"
            f"拒绝静默覆盖: {last_err}")

    def _range_all(self, table: str) -> list[dict[str, Any]]:
        """全表 GetRange（主键升序）。返回 [{pk_dict, attrs_dict}]。"""
        from tablestore import INF_MAX, INF_MIN

        if table == TABLE_NOTIFICATION:
            start: list = [("notification_id", INF_MIN)]
            end: list = [("notification_id", INF_MAX)]
        else:
            start = [("patient_id", INF_MIN)]
            end = [("patient_id", INF_MAX)]
        rows: list[dict[str, Any]] = []
        next_start = start
        while next_start is not None:
            consumed, next_start, row_list, _ = self._get_client().get_range(
                table, "FORWARD", next_start, end, limit=200)
            for row in row_list:
                pk_dict = {}
                for k, v in row.primary_key:
                    pk_dict[k] = v.decode() if isinstance(v, bytes) else v
                attrs_dict = {name: value for name, value, _ in row.attribute_columns}
                rows.append({"pk": pk_dict, "attrs": attrs_dict})
        return rows

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
                    raise ValueError(
                        f"随访数据列 {key} 损坏（非法 JSON）：{exc}——拒绝静默清空，"
                        "请人工修复 Tablestore 该行数据") from exc
            elif isinstance(raw, list):
                out[key] = raw
            else:
                out[key] = []
        return out

    @staticmethod
    def _serialize_followup(data: dict[str, Any]) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        for key in ("records", "plans", "adherence"):
            val = data.get(key)
            attrs[key] = json.dumps(val or [], ensure_ascii=False) if val else json.dumps([], ensure_ascii=False)
        return attrs

    def load_followup(self, patient_id: str) -> dict[str, Any] | None:
        row = self._get_row(TABLE_FOLLOWUP, self._pk(patient_id))
        if row is None:
            return None
        return self._deserialize_followup(row)

    def save_followup(self, patient_id: str, data: dict[str, Any]) -> None:
        attrs = self._serialize_followup(data)
        self._save_row_with_optimistic_lock(TABLE_FOLLOWUP, self._pk(patient_id), attrs)

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
        self._save_row_with_optimistic_lock(
            TABLE_NOTIFICATION, self._pk_nid(notification_id), attrs)

    def all_notifications(self) -> list[dict[str, Any]]:
        rows = self._range_all(TABLE_NOTIFICATION)
        return [self._deserialize_notification(item["attrs"]) for item in rows]


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def ensure_tablestore_tables() -> None:
    """创建/校验 Tablestore 表（幂等，仅建缺失表）。"""
    from tablestore import (CapacityUnit, Condition, OTSClient,
                            ReservedThroughput, RowExistenceExpectation,
                            TableMeta, TableOptions)

    endpoint = os.environ[OTS_ENDPOINT_ENV]
    instance = os.environ[OTS_INSTANCE_ENV]
    ak = os.environ[OTS_AK_ID_ENV]
    sk = os.environ[OTS_AK_SECRET_ENV]
    client = OTSClient(endpoint, ak, sk, instance)
    existing = set(client.list_table())

    def _create(table_name: str, pk_schema: list[tuple[str, str]]) -> None:
        if table_name in existing:
            return
        meta = TableMeta(table_name, pk_schema)
        options = TableOptions(time_to_live=-1, max_version=1)
        throughput = ReservedThroughput(capacity_unit=CapacityUnit(0, 0))
        client.create_table(meta, options, throughput)
        print(f"[ensure] 已创建表 {table_name}")

    _create(TABLE_FOLLOWUP, [("patient_id", "STRING")])
    _create(TABLE_NOTIFICATION, [("notification_id", "STRING")])
    print(f"[ensure] Tablestore 表就绪：{sorted(existing | {TABLE_FOLLOWUP, TABLE_NOTIFICATION})}")


def get_repository() -> CareRepository:
    """按环境变量选择存储后端：缺省 tablestore（生产，缺 OTS 参数 fail-fast）；
    显式 A207_STORAGE_BACKEND=json 用本地 JSON（开发模式）。实例按 backend 缓存。"""
    backend = os.environ.get(STORAGE_BACKEND_ENV, "tablestore").strip().lower()
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
