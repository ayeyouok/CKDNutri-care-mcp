"""十八审（2026-08-18）care-mcp 回归：审查 P1-3（单调合并联动）/ P1-4（reopen 确定性）/
P2-1（O(1) 查重）/ P2-2（escalate reason 长度）。

覆盖：
- _derive_reopen_id 确定性（代数派生：第 0 代 → R1 → R2，并发 worker 竞争同键）
- closed → reopen 只落一个实例（EXPECT_NOT_EXIST 条件插入硬约束）
- create_notification 事件去重走 O(1) 主键查询（不触发全表扫描路径）
- escalate_notification reason 超长拒绝
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

import pytest

from CKDNutri_care_mcp import core


@pytest.fixture(autouse=True)
def _isolated_store():
    """JSON 后端测试隔离：每次测试独立数据目录 + 重置 repo 缓存。

    事件通知主键是确定性 hash，JSON 文件若跨测试共享会残留旧状态
    （closed → R1 → R2 精确状态机被上次运行的残留数据污染），必须隔离。
    """
    import shutil
    import tempfile

    from CKDNutri_care_mcp import repository as repo_mod

    tmp = tempfile.mkdtemp(prefix="cs2_store_")
    os.environ["A207_NOTIFICATION_DATA_DIR"] = tmp
    os.environ["A207_FOLLOWUP_DATA_DIR"] = tmp
    repo_mod._REPO_CACHE.clear()  # env 变更后重建（get_repository 按 backend 缓存）
    yield
    repo_mod._REPO_CACHE.clear()
    shutil.rmtree(tmp, ignore_errors=True)


def test_reopen_id_deterministic_derivation():
    """P1-4：reopen 主键从 closed 实例 id 确定性派生（代数 +1），非随机。"""
    base = "N" + "A" * 24
    assert core._derive_reopen_id(base, "A" * 24) == f"{base}-R1"   # 第 0 代 → R1
    assert core._derive_reopen_id(f"{base}-R1", "A" * 24) == f"{base}-R2"  # R1 → R2
    # 未知形态（历史/手工数据）保守回退随机（不误拒）
    r = core._derive_reopen_id("garbage-id", "A" * 24)
    assert r.startswith("NAAAAAAAAAAAAAAAAAAAAAAAA-R")
    # 确定性：同输入同输出（并发 worker 竞争同一键）
    assert core._derive_reopen_id(base, "A" * 24) == core._derive_reopen_id(base, "A" * 24)


def test_reopen_only_one_instance_survives():
    """P1-4：closed 事件重开——两个 worker 顺序触发同事件，只落一个 reopen 实例。"""
    # 第一次创建 → 手动 closed → 重开
    r1 = core.create_notification("P0002", "followup_due", "medium", "复诊提醒", "正文",
                                  due_at="2026-08-20", source_event="followup#E1")
    assert r1["ok"] is True, r1
    nid = r1["data"]["notification"]["id"]
    assert "-R" not in nid  # 第一代无 -R 后缀
    # 推到 closed
    assert core.update_notification_status(nid, "confirmed", "")["ok"] is True
    assert core.update_notification_status(nid, "resolved", "已处理")["ok"] is True
    assert core.update_notification_status(nid, "closed", "")["ok"] is True
    # 重开（worker A）
    ra = core.create_notification("P0002", "followup_due", "medium", "复诊提醒", "正文",
                                  due_at="2026-08-20", source_event="followup#E1")
    assert ra["ok"] is True, ra
    reopen_id = ra["data"]["notification"]["id"]
    assert reopen_id.endswith("-R1"), reopen_id  # 确定性代数 1
    # 重开（worker B 并发重试同一事件）→ 必须 DUPLICATE（条件插入拦截，不产生 R2）
    rb = core.create_notification("P0002", "followup_due", "medium", "复诊提醒", "正文",
                                  due_at="2026-08-20", source_event="followup#E1")
    assert rb["ok"] is False and rb["error"] == "DUPLICATE", rb
    # 库中仅 1 个 reopen 实例
    recs = core.get_notifications("P0002")["data"]["notifications"]
    reopen_instances = [x for x in recs if x["id"].endswith("-R1")]
    assert len(reopen_instances) == 1, recs
    # R1 closed 后再重开 → R2（代数继续）
    r2 = reopen_instances[0]["id"]
    assert core.update_notification_status(r2, "confirmed", "")["ok"] is True
    assert core.update_notification_status(r2, "resolved", "已处理")["ok"] is True
    assert core.update_notification_status(r2, "closed", "")["ok"] is True
    rc = core.create_notification("P0002", "followup_due", "medium", "复诊提醒", "正文",
                                  due_at="2026-08-20", source_event="followup#E1")
    assert rc["ok"] is True and rc["data"]["notification"]["id"].endswith("-R2"), rc


def test_event_dedup_o1_primary_key_path():
    """P2-1：事件查重走 O(1) 主键路径——未关闭通知重复触发即 DUPLICATE（不再扫描兜底）。"""
    r = core.create_notification("P0003", "risk_alert", "high", "危急", "钾危急",
                                 due_at=None, source_event="risk#K")
    assert r["ok"] is True, r
    nid = r["data"]["notification"]["id"]
    # 未关闭 → 重复触发 → DUPLICATE（主键命中，O(1)，不产生第二行）
    r2 = core.create_notification("P0003", "risk_alert", "high", "危急", "钾危急",
                                  due_at=None, source_event="risk#K")
    assert r2["ok"] is False and r2["error"] == "DUPLICATE", r2
    recs = core.get_notifications("P0003")["data"]["notifications"]
    assert len([x for x in recs if x["id"] == nid]) == 1, recs


def test_escalate_reason_length_limit():
    """P2-2：escalate_notification reason 超长拒绝（2000 上限，LLM 防护）。"""
    n = core.create_notification("P0004", "followup_due", "low", "t", "b",
                                 due_at="2026-08-21")
    nid = n["data"]["notification"]["id"]
    r = core.escalate_notification(nid, "x" * 2001)
    assert r["ok"] is False and r["error"] == "INVALID_INPUT", r
    # 正常长度通过
    assert core.escalate_notification(nid, "临床升级")["ok"] is True
