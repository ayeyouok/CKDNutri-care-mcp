"""P3 冒烟自测：导入 server 不报错 + 通知闭环可跑通。

运行：pytest tests/test_import_smoke.py  (或 python tests/test_import_smoke.py)
依赖：a207-policy 已随 pip install -e . 安装。
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("A207_CALLER", "doctor_assistant")
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
        patient_id="P001", category="followup_due",
        priority="high", title="随访到期", body="P001 需复查",
    )
    assert created.get("ok") is True
    got = core.get_notifications(patient_id="P001")
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
        json.dumps({"P001": {
            "token": tok,
            "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "issued_by": "doctor_assistant",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds"),
        }}), encoding="utf-8")

    with a207_policy.as_caller("doctor_assistant"):
        nid = core.create_notification("P001", "followup_due", "high", "t", "b")["data"]["notification"]["id"]

    with a207_policy.as_caller("parent_assistant"):
        # 无 token → GUARDIAN_UNVERIFIED
        for fn, args in ((core.get_followup_records, ("P001",)),
                         (core.get_notifications, ("P001",)),
                         (core.get_pew_timeline, ("P001",))):
            r = fn(*args)
            assert r.get("ok") is False and r.get("error") == "GUARDIAN_UNVERIFIED", r
        r = core.ack_notification(nid)
        assert r.get("ok") is False and r.get("error") == "GUARDIAN_UNVERIFIED", r
        # 错误 token → FORBIDDEN
        r = core.get_followup_records("P001", guardian_token="wrong-token")
        assert r.get("ok") is False and r.get("error") == "FORBIDDEN", r
        # 正确 token → 放行（受限摘要视图）
        r = core.get_followup_records("P001", guardian_token=tok)
        assert r.get("ok") is True and r["data"]["visibility"] == "summary_only", r
        r = core.ack_notification(nid, guardian_token=tok)
        assert r.get("ok") is True and r["data"]["notification"]["status"] == "acked", r


if __name__ == "__main__":
    test_server_imports()
    test_notification_lifecycle()
    test_parent_guardian_binding()
    print("P3 SMOKE OK")
