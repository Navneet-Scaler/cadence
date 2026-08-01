"""Tests for the synthetic data generator.

These run entirely in memory — no database — and assert the properties the
analysis downstream depends on: reproducibility, valid archetype parameters,
correct experiment randomisation, and that each seeded data-quality defect is
present at the intended rate.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.simulate import generate_daily_sip_data as sim


@pytest.fixture(scope="module")
def small_config() -> sim.SimulationConfig:
    return sim.SimulationConfig(
        n_users=120,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 6, 30),
        random_seed=7,
    )


@pytest.fixture(scope="module")
def result(small_config: sim.SimulationConfig) -> sim.SimulationResult:
    return sim.simulate_all(small_config)


def test_archetype_weights_sum_to_one() -> None:
    assert sum(a.weight for a in sim.ARCHETYPES) == pytest.approx(1.0)


def test_archetype_parameter_ranges_are_valid_probabilities() -> None:
    for archetype in sim.ARCHETYPES:
        low, high = archetype.base_success_prob
        assert 0.0 < low < high <= 1.0, archetype.name
        low, high = archetype.return_propensity
        assert 0.0 < low < high <= 1.0, archetype.name
        low, high = archetype.decay_rate
        assert 0.0 < low < high, archetype.name


def test_simulation_is_reproducible(small_config: sim.SimulationConfig) -> None:
    """Same seed, same data — otherwise no finding in the memo is checkable."""
    first = sim.simulate_all(small_config)
    second = sim.simulate_all(small_config)
    pd.testing.assert_frame_equal(first.transactions, second.transactions)
    pd.testing.assert_frame_equal(first.users, second.users)


def test_every_user_has_exactly_one_profile_and_assignment(
    result: sim.SimulationResult, small_config: sim.SimulationConfig
) -> None:
    assert len(result.users) == small_config.n_users
    assert set(result.profiles["user_id"]) == set(result.users["user_id"])
    assert result.assignments["user_id"].is_unique


def test_experiment_arms_are_roughly_balanced(result: sim.SimulationResult) -> None:
    share = (result.assignments["arm"] == "treatment").mean()
    assert 0.35 < share < 0.65


def test_no_transaction_precedes_signup_before_seeding(result: sim.SimulationResult) -> None:
    """The behaviour model itself must be clean; pre-signup rows are seeded later."""
    signup = dict(zip(result.users["user_id"], result.users["signup_date"], strict=True))
    earliest = result.transactions.groupby("user_id")["txn_date"].min()
    assert all(earliest[uid] >= signup[uid] for uid in earliest.index)


def test_clean_run_has_no_null_amounts_or_duplicates(result: sim.SimulationResult) -> None:
    assert result.transactions["amount"].notna().all()
    assert not result.transactions.duplicated(subset=["user_id", "txn_date"]).any()


def test_only_known_statuses_before_seeding(result: sim.SimulationResult) -> None:
    assert set(result.transactions["status"]) <= {"success", "failed", "skipped"}


def test_nudges_only_go_to_treatment_users(result: sim.SimulationResult) -> None:
    treatment = set(result.assignments.loc[result.assignments["arm"] == "treatment", "user_id"])
    assert set(result.nudges["user_id"]) <= treatment


def test_nudge_thresholds_come_from_the_configured_set(result: sim.SimulationResult) -> None:
    assert set(result.nudges["days_missed_at_send"]) <= set(sim.NUDGE_THRESHOLD_CHOICES)


def test_archetypes_produce_distinguishable_behaviour(result: sim.SimulationResult) -> None:
    """If the archetypes don't separate, segmentation downstream is meaningless."""
    successes = result.transactions[result.transactions["status"] == "success"]
    per_user = successes.groupby("user_id").size().rename("days")
    merged = result.profiles.merge(per_user, on="user_id", how="left").fillna({"days": 0})
    means = merged.groupby("archetype")["days"].mean()

    assert means["sticky_former"] > means["early_dropper"] * 3


def test_seeded_issues_appear_at_the_configured_rates(result: sim.SimulationResult) -> None:
    rng = np.random.default_rng(99)
    n_clean = len(result.transactions)
    corrupted = sim.seed_quality_issues(result.transactions, result.users, rng)

    # DQ-01 null amounts
    assert corrupted["amount"].isna().sum() == int(n_clean * sim.SEED_RATES["null_amount"])

    # DQ-02 duplicates, DQ-03 pre-signup rows: both add rows
    expected_added = int(n_clean * sim.SEED_RATES["duplicate_txn"]) + int(
        n_clean * sim.SEED_RATES["txn_before_signup"]
    )
    assert len(corrupted) == n_clean + expected_added

    # DQ-04 undocumented status value
    assert (corrupted["status"] == "SUCCESS").sum() > 0

    # DQ-03 is actually detectable
    signup = dict(zip(result.users["user_id"], result.users["signup_date"], strict=True))
    before = sum(
        1
        for uid, txn in zip(corrupted["user_id"], corrupted["txn_date"], strict=True)
        if txn < signup[uid]
    )
    assert before == int(n_clean * sim.SEED_RATES["txn_before_signup"])


def test_seeding_does_not_mutate_the_clean_frame(result: sim.SimulationResult) -> None:
    rng = np.random.default_rng(5)
    before = result.transactions.copy()
    sim.seed_quality_issues(result.transactions, result.users, rng)
    pd.testing.assert_frame_equal(result.transactions, before)


def test_payday_multiplier_peaks_in_the_first_week() -> None:
    assert sim._payday_multiplier(3) > sim._payday_multiplier(10)
    assert sim._payday_multiplier(10) > sim._payday_multiplier(20)
    assert sim._payday_multiplier(20) > sim._payday_multiplier(28)


def test_load_refuses_to_destroy_existing_users_without_reseed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards downstream projects whose foreign keys point at users(user_id).

    TRUNCATE ... RESTART IDENTITY CASCADE would orphan every dependent row, so a
    plain run must refuse rather than do it silently.
    """
    monkeypatch.setattr(sim, "count_existing_users", lambda: 5000)
    monkeypatch.setattr(sim, "truncate_all", lambda: pytest.fail("must not truncate"))
    monkeypatch.setattr(sim, "load", lambda *a, **k: pytest.fail("must not load"))
    monkeypatch.setattr("sys.argv", ["generate_daily_sip_data", "--users", "5", "--seed", "1"])

    with pytest.raises(sim.ExistingDataError, match="--reseed"):
        sim.main()
