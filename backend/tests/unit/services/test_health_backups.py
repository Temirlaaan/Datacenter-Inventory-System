"""Unit tests for app.api.v1.health._backups_sub_object (Sprint 9 Task 3).

Placed under tests/unit/services/ rather than tests/unit/api/v1/ to dodge
the api-v1 conftest's autouse alembic-env fixture — these tests touch no
DB, only os.path and tmp files."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


def test_backups_sub_object_returns_configured_false_when_marker_path_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``DCINV_BACKUP_MARKER_PATH`` → ``configured: False``. Tells
    the operator: "this deployment doesn't have backups wired up yet"."""
    monkeypatch.setenv("DCINV_BACKUP_MARKER_PATH", "")
    from app.api.v1.health import _backups_sub_object

    assert _backups_sub_object() == {"configured": False}


def test_backups_sub_object_returns_null_age_when_marker_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Marker path set but file missing → configured=True, age=None.
    Distinguishes "you set it up but cron hasn't run yet / has been
    failing" from "you haven't set it up at all"."""
    missing_marker = tmp_path / "never-touched"
    monkeypatch.setenv("DCINV_BACKUP_MARKER_PATH", str(missing_marker))
    from app.api.v1.health import _backups_sub_object

    result = _backups_sub_object()
    assert result == {
        "configured": True,
        "last_completed_at": None,
        "age_seconds": None,
    }


def test_backups_sub_object_returns_age_from_marker_mtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Marker present → ISO timestamp + age_seconds from its mtime."""
    marker = tmp_path / "last-success"
    marker.write_text("")
    past = time.time() - 3600
    os.utime(marker, (past, past))

    monkeypatch.setenv("DCINV_BACKUP_MARKER_PATH", str(marker))
    from app.api.v1.health import _backups_sub_object

    result = _backups_sub_object()
    assert result["configured"] is True
    assert isinstance(result["last_completed_at"], str)
    assert result["last_completed_at"].endswith("+00:00")
    age = result["age_seconds"]
    assert isinstance(age, int)
    assert 3590 <= age <= 3700


# ---------- restore_validation sub-object (Sprint 10 Task 0) ----------------


def test_restore_validation_sub_object_returns_configured_false_when_marker_path_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``DCINV_RESTORE_MARKER_PATH`` → ``configured: False``."""
    monkeypatch.setenv("DCINV_RESTORE_MARKER_PATH", "")
    from app.api.v1.health import _restore_validation_sub_object

    assert _restore_validation_sub_object() == {"configured": False}


def test_restore_validation_sub_object_returns_age_from_marker_mtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Marker present → ISO timestamp + age_seconds. Same shape as the
    backups sub-object — operators can grep on one field name across
    both."""
    marker = tmp_path / "last-restore-validate"
    marker.write_text("")
    # 6 days ago — well under the recommended 8-day alert threshold.
    past = time.time() - 6 * 24 * 3600
    os.utime(marker, (past, past))

    monkeypatch.setenv("DCINV_RESTORE_MARKER_PATH", str(marker))
    from app.api.v1.health import _restore_validation_sub_object

    result = _restore_validation_sub_object()
    assert result["configured"] is True
    assert isinstance(result["last_completed_at"], str)
    age = result["age_seconds"]
    assert isinstance(age, int)
    # 6 days ± a few seconds for test-scheduling jitter.
    assert 6 * 24 * 3600 - 10 <= age <= 6 * 24 * 3600 + 60
