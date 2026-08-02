"""Tests for the weekly report's alerting and rendering.

Alerting is the part that runs unattended and is therefore the part nobody will
notice is wrong. These tests pin the two failure modes that matter: crying wolf
on a partial week, and staying silent when something genuinely moved.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.reporting import generate_weekly_report as report


def make_activity(active_users: list[int]) -> pd.DataFrame:
    weeks = pd.date_range("2025-01-06", periods=len(active_users), freq="W-MON").date
    return pd.DataFrame(
        {
            "week_start": weeks,
            "active_users": active_users,
            "successful_txns": [n * 6 for n in active_users],
            "total_invested": [n * 130.0 for n in active_users],
            "avg_contribution": [21.0] * len(active_users),
        }
    )


def make_recovery(rates: list[float]) -> pd.DataFrame:
    weeks = pd.date_range("2025-01-06", periods=len(rates), freq="W-MON").date
    return pd.DataFrame(
        {"week_start": weeks, "breaks": [500] * len(rates), "recovery_rate_pct": rates}
    )


def clean_dq() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "DQ-01",
                "table": "t",
                "issue": "i",
                "severity": "high",
                "rows": 0,
                "status": "PASS",
            }
        ]
    )


def failing_dq(severity: str = "high") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "DQ-01",
                "table": "t",
                "issue": "i",
                "severity": severity,
                "rows": 10,
                "status": "FAIL",
            }
        ]
    )


def test_no_alerts_when_everything_is_stable() -> None:
    alerts = report.detect_alerts(
        make_activity([1000, 1010, 1005, 1008]), make_recovery([90.0, 90.5]), clean_dq()
    )
    assert alerts == []


def test_partial_final_week_does_not_trigger_a_false_alarm() -> None:
    """The classic scheduled-reporting false positive.

    A mid-week run sees a half-finished week. Comparing it to a complete one
    would alert every single time, purely because of the calendar.
    """
    # Weeks are flat, then the last is half — because the week isn't over.
    activity = make_activity([1000, 1000, 1000, 400])

    alerts = report.detect_alerts(activity, make_recovery([90.0, 90.0]), clean_dq())

    assert not [a for a in alerts if a.metric == "active_users"]


def test_a_real_drop_in_the_last_complete_week_does_alert() -> None:
    # -2 is the last complete week and it fell 20% against -3.
    activity = make_activity([1000, 1000, 800, 390])

    alerts = report.detect_alerts(activity, make_recovery([90.0, 90.0]), clean_dq())

    active_alerts = [a for a in alerts if a.metric == "active_users"]
    assert len(active_alerts) == 1
    assert active_alerts[0].severity == "critical"
    assert "20.0%" in active_alerts[0].message


def test_a_drop_just_under_the_threshold_stays_quiet() -> None:
    activity = make_activity([1000, 1000, 910, 400])  # -9%, under the 10% bar
    alerts = report.detect_alerts(activity, make_recovery([90.0, 90.0]), clean_dq())
    assert not [a for a in alerts if a.metric == "active_users"]


def test_recovery_rate_drop_alerts() -> None:
    alerts = report.detect_alerts(
        make_activity([1000, 1000, 1000, 400]), make_recovery([90.0, 80.0]), clean_dq()
    )
    recovery_alerts = [a for a in alerts if a.metric == "recovery_rate"]
    assert len(recovery_alerts) == 1
    assert "10.0 points" in recovery_alerts[0].message


def test_recovery_improvement_never_alerts() -> None:
    alerts = report.detect_alerts(
        make_activity([1000, 1000, 1000, 400]), make_recovery([80.0, 92.0]), clean_dq()
    )
    assert not [a for a in alerts if a.metric == "recovery_rate"]


def test_failing_data_quality_checks_alert() -> None:
    alerts = report.detect_alerts(
        make_activity([1000, 1000, 1000, 400]), make_recovery([90.0, 90.0]), failing_dq()
    )
    dq_alerts = [a for a in alerts if a.metric == "data_quality"]
    assert len(dq_alerts) == 1
    assert dq_alerts[0].severity == "critical"


def test_only_low_severity_failures_are_a_warning_not_critical() -> None:
    alerts = report.detect_alerts(
        make_activity([1000, 1000, 1000, 400]),
        make_recovery([90.0, 90.0]),
        failing_dq(severity="medium"),
    )
    assert [a for a in alerts if a.metric == "data_quality"][0].severity == "warning"


def test_too_little_history_produces_no_alerts_rather_than_a_crash() -> None:
    """A first run has nothing to compare against; it must not divide by nothing."""
    alerts = report.detect_alerts(make_activity([1000]), make_recovery([90.0]), clean_dq())
    assert not [a for a in alerts if a.metric in {"active_users", "recovery_rate"}]


def test_markdown_table_renders_headers_and_rows() -> None:
    frame = pd.DataFrame({"week": ["2025-01-06"], "users": [1000]})
    rendered = report._table(frame)

    assert "| week | users |" in rendered
    assert "| 2025-01-06 | 1000 |" in rendered


def test_markdown_table_handles_an_empty_frame() -> None:
    assert "_No data._" in report._table(pd.DataFrame())


def test_markdown_table_renders_nulls_as_blank_not_nan() -> None:
    frame = pd.DataFrame({"a": [1], "b": [None]})
    assert "nan" not in report._table(frame).lower()


def test_report_body_states_no_alerts_when_clean() -> None:
    content = report.render(
        make_activity([1000, 1000]),
        pd.DataFrame({"live_streaks": [10]}),
        make_recovery([90.0]),
        clean_dq(),
        pd.DataFrame({"days_missed": [1], "p_return_within_horizon": [0.9]}),
        [],
        pd.Timestamp("2026-01-01"),
    )
    assert "No alerts" in content


def test_report_body_lists_each_alert() -> None:
    alerts = [report.Alert("critical", "active_users", "fell 20%")]
    content = report.render(
        make_activity([1000, 1000]),
        pd.DataFrame({"live_streaks": [10]}),
        make_recovery([90.0]),
        clean_dq(),
        pd.DataFrame({"days_missed": [1], "p_return_within_horizon": [0.9]}),
        alerts,
        pd.Timestamp("2026-01-01"),
    )
    assert "Alerts" in content
    assert "fell 20%" in content


@pytest.mark.parametrize(
    ("name", "value"), [("EXIT_OK", 0), ("EXIT_ALERTS", 1), ("EXIT_FAILED", 2)]
)
def test_exit_codes_are_stable(name: str, value: int) -> None:
    """A cron wrapper branches on these; changing them silently breaks alerting."""
    assert getattr(report, name) == value
