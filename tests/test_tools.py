# -*- coding: utf-8 -*-
"""M4 (a207-followup-mcp) 纯 core 单测。不依赖 fastmcp，直接 import core。

P0-1：caller 由部署环境注入（A207_CALLER），故必须先注入身份再 import core，
否则所有工具 fail-closed 抛 CallerUnknown。用例需要其他身份时显式传 caller 形参覆盖。
"""
from __future__ import annotations

import ast
import os
import re
import tempfile

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# P0-1：模拟部署侧注入身份，必须在 import 包之前完成。
os.environ.setdefault("A207_CALLER", "doctor_assistant")

from a207_policy import (  # noqa: E402
    FOLLOWUP_CLINICIAN,
    FOLLOWUP_WRITE_ALLOWED,
    CallerUnknown,
    PermissionDenied,
    as_caller,
)

from a207_followup_mcp import core  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def _use_tmp_store():
    """P1-3：写库经 store_path() 解析，测试用 A207_FOLLOWUP_DATA_DIR 指到临时目录。"""
    tmp_dir = tempfile.mkdtemp()
    old = os.environ.get(core.DATA_DIR_ENV)
    os.environ[core.DATA_DIR_ENV] = tmp_dir
    return os.path.join(tmp_dir, "followup_store.json"), old


def _restore_store(old: str | None) -> None:
    if old is None:
        os.environ.pop(core.DATA_DIR_ENV, None)
    else:
        os.environ[core.DATA_DIR_ENV] = old


# ---------------------------------------------------------------------------
# 1. KDIGO 2024 随访频率推荐
# ---------------------------------------------------------------------------
def test_followup_interval_by_stage():
    g2 = core.recommend_followup_interval("G2")
    g3 = core.recommend_followup_interval("G3b")
    g5 = core.recommend_followup_interval("G5")
    g5d = core.recommend_followup_interval("G5D")
    check("G2 间隔=180 天（1-2次/年）", g2["recommended_interval_days"] == 180, str(g2))
    check("G3b 间隔=90 天（≥3-4次/年）", g3["recommended_interval_days"] == 90, str(g3))
    check("G5 间隔=60 天（>4次/年）", g5["recommended_interval_days"] == 60, str(g5))
    check("G5D 间隔=30 天（每月）", g5d["recommended_interval_days"] == 30, str(g5d))
    check("带 KDIGO 引用", "KDIGO 2024" in g2["citation"], g2["citation"])


def test_followup_interval_albuminuria_escalation():
    a1 = core.recommend_followup_interval("G4", "A1")
    a3 = core.recommend_followup_interval("G4", "A3")
    check("G4 A1 = 90 天", a1["recommended_interval_days"] == 90, str(a1))
    check("G4 A3 缩短到 30 天（90-60）", a3["recommended_interval_days"] == 30, str(a3))
    bad = core.recommend_followup_interval("GX")
    check("非法分期被拒", bad["ok"] is False, str(bad))


# ---------------------------------------------------------------------------
# 2. 随访计划（写权限收口 + 频率落 cadence）
# ---------------------------------------------------------------------------
def test_schedule_followup_write_guard():
    tmp, old = _use_tmp_store()
    try:
        # 医生创建
        with as_caller("doctor_assistant"):
            ok = core.schedule_followup("P1001", "G3b", "A1", "outpatient", "2026-08-01",
                                        plan_summary="每 3 月随访一次", note_to_clinician="注意生长曲线")
        check("医生可创建计划", ok["ok"] is True, str(ok))
        check("next_due 自动算 90 天", ok["plan"]["cadence"]["next_due_date"] == "2026-10-30",
              str(ok["plan"]["cadence"]))
        check("note_to_clinician 落库", ok["plan"]["note_to_clinician"] == "注意生长曲线")
        # 家长越权创建 → 拒绝
        with as_caller("parent_assistant"):
            forbidden = core.schedule_followup("P1001", "G3b", "A1", "outpatient", "2026-08-01",
                                               plan_summary="摘要", note_to_clinician="x")
        check("家长越权被拒 FORBIDDEN", forbidden["ok"] is False and forbidden["error"] == "FORBIDDEN",
              str(forbidden))
    finally:
        _restore_store(old)
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------
# 3. 随访记录可见性（摘要可见 / 医生备注剔除）
# ---------------------------------------------------------------------------
def test_followup_record_visibility():
    tmp, old = _use_tmp_store()
    try:
        with as_caller("nutritionist"):
            core.schedule_followup("P1002", "G4", "A2", "nutrition_counsel", "2026-08-01",
                                   plan_summary="营养随访", note_to_clinician="临床备注：限磷")
            core.add_followup_record("P1002", "2026-08-01", "nutrition_counsel", "G4",
                                     {"weight_kg": 20.0, "egfr": 40}, "本次营养评估", "原始医生备注：限钾")
        # 医生看全
        with as_caller("doctor_assistant"):
            doc = core.get_followup_records("P1002")
        check("医生见完整记录", len(doc["records"]) == 1 and doc["visibility"] == "full", str(doc))
        check("医生见 doctor_notes", doc["records"][0].get("doctor_notes") == "原始医生备注：限钾")
        # 家属只见摘要
        with as_caller("parent_assistant"):
            par = core.get_followup_records("P1002")
        check("家属 visibility=summary_only", par["visibility"] == "summary_only", str(par))
        check("家属被剔除 doctor_notes", "doctor_notes" not in par["records"][0], str(par["records"][0]))
        check("家属可见 plan_summary", par["plans"][0]["plan_summary"] == "营养随访")
        check("家属被剔除 note_to_clinician",
              "note_to_clinician" not in par["plans"][0], str(par["plans"][0]))
    finally:
        _restore_store(old)
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------
# 4. 依从性评分（复合分 + 等级 + 落库）
# ---------------------------------------------------------------------------
def test_adherence_score():
    tmp, old = _use_tmp_store()
    try:
        # 全 1.0 → 100 好
        with as_caller("orchestrator"):
            hi = core.get_adherence_score("P1003", 1.0, 1.0, 1.0)
        check("全达标 composite=100 good", hi["composite_score"] == 100.0 and hi["level"] == "good",
              str(hi))
        # 各 0.6 → 60 中
        mid = core.calc_adherence_score(0.6, 0.6, 0.6)
        check("0.6/0.6/0.6 → 60 fair", mid["composite_score"] == 60.0 and mid["level"] == "fair",
              str(mid))
        # 各 0.3 → 30 差
        lo = core.calc_adherence_score(0.3, 0.3, 0.3)
        check("0.3/0.3/0.3 → 30 poor", lo["composite_score"] == 30.0 and lo["level"] == "poor",
              str(lo))
        # 越界比率被拒
        bad = core.calc_adherence_score(1.5, 0.5, 0.5)
        check("越界比率被拒", bad["ok"] is False, str(bad))
        # 落库历史
        check("依从性快照落库", len(hi["history"]) == 1 and hi["history"][0]["level"] == "good",
              str(hi["history"]))
    finally:
        _restore_store(old)
        if os.path.exists(tmp):
            os.remove(tmp)


def test_adherence_write_permission():
    """OD-014（P1-1）：get_adherence_score 落库为写操作，仅临床/编排角色可写（MX-3 收口）。

    此前漏校验：parent_assistant / child_companion（矩阵 M4=RL 只读）实测可越权写入
    依从性历史，污染后续报告与随访评估数据可信度。回归锁：越权必须抛 PermissionDenied。
    """
    tmp, old = _use_tmp_store()
    try:
        for role in ("parent_assistant", "child_companion"):
            try:
                with as_caller(role):
                    core.get_adherence_score("P1006", 1.0, 1.0, 1.0)
                check(f"{role} 写依从性被拒", False, "未抛 PermissionDenied")
            except PermissionDenied:
                check(f"{role} 写依从性被拒（OD-014）", True)
        # 合法角色（doctor / nutritionist / orchestrator）可写
        for role in ("doctor_assistant", "nutritionist", "orchestrator"):
            with as_caller(role):
                ok = core.get_adherence_score("P1006", 0.8, 0.8, 0.8)
            check(f"{role} 写依从性放行", ok.get("ok") is True, str(ok))
        # 纯计算函数始终开放（无写副作用）
        with as_caller("child_companion"):
            pure = core.calc_adherence_score(0.5, 0.5, 0.5)
        check("纯计算 calc_adherence_score 不受写权限制", pure.get("ok") is True, str(pure))
    finally:
        _restore_store(old)
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------
# 5. PEW 时间线 facade（ADR-007：接受 M3 输出；P1-2 已由 get_pew_history 改名）
# ---------------------------------------------------------------------------
def test_pew_timeline_facade():
    ph = [
        {"date": "2026-01-10", "score": 2.0, "level": "low"},
        {"date": "2026-04-10", "score": 4.0, "level": "high"},
    ]
    r = core.get_pew_timeline("P1004", ph)
    check("facade 接受 M3 历史", r["count"] == 2, str(r))
    check("facade 趋势 worsening", r["trend"] == "worsening", r["trend"])
    check("facade 标注来源 M3/ADR-007", "M3" in r["source"] and "ADR-007" in r["source"], r["source"])
    empty = core.get_pew_timeline("P1005", None)
    check("空历史 trend=no_data", empty["trend"] == "no_data", str(empty))
    check("P1-2 旧名 get_pew_history 已彻底移除（不保留别名）",
          not hasattr(core, "get_pew_history"))


# ---------------------------------------------------------------------------
# 6. Plan A 安全铁律自查（P0-1 身份来源 / P1-1 常量出处 / P1-3 状态外置）
# ---------------------------------------------------------------------------
def test_caller_fail_closed():
    """身份未注入时任何工具一律拒绝，不得回落到任何默认身份。"""
    tmp, old_dir = _use_tmp_store()
    saved = os.environ.pop("A207_CALLER", None)
    try:
        for fn, args in (
            (core.schedule_followup, ("P1006", "G3b", "A1", "outpatient", "2026-08-01")),
            (core.get_followup_records, ("P1006",)),
            (core.get_adherence_score, ("P1006", 1.0, 1.0, 1.0)),
            (core.get_pew_timeline, ("P1006",)),
            (core.add_followup_record, ("P1006", "2026-08-01", "outpatient", "G3b", {}, "摘要")),
        ):
            try:
                fn(*args)
            except CallerUnknown:
                check(f"{fn.__name__} 无身份注入时 fail-closed", True)
            else:
                check(f"{fn.__name__} 无身份注入时 fail-closed", False, "未抛 CallerUnknown")
        check("fail-closed 期间未产生写库副作用", not os.path.exists(tmp))
    finally:
        if saved is not None:
            os.environ["A207_CALLER"] = saved
        _restore_store(old_dir)


def test_server_hides_caller():
    """server 工具签名不得暴露 caller 形参（AST 校验，避免 docstring 误报）。"""
    tree = ast.parse((ROOT / "src" / "a207_followup_mcp" / "server.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        if "caller" in [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]:
            offenders.append(node.name)
    check("server 工具层无 caller 形参", not offenders, "; ".join(offenders))


def test_policy_single_source():
    """零跨包引用（a207_policy 是统一策略共享包，Plan A 下豁免）+ 写库不落安装目录。"""
    pattern = re.compile(r"(?:^|\s)(?:import|from)\s+a207_(?!followup_mcp|policy)", re.M)
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")
                 if "__pycache__" not in p.parts and pattern.search(p.read_text(encoding="utf-8"))]
    check("无跨 a207-* 领域包引用", not offenders, "; ".join(offenders))
    old_dir = os.environ.pop(core.DATA_DIR_ENV, None)
    try:
        resolved = core.store_path().resolve()
    finally:
        if old_dir is not None:
            os.environ[core.DATA_DIR_ENV] = old_dir
    check("写库不落在 src/ 安装目录内", (ROOT / "src").resolve() not in resolved.parents,
          str(resolved))
    check("放行集合取自 a207_policy 而非包内硬编码",
          core._WRITE_ALLOWED is FOLLOWUP_WRITE_ALLOWED and core._CLINICIAN is FOLLOWUP_CLINICIAN)


def main():
    test_followup_interval_by_stage()
    test_followup_interval_albuminuria_escalation()
    test_schedule_followup_write_guard()
    test_followup_record_visibility()
    test_adherence_score()
    test_adherence_write_permission()
    test_pew_timeline_facade()
    test_caller_fail_closed()
    test_server_hides_caller()
    test_policy_single_source()
    print(f"\n==== M4 测试: {_PASS} passed, {_FAIL} failed ====")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
