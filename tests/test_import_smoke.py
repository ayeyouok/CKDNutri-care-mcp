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
    assert got.get("count", 0) >= 1
    nid = got["notifications"][0]["id"]
    assert core.ack_notification(nid).get("ok") is True
    assert core.update_notification_status(nid, "confirmed").get("ok") is True
    assert core.update_notification_status(nid, "resolved").get("ok") is True
    assert core.update_notification_status(nid, "closed").get("ok") is True
    # 逆向流转必须被拒
    reopen = core.update_notification_status(nid, "acked")
    assert reopen.get("ok") is False and reopen.get("error") == "INVALID_TRANSITION"


if __name__ == "__main__":
    test_server_imports()
    test_notification_lifecycle()
    print("P3 SMOKE OK")
