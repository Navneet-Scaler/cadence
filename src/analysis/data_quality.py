"""Automated data quality checks.

Each check answers three questions, because a bug report that answers only the
first is work handed to someone else rather than work done:

1. **What is wrong** — a detection query against the *raw* tables.
2. **What it costs** — the blast radius, in the metric a reader cares about.
3. **How to fix it** — the exact DDL or validation rule that prevents recurrence.

Checks read the raw tables deliberately, never ``v_clean_transactions``. The view
already hides duplicates and pre-signup rows; running the checks through it would
report a clean bill of health on a database that still has the problem. The view
protects the analysis; these checks protect the ledger.

Findings are written to ``data_quality_flags`` so the log is queryable from the
dashboard rather than living in a notebook cell, and so a trend in defect counts
is visible over time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from src import db

logger = logging.getLogger(__name__)

# Row references are truncated in the flags table: the point is to give an
# engineer a handle to reproduce with, not to duplicate the table into a log.
MAX_ROW_REFERENCES = 20


@dataclass
class Check:
    """One data quality check, with its fix already written."""

    code: str
    table_name: str
    issue_type: str
    severity: str  # high | medium | low
    description: str
    detect_sql: str
    impact_sql: str | None
    proposed_fix: str
    reference_column: str = "txn_id"


@dataclass
class Finding:
    """The outcome of running a check."""

    check: Check
    row_count: int
    references: list[str] = field(default_factory=list)
    impact: str = ""

    @property
    def passed(self) -> bool:
        return self.row_count == 0


CHECKS: list[Check] = [
    Check(
        code="DQ-01",
        table_name="sip_daily_transactions",
        issue_type="null_amount",
        severity="high",
        description=(
            "Transactions with a NULL amount. The row asserts money moved while "
            "recording no sum, so any revenue or AUM figure silently under-counts."
        ),
        detect_sql="SELECT txn_id FROM sip_daily_transactions WHERE amount IS NULL",
        impact_sql="""
            SELECT ROUND(
                       100.0 * COUNT(*) FILTER (WHERE amount IS NULL) / NULLIF(COUNT(*), 0), 3
                   ) AS pct_rows,
                   COUNT(*) FILTER (WHERE amount IS NULL AND status = 'success') AS successful_rows
            FROM sip_daily_transactions
        """,
        proposed_fix=(
            "ALTER TABLE sip_daily_transactions ALTER COLUMN amount SET NOT NULL; "
            "ALTER TABLE sip_daily_transactions ADD CONSTRAINT chk_amount_positive "
            "CHECK (amount > 0); "
            "Backfill from the payment gateway ledger before applying, and make the "
            "writer fail loudly rather than inserting a partial row."
        ),
    ),
    Check(
        code="DQ-02",
        table_name="sip_daily_transactions",
        issue_type="duplicate_txn",
        severity="high",
        description=(
            "More than one transaction for the same (user_id, txn_date). Almost "
            "certainly a retried write with no idempotency key. This is the most "
            "dangerous defect here: a duplicate consumes an extra row number in the "
            "gaps-and-islands query and shatters one real streak into several."
        ),
        detect_sql="""
            SELECT MIN(txn_id)::text || ' (+' || (COUNT(*) - 1) || ' dupes)' AS txn_id
            FROM sip_daily_transactions
            GROUP BY user_id, txn_date
            HAVING COUNT(*) > 1
        """,
        impact_sql="""
            WITH dupes AS (
                SELECT user_id, txn_date, COUNT(*) AS n
                FROM sip_daily_transactions
                GROUP BY user_id, txn_date
                HAVING COUNT(*) > 1
            )
            SELECT COUNT(*) AS affected_user_days,
                   COUNT(DISTINCT user_id) AS affected_users,
                   SUM(n - 1) AS excess_rows
            FROM dupes
        """,
        proposed_fix=(
            "De-duplicate keeping the earliest created_at, then: "
            "ALTER TABLE sip_daily_transactions ADD CONSTRAINT uq_user_day "
            "UNIQUE (user_id, txn_date); "
            "Give the payment writer an idempotency key so a retry updates rather "
            "than inserts."
        ),
    ),
    Check(
        code="DQ-03",
        table_name="sip_daily_transactions",
        issue_type="txn_before_signup",
        severity="medium",
        description=(
            "Transactions dated before the user's signup_date. Classic symptom of "
            "trusting a client-supplied timestamp over server time. Produces "
            "negative day-since-signup indices, which quietly corrupt cohort "
            "retention."
        ),
        detect_sql="""
            SELECT t.txn_id
            FROM sip_daily_transactions t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.txn_date < u.signup_date
        """,
        impact_sql="""
            SELECT COUNT(*) AS rows_affected,
                   COUNT(DISTINCT t.user_id) AS users_affected,
                   MIN(u.signup_date - t.txn_date) AS min_days_early,
                   MAX(u.signup_date - t.txn_date) AS max_days_early
            FROM sip_daily_transactions t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.txn_date < u.signup_date
        """,
        proposed_fix=(
            "Stamp txn_date server-side from the settlement event rather than the "
            "client payload. Enforce with a trigger or a CHECK against a "
            "denormalised signup_date, since PostgreSQL cannot express a "
            "cross-table CHECK directly."
        ),
    ),
    Check(
        code="DQ-04",
        table_name="sip_daily_transactions",
        issue_type="invalid_status",
        severity="high",
        description=(
            "Status values outside the documented set (success, failed, skipped). "
            "Every downstream query filters on status = 'success', so a wrong-case "
            "'SUCCESS' is invisible to the analysis while being a real "
            "contribution — it manufactures phantom streak breaks."
        ),
        detect_sql="""
            SELECT txn_id
            FROM sip_daily_transactions
            WHERE status NOT IN ('success', 'failed', 'skipped')
        """,
        impact_sql="""
            SELECT status, COUNT(*) AS rows_affected, COUNT(DISTINCT user_id) AS users_affected
            FROM sip_daily_transactions
            WHERE status NOT IN ('success', 'failed', 'skipped')
            GROUP BY status
        """,
        proposed_fix=(
            "Normalise existing values to lower case, then: "
            "ALTER TABLE sip_daily_transactions ADD CONSTRAINT chk_status "
            "CHECK (status IN ('success', 'failed', 'skipped')); "
            "Better still, promote status to an ENUM so the database rejects "
            "anything undocumented at write time."
        ),
    ),
    Check(
        code="DQ-05",
        table_name="users",
        issue_type="unverified_user_transacting",
        severity="high",
        description=(
            "Users transacting while kyc_status is not 'verified'. Either the KYC "
            "gate is not enforced at the payment path, or kyc_status is not being "
            "updated on completion. Both are reportable; the first is a compliance "
            "exposure, not merely a data defect."
        ),
        detect_sql="""
            SELECT DISTINCT u.user_id::text AS txn_id
            FROM users u
            JOIN sip_daily_transactions t ON t.user_id = u.user_id
            WHERE u.kyc_status <> 'verified'
              AND t.status = 'success'
        """,
        impact_sql="""
            SELECT u.kyc_status,
                   COUNT(DISTINCT u.user_id) AS users_affected,
                   COUNT(*) AS successful_transactions,
                   ROUND(SUM(COALESCE(t.amount, 0)), 2) AS value_at_risk
            FROM users u
            JOIN sip_daily_transactions t ON t.user_id = u.user_id
            WHERE u.kyc_status <> 'verified'
              AND t.status = 'success'
            GROUP BY u.kyc_status
        """,
        proposed_fix=(
            "Confirm with engineering whether the gate is missing or the status "
            "field is stale — the fix differs entirely. If the gate is missing, "
            "block at the payment service. If the field is stale, make KYC "
            "completion update users.kyc_status transactionally with "
            "kyc_completed_at, which are currently able to disagree."
        ),
        reference_column="user_id",
    ),
    Check(
        code="DQ-06",
        table_name="users",
        issue_type="kyc_status_timestamp_mismatch",
        severity="medium",
        description=(
            "kyc_status and kyc_completed_at disagree: verified users with no "
            "completion timestamp, or unverified users that have one. The two "
            "fields are written separately and can drift, which makes any "
            "KYC-speed analysis unreliable."
        ),
        detect_sql="""
            SELECT user_id::text AS txn_id
            FROM users
            WHERE (kyc_status = 'verified' AND kyc_completed_at IS NULL)
               OR (kyc_status <> 'verified' AND kyc_completed_at IS NOT NULL)
        """,
        impact_sql=None,
        proposed_fix=(
            "Derive kyc_status from kyc_completed_at in a generated column, or "
            "write both in one transaction. Two fields encoding one fact will "
            "always drift eventually."
        ),
        reference_column="user_id",
    ),
]


def run_check(check: Check) -> Finding:
    """Execute one check and collect its row references and impact."""
    rows = db.read_sql(check.detect_sql)
    references = [str(value) for value in rows.iloc[:, 0].head(MAX_ROW_REFERENCES)]

    impact = ""
    if check.impact_sql and not rows.empty:
        impact_frame = db.read_sql(check.impact_sql)
        if not impact_frame.empty:
            impact = "; ".join(
                f"{column}={impact_frame[column].iat[0]}" for column in impact_frame.columns
            )

    finding = Finding(check=check, row_count=len(rows), references=references, impact=impact)
    level = logging.INFO if finding.passed else logging.WARNING
    logger.log(
        level,
        "%s %s: %s rows%s",
        check.code,
        check.issue_type,
        f"{finding.row_count:,}",
        f" — {impact}" if impact else "",
    )
    return finding


def run_all() -> list[Finding]:
    """Run every check in order."""
    return [run_check(check) for check in CHECKS]


def persist(findings: list[Finding], truncate: bool = True) -> int:
    """Write failing findings to ``data_quality_flags``.

    Only failures are written. A passing check has nothing to report, and filling
    the table with zero-row rows would make the genuine problems harder to see.
    """
    if truncate:
        db.execute("TRUNCATE data_quality_flags RESTART IDENTITY")

    rows = [
        {
            "table_name": f.check.table_name,
            "issue_type": f.check.issue_type,
            "row_reference": (
                f"{f.check.code} | {f.row_count} rows | "
                f"examples: {', '.join(f.references)}"
                + (f" | impact: {f.impact}" if f.impact else "")
            )[:4000],
            "proposed_fix": f.check.proposed_fix,
            "severity": f.check.severity,
        }
        for f in findings
        if not f.passed
    ]

    if not rows:
        logger.info("no data quality issues found")
        return 0

    db.bulk_load(
        pd.DataFrame(rows),
        "data_quality_flags",
        ["table_name", "issue_type", "row_reference", "proposed_fix", "severity"],
    )
    return len(rows)


def summary_frame(findings: list[Finding]) -> pd.DataFrame:
    """Tidy summary for logging and the weekly report."""
    return pd.DataFrame(
        [
            {
                "code": f.check.code,
                "table": f.check.table_name,
                "issue": f.check.issue_type,
                "severity": f.check.severity,
                "rows": f.row_count,
                "status": "PASS" if f.passed else "FAIL",
            }
            for f in findings
        ]
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    findings = run_all()
    written = persist(findings)

    summary = summary_frame(findings)
    logger.info("data quality summary:\n%s", summary.to_string(index=False))
    logger.info(
        "%d of %d checks failing, %d flags written",
        int((summary["status"] == "FAIL").sum()),
        len(summary),
        written,
    )


if __name__ == "__main__":
    main()
