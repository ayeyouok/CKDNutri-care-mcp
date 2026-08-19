"""二十审（2026-08-19）care-mcp 回归：审查 BUG-1~BUG-11 修复固化。

覆盖（对应用户回归清单 1/3/4/5/6/7/8/11/12/13/14/15/16/17/18/19/20；第 2 项
cs2 已覆盖、第 10 项在 a207-policy pb2）：
- BUG-1：非条件冲突 OTSClientError 不得返回 DUPLICATE（继续 raise）
- BUG-2/4：家长看不到 created_by / note_to_clinician
- BUG-3：records/plans/adherence 为 "{}"/"123" 时 fail-closed（读取端）
- BUG-5：source_event dict/list/数字/超长 → INVALID_INPUT
- BUG-6：异常 escalated_history 对家长隐藏
- BUG-7：plans 超过上限不无限增长
- BUG-8：notification_id 超 128 → INVALID_INPUT
- BUG-9：分页游标不推进 → RuntimeError
- BUG-10：schedule_followup 同请求重试幂等（不创建第二个计划）
- BUG-11：plans 按 next_due_date/anchor_date 稳定排序

pytest + 直接运行双模式（CI 逐文件 `python tests/test_*.py`，不依赖 pytest）。
"""
import os

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from a207_policy import as_caller  # noqa: E402

from CKDNutri_care_mcp import core  # noqa: E402
from CKDNutri_care_mcp import repository as repo_mod  # noqa: E402


def _reset_store() -> None:
    """JSON 后端隔离：独立数据目录（随访/通知/令牌库）+ 重置 repo 缓存。"""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="cs3_store_")
    os.environ["A207_NOTIFICATION_DATA_DIR"] = tmp
    os.environ["A207_FOLLOWUP_DATA_DIR"] = tmp
    os.environ["A207_GUARDIAN_TOKEN_DIR"] = tmp
    repo_mod._REPO_CACHE.clear()


def _parent_token(patient_id: str) -> str:
    """模拟已签发监护人令牌（写令牌库 JSON；签发逻辑在 clinical-data，测试不跨包 import）。"""
    import json as _json
    from datetime import datetime, timedelta, timezone

    from a207_policy import resolve_state_path

    tok = f"tok-{patient_id}-{abs(hash(patient_id)) % 100000}"
    path = resolve_state_path("guardian_tokens.json",
                              base=os.environ.get("A207_GUARDIAN_TOKEN_DIR"))
    store = {}
    if path.exists():
        store = _json.loads(path.read_text(encoding="utf-8"))
    store[patient_id] = {
        "token": tok,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(store, ensure_ascii=False), encoding="utf-8")
    return tok


# ---- BUG-1：非条件冲突不得误判 DUPLICATE ----

def test_non_condition_conflict_raises_not_duplicate():
    """BUG-1：非条件冲突 OTSClientError（网络/鉴权/表不存在）必须继续 raise。"""

    from a207_policy.storage import is_condition_conflict

    from CKDNutri_care_mcp.repository import TablestoreRepository

    class _FakeClient:
        """最小 fake：put_row 抛非条件冲突 OTSClientError（模拟网络错误）。"""

        def put_row(self, table, row, condition):
            from tablestore import OTSClientError
            raise OTSClientError("connection reset by peer")

        def get_row(self, table, pk):  # pragma: no cover
            return None, None, None

        def list_table(self):  # pragma: no cover
            return []

        def get_range(self, *a, **k):  # pragma: no cover
            return None, None, [], None

    repo = TablestoreRepository(client=_FakeClient())
    raised = False
    try:
        repo.save_notification_expect_not_exist(
            "N" + "A" * 24, {"id": "N" + "A" * 24, "workflow_status": "unacked"})
    except Exception:  # noqa: BLE001 - 非条件冲突必须抛
        raised = True
    assert raised, "非条件冲突 OTSClientError 不得被吞成 DUPLICATE"
    # 条件冲突判定本身：真冲突 → True，网络错误 → False
    assert is_condition_conflict(_FakeOTSError("ConditionCheck failed")) is True
    assert is_condition_conflict(_FakeOTSError("connection reset")) is False


class _FakeOTSError(Exception):
    """模拟 OTSClientError 的 code/message 属性（测试条件冲突判定）。"""

    def __init__(self, message):
        self.code = ""
        self.message = message
        super().__init__(message)


# ---- BUG-2：家长看不到 created_by / note_to_clinician ----

def test_parent_cannot_see_plan_clinician_fields():
    """BUG-2：家长看随访计划不得含 created_by / note_to_clinician（与 record 同口径）。"""
    _reset_store()
    with as_caller("doctor_assistant"):
        r = core.schedule_followup("P0020", "G3a", "A1", "outpatient", "2026-08-01",
                                   plan_summary="三月后复查", note_to_clinician="注意血压")
    assert r["ok"] is True, r
    tok = _parent_token("P0020")
    with as_caller("parent_assistant"):
        data = core.get_followup_records("P0020", guardian_token=tok)["data"]
    assert data["plans"], data
    plan = data["plans"][0]
    assert "created_by" not in plan, plan
    assert "note_to_clinician" not in plan, plan
    assert plan["plan_summary"] == "三月后复查", plan  # 可见字段保留
    # 临床角色仍可见
    with as_caller("doctor_assistant"):
        data2 = core.get_followup_records("P0020")["data"]
    assert data2["plans"][0].get("created_by") == "doctor_assistant", data2["plans"][0]
    assert "note_to_clinician" in data2["plans"][0], data2["plans"][0]


# ---- BUG-3：Tablestore 随访 JSON 非 list fail-closed ----

def test_followup_json_non_list_fail_closed():
    """BUG-3：records/plans/adherence 存储为 {} / 123 时读取必须抛错（不静默进业务层）。"""

    from CKDNutri_care_mcp.repository import TablestoreRepository

    class _FakeClient:
        def __init__(self, attrs):
            self._attrs = attrs

        def get_row(self, table, pk):
            class _R:
                attribute_columns = [(k, v, 0) for k, v in self._attrs.items()]
            return None, _R(), None

        def put_row(self, table, row, condition):  # pragma: no cover
            pass

        def list_table(self):  # pragma: no cover
            return []

        def get_range(self, *a, **k):  # pragma: no cover
            return None, None, [], None

    for bad_json in ("{}", "123", "null", '"hello"'):
        repo = TablestoreRepository(client=_FakeClient({
            "records": bad_json, "plans": bad_json, "adherence": bad_json}))
        raised = False
        try:
            repo.load_followup("P0001")
        except RuntimeError:
            raised = True
        assert raised, f"records/plans/adherence={bad_json} 应 fail-closed 抛 RuntimeError"
    # 非法 JSON 串同样抛错（存量行为）
    repo = TablestoreRepository(client=_FakeClient({"records": "{broken"}))
    raised = False
    try:
        repo.load_followup("P0001")
    except RuntimeError:
        raised = True
    assert raised, "损坏 JSON 应抛 RuntimeError"


# ---- BUG-5：source_event 类型/长度校验 ----

def test_source_event_invalid_types_rejected():
    """BUG-5：source_event 为 dict/list/数字 → INVALID_INPUT。"""
    _reset_store()
    for bad in ({}, [], 123, 4.5):
        r = core.create_notification("P0021", "followup_due", "medium", "t", "b",
                                     due_at="2026-08-22", source_event=bad)
        assert r["ok"] is False and r["error"] == "INVALID_INPUT", (bad, r)
    # 超长
    r = core.create_notification("P0021", "followup_due", "medium", "t", "b",
                                 due_at="2026-08-22", source_event="x" * 201)
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    # 合法字符串 / 空串按 None（手动通知，随机 id）
    r2 = core.create_notification("P0021", "followup_due", "medium", "t", "b",
                                  due_at="2026-08-22", source_event="evt#1")
    assert r2["ok"] is True and r2["data"]["notification"]["source_event"] == "evt#1", r2
    r3 = core.create_notification("P0021", "followup_due", "medium", "t", "b",
                                  due_at="2026-08-22", source_event="   ")
    assert r3["ok"] is True and r3["data"]["notification"]["source_event"] is None, r3


# ---- BUG-6：异常 escalated_history 对家长隐藏 ----

def test_abnormal_escalated_history_hidden_from_parent():
    """BUG-6：escalated_history 顶层非 list / 损坏时，家长视角直接删除该字段。"""
    _reset_store()
    from CKDNutri_care_mcp.core import _mask_notification

    rec = {"id": "N1", "escalated_history": '{"by": "doctor"}'}  # 合法 JSON 但非 list
    masked = _mask_notification(rec, "parent_assistant")
    assert "escalated_history" not in masked, masked  # fail-closed 删除
    rec2 = {"id": "N2", "escalated_history": "{broken"}
    masked2 = _mask_notification(rec2, "parent_assistant")
    assert "escalated_history" not in masked2, masked2
    # 合法 list 正常脱敏（by 剥除、id/at/reason 保留）
    rec3 = {"id": "N3", "escalated_history": '[{"by": "doctor", "at": "2026-08-01", "reason": "x"}]'}
    masked3 = _mask_notification(rec3, "parent_assistant")
    assert masked3["escalated_history"] == [{"at": "2026-08-01", "reason": "x"}], masked3


# ---- BUG-7：plans 数量上限 ----

def test_plans_capped_not_unbounded():
    """BUG-7：plans 超过上限只保留最新（用测试小值验证截断逻辑）。"""
    _reset_store()
    core._FOLLOWUP_PLAN_CAP = 3  # 测试用小上限（原 10_000）
    try:
        with as_caller("doctor_assistant"):
            for i in range(5):
                core.schedule_followup("P0022", "G3a", "A1", "outpatient",
                                       f"2026-08-0{i + 1}", plan_summary=f"计划{i}")
        tok = _parent_token("P0022")
        with as_caller("parent_assistant"):
            data = core.get_followup_records("P0022", guardian_token=tok)["data"]
        assert len(data["plans"]) == 3, data["plans"]
    finally:
        core._FOLLOWUP_PLAN_CAP = 10_000  # 还原


# ---- BUG-8：notification_id 长度 ----

def test_notification_id_length_limit():
    """BUG-8：notification_id 超过 128 字符 → INVALID_INPUT（三个入口一致）。"""
    _reset_store()
    long_id = "N" * 129
    assert core.ack_notification(long_id)["error"] == "INVALID_INPUT"
    assert core.update_notification_status(long_id, "confirmed")["error"] == "INVALID_INPUT"
    assert core.escalate_notification(long_id)["error"] == "INVALID_INPUT"
    # 正常 id 走原逻辑（NOT_FOUND 而非 INVALID_INPUT）
    assert core.ack_notification("N" * 64)["error"] == "NOT_FOUND"


# ---- BUG-9：分页游标不推进 ----

def test_range_all_pagination_stall_raises():
    """BUG-9：GetRange 分页游标不推进时 _range_all 必须抛 RuntimeError（防死循环）。"""
    from CKDNutri_care_mcp.repository import TablestoreRepository

    class _StallClient:
        def __init__(self):
            self.calls = 0

        def get_range(self, table, direction, start, end, limit=200):
            self.calls += 1
            return 0, start, [], None  # next_start 恒等于 start → 不推进

        def get_row(self, *a, **k):  # pragma: no cover
            return None, None, None

        def put_row(self, *a, **k):  # pragma: no cover
            pass

        def list_table(self):  # pragma: no cover
            return []

    repo = TablestoreRepository(client=_StallClient())
    raised = False
    try:
        repo._range_all("notification_store", ["notification_id"])
    except RuntimeError:
        raised = True
    assert raised, "分页游标不推进应抛 RuntimeError（拒绝无限循环）"


# ---- BUG-10：schedule_followup 幂等 ----

def test_schedule_followup_idempotent_retry():
    """BUG-10：同请求重试（同患者/基准日/内容/操作者）返回已有计划，不创建第二个。"""
    _reset_store()
    with as_caller("doctor_assistant"):
        r1 = core.schedule_followup("P0023", "G3a", "A1", "outpatient", "2026-08-01",
                                    plan_summary="复查", note_to_clinician="备注")
        assert r1["ok"] is True, r1
        r2 = core.schedule_followup("P0023", "G3a", "A1", "outpatient", "2026-08-01",
                                    plan_summary="复查", note_to_clinician="备注")
        assert r2["ok"] is True and r2["data"].get("idempotent_hit") is True, r2
        assert r2["data"]["plan"]["plan_id"] == r1["data"]["plan"]["plan_id"], "重试应返回同一计划"
        # 内容不同 → 创建新计划（不误拦截）
        r3 = core.schedule_followup("P0023", "G3a", "A1", "outpatient", "2026-08-01",
                                    plan_summary="复查改期", note_to_clinician="备注")
        assert r3["ok"] is True and r3["data"].get("idempotent_hit") is None, r3
    tok = _parent_token("P0023")
    with as_caller("parent_assistant"):
        data = core.get_followup_records("P0023", guardian_token=tok)["data"]
    assert len(data["plans"]) == 2, data["plans"]


# ---- BUG-11：plans 稳定排序 ----

def test_plans_sorted_by_next_due_date():
    """BUG-11：plans 按 next_due_date 升序（anchor_date 兜底），不依赖追加序。"""
    _reset_store()
    with as_caller("doctor_assistant"):
        core.schedule_followup("P0024", "G3a", "A1", "outpatient", "2026-09-10",
                               plan_summary="远期")
        core.schedule_followup("P0024", "G3a", "A1", "outpatient", "2026-08-10",
                               plan_summary="近期")
        core.schedule_followup("P0024", "G3a", "A1", "outpatient", "2026-08-20",
                               plan_summary="中期")
    tok = _parent_token("P0024")
    with as_caller("parent_assistant"):
        data = core.get_followup_records("P0024", guardian_token=tok)["data"]
    dates = [(p["cadence"]["anchor_date"], p["plan_summary"]) for p in data["plans"]]
    assert dates == sorted(dates), dates  # 按 anchor_date（next_due_date 同序）升序


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"P3 CS3 REGRESSION OK（{len(fns)} 个用例）")
