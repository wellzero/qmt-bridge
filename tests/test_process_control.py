"""进程控制模块单元测试。

用临时编写的假编排脚本（暴露与 ``run_all_paper_tests.py`` 相同的
``discover_strategies`` / ``status_strategy`` / ``start_strategy`` /
``stop_strategy`` 接口）验证 ``process_control`` 的加载、列表与动作分发，
不触碰真实策略进程。
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

_DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "paper-trading"
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

import process_control  # noqa: E402


FAKE_ORCHESTRATOR = textwrap.dedent(
    """
    from pathlib import Path

    BASE_DIR = Path("/tmp/does-not-matter")

    class FakeTarget:
        def __init__(self, stem):
            self.stem = stem

    TARGETS = [FakeTarget("alpha_paper_test"), FakeTarget("beta_paper_test")]

    def discover_strategies(base_dir):
        return list(TARGETS)

    def status_strategy(target, pid_dir):
        statuses = {
            "alpha_paper_test": {"name": target.stem, "status": "RUNNING", "pid": 111},
            "beta_paper_test": {"name": target.stem, "status": "STOPPED", "pid": None},
        }
        return statuses[target.stem]

    def start_strategy(target, log_dir, pid_dir):
        return {"name": target.stem, "status": "STARTED", "pid": 222}

    def stop_strategy(target, pid_dir):
        return {"name": target.stem, "status": "STOPPED", "pid": 111}
    """
)


@pytest.fixture()
def fake_orchestrator(tmp_path, monkeypatch):
    """把编排脚本指向临时假模块并清空模块缓存。"""
    script = tmp_path / "fake_orchestrator.py"
    script.write_text(FAKE_ORCHESTRATOR, encoding="utf-8")
    monkeypatch.setattr(process_control, "ORCHESTRATOR_FILE", script)
    monkeypatch.setattr(process_control, "_module", None)
    return script


def test_list_strategy_controls_reports_pid_status(fake_orchestrator):
    controls = process_control.list_strategy_controls()
    assert controls is not None
    by_stem = {c["stem"]: c for c in controls}
    assert by_stem["alpha_paper_test"]["status"] == "RUNNING"
    assert by_stem["alpha_paper_test"]["pid"] == 111
    assert by_stem["beta_paper_test"]["status"] == "STOPPED"
    assert by_stem["beta_paper_test"]["pid"] is None


def test_list_returns_none_when_orchestrator_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        process_control, "ORCHESTRATOR_FILE", tmp_path / "no_such_file.py"
    )
    monkeypatch.setattr(process_control, "_module", None)
    assert process_control.list_strategy_controls() is None


def test_perform_action_dispatches_start_stop_restart(fake_orchestrator):
    assert process_control.perform_action("alpha_paper_test", "start") == {
        "name": "alpha_paper_test",
        "status": "STARTED",
        "pid": 222,
    }
    assert process_control.perform_action("alpha_paper_test", "STOP") == {
        "name": "alpha_paper_test",
        "status": "STOPPED",
        "pid": 111,
    }
    # restart = 先停再启动，两步结果拼接
    result = process_control.perform_action("beta_paper_test", "restart")
    assert result["status"] == "STOPPED -> STARTED"
    assert result["pid"] == 222


def test_perform_action_rejects_bad_input(fake_orchestrator):
    assert "BAD_ACTION" in process_control.perform_action("alpha", "pause")["status"]
    assert "NOT_FOUND" in process_control.perform_action("ghost", "start")["status"]


def test_perform_action_unavailable_without_orchestrator(tmp_path, monkeypatch):
    monkeypatch.setattr(
        process_control, "ORCHESTRATOR_FILE", tmp_path / "no_such_file.py"
    )
    monkeypatch.setattr(process_control, "_module", None)
    result = process_control.perform_action("alpha_paper_test", "start")
    assert result["status"] == "ORCHESTRATOR_UNAVAILABLE"
