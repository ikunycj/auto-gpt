from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest

from core import sqlite_store
from registration.application import job_service, use_case
from registration.ports.stop_signal import StopRequested


@pytest.fixture
def isolated_job(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("TURB_SQLITE_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(job_service.db, "get_job", lambda job_id: {"id": job_id, "status": "running"})
    update = Mock()
    monkeypatch.setattr(job_service.db, "update_job", update)
    monkeypatch.setattr(
        job_service, "_prepare_registration_args",
        lambda: ("test@example.com", "Test", "1990-01-01"),
    )
    monkeypatch.setattr(job_service, "_release_unconsumed_job_email", Mock())
    monkeypatch.setattr(job_service, "_disable_job_email", Mock())
    caplog.set_level(logging.INFO)
    return tmp_path / "job.log", update


@pytest.mark.parametrize(
    "exception,status,expected_log",
    [
        (
            RuntimeError("Roxy API 返回失败 POST /browser/create: 请在团队中检查是否关联该项目！"),
            "failed",
            "RuntimeError: Roxy API 返回失败 POST /browser/create: 请在团队中检查是否关联该项目！",
        ),
        (StopRequested("用户手动停止"), "stopped", "[Job 3] 已停止: 用户手动停止"),
    ],
)
def test_exception_and_stop_details_reach_job_log(
    monkeypatch, isolated_job, exception, status, expected_log,
):
    log_path, update = isolated_job
    monkeypatch.setattr(use_case, "run_registration", Mock(side_effect=exception))
    handlers_before = list(logging.getLogger().handlers)

    job_service._run_one_job(3, str(log_path))

    text = sqlite_store.read_text_file(log_path, category="registration_logs", import_legacy=False)
    assert expected_log in text
    assert update.call_args.kwargs["status"] == status
    assert 3 not in job_service._ACTIVE_JOBS
    assert logging.getLogger().handlers == handlers_before


@pytest.mark.parametrize("success", [True, False])
def test_completed_job_keeps_result_log_and_releases_handler(monkeypatch, isolated_job, success):
    log_path, update = isolated_job
    monkeypatch.setattr(
        use_case, "run_registration",
        Mock(return_value={"success": success, "email": "test@example.com", "error": "test failure"}),
    )
    handlers_before = list(logging.getLogger().handlers)

    job_service._run_one_job(3, str(log_path))

    text = sqlite_store.read_text_file(log_path, category="registration_logs", import_legacy=False)
    assert ("[Job 3] 成功:" if success else "[Job 3] 失败: test failure") in text
    assert update.call_args.kwargs["status"] == ("success" if success else "failed")
    assert 3 not in job_service._ACTIVE_JOBS
    assert logging.getLogger().handlers == handlers_before
