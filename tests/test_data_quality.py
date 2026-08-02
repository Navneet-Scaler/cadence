"""Tests for the data quality check registry.

No database: these assert the *contract* every check must satisfy, so a new check
cannot be added in a state that would produce an unactionable flag.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.analysis import data_quality as dq

VALID_SEVERITIES = {"high", "medium", "low"}


def test_check_codes_are_unique() -> None:
    codes = [check.code for check in dq.CHECKS]
    assert len(codes) == len(set(codes))


def test_every_check_has_an_actionable_fix() -> None:
    """A flag without a proposed fix is work handed to someone else, not work done."""
    for check in dq.CHECKS:
        assert check.proposed_fix.strip(), check.code
        assert len(check.proposed_fix) > 40, f"{check.code} fix is too vague to act on"


def test_every_check_explains_why_it_matters() -> None:
    for check in dq.CHECKS:
        assert len(check.description) > 60, check.code


def test_severities_are_from_the_known_set() -> None:
    for check in dq.CHECKS:
        assert check.severity in VALID_SEVERITIES, check.code


def test_detection_queries_target_raw_tables_not_the_clean_view() -> None:
    """The view hides duplicates and pre-signup rows.

    Running the checks through it would report a clean bill of health on a
    database that still has the problem.
    """
    for check in dq.CHECKS:
        assert "v_clean_transactions" not in check.detect_sql, check.code
        if check.impact_sql:
            assert "v_clean_transactions" not in check.impact_sql, check.code


def test_detection_queries_select_the_declared_reference_column() -> None:
    for check in dq.CHECKS:
        assert check.reference_column in check.detect_sql, check.code


def test_finding_passes_only_on_zero_rows() -> None:
    check = dq.CHECKS[0]
    assert dq.Finding(check=check, row_count=0).passed
    assert not dq.Finding(check=check, row_count=1).passed


def test_summary_frame_reports_one_row_per_check() -> None:
    findings = [dq.Finding(check=c, row_count=0) for c in dq.CHECKS]
    summary = dq.summary_frame(findings)

    assert len(summary) == len(dq.CHECKS)
    assert set(summary["status"]) == {"PASS"}


def test_persist_writes_only_failing_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing zero-row passes would bury the genuine problems."""
    written: dict[str, pd.DataFrame] = {}

    monkeypatch.setattr(dq.db, "execute", lambda *a, **k: None)
    monkeypatch.setattr(
        dq.db, "bulk_load", lambda df, table, cols: written.setdefault(table, df) is None or len(df)
    )

    findings = [
        dq.Finding(check=dq.CHECKS[0], row_count=5, references=["1", "2"], impact="pct=1.5"),
        dq.Finding(check=dq.CHECKS[1], row_count=0),
    ]
    count = dq.persist(findings)

    assert count == 1
    frame = written["data_quality_flags"]
    assert len(frame) == 1
    assert frame["issue_type"].iat[0] == dq.CHECKS[0].issue_type
    assert frame["severity"].iat[0] == dq.CHECKS[0].severity
    assert dq.CHECKS[0].code in frame["row_reference"].iat[0]


def test_persist_returns_zero_when_everything_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dq.db, "execute", lambda *a, **k: None)
    monkeypatch.setattr(
        dq.db, "bulk_load", lambda *a, **k: pytest.fail("must not write when all checks pass")
    )

    assert dq.persist([dq.Finding(check=dq.CHECKS[0], row_count=0)]) == 0


def test_row_references_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flag is a handle to reproduce with, not a second copy of the table."""
    captured: dict[str, pd.DataFrame] = {}
    monkeypatch.setattr(dq.db, "execute", lambda *a, **k: None)
    monkeypatch.setattr(
        dq.db, "bulk_load", lambda df, table, cols: captured.setdefault(table, df) is None or 1
    )

    finding = dq.Finding(
        check=dq.CHECKS[0], row_count=10_000, references=[str(i) for i in range(200)]
    )
    dq.persist([finding])

    assert len(captured["data_quality_flags"]["row_reference"].iat[0]) <= 4000
