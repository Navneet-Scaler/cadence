"""Weekly retention report, generated unattended.

Written to run on a schedule with nobody watching, which drives three choices
that would be over-engineering in a notebook:

* **Logging, not printing.** Output goes through the ``logging`` module to both
  stderr and a rotating file, so a failed 6am run leaves evidence.
* **Exit codes that mean something.** ``0`` healthy, ``1`` alerts raised, ``2``
  the run itself failed. A cron wrapper can act on that without parsing prose.
* **Alert thresholds, not just numbers.** The report compares this week against
  last and flags material moves. A report nobody reads is worthless; a report
  that only speaks up when something changed gets read.

The report deliberately recomputes streaks before analysing, rather than trusting
whatever was in ``user_streaks`` from a previous run. A stale derived table is the
most common way a scheduled report quietly reports last month's numbers forever.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from src import db
from src.analysis import data_quality, streak_builder, survival_analysis

logger = logging.getLogger("cadence.report")

REPORT_DIR = Path("reports")
LOG_DIR = Path("reports/logs")

# Week-over-week moves larger than these raise an alert.
THRESHOLDS = {
    "active_users_pct_drop": 10.0,  # % fall in weekly active investors
    "recovery_rate_pct_drop": 5.0,  # percentage-point fall in break recovery
    "new_dq_failures": 1,  # any newly failing data quality check
}

EXIT_OK = 0
EXIT_ALERTS = 1
EXIT_FAILED = 2


@dataclass
class Alert:
    """Something that moved enough to deserve a human's attention."""

    severity: str  # critical | warning
    metric: str
    message: str


def configure_logging(verbose: bool = False) -> None:
    """Send logs to stderr and a rotating file.

    The file handler is what makes an unattended failure diagnosable: if the 6am
    run dies, the traceback is on disk rather than in a terminal nobody was
    watching.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)

    rotating = logging.handlers.RotatingFileHandler(
        LOG_DIR / "weekly_report.log", maxBytes=2_000_000, backupCount=5
    )
    rotating.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(stream)
    root.addHandler(rotating)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def weekly_activity(weeks: int = 8) -> pd.DataFrame:
    """Active investors and contribution volume per ISO week."""
    return db.read_sql(
        """
        SELECT DATE_TRUNC('week', txn_date)::date        AS week_start,
               COUNT(DISTINCT user_id)                   AS active_users,
               COUNT(*)                                  AS successful_txns,
               ROUND(SUM(COALESCE(amount, 0)), 2)        AS total_invested,
               ROUND(AVG(COALESCE(amount, 0)), 2)        AS avg_contribution
        FROM v_clean_transactions
        WHERE status = 'success'
        GROUP BY 1
        ORDER BY 1 DESC
        LIMIT :weeks
        """,
        {"weeks": weeks},
    ).sort_values("week_start")


def streak_health() -> pd.DataFrame:
    """Current streak distribution — the top-line habit metric."""
    return db.read_sql(
        """
        SELECT COUNT(*)                                          AS live_streaks,
               COUNT(*) FILTER (WHERE streak_length >= 7)        AS streaks_7_plus,
               COUNT(*) FILTER (WHERE streak_length >= 30)       AS streaks_30_plus,
               ROUND(AVG(streak_length), 2)                      AS avg_live_length,
               MAX(streak_length)                                AS longest_live
        FROM user_streaks
        WHERE is_censored
        """
    )


def weekly_recovery(weeks: int = 8) -> pd.DataFrame:
    """Share of streak breaks that recovered, by the week the break happened.

    Only breaks with a full 30-day window are counted, so the most recent weeks
    are absent rather than showing an artificially low rate. A scheduled report
    that ignores this "reports" a catastrophic recovery collapse every single
    week, purely because the newest breaks have not had time to recover.
    """
    return db.read_sql(
        """
        WITH window_end AS (SELECT MAX(txn_date) AS last_day FROM v_clean_transactions)
        SELECT DATE_TRUNC('week', s.streak_end)::date AS week_start,
               COUNT(*)                               AS breaks,
               ROUND(100.0 * AVG(
                   (s.days_to_next_streak IS NOT NULL AND s.days_to_next_streak <= 30)::int
               ), 2)                                  AS recovery_rate_pct
        FROM user_streaks s
        CROSS JOIN window_end w
        WHERE NOT s.is_censored
          AND s.streak_end <= w.last_day - 30
        GROUP BY 1
        ORDER BY 1 DESC
        LIMIT :weeks
        """,
        {"weeks": weeks},
    ).sort_values("week_start")


# --------------------------------------------------------------------------- #
# Alerting
# --------------------------------------------------------------------------- #


def detect_alerts(activity: pd.DataFrame, recovery: pd.DataFrame, dq: pd.DataFrame) -> list[Alert]:
    """Compare the latest complete week against the one before it.

    The most recent week is dropped from the activity comparison: a report run
    mid-week sees a partial week and would alert on a "drop" that is only the
    calendar. This is the single most common false alarm in scheduled reporting.
    """
    alerts: list[Alert] = []

    if len(activity) >= 3:
        # -1 is likely partial; compare -2 against -3.
        latest, previous = activity.iloc[-2], activity.iloc[-3]
        change = (
            100 * (latest["active_users"] - previous["active_users"]) / previous["active_users"]
        )
        if change <= -THRESHOLDS["active_users_pct_drop"]:
            alerts.append(
                Alert(
                    severity="critical",
                    metric="active_users",
                    message=(
                        f"Weekly active investors fell {abs(change):.1f}% "
                        f"({int(previous['active_users']):,} → {int(latest['active_users']):,}) "
                        f"in the week of {latest['week_start']}."
                    ),
                )
            )

    if len(recovery) >= 2:
        latest, previous = recovery.iloc[-1], recovery.iloc[-2]
        drop = previous["recovery_rate_pct"] - latest["recovery_rate_pct"]
        if drop >= THRESHOLDS["recovery_rate_pct_drop"]:
            alerts.append(
                Alert(
                    severity="critical",
                    metric="recovery_rate",
                    message=(
                        f"Break recovery fell {drop:.1f} points "
                        f"({previous['recovery_rate_pct']:.1f}% → {latest['recovery_rate_pct']:.1f}%) "
                        f"for breaks in the week of {latest['week_start']}."
                    ),
                )
            )

    failing = dq[dq["status"] == "FAIL"]
    if len(failing) >= THRESHOLDS["new_dq_failures"]:
        high = failing[failing["severity"] == "high"]
        alerts.append(
            Alert(
                severity="warning" if high.empty else "critical",
                metric="data_quality",
                message=(
                    f"{len(failing)} data quality checks failing "
                    f"({len(high)} high severity): {', '.join(failing['code'])}."
                ),
            )
        )

    return alerts


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _table(frame: pd.DataFrame) -> str:
    """Render a DataFrame as a markdown table."""
    if frame.empty:
        return "_No data._\n"
    header = "| " + " | ".join(str(c) for c in frame.columns) + " |"
    divider = "|" + "|".join("---" for _ in frame.columns) + "|"
    rows = [
        "| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |"
        for row in frame.itertuples(index=False)
    ]
    return "\n".join([header, divider, *rows]) + "\n"


def render(
    activity: pd.DataFrame,
    streaks: pd.DataFrame,
    recovery: pd.DataFrame,
    dq: pd.DataFrame,
    recovery_table: pd.DataFrame,
    alerts: list[Alert],
    generated_at: datetime,
) -> str:
    """Assemble the markdown report."""
    lines: list[str] = [
        "# Cadence — Weekly Retention Report",
        "",
        f"_Generated {generated_at:%Y-%m-%d %H:%M} from {len(activity)} weeks of data._",
        "",
    ]

    if alerts:
        lines += ["## ⚠️ Alerts", ""]
        for alert in alerts:
            marker = "🔴" if alert.severity == "critical" else "🟡"
            lines.append(f"- {marker} **{alert.metric}** — {alert.message}")
        lines.append("")
    else:
        lines += ["## ✅ No alerts", "", "All monitored metrics within thresholds.", ""]

    lines += [
        "## Weekly activity",
        "",
        _table(activity),
        "> The most recent week may be partial and is excluded from alerting.",
        "",
        "## Live streak health",
        "",
        _table(streaks),
        "",
        "## Break recovery by week",
        "",
        _table(recovery),
        "> Only breaks with a full 30-day observation window are counted, so recent",
        "> weeks are absent rather than showing an artificially low rate.",
        "",
        "## Recovery by days missed",
        "",
        _table(recovery_table),
        "",
        "## Data quality",
        "",
        _table(dq),
        "",
        "See `data_quality_findings.md` for detection queries and proposed fixes.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def generate(rebuild: bool = True, weeks: int = 8) -> tuple[Path, list[Alert]]:
    """Build the report end to end and write it to disk."""
    if rebuild:
        # Never trust a derived table from a previous run: a stale user_streaks
        # is how a scheduled report quietly serves last month's numbers forever.
        logger.info("rebuilding streaks before reporting")
        streak_builder.rebuild_streaks_in_db()

    activity = weekly_activity(weeks)
    streaks = streak_health()
    recovery = weekly_recovery(weeks)

    findings = data_quality.run_all()
    data_quality.persist(findings)
    dq = data_quality.summary_frame(findings)

    all_streaks = streak_builder.load_streaks()
    observation_end = pd.to_datetime(
        db.read_sql("SELECT MAX(txn_date) AS d FROM v_clean_transactions")["d"].iat[0]
    )
    gaps = survival_analysis.build_gap_frame(all_streaks, observation_end)
    recovery_table = survival_analysis.recovery_by_gap_length(survival_analysis.fit_recovery(gaps))

    alerts = detect_alerts(activity, recovery, dq)
    for alert in alerts:
        logger.warning("ALERT [%s] %s: %s", alert.severity, alert.metric, alert.message)

    generated_at = datetime.now()
    content = render(activity, streaks, recovery, dq, recovery_table, alerts, generated_at)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"weekly_report_{generated_at:%Y%m%d}.md"
    path.write_text(content)
    logger.info("wrote %s (%s bytes)", path, f"{len(content):,}")

    latest = REPORT_DIR / "weekly_report_latest.md"
    latest.write_text(content)

    return path, alerts


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the weekly retention report.")
    parser.add_argument("--weeks", type=int, default=8, help="weeks of history to include")
    parser.add_argument("--no-rebuild", action="store_true", help="skip rebuilding streaks")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    configure_logging(args.verbose)
    started = datetime.now()
    logger.info("weekly report run started")

    try:
        path, alerts = generate(rebuild=not args.no_rebuild, weeks=args.weeks)
    except Exception:
        # Log the traceback before exiting: on a scheduled run this file is the
        # only record that the failure happened at all.
        logger.exception("weekly report run FAILED")
        return EXIT_FAILED

    elapsed = (datetime.now() - started).total_seconds()
    logger.info(
        "weekly report run finished in %.1fs — %d alert(s), output at %s",
        elapsed,
        len(alerts),
        path,
    )
    return EXIT_ALERTS if alerts else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
