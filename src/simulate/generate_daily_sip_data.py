"""Synthetic daily-SIP behaviour generator.

The point of this module is that the data is *behavioural*, not random. Survival
curves computed off a flat coin flip are exponential by construction and tell you
nothing; the shapes here come from a per-user state machine whose parameters are
recorded in ``sim_user_profile`` so the analysis can be checked against what was
actually planted.

Model
-----
Each user walks their own timeline one day at a time in one of two states:

* **active** — currently holding a streak. Each day they invest with probability
  ``base_success_prob``, modulated by day-of-week and payday seasonality and by a
  mild habit-strength term (a long streak is a stronger habit and is slightly
  harder to break, which is what makes the survival curve concave rather than a
  straight exponential).

* **lapsed** — inside a gap of ``k`` consecutive missed days. The chance of coming
  back on any given day is ``return_propensity * exp(-decay_rate * k)``. That
  exponential decay in ``k`` is the single most important line in the file: it is
  what makes the first missed day recoverable and the seventh missed day close to
  terminal, which is the entire question the project exists to answer.

Archetypes
----------
Four behavioural profiles with different parameter draws, so segmentation has
something real to find rather than noise:

===================  ===========================================================
sticky_former        Forms the habit. High continuation, slow gap decay, comes back.
early_dropper        Churns in the first fortnight. Fast gap decay, rarely returns.
weekday_only         Reliable Mon-Fri, systematically skips weekends.
payday_spiker        Invests hard in the first week after salary, fades mid-month.
===================  ===========================================================

Experiment
----------
Half the users are randomised into a ``treatment`` arm at signup — before any
behaviour is observed, so the comparison is not contaminated by selection.
Treatment users receive a nudge when their gap first reaches their assigned
threshold, which multiplies the return probability for a short window. Each user
draws a threshold from {1, 2, 3, 5, 7} so the effect can be segmented by
days-missed-at-send, which is what identifies the highest-leverage day.

Data quality seeding
--------------------
Realistic mess is injected *after* the clean behavioural data is generated, so it
is a corruption layer rather than something tangled into the behaviour model.
See ``SEED_RATES`` and ``data_quality_findings.md``.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from src import db

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

ACQUISITION_CHANNELS = ["organic", "referral", "paid_social", "influencer", "app_store"]
ACQUISITION_WEIGHTS = [0.24, 0.21, 0.30, 0.15, 0.10]

CITY_TIERS = ["tier_1", "tier_2", "tier_3"]
CITY_TIER_WEIGHTS = [0.42, 0.36, 0.22]

NUDGE_TYPES = ["push", "sms", "email"]
NUDGE_TYPE_WEIGHTS = [0.70, 0.20, 0.10]

EXPERIMENT_NAME = "daily_sip_gap_nudge_v1"

# Fraction of rows corrupted for each seeded data-quality issue.
SEED_RATES = {
    "null_amount": 0.015,  # DQ-01
    "duplicate_txn": 0.010,  # DQ-02
    "txn_before_signup": 0.004,  # DQ-03
    "invalid_status": 0.002,  # DQ-04
}

# Day-of-week multipliers on the daily invest probability (Mon=0 ... Sun=6).
# Weekend softness is real in Indian retail fintech: UPI autopay mandates present
# on banking days and discretionary top-ups cluster on weekdays.
DOW_MULTIPLIER = np.array([1.04, 1.03, 1.00, 1.00, 1.02, 0.88, 0.84])

# Weekend multiplier applied on top, for the weekday_only archetype.
WEEKDAY_ONLY_WEEKEND_MULTIPLIER = 0.12

# Nudge effect: multiplies return probability, and persists for this many days.
NUDGE_UPLIFT = 2.35
NUDGE_EFFECT_WINDOW_DAYS = 3
NUDGE_THRESHOLD_CHOICES = [1, 2, 3, 5, 7]

# Payment-rail noise, applied to days the user *intended* to invest.
FAILED_TXN_RATE = 0.028  # mandate/payment failure — the product's fault
SKIPPED_TXN_RATE = 0.012  # user explicitly opted out for the day

SIP_AMOUNTS = [21.00, 21.00, 21.00, 51.00, 101.00]
SIP_AMOUNT_WEIGHTS = [0.62, 0.10, 0.06, 0.14, 0.08]


@dataclass(frozen=True)
class Archetype:
    """A behavioural profile and the parameter ranges its users are drawn from."""

    name: str
    weight: float
    base_success_prob: tuple[float, float]
    decay_rate: tuple[float, float]
    return_propensity: tuple[float, float]
    habit_strength: float = 0.0  # per-day bonus to continuation as a streak lengthens
    weekend_averse: bool = False
    payday_driven: bool = False


ARCHETYPES: list[Archetype] = [
    Archetype(
        name="sticky_former",
        weight=0.24,
        base_success_prob=(0.955, 0.988),
        decay_rate=(0.015, 0.055),
        return_propensity=(0.42, 0.62),
        habit_strength=0.0016,
    ),
    Archetype(
        name="early_dropper",
        weight=0.31,
        base_success_prob=(0.780, 0.880),
        decay_rate=(0.240, 0.420),
        return_propensity=(0.06, 0.16),
        habit_strength=0.0002,
    ),
    Archetype(
        name="weekday_only",
        weight=0.25,
        base_success_prob=(0.900, 0.960),
        decay_rate=(0.075, 0.150),
        return_propensity=(0.28, 0.45),
        habit_strength=0.0009,
        weekend_averse=True,
    ),
    Archetype(
        name="payday_spiker",
        weight=0.20,
        base_success_prob=(0.860, 0.935),
        decay_rate=(0.110, 0.210),
        return_propensity=(0.20, 0.38),
        habit_strength=0.0006,
        payday_driven=True,
    ),
]


@dataclass
class SimulationConfig:
    """Everything the run depends on, so a run is reproducible from one object."""

    n_users: int = 5_000
    start_date: date = date(2025, 1, 1)
    end_date: date = date(2025, 12, 31)
    random_seed: int = 42
    seed_quality_issues: bool = True
    signup_window_fraction: float = 0.62  # signups stop this far into the window

    @classmethod
    def from_env(cls) -> SimulationConfig:
        """Build config from environment variables, falling back to defaults."""
        return cls(
            n_users=int(os.getenv("SIM_N_USERS", "5000")),
            start_date=date.fromisoformat(os.getenv("SIM_START_DATE", "2025-01-01")),
            end_date=date.fromisoformat(os.getenv("SIM_END_DATE", "2025-12-31")),
            random_seed=int(os.getenv("SIM_RANDOM_SEED", "42")),
        )


@dataclass
class SimulationResult:
    """The generated tables, ready to load."""

    users: pd.DataFrame = field(default_factory=pd.DataFrame)
    profiles: pd.DataFrame = field(default_factory=pd.DataFrame)
    transactions: pd.DataFrame = field(default_factory=pd.DataFrame)
    nudges: pd.DataFrame = field(default_factory=pd.DataFrame)
    assignments: pd.DataFrame = field(default_factory=pd.DataFrame)


# --------------------------------------------------------------------------- #
# User generation
# --------------------------------------------------------------------------- #


def _payday_multiplier(day_of_month: int) -> float:
    """Salary-cycle effect: a spike in the first week, a trough before month end."""
    if day_of_month <= 7:
        return 1.18
    if day_of_month <= 14:
        return 1.02
    if day_of_month <= 24:
        return 0.94
    return 0.82


def generate_users(
    config: SimulationConfig, rng: np.random.Generator
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create the ``users`` rows and their matching simulation ground truth.

    Signups are spread over the first ``signup_window_fraction`` of the window so
    that even the last cohort has room to exhibit a streak before the data ends.
    Users signing up near the end are right-censored, which is realistic and is
    handled explicitly by the Kaplan-Meier estimator downstream.
    """
    n = config.n_users
    total_days = (config.end_date - config.start_date).days
    signup_span = int(total_days * config.signup_window_fraction)

    # Slight growth over time: later weeks acquire more users than earlier ones.
    day_offsets = np.sort(rng.triangular(0, signup_span * 0.75, signup_span, size=n).astype(int))
    signup_dates = [config.start_date + timedelta(days=int(d)) for d in day_offsets]

    archetype_names = [a.name for a in ARCHETYPES]
    archetype_weights = np.array([a.weight for a in ARCHETYPES], dtype=float)
    archetype_weights /= archetype_weights.sum()
    assigned = rng.choice(archetype_names, size=n, p=archetype_weights)

    channels = rng.choice(ACQUISITION_CHANNELS, size=n, p=ACQUISITION_WEIGHTS)
    tiers = rng.choice(CITY_TIERS, size=n, p=CITY_TIER_WEIGHTS)

    # KYC: paid_social skews toward slower/incomplete verification, which gives the
    # Cox model a genuine confound to surface rather than an invented one.
    kyc_status: list[str] = []
    kyc_completed: list[datetime | None] = []
    for i in range(n):
        pending_prob = 0.14 if channels[i] == "paid_social" else 0.07
        roll = rng.random()
        if roll < pending_prob:
            kyc_status.append("pending")
            kyc_completed.append(None)
        elif roll < pending_prob + 0.025:
            kyc_status.append("rejected")
            kyc_completed.append(None)
        else:
            kyc_status.append("verified")
            lag_hours = float(rng.gamma(shape=2.0, scale=14.0))
            kyc_completed.append(
                datetime.combine(signup_dates[i], datetime.min.time())
                + timedelta(hours=min(lag_hours, 24 * 21))
            )

    users = pd.DataFrame(
        {
            "user_id": np.arange(1, n + 1),
            "signup_date": signup_dates,
            "acquisition_channel": channels,
            "city_tier": tiers,
            "kyc_status": kyc_status,
            "kyc_completed_at": kyc_completed,
        }
    )

    lookup = {a.name: a for a in ARCHETYPES}
    profiles = pd.DataFrame(
        {
            "user_id": users["user_id"],
            "archetype": assigned,
            "base_success_prob": [
                round(float(rng.uniform(*lookup[a].base_success_prob)), 4) for a in assigned
            ],
            "decay_rate": [round(float(rng.uniform(*lookup[a].decay_rate)), 4) for a in assigned],
            "return_propensity": [
                round(float(rng.uniform(*lookup[a].return_propensity)), 4) for a in assigned
            ],
        }
    )

    logger.info("generated %s users across %d archetypes", f"{n:,}", len(ARCHETYPES))
    return users, profiles


def assign_experiment_arms(
    users: pd.DataFrame, rng: np.random.Generator
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """Randomise users 50/50 into treatment and control at signup.

    Assignment happens at signup time, before any behaviour is observed, so the
    arms are exchangeable and the later two-proportion test is a clean causal
    contrast rather than an after-the-fact split on outcomes.

    Returns:
        The ``experiment_assignments`` rows, a boolean treatment mask, and each
        user's nudge threshold (days missed before the nudge fires).
    """
    n = len(users)
    is_treatment = rng.random(n) < 0.5
    thresholds = rng.choice(NUDGE_THRESHOLD_CHOICES, size=n)

    assignments = pd.DataFrame(
        {
            "user_id": users["user_id"],
            "experiment_name": EXPERIMENT_NAME,
            "arm": np.where(is_treatment, "treatment", "control"),
            "assigned_at": [datetime.combine(d, datetime.min.time()) for d in users["signup_date"]],
        }
    )
    logger.info(
        "experiment arms: %s treatment / %s control",
        f"{int(is_treatment.sum()):,}",
        f"{int((~is_treatment).sum()):,}",
    )
    return assignments, is_treatment, thresholds


# --------------------------------------------------------------------------- #
# Behavioural walk
# --------------------------------------------------------------------------- #


def simulate_user_timeline(
    user_id: int,
    signup: date,
    end_date: date,
    archetype: Archetype,
    base_prob: float,
    decay_rate: float,
    return_propensity: float,
    is_treatment: bool,
    nudge_threshold: int,
    rng: np.random.Generator,
) -> tuple[list[tuple], list[tuple]]:
    """Walk one user day by day, emitting transactions and any nudges sent.

    This is the state machine described in the module docstring. It is written as
    an explicit loop rather than vectorised because the return probability depends
    on the *current* gap length, which depends on every prior day — there is no
    closed form to vectorise over.

    Returns:
        ``(transaction_rows, nudge_rows)`` as plain tuples for cheap DataFrame
        construction.
    """
    transactions: list[tuple] = []
    nudges: list[tuple] = []

    current_day = signup
    streak_len = 0  # consecutive successful days right now
    gap_len = 0  # consecutive missed days right now
    nudge_fired_this_gap = False
    nudge_effect_remaining = 0

    while current_day <= end_date:
        dow = current_day.weekday()
        seasonality = DOW_MULTIPLIER[dow]
        if archetype.weekend_averse and dow >= 5:
            seasonality *= WEEKDAY_ONLY_WEEKEND_MULTIPLIER
        if archetype.payday_driven:
            seasonality *= _payday_multiplier(current_day.day)

        if gap_len == 0:
            # Active: habit strength makes a long streak slightly stickier.
            p_invest = base_prob * seasonality + archetype.habit_strength * min(streak_len, 60)
        else:
            # Lapsed: chance of return decays exponentially in the gap length.
            p_invest = return_propensity * np.exp(-decay_rate * gap_len) * seasonality
            if nudge_effect_remaining > 0:
                p_invest *= NUDGE_UPLIFT

        p_invest = float(np.clip(p_invest, 0.0, 0.995))

        if rng.random() < p_invest:
            roll = rng.random()
            if roll < FAILED_TXN_RATE:
                status = "failed"
            elif roll < FAILED_TXN_RATE + SKIPPED_TXN_RATE:
                status = "skipped"
            else:
                status = "success"

            amount = float(rng.choice(SIP_AMOUNTS, p=SIP_AMOUNT_WEIGHTS))
            created = datetime.combine(current_day, datetime.min.time()) + timedelta(
                hours=float(rng.uniform(6, 23)), minutes=float(rng.uniform(0, 59))
            )
            transactions.append((user_id, current_day, amount, status, created))

            if status == "success":
                # Only a successful contribution continues the habit.
                streak_len += 1
                gap_len = 0
                nudge_fired_this_gap = False
                nudge_effect_remaining = 0
            else:
                # A failed or skipped attempt still breaks the streak.
                streak_len = 0
                gap_len += 1
        else:
            streak_len = 0
            gap_len += 1

        # Fire the nudge the first time this gap reaches the user's threshold.
        if (
            is_treatment
            and gap_len == nudge_threshold
            and not nudge_fired_this_gap
            and current_day < end_date
        ):
            nudge_type = str(rng.choice(NUDGE_TYPES, p=NUDGE_TYPE_WEIGHTS))
            nudges.append((user_id, current_day, gap_len, nudge_type))
            nudge_fired_this_gap = True
            nudge_effect_remaining = NUDGE_EFFECT_WINDOW_DAYS

        if nudge_effect_remaining > 0:
            nudge_effect_remaining -= 1

        # Long-dormant users stop being simulated: once a gap is very long the
        # return probability is effectively zero and the loop is wasted work.
        if gap_len > 120:
            break

        current_day += timedelta(days=1)

    return transactions, nudges


def simulate_all(config: SimulationConfig) -> SimulationResult:
    """Run the full simulation and return every table, uncorrupted."""
    rng = np.random.default_rng(config.random_seed)
    users, profiles = generate_users(config, rng)
    assignments, is_treatment, thresholds = assign_experiment_arms(users, rng)

    lookup = {a.name: a for a in ARCHETYPES}
    all_txns: list[tuple] = []
    all_nudges: list[tuple] = []

    signup_dates = list(users["signup_date"])
    archetype_names = list(profiles["archetype"])
    base_probs = list(profiles["base_success_prob"])
    decay_rates = list(profiles["decay_rate"])
    returns = list(profiles["return_propensity"])

    for i in range(len(users)):
        txns, nudges = simulate_user_timeline(
            user_id=int(users["user_id"].iat[i]),
            signup=signup_dates[i],
            end_date=config.end_date,
            archetype=lookup[archetype_names[i]],
            base_prob=float(base_probs[i]),
            decay_rate=float(decay_rates[i]),
            return_propensity=float(returns[i]),
            is_treatment=bool(is_treatment[i]),
            nudge_threshold=int(thresholds[i]),
            rng=rng,
        )
        all_txns.extend(txns)
        all_nudges.extend(nudges)

        if (i + 1) % 1000 == 0:
            logger.info("simulated %s / %s users", f"{i + 1:,}", f"{len(users):,}")

    transactions = pd.DataFrame(
        all_txns, columns=["user_id", "txn_date", "amount", "status", "created_at"]
    )
    nudge_df = pd.DataFrame(
        all_nudges, columns=["user_id", "sent_date", "days_missed_at_send", "nudge_type"]
    )

    logger.info(
        "generated %s transactions (%s successful) and %s nudges",
        f"{len(transactions):,}",
        f"{int((transactions['status'] == 'success').sum()):,}",
        f"{len(nudge_df):,}",
    )
    return SimulationResult(
        users=users,
        profiles=profiles,
        transactions=transactions,
        nudges=nudge_df,
        assignments=assignments,
    )


# --------------------------------------------------------------------------- #
# Data quality seeding
# --------------------------------------------------------------------------- #


def seed_quality_issues(
    transactions: pd.DataFrame, users: pd.DataFrame, rng: np.random.Generator
) -> pd.DataFrame:
    """Corrupt a copy of the clean transactions with realistic ledger defects.

    Applied as a separate layer *after* the behaviour model so the planted
    behaviour stays recoverable and each defect maps to exactly one DQ finding.

    Seeded defects:
        DQ-01 null_amount        — amount lost between the payment gateway and the ledger
        DQ-02 duplicate_txn      — retried write with no idempotency key
        DQ-03 txn_before_signup  — client-supplied timestamp trusted over server time
        DQ-04 invalid_status     — an undocumented status value the schema permits
    """
    df = transactions.copy()
    n = len(df)

    # DQ-01: NULL amounts.
    null_idx = rng.choice(n, size=int(n * SEED_RATES["null_amount"]), replace=False)
    df.loc[df.index[null_idx], "amount"] = np.nan

    # DQ-04: a status value nothing downstream knows how to handle.
    bad_status_idx = rng.choice(n, size=int(n * SEED_RATES["invalid_status"]), replace=False)
    df.loc[df.index[bad_status_idx], "status"] = "SUCCESS"  # wrong case, undocumented

    # DQ-02: duplicated rows for the same (user_id, txn_date), written moments apart.
    dup_idx = rng.choice(n, size=int(n * SEED_RATES["duplicate_txn"]), replace=False)
    duplicates = df.iloc[dup_idx].copy()
    duplicates["created_at"] = duplicates["created_at"] + pd.to_timedelta(
        rng.integers(1, 400, size=len(duplicates)), unit="s"
    )

    # DQ-03: transactions dated before the user ever signed up.
    signup_map = dict(zip(users["user_id"], users["signup_date"], strict=True))
    pre_idx = rng.choice(n, size=int(n * SEED_RATES["txn_before_signup"]), replace=False)
    pre_signup = df.iloc[pre_idx].copy()
    pre_signup["txn_date"] = [
        signup_map[uid] - timedelta(days=int(rng.integers(1, 30))) for uid in pre_signup["user_id"]
    ]

    corrupted = pd.concat([df, duplicates, pre_signup], ignore_index=True)
    corrupted = corrupted.sort_values(["user_id", "txn_date", "created_at"]).reset_index(drop=True)

    logger.info(
        "seeded data quality issues: %s null amounts, %s duplicates, %s pre-signup, %s invalid status",
        f"{len(null_idx):,}",
        f"{len(duplicates):,}",
        f"{len(pre_signup):,}",
        f"{len(bad_status_idx):,}",
    )
    return corrupted


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #


def truncate_all() -> None:
    """Clear every table so a re-run is deterministic rather than additive."""
    db.execute(
        "TRUNCATE streak_observations, user_streaks, data_quality_flags, "
        "experiment_assignments, nudges_sent, sip_daily_transactions, "
        "sim_user_profile, users RESTART IDENTITY CASCADE"
    )
    logger.info("truncated all tables")


def load(result: SimulationResult, transactions: pd.DataFrame) -> None:
    """COPY every generated table into Postgres."""
    db.bulk_load(
        result.users,
        "users",
        [
            "user_id",
            "signup_date",
            "acquisition_channel",
            "city_tier",
            "kyc_status",
            "kyc_completed_at",
        ],
    )
    db.reset_sequence("users", "user_id")
    db.bulk_load(
        result.profiles,
        "sim_user_profile",
        ["user_id", "archetype", "base_success_prob", "decay_rate", "return_propensity"],
    )
    db.bulk_load(
        result.assignments,
        "experiment_assignments",
        ["user_id", "experiment_name", "arm", "assigned_at"],
    )
    db.bulk_load(
        transactions,
        "sip_daily_transactions",
        ["user_id", "txn_date", "amount", "status", "created_at"],
    )
    db.bulk_load(
        result.nudges,
        "nudges_sent",
        ["user_id", "sent_date", "days_missed_at_send", "nudge_type"],
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    parser = argparse.ArgumentParser(description="Generate and load synthetic daily SIP data.")
    parser.add_argument("--users", type=int, default=None, help="override SIM_N_USERS")
    parser.add_argument("--seed", type=int, default=None, help="override SIM_RANDOM_SEED")
    parser.add_argument("--clean", action="store_true", help="skip data-quality seeding")
    parser.add_argument(
        "--no-load", action="store_true", help="generate but do not write to the DB"
    )
    args = parser.parse_args()

    config = SimulationConfig.from_env()
    if args.users is not None:
        config.n_users = args.users
    if args.seed is not None:
        config.random_seed = args.seed
    if args.clean:
        config.seed_quality_issues = False

    logger.info(
        "simulating %s users from %s to %s (seed=%d)",
        f"{config.n_users:,}",
        config.start_date,
        config.end_date,
        config.random_seed,
    )

    result = simulate_all(config)

    transactions = result.transactions
    if config.seed_quality_issues:
        rng = np.random.default_rng(config.random_seed + 1)
        transactions = seed_quality_issues(transactions, result.users, rng)

    if args.no_load:
        logger.info("--no-load set; skipping database write")
        return

    truncate_all()
    load(result, transactions)
    logger.info("simulation complete")


if __name__ == "__main__":
    main()
