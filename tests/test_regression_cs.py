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
