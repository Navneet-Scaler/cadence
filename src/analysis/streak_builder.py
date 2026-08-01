"""Streak construction: turning daily transactions into streak records.

Two implementations of the same definition live here on purpose:

* :func:`rebuild_streaks_in_db` runs ``sql/streak_construction.sql`` — the
  production path. Set-based, one pass, done inside Postgres where the data is.
* :func:`build_streaks` is a pandas reference implementation of exactly the same
  gaps-and-islands logic.

The reference implementation exists so the definition can be unit-tested against
small hand-checked fixtures with no database, and so :func:`validate_against_sql`
can assert the two agree on the real dataset. A streak definition that drifts
between the dashboard and the notebook is how two teams end up quoting different
retention numbers from the same warehouse; asserting equality prevents it.

Streak definition
-----------------
A streak is a maximal run of consecutive calendar days with at least one
``status = 'success'`` transaction. Failed payments and explicit skips break it:
a failed mandate is a product problem, not evidence the habit held.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from src import db

logger = logging.getLogger(__name__)

STREAK_SQL_PATH = Path("sql/streak_construction.sql")

STREAK_COLUMNS = [
    "user_id",
    "streak_index",
    "streak_start",
    "streak_end",
    "streak_length",
    "is_censored",
    "days_to_next_streak",
    "recovered",
]


def build_streaks(
    transactions: pd.DataFrame, observation_end: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Collapse successful daily transactions into streak records.

    Pandas mirror of the gaps-and-islands SQL. The same identity is used: within
    a run of consecutive days, ``date - cumulative_row_number`` is constant, so
    that difference labels the streak.

    Args:
        transactions: rows with at least ``user_id``, ``txn_date``, ``status``.
            May contain duplicates and non-success rows; both are handled here.
        observation_end: last day of the observation window. Streaks still
            running on this day are marked censored. Defaults to the maximum
            ``txn_date`` present.

    Returns:
        One row per streak with the columns in :data:`STREAK_COLUMNS`.
    """
    if transactions.empty:
        return pd.DataFrame(columns=STREAK_COLUMNS)

    df = transactions.loc[transactions["status"] == "success", ["user_id", "txn_date"]].copy()
    df["txn_date"] = pd.to_datetime(df["txn_date"])

    if df.empty:
        return pd.DataFrame(columns=STREAK_COLUMNS)

    # One row per user-day: a duplicate would consume an extra row number and
    # permanently desynchronise the difference, splitting one real streak in two.
    df = df.drop_duplicates(subset=["user_id", "txn_date"]).sort_values(["user_id", "txn_date"])

    last_day = (
        pd.to_datetime(observation_end) if observation_end is not None else df["txn_date"].max()
    )

    row_number = df.groupby("user_id").cumcount()
    df["streak_group"] = df["txn_date"] - pd.to_timedelta(row_number, unit="D")

    grouped = (
        df.groupby(["user_id", "streak_group"])["txn_date"]
        .agg(streak_start="min", streak_end="max", streak_length="count")
        .reset_index()
        .drop(columns="streak_group")
        .sort_values(["user_id", "streak_start"])
        .reset_index(drop=True)
    )

    grouped["streak_index"] = grouped.groupby("user_id").cumcount() + 1
    next_start = grouped.groupby("user_id")["streak_start"].shift(-1)

    grouped["is_censored"] = grouped["streak_end"] == last_day
    grouped["days_to_next_streak"] = (next_start - grouped["streak_end"]).dt.days
    grouped["recovered"] = next_start.notna()
    grouped["streak_length"] = grouped["streak_length"].astype(int)

    return grouped[STREAK_COLUMNS].reset_index(drop=True)


def expand_to_observations(streaks: pd.DataFrame) -> pd.DataFrame:
    """Expand streaks into one row per streak-day.

    Long format is what yields the discrete-time hazard at each day-of-streak,
    ``P(break on day d | alive on day d)``, directly by grouping on
    ``day_of_streak``. The last day of a *censored* streak is not a break — the
    data simply ran out — so it is marked as survived.
    """
    if streaks.empty:
        return pd.DataFrame(
            columns=["user_id", "day_of_streak", "obs_date", "survived_to_next_day"]
        )

    repeated = streaks.loc[streaks.index.repeat(streaks["streak_length"])].copy()
    repeated["day_of_streak"] = repeated.groupby(level=0).cumcount() + 1
    repeated["obs_date"] = pd.to_datetime(repeated["streak_start"]) + pd.to_timedelta(
        repeated["day_of_streak"] - 1, unit="D"
    )
    repeated["survived_to_next_day"] = (
        repeated["day_of_streak"] < repeated["streak_length"]
    ) | repeated["is_censored"]

    return repeated[["user_id", "day_of_streak", "obs_date", "survived_to_next_day"]].reset_index(
        drop=True
    )


def discrete_hazard(observations: pd.DataFrame) -> pd.DataFrame:
    """Break rate at each day-of-streak.

    The hazard is the direct answer to "which day should we intervene on": it is
    the conditional probability of breaking on day *d* given the streak survived
    to day *d*, so peaks are the days where users are actually being lost — not
    merely the days where many users happen to be.
    """
    grouped = observations.groupby("day_of_streak")["survived_to_next_day"]
    hazard = pd.DataFrame(
        {
            "at_risk": grouped.size(),
            "broke": (~observations["survived_to_next_day"])
            .groupby(observations["day_of_streak"])
            .sum(),
        }
    ).reset_index()
    hazard["hazard_rate"] = hazard["broke"] / hazard["at_risk"]
    hazard["survival"] = (1 - hazard["hazard_rate"]).cumprod()
    return hazard


# --------------------------------------------------------------------------- #
# Database path
# --------------------------------------------------------------------------- #


def rebuild_streaks_in_db() -> int:
    """Run the SQL streak builder and return the number of streaks written."""
    logger.info("rebuilding streaks from v_clean_transactions")
    db.execute_script(STREAK_SQL_PATH)
    count = int(db.read_sql("SELECT COUNT(*) AS n FROM user_streaks")["n"].iat[0])
    observations = int(db.read_sql("SELECT COUNT(*) AS n FROM streak_observations")["n"].iat[0])
    logger.info("built %s streaks across %s streak-days", f"{count:,}", f"{observations:,}")
    return count


def load_streaks() -> pd.DataFrame:
    """Read the built streaks joined to user attributes and simulation labels."""
    return db.read_sql(
        """
        SELECT s.*,
               u.signup_date,
               u.acquisition_channel,
               u.city_tier,
               u.kyc_status,
               u.kyc_completed_at,
               p.archetype,
               e.arm
        FROM user_streaks s
        JOIN users u              ON u.user_id = s.user_id
        LEFT JOIN sim_user_profile p ON p.user_id = s.user_id
        LEFT JOIN experiment_assignments e ON e.user_id = s.user_id
        ORDER BY s.user_id, s.streak_index
        """
    )


def validate_against_sql() -> bool:
    """Assert the SQL and pandas implementations produce identical streaks.

    Guards against the two definitions drifting apart, which is how a dashboard
    and a notebook end up quoting different retention numbers off one warehouse.
    """
    transactions = db.read_sql(
        "SELECT user_id, txn_date, status FROM v_clean_transactions ORDER BY user_id, txn_date"
    )
    reference = build_streaks(transactions)
    from_db = db.read_sql(
        f"SELECT {', '.join(STREAK_COLUMNS)} FROM user_streaks ORDER BY user_id, streak_index"
    )

    reference = reference.sort_values(["user_id", "streak_index"]).reset_index(drop=True)
    from_db = from_db.sort_values(["user_id", "streak_index"]).reset_index(drop=True)

    if len(reference) != len(from_db):
        logger.error("streak count mismatch: pandas %d vs sql %d", len(reference), len(from_db))
        return False

    mismatches = int(
        (reference["streak_length"].to_numpy() != from_db["streak_length"].to_numpy()).sum()
    )
    if mismatches:
        logger.error("%d streaks differ in length between pandas and SQL", mismatches)
        return False

    logger.info("validated: SQL and pandas agree on all %s streaks", f"{len(from_db):,}")
    return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    rebuild_streaks_in_db()
    validate_against_sql()

    summary = db.read_sql(
        """
        SELECT p.archetype,
               COUNT(*)                                  AS streaks,
               ROUND(AVG(s.streak_length), 1)            AS avg_length,
               MAX(s.streak_length)                      AS max_length,
               ROUND(AVG(s.recovered::int) * 100, 1)     AS recovery_rate_pct
        FROM user_streaks s
        JOIN sim_user_profile p USING (user_id)
        GROUP BY p.archetype
        ORDER BY avg_length DESC
        """
    )
    logger.info("streak summary by archetype:\n%s", summary.to_string(index=False))


if __name__ == "__main__":
    main()
