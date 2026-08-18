# -*- coding: utf-8 -*-
"""C-S1/C-S2/C-B3/C-B4/C-B7/D1 回归测试（2026-08-14 修复后固化）。pytest + 直接运行双模式。"""
import os
os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_POLICY = Path(__file__).resolve().parents[1].parents[1] / "a207-policy" / "src"
for p in (_SRC, _POLICY):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("A207_CALLER", "doctor_assistant")
os.environ.setdefault("A207_STORAGE_BACKEND", "json")


def test_c_s1_partial_update_preserves_fields():
    """C-S1：部分更新语义——escalate 后并发者字段/标题保留（不再整行覆盖）。"""
    from CKDNutri_care_mcp import core

    n = core.create_notification("P0001", "followup_due", "medium", "标题", "正文",
                                 due_at="2026-08-05")
    nid = n["data"]["notification"]["id"]
    core.escalate_notification(nid, "临床升级")
    rec = core.get_notifications("P0001")["data"]["notifications"]
    target = [x for x in rec if x["id"] == nid][0]
    assert target.get("escalated") is True, target
    assert target.get("title") == "标题", "部分更新不应丢失未变更字段"
    assert target.get("workflow_status") == "unacked", "escalate 不应改变 workflow_status"


def test_c_s2_due_at_full_validation():
    """C-S2：due_at 完整校验——"2026-08-01garbage" 拒绝（[:10] 截断绕过已堵）。"""
    from CKDNutri_care_mcp import core

    r = core.create_notification("P0001", "followup_due", "high", "t", "b",
                                 due_at="2026-08-01garbage")
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    r2 = core.create_notification("P0001", "followup_due", "high", "t", "b",
                                  due_at="2026-08-01")
    assert r2["ok"] is True, r2


def test_c_b7_category_enum():
    """C-B7：category 枚举校验（与 priority 对称）；非法值拒绝。"""
    from CKDNutri_care_mcp import core

    r = core.create_notification("P0001", "random_cat", "high", "t", "b")
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    r2 = core.create_notification("P0001", "report_ready", "high", "t", "b")
    assert r2["ok"] is True, r2


def test_c_b7_ack_none_rejected():
    """C-B7：ack_notification None/空 id 拒绝（不穿透到存储层）。"""
    from CKDNutri_care_mcp import core

    r = core.ack_notification(None)
    assert r["ok"] is False, r
    r2 = core.ack_notification("   ")
    assert r2["ok"] is False, r2


def test_c_b3_deserialize_corrupt_raises():
    """C-B3：Tablestore 损坏 JSON 抛错（fail-closed，不静默清空致数据永久丢失）。

    P3-2（2026-08-15）：数据损坏改抛 RuntimeError（服务端存储问题 → INTERNAL_ERROR +
    脱敏）——此前 ValueError 被 server 归 INVALID_INPUT（客户端错误码），与 P4 #8
    规则库损坏同口径修正。测试同时接受两者（历史断言兼容）。
    """
    from CKDNutri_care_mcp import repository as repo

    try:
        repo.TablestoreRepository._deserialize_followup({"records": "{broken"})
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("损坏 JSON 应抛异常（静默 [] 会致 save 覆盖丢数据）")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"P3 C-S1/C-S2/C-B3/C-B4/C-B7/D1 REGRESSION OK（{len(fns)} 个用例）")


def test_d1_parent_notification_masked():
    """D1：家长视角通知裁剪医生内部字段（resolution_note/escalation_reason 等）。

    直测 _mask_notification（get_notifications / ack 共用的裁剪函数）——
    家长读路径需 guardian_token 才能过 _guard_guardian，用 token 链路会使本测试
    依赖 P1 签发，故直接验证裁剪函数本体（同一 _CLINICIAN 判定）。
    """
    from CKDNutri_care_mcp.core import _mask_notification, _NOTIF_CLINICIAN_ONLY

    rec = {"id": "N-1", "patient_id": "P0001", "title": "标题", "status": "unacked",
           "workflow_status": "resolved", "escalated": True,
           "resolution_note": "医生处置备注-保密", "escalation_reason": "升级理由-保密",
           "status_updated_by": "doctor_assistant", "escalated_by": "doctor_assistant"}
    # 家长视角：医生内部字段全剥离，核心字段保留
    masked = _mask_notification(rec, "parent_assistant")
    for key in _NOTIF_CLINICIAN_ONLY:
        assert key not in masked, f"家长视角泄露 {key}"
    assert masked.get("id") and masked.get("title") and masked.get("workflow_status"), "核心字段应保留"
    # 临床视角：原样返回
    full = _mask_notification(rec, "doctor_assistant")
    assert "resolution_note" in full and "escalated_by" in full, "医生视角应保留"
    # 幂等：家长裁剪后再次裁剪无变化
    assert _mask_notification(masked, "parent_assistant") == masked


def test_p01_event_notification_idempotent_dedup():
    """P0-1（2026-08-18）：事件驱动通知确定性主键 + 条件插入——跨 Worker 并发
    创建同事件（同 patient/category/source_event/due_at）只落一行，重复创建
    DUPLICATE；不同 due_at（重排）生成不同主键放行；手动通知（无 source_event）
    维持随机 id 不幂等。"""
    import tempfile

    from CKDNutri_care_mcp import core

    # 隔离：JSON 后端持久化会让确定性主键在重复 pytest 运行间残留（第二次运行
    # 首次创建即撞 DUPLICATE，测试不幂等）——通知库指到独立临时目录。
    os.environ["A207_NOTIFICATION_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-care-test-")

    # 同事件同 due_at 两次创建：第一次成功、第二次 DUPLICATE，且 id 确定性一致
    a = core.create_notification("P0001", "followup_due", "high", "随访到期",
                                 "请及时预约", due_at="2026-09-01",
                                 source_event="followup_due#2026-09-01")
    assert a["ok"] is True, a
    nid = a["data"]["notification"]["id"]
    b = core.create_notification("P0001", "followup_due", "high", "随访到期",
                                 "请及时预约", due_at="2026-09-01",
                                 source_event="followup_due#2026-09-01")
    assert b["ok"] is False and b["error"] == "DUPLICATE", b
    # 重排（due_at 变化）→ 新主键，放行
    c = core.create_notification("P0001", "followup_due", "high", "随访到期",
                                 "请及时预约", due_at="2026-09-15",
                                 source_event="followup_due#2026-09-01")
    assert c["ok"] is True, c
    assert c["data"]["notification"]["id"] != nid, "due_at 变化应生成新主键"
    # 手动通知（无 source_event）→ 随机 id，两次创建均成功（不幂等）
    m1 = core.create_notification("P0001", "report_ready", "medium", "报告", "b")
    m2 = core.create_notification("P0001", "report_ready", "medium", "报告", "b")
    assert m1["ok"] is True and m2["ok"] is True, (m1, m2)
    assert m1["data"]["notification"]["id"] != m2["data"]["notification"]["id"]


def test_p01_pew_utc_normalize():
    """P0-2（2026-08-18）：_parse_pew_date 对带时区 ISO 串归一化到 UTC——
    '2024-01-10T08:30:00+08:00'（=UTC 00:30）与 '2024-01-10T00:30:00Z' 解析结果
    必须相等，跨时区时间线排序不颠倒。"""
    from CKDNutri_care_mcp.core import _parse_pew_date

    d1 = _parse_pew_date("2024-01-10T08:30:00+08:00")
    d2 = _parse_pew_date("2024-01-10T00:30:00Z")
    assert d1 == d2, (d1, d2)
    assert d1.tzinfo is None, "应返回 naive（UTC 归一化后剥时区）"
    # naive 输入（无时区语义）原样返回墙钟时间，不做位移——断言墙钟与 tzinfo
    from datetime import datetime

    d3 = _parse_pew_date("2024-01-10T08:30:00")
    assert d3 == datetime(2024, 1, 10, 8, 30) and d3.tzinfo is None, d3


def test_p21_adherence_ratio_type_guard():
    """P2-1（2026-08-18）：calc_adherence_score 比率类型保护——bool（True 可过
    0<=x<=1）与字符串（比较 TypeError）一律 INVALID_INPUT 信封，不 500。"""
    from CKDNutri_care_mcp import core

    assert core.calc_adherence_score(True, 0.5, 0.5)["ok"] is False
    r = core.calc_adherence_score("0.5", 0.5, 0.5)
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    assert core.calc_adherence_score(0.8, 0.7, 0.9)["ok"] is True


def test_reopen_after_closed_same_event():
    """P0-1 衍生（2026-08-18）：确定性主键与 closed 重开语义冲突修复——事件闭环
    （closed 终态）后，同事件同 due_at 重新创建必须成功（新实例主键 -R<id>），
    不被 EXPECT_NOT_EXIST 误判 DUPLICATE 阻塞。"""
    import tempfile

    from CKDNutri_care_mcp import core

    os.environ["A207_NOTIFICATION_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-care-test-")
    ev = "reopen#2026-10-01"
    a = core.create_notification("P0001", "followup_due", "high", "随访到期",
                                 "正文", due_at="2026-10-01", source_event=ev)
    assert a["ok"] is True, a
    nid = a["data"]["notification"]["id"]
    # 走到 closed 终态（unacked→confirmed→resolved→closed）
    assert core.update_notification_status(nid, "confirmed")["ok"] is True
    assert core.update_notification_status(nid, "resolved", "已处置")["ok"] is True
    assert core.update_notification_status(nid, "closed")["ok"] is True
    # 同事件同 due_at 重开 → 成功，新实例 id 不同（-R 后缀）
    b = core.create_notification("P0001", "followup_due", "high", "随访到期",
                                 "正文", due_at="2026-10-01", source_event=ev)
    assert b["ok"] is True, b
    assert b["data"]["notification"]["id"] != nid, "closed 后重开应生成新实例"


def test_due_at_canonical_dedup():
    """P0 衍生 2（2026-08-18）：due_at 规范化去重——同一时刻的不同字面
    （2026-08-20T00:00:00Z vs 2026-08-20T08:00:00+08:00）必须命中同一去重键，
    第二次创建 DUPLICATE（不再生成两条重复通知）。"""
    import tempfile

    from CKDNutri_care_mcp import core

    os.environ["A207_NOTIFICATION_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-care-test-")
    ev = "canon#due"
    a = core.create_notification("P0001", "followup_due", "high", "t", "b",
                                 due_at="2026-08-20T00:00:00Z", source_event=ev)
    assert a["ok"] is True, a
    b = core.create_notification("P0001", "followup_due", "high", "t", "b",
                                 due_at="2026-08-20T08:00:00+08:00", source_event=ev)
    assert b["ok"] is False and b["error"] == "DUPLICATE", b


def test_escalated_history_corrupt_fail_closed():
    """问题 4（2026-08-18）：escalated_history 损坏（非法 JSON）时 escalate 必须
    抛 RuntimeError（拒绝覆盖审计历史），不再静默置 [] 覆盖写。"""
    import tempfile

    from CKDNutri_care_mcp import core
    from CKDNutri_care_mcp.repository import get_repository

    os.environ["A207_NOTIFICATION_DATA_DIR"] = tempfile.mkdtemp(prefix="a207-care-test-")
    n = core.create_notification("P0001", "followup_due", "medium", "t", "b")
    nid = n["data"]["notification"]["id"]
    # 直接写坏 escalated_history（JSON 字符串损坏）
    repo = get_repository()
    repo.save_notification(nid, {"escalated_history": "{broken json"})
    try:
        core.escalate_notification(nid, "升级理由")
    except RuntimeError:
        pass
    else:
        raise AssertionError("损坏的 escalated_history 未被 fail-closed 拒绝")


# ---- 十七审（2026-08-18）：P1-3 文本长度限制 / P2-4 PEW 峰值 / P2-5 日志脱敏 ----


def test_p13_text_length_limits():
    """P1-3：title/body/plan_summary/doctor_notes/resolution_note 超限拒绝（LLM 防护）。"""
    from CKDNutri_care_mcp import core

    r = core.create_notification("P0010", "risk_alert", "high",
                                 "t" * 201, "body")
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    r = core.create_notification("P0010", "risk_alert", "high",
                                 "title", "b" * 2001)
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    assert core.create_notification("P0010", "risk_alert", "high",
                                    "title", "body")["ok"] is True

    r = core.schedule_followup("P0010", "CKD4", "A2", "outpatient",
                               "2026-09-15", plan_summary="x" * 2001)
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r

    r = core.add_followup_record("P0010", "2026-08-18", "outpatient",
                                 "CKD4", {}, "", doctor_notes="n" * 5001)
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r


def test_p24_pew_peak_risk():
    """P2-4：low→high→low 首末同档 trend=stable 但 peak_risk 必须报 high（漏报修复）。"""
    from CKDNutri_care_mcp import core

    r = core.get_pew_timeline("P0010", pew_history=[
        {"date": "2026-08-01", "level": "low"},
        {"date": "2026-08-10", "level": "high"},
        {"date": "2026-08-18", "level": "low"},
    ])
    assert r["ok"] is True, r
    d = r["data"]
    assert d["trend"] == "stable", d          # 首末 low==low
    assert d["peak_risk"] == "high", d        # 中间峰值不丢
    assert d["recent_trend"] == "improving", d  # 最近两点 high→low


def test_p25_mask_pk():
    """P2-5：a207_policy.storage._mask_pk 掩码明文主键（患者 id 不落日志）。"""
    from a207_policy.storage import _mask_pk

    assert _mask_pk([("patient_id", "P0010")]) == "patient_id=P001***"
    assert _mask_pk([("patient_id", "P0010"), ("sample_id", "P0010-Sabc1234")]) == \
        "patient_id=P001***,sample_id=P001***"
    assert "P0010" not in _mask_pk([("patient_id", "P0010")])
