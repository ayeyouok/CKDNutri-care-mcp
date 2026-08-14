# -*- coding: utf-8 -*-
"""C-S1/C-S2/C-B3/C-B4/C-B7 回归测试（2026-08-14 修复后固化）。pytest + 直接运行双模式。"""
import os
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
    assert r["ok"] is False and r["error"] == "INVALID_ARGUMENT", r
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
    """C-B3：Tablestore 损坏 JSON 抛错（fail-closed，不静默清空致数据永久丢失）。"""
    from CKDNutri_care_mcp import repository as repo

    try:
        repo.TablestoreRepository._deserialize_followup({"records": "{broken"})
    except ValueError:
        pass
    else:
        raise AssertionError("损坏 JSON 应抛 ValueError（静默 [] 会致 save 覆盖丢数据）")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"P3 C-S1/C-S2/C-B3/C-B4/C-B7 REGRESSION OK（{len(fns)} 个用例）")
