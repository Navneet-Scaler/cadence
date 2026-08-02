"""Cohort retention, redefined for a daily habit.

Why the standard definition is the wrong tool
---------------------------------------------
The industry convention — D1/D7/D30/D90 measured from signup — is kept here,
because a founder already knows how to read it. What is redefined is the word
*retained*.

The usual definition is "the account still exists" or "the user opened the app in
the window". For a product whose entire thesis is a ₹21 contribution **every
day**, that measures almost nothing: a user who invested once in week one and
never again is "retained at D30" under an account-based definition, while being
exactly the churn the business needs to see.

So this module reports three definitions side by side, because the gap *between*
them is itself the finding. Two look backwards and are nested; the third looks
forwards and deliberately is not:

``active_on_day`` *(backward-looking, strictest)*
    Did the user successfully invest on exactly day *N*? The reading that matches
    the product promise. Noisy for weekday-only investors, since day *N* may land
    on a Saturday.

``active_in_window`` *(backward-looking, a superset of the above)*
    Did the user invest at all in the 7 days ending on day *N*? Absorbs
    day-of-week effects, so it is the fairest single number for comparing
    cohorts, and it is the one the memo quotes.

``ever_active_after`` *(forward-looking — not comparable as "looser")*
    Did the user invest at any point on or **after** day *N*? This is closest to
    what a conventional retention chart claims, and it is reported to show how
    much that convention flatters. It is not a third step on the same scale: it
    asks about the future rather than the recent past, so it can sit either side
    of ``active_in_window``. At D1 it is higher (89% vs 83% — plenty of users have
    a future, few invested in a window that barely exists yet); by D3 it is lower
    (82% vs 89%), because surviving *from* day 3 onward is a harder test than
    having invested in the week ending on day 3. Where the two cross is roughly
    where "has an account" stops resembling "has a habit".

Exposure and censoring
----------------------
A cohort that signed up 40 days before the data ends cannot have a D90 number.
Every metric here is computed only over users with **full exposure** to the
horizon, and cohorts without it return NaN rather than a small number computed
from a partial denominator. Silently dividing by whoever happens to be present is
how retention charts end up showing a fake cliff in the most recent cohorts.
"""

from __future__ import annotations

import logging

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src import db
from src.analysis import viz

logger = logging.getLogger(__name__)

# The conventional milestones, kept so the output is legible to anyone who has
# read a retention deck before.
HORIZONS = [1, 3, 7, 14, 30, 60, 90]

# Width of the trailing window for ``active_in_window``, in days.
WINDOW_DAYS = 7

# Cohorts smaller than this are dropped: a 12-user cohort produces a retention
# curve that swings 8 points per user and reads as signal.
MIN_COHORT_SIZE = 50


def load_activity() -> tuple[pd.DataFrame, pd.Timestamp]:
    """Load one row per user-day of successful investing, plus the window end.

    Reads ``v_clean_transactions`` so dedup and signup-date validity are already
    applied — a duplicated row would otherwise inflate nothing here, but a
    pre-signup row would produce a negative day index.
    """
    activity = db.read_sql(
        """
        SELECT t.user_id,
               t.txn_date,
               u.signup_date,
               u.acquisition_channel,
               u.city_tier
        FROM v_clean_transactions t
        JOIN users u ON u.user_id = t.user_id
        WHERE t.status = 'success'
        ORDER BY t.user_id, t.txn_date
        """
    )
    observation_end = pd.to_datetime(
        db.read_sql("SELECT MAX(txn_date) AS d FROM v_clean_transactions")["d"].iat[0]
    )
    activity["txn_date"] = pd.to_datetime(activity["txn_date"])
    activity["signup_date"] = pd.to_datetime(activity["signup_date"])

    # Day 0 is the signup date itself, so "day 1" is the first full day after.
    activity["day_index"] = (activity["txn_date"] - activity["signup_date"]).dt.days

    logger.info(
        "loaded %s active user-days for %s users, window ends %s",
        f"{len(activity):,}",
        f"{activity['user_id'].nunique():,}",
        observation_end.date(),
    )
    return activity, observation_end


def load_users() -> pd.DataFrame:
    """All users, including those who never transacted.

    Loaded separately and deliberately: users with zero successful days never
    appear in the activity table, and building the denominator from activity
    alone would silently drop them — inflating every retention number by
    excluding the people who churned hardest.
    """
    users = db.read_sql("SELECT user_id, signup_date, acquisition_channel, city_tier FROM users")
    users["signup_date"] = pd.to_datetime(users["signup_date"])
    users["cohort_week"] = users["signup_date"].dt.to_period("W").dt.start_time
    return users


def retention_at_horizon(
    activity: pd.DataFrame,
    users: pd.DataFrame,
    observation_end: pd.Timestamp,
    horizon: int,
) -> pd.DataFrame:
    """Retention at a single horizon under all three definitions.

    Args:
        activity: user-days of successful investing, with ``day_index``.
        users: the full user table — the denominator.
        observation_end: last day of data.
        horizon: day-since-signup to measure at.

    Returns:
        One row per cohort week, with NaN where the cohort lacks full exposure.
    """
    eligible = users[(observation_end - users["signup_date"]).dt.days >= horizon].copy()
    if eligible.empty:
        return pd.DataFrame()

    subset = activity[activity["user_id"].isin(eligible["user_id"])]

    exact = subset.loc[subset["day_index"] == horizon, "user_id"].unique()
    in_window = subset.loc[
        subset["day_index"].between(horizon - WINDOW_DAYS + 1, horizon), "user_id"
    ].unique()
    ever_after = subset.loc[subset["day_index"] >= horizon, "user_id"].unique()

    eligible["active_on_day"] = eligible["user_id"].isin(exact)
    eligible["active_in_window"] = eligible["user_id"].isin(in_window)
    eligible["ever_active_after"] = eligible["user_id"].isin(ever_after)

    grouped = (
        eligible.groupby("cohort_week")
        .agg(
            cohort_size=("user_id", "size"),
            active_on_day=("active_on_day", "mean"),
            active_in_window=("active_in_window", "mean"),
            ever_active_after=("ever_active_after", "mean"),
        )
        .reset_index()
    )
    grouped["horizon"] = horizon
    return grouped[grouped["cohort_size"] >= MIN_COHORT_SIZE]


def build_retention_table(
    activity: pd.DataFrame, users: pd.DataFrame, observation_end: pd.Timestamp
) -> pd.DataFrame:
    """Retention at every horizon, for every cohort with full exposure."""
    frames = [retention_at_horizon(activity, users, observation_end, h) for h in HORIZONS]
    table = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    logger.info(
        "retention table: %s cohort-horizon cells across %s cohorts",
        f"{len(table):,}",
        table["cohort_week"].nunique(),
    )
    return table


def overall_retention(table: pd.DataFrame) -> pd.DataFrame:
    """Retention pooled across cohorts, weighted by cohort size.

    Weighted rather than a mean of means: an unweighted average lets a small
    cohort move the headline as much as one ten times its size.
    """
    rows = []
    for horizon, group in table.groupby("horizon"):
        weights = group["cohort_size"]
        rows.append(
            {
                "horizon": horizon,
                "users": int(weights.sum()),
                "active_on_day": np.average(group["active_on_day"], weights=weights),
                "active_in_window": np.average(group["active_in_window"], weights=weights),
                "ever_active_after": np.average(group["ever_active_after"], weights=weights),
            }
        )
    return pd.DataFrame(rows).round(4)


def cohort_trend(table: pd.DataFrame, horizon: int = 30) -> pd.DataFrame:
    """Retention at one horizon over successive signup cohorts.

    This is the question a snapshot cannot answer: not "what is retention", but
    "is it getting better or worse for users we acquire now than for users we
    acquired two months ago".
    """
    trend = table[table["horizon"] == horizon].sort_values("cohort_week")
    return trend[["cohort_week", "cohort_size", "active_in_window", "active_on_day"]]


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #


def plot_definition_comparison(overall: pd.DataFrame, theme: viz.Theme) -> None:
    """The three definitions on one axis, to show how much the loose one flatters."""
    viz.apply_style(theme)
    fig, ax = plt.subplots(figsize=(8.4, 4.8))

    definitions = [
        ("ever_active_after", "invested any time after (forward)"),
        ("active_in_window", f"invested in prior {WINDOW_DAYS}d"),
        ("active_on_day", "invested on exactly that day"),
    ]

    x_end = float(overall["horizon"].iloc[-1])
    ends = []
    for slot, (column, label) in enumerate(definitions):
        color = theme.series[slot]
        ax.plot(
            overall["horizon"], overall[column], color=color, marker="o", markersize=5, label=label
        )
        y_end = float(overall[column].iloc[-1])
        viz.marker(ax, x_end, y_end, color, theme)
        ends.append(y_end)

    # Two of the three curves converge by D90, so their end labels would sit on
    # top of each other. Nudge them apart in screen space, keeping each anchored
    # to its own marker — the label still points at the right curve, it just no
    # longer overprints its neighbour.
    min_separation = 0.035
    ends.sort(reverse=True)
    offsets: list[float] = []
    for index, y_end in enumerate(ends):
        shift = 0.0
        if index > 0:
            previous_y = ends[index - 1] + offsets[index - 1]
            if y_end > previous_y - min_separation:
                shift = (previous_y - min_separation) - y_end
        offsets.append(shift)

    for y_end, shift in zip(ends, offsets, strict=True):
        viz.label_line_end(ax, x_end, y_end + shift, f"{y_end:.0%}", theme)

    ax.set_xticks(HORIZONS)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Days since signup")
    ax.set_ylabel("Share of cohort retained")
    ax.set_title("The same cohort, three definitions of “retained”")
    ax.legend(loc="upper right")
    viz.save(fig, "retention_definitions", theme)


def plot_cohort_heatmap(table: pd.DataFrame, theme: viz.Theme) -> None:
    """Cohort × horizon retention grid.

    Sequential single-hue ramp, because the encoded quantity is magnitude with no
    meaningful midpoint. Cells without full exposure are left blank rather than
    filled with a partial-denominator number.
    """
    viz.apply_style(theme)
    grid = table.pivot_table(
        index="cohort_week", columns="horizon", values="active_in_window"
    ).sort_index()

    fig, ax = plt.subplots(figsize=(8.4, 0.32 * len(grid) + 2.2))
    image = ax.imshow(
        grid.values, cmap="Blues", aspect="auto", vmin=0, vmax=max(grid.max().max(), 0.01)
    )

    ax.set_xticks(range(len(grid.columns)))
    ax.set_xticklabels([f"D{c}" for c in grid.columns])
    ax.set_yticks(range(len(grid.index)))
    ax.set_yticklabels([d.strftime("%d %b") for d in grid.index], fontsize=8)
    ax.set_xlabel("Days since signup")
    ax.set_ylabel("Signup cohort (week starting)")
    ax.set_title(f"Retention by weekly cohort — invested within {WINDOW_DAYS} days")
    ax.grid(False)

    # Value labels: the grid is small enough that a reader should get the number
    # without a colour-to-value lookup.
    for i in range(len(grid.index)):
        for j in range(len(grid.columns)):
            value = grid.values[i, j]
            if np.isnan(value):
                continue
            ax.text(
                j,
                i,
                f"{value:.0%}",
                ha="center",
                va="center",
                fontsize=7.5,
                color=theme.surface if value > 0.55 else theme.ink_secondary,
            )

    bar = fig.colorbar(image, ax=ax, shrink=0.6)
    bar.outline.set_visible(False)
    bar.ax.tick_params(labelsize=8, color=theme.ink_muted)
    viz.save(fig, "cohort_retention_heatmap", theme)


def plot_cohort_trend(trend: pd.DataFrame, horizon: int, theme: viz.Theme) -> None:
    """D30 retention across successive cohorts, with a trend direction."""
    viz.apply_style(theme)
    fig, ax = plt.subplots(figsize=(8.4, 4.4))

    x = np.arange(len(trend))
    y = trend["active_in_window"].to_numpy()
    ax.plot(x, y, color=theme.series[0], marker="o", markersize=5)

    if len(trend) >= 3:
        slope, intercept = np.polyfit(x, y, 1)
        ax.plot(x, slope * x + intercept, color=theme.deemphasis, linewidth=1.5, zorder=1)
        direction = "improving" if slope > 0 else "declining"
        ax.text(
            0.99,
            0.06,
            f"{direction}: {slope * 100:+.2f} pts per weekly cohort",
            transform=ax.transAxes,
            ha="right",
            fontsize=8,
            color=theme.ink_muted,
        )

    ax.set_xticks(x)
    ax.set_xticklabels(
        [d.strftime("%d %b") for d in trend["cohort_week"]], rotation=45, ha="right", fontsize=8
    )
    ax.set_ylim(0, max(y.max() * 1.25, 0.05))
    ax.set_xlabel("Signup cohort (week starting)")
    ax.set_ylabel(f"D{horizon} retention")
    ax.set_title(f"Is D{horizon} retention improving for newer cohorts?")
    viz.save(fig, "cohort_trend", theme)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run() -> pd.DataFrame:
    """Build every cohort artifact and write figures. Returns the pooled table."""
    activity, observation_end = load_activity()
    users = load_users()

    table = build_retention_table(activity, users, observation_end)
    overall = overall_retention(table)
    trend = cohort_trend(table, horizon=30)

    logger.info("pooled retention by definition:\n%s", overall.to_string(index=False))
    logger.info(
        "D30 retention by cohort:\n%s",
        trend.to_string(index=False, formatters={"active_in_window": "{:.1%}".format}),
    )

    for theme in viz.THEMES.values():
        plot_definition_comparison(overall, theme)
        plot_cohort_heatmap(table, theme)
        plot_cohort_trend(trend, 30, theme)

    return overall


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    run()


if __name__ == "__main__":
    main()
