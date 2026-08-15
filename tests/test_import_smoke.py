"""P3 冒烟自测：导入 server 不报错 + 通知闭环可跑通。

运行：pytest tests/test_import_smoke.py  (或 python tests/test_import_smoke.py)
依赖：a207-policy 已随 pip install -e . 安装。
"""
from __future__ import annotations

import importlib
import os
os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("A207_CALLER", "doctor_assistant")
# v0.6（2026-08-13）：存储默认 Tablestore（生产）；测试显式用 json 后端（LocalJson，
# 与旧行为一致），Tablestore 后端语义由 test_repository_backend 覆盖。
os.environ.setdefault("A207_STORAGE_BACKEND", "json")
# 把通知存储指向临时目录，避免污染仓库产物
_TMP = tempfile.mkdtemp(prefix="a207-care-test-")
os.environ["A207_NOTIFICATION_DATA_DIR"] = _TMP

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_server_imports():
    """导入 server 不可抛错（回归：合并时通知段引用未定义符号 _WRITE_ROLES 等）。"""
    mod = importlib.import_module("CKDNutri_care_mcp.server")
    assert mod.mcp is not None


def test_notification_lifecycle():
    from CKDNutri_care_mcp import core

    created = core.create_notification(
        patient_id="P0001", category="followup_due",
        priority="high", title="随访到期", body="P001 需复查",
    )
    assert created.get("ok") is True
    got = core.get_notifications(patient_id="P0001")
    assert got["data"]["count"] >= 1
    nid = got["data"]["notifications"][0]["id"]
    assert core.ack_notification(nid).get("ok") is True
    # 严格一步流转：unacked→confirmed→resolved(需 note)→closed（BUG-09/10 修复后）
    assert core.update_notification_status(nid, "confirmed").get("ok") is True
    assert core.update_notification_status(nid, "resolved", "已处理").get("ok") is True
    assert core.update_notification_status(nid, "closed").get("ok") is True
    # 终态 closed 后不可回退/跳转（INVALID_TRANSITION）；acked 非 workflow 态（INVALID_STATUS）
    skip = core.update_notification_status(nid, "resolved")
    assert skip.get("ok") is False and skip.get("error") == "INVALID_TRANSITION"
    reopen = core.update_notification_status(nid, "acked")
    assert reopen.get("ok") is False and reopen.get("error") == "INVALID_STATUS"


def test_parent_guardian_binding():
    """BUG-40 回归锁定：家长读随访/通知/PEW/ack 必须带匹配 guardian_token。

    令牌状态库格式与 P1 his.issue_guardian_token 一致（A207_GUARDIAN_TOKEN_DIR/
    guardian_tokens.json），由 a207_policy.verify_guardian_token 统一校验（BUG-36）。
    本测试手工构造令牌文件，不跨包 import，避免对其他包的位置依赖。
    """
    import json
    import secrets
    from datetime import datetime, timedelta, timezone

    import a207_policy
    from CKDNutri_care_mcp import core

    # 令牌状态库路径与 a207_policy.verify_guardian_token 读取口径一致（BUG-36）
    os.environ["A207_GUARDIAN_TOKEN_DIR"] = _TMP

    tok = secrets.token_urlsafe(32)
    store_dir = Path(_TMP)
    (store_dir / "guardian_tokens.json").write_text(
        json.dumps({"P0001": {
            "token": tok,
            "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "issued_by": "doctor_assistant",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds"),
        }}), encoding="utf-8")

    with a207_policy.as_caller("doctor_assistant"):
        nid = core.create_notification("P0001", "followup_due", "high", "t", "b")["data"]["notification"]["id"]

    with a207_policy.as_caller("parent_assistant"):
        # 无 token → GUARDIAN_UNVERIFIED
        for fn, args in ((core.get_followup_records, ("P0001",)),
                         (core.get_notifications, ("P0001",)),
                         (core.get_pew_timeline, ("P0001",))):
            r = fn(*args)
            assert r.get("ok") is False and r.get("error") == "GUARDIAN_UNVERIFIED", r
        r = core.ack_notification(nid)
        assert r.get("ok") is False and r.get("error") == "GUARDIAN_UNVERIFIED", r
        # 错误 token → FORBIDDEN
        r = core.get_followup_records("P0001", guardian_token="wrong-token")
        assert r.get("ok") is False and r.get("error") == "FORBIDDEN", r
        # 正确 token → 放行（受限摘要视图）
        r = core.get_followup_records("P0001", guardian_token=tok)
        assert r.get("ok") is True and r["data"]["visibility"] == "summary_only", r
        r = core.ack_notification(nid, guardian_token=tok)
        assert r.get("ok") is True and r["data"]["notification"]["status"] == "acked", r


def test_visit_date_validation():
    """C/D（2026-08-12 三审）回归：visit_date 格式/未来日期 fail-closed。"""
    from CKDNutri_care_mcp import core

    def _raises(fn, label):
        try:
            fn()
        except ValueError:
            return
        raise AssertionError(f"期望 {label} 抛 ValueError")

    # 非法格式：schedule_followup（计划基准日期）拒绝非 ISO 格式
    _raises(lambda: core.schedule_followup("P0001", "G3a", "A2", "outpatient", "2026/08/01"),
            "schedule_followup 非法日期格式")
    _raises(lambda: core.schedule_followup("P0001", "G3a", "A2", "outpatient", "20260801"),
            "schedule_followup 非 ISO 格式")
    # 未来日期：add_followup_record（实际就诊记录）拒绝
    from datetime import date, timedelta
    future = (date.today() + timedelta(days=7)).isoformat()
    _raises(lambda: core.add_followup_record("P0001", future, "outpatient", "G3a", {},
                                             "plan"), "add_followup_record 未来日期")
    # 合法日期不误拦
    r = core.schedule_followup("P0001", "G3a", "A2", "outpatient", date.today().isoformat())
    assert r.get("ok") is True, r


def test_adherence_weights_validation():
    """H（2026-08-12 三审）回归：weights 元素级数值/有限性校验（core 直调防线）。

    core 契约：校验失败返回 {ok:false, error:INVALID_INPUT} 信封（不抛异常）。
    """
    from CKDNutri_care_mcp import core

    def _invalid(fn, label):
        r = fn()
        assert r.get("ok") is False and r.get("error") == "INVALID_INPUT", f"{label}: {r}"

    _invalid(lambda: core.calc_adherence_score(0.8, 0.9, 0.7,
                                               weights=(1 / 3, 1 / 3, float("nan"))),
             "weights 含 NaN")
    _invalid(lambda: core.calc_adherence_score(0.8, 0.9, 0.7,
                                               weights=(1 / 3, 1 / 3, float("inf"))),
             "weights 含 Inf")
    _invalid(lambda: core.calc_adherence_score(0.8, 0.9, 0.7, weights=(True, 0.0, 0.0)),
             "weights 含 bool")
    # 合法权重不误拦
    r = core.calc_adherence_score(0.8, 0.9, 0.7)
    assert r.get("ok") is True and r["data"]["composite_score"] >= 0


def test_store_missing_keys_defensive():
    """A（2026-08-12 三审）回归：store 缺 plans 键不崩溃（.get() 防御）。"""
    import tempfile

    from CKDNutri_care_mcp import core

    tmp = tempfile.mkdtemp(prefix="a207-care-keys-")
    os.environ["A207_FOLLOWUP_DATA_DIR"] = tmp
    try:
        # 手工构造缺 "plans" 键的 store（模拟早期版本/脏数据）
        import json as _json
        (Path(tmp) / core.STORE_FILENAME).write_text(
            _json.dumps({"P0001": {"records": [{"record_id": "R1", "visit_date": "2026-08-01",
                                               "doctor_notes": "x"}]}}),
            encoding="utf-8")
        r = core.get_followup_records("P0001")
        assert r.get("ok") is True, r
        # 缺 plans 键不影响 records 读取（.get() 防御），临床角色可见完整记录
        assert len(r["data"]["records"]) == 1, r
        # 无数据分支 visibility 按临床角色返回 full（E）
        empty = core.get_followup_records("P9999")
        assert empty["data"]["visibility"] == "full", empty
    finally:
        os.environ.pop("A207_FOLLOWUP_DATA_DIR", None)


def test_repository_backend():
    """v0.6（2026-08-13）回归：后端语义——缺省 tablestore（缺参 fail-fast）+
    LocalJson 开发模式读写 + 损坏文件 fail-closed。"""
    import tempfile

    from CKDNutri_care_mcp import repository as repo_mod

    # 缺省后端 = tablestore（生产），缺 OTS 参数 → fail-fast（不静默回退）
    saved = os.environ.pop("A207_STORAGE_BACKEND", None)
    try:
        try:
            repo_mod.get_repository()
        except RuntimeError:
            pass
        else:
            raise AssertionError("缺省 tablestore 后端缺连接参数应 fail-fast")
    finally:
        if saved is not None:
            os.environ["A207_STORAGE_BACKEND"] = saved

    # 显式 json → LocalJson（开发模式）
    os.environ["A207_STORAGE_BACKEND"] = "json"
    try:
        repo = repo_mod.get_repository()
        assert isinstance(repo, repo_mod.LocalJsonRepository), type(repo)
    finally:
        if saved is not None:
            os.environ["A207_STORAGE_BACKEND"] = saved

    tmp = tempfile.mkdtemp(prefix="a207-care-repo-")
    os.environ["A207_FOLLOWUP_DATA_DIR"] = tmp
    os.environ["A207_NOTIFICATION_DATA_DIR"] = tmp
    try:
        # LocalJson 读写（随访 + 通知）
        assert repo.load_followup("P0001") is None
        repo.save_followup("P0001", {"records": [{"r": 1}], "plans": [], "adherence": []})
        assert repo.load_followup("P0001")["records"][0]["r"] == 1
        repo.save_notification("N1", {"id": "N1", "patient_id": "P0001", "status": "unacked"})
        assert repo.load_notification("N1")["status"] == "unacked"
        assert len(repo.all_notifications()) == 1
        # 损坏文件 fail-closed（防静默清空，B1 同口径）
        (Path(tmp) / repo_mod.FOLLOWUP_STORE_FILENAME).write_text("{broken", encoding="utf-8")
        try:
            repo.load_followup("P0001")
        except RuntimeError:
            pass
        else:
            raise AssertionError("损坏随访库应抛 RuntimeError")
    finally:
        os.environ.pop("A207_FOLLOWUP_DATA_DIR", None)
        os.environ.pop("A207_NOTIFICATION_DATA_DIR", None)


def test_s4_unauthorized_nan():
    """S4（2026-08-13）补全：越权（家长建通知）+ NaN 比率。"""
    from CKDNutri_care_mcp import core

    # ① 越权：create_notification 仅 {doctor, risk}（矩阵），家长必须 FORBIDDEN 信封
    os.environ["A207_CALLER"] = "parent_assistant"
    try:
        r = core.create_notification("P0001", "cat", "high", "标题", "内容")
    finally:
        os.environ["A207_CALLER"] = "doctor_assistant"
    assert r.get("ok") is False and r.get("error") == "FORBIDDEN", r

    # ② NaN：比率 NaN 拒绝（INVALID_INPUT 信封，不崩溃）
    r = core.calc_adherence_score(float("nan"), 0.5, 0.5)
    assert r.get("ok") is False and r.get("error") == "INVALID_INPUT", r


if __name__ == "__main__":
    test_server_imports()
    test_notification_lifecycle()
    test_parent_guardian_binding()
    test_visit_date_validation()
    test_adherence_weights_validation()
    test_store_missing_keys_defensive()
    test_repository_backend()
    test_s4_unauthorized_nan()
    print("P3 SMOKE OK")
