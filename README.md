# Cadence

**Daily SIP habit & streak retention engine.**

Turns raw daily-investment transactions into streak health signals, finds where
and why habits break, and tests whether an intervention actually changes the
outcome. Designed to run on a schedule (`scripts/run_weekly_report.sh` + cron)
and refresh a live dashboard — not a one-off notebook.

[![CI](https://github.com/Navneet-Scaler/cadence/actions/workflows/ci.yml/badge.svg)](https://github.com/Navneet-Scaler/cadence/actions/workflows/ci.yml)

---

## Why this exists

Most fintech retention frameworks are built for **monthly** behaviour — monthly
SIPs, monthly billing, monthly cohorts. A product built on investing ₹21 *every
day* has completely different failure modes, and the monthly toolkit hides them:

- A monthly SIP user who misses a payment might be fine. The next one is 30 days away.
- A daily SIP user who misses **one day** is already showing a churn signal.

The whole retention problem lives in the gap between "missed once" and "gone for
good". Cadence measures that gap.

> **The findings below come from simulated data.** The pipeline, statistics, and
> tests are real and would run unchanged against production. The numbers are a
> demonstration that this method can locate an answer — not a claim about any
> real user base. See [ASSUMPTIONS.md](ASSUMPTIONS.md).

---

## The dashboard

![Cadence dashboard — daily active investors, streak length distribution, recovery rate by days missed, nudge effect, weekly cohort retention, consistency distribution, and day-of-week effect](dashboards/images/dashboard_hero.png)

Live Metabase instance, screenshotted end to end — not a mockup. Full-height
version and every card's SQL in
[`dashboards/metabase_notes.md`](dashboards/metabase_notes.md) and
[`sql/dashboard_questions.sql`](sql/dashboard_questions.sql). `make dashboard`
rebuilds this exact instance from that SQL in about 90 seconds.

---

## The answer

**Nudge on day 5 of a lapse.**

| Days missed | Recover within 30d |
|---|---|
| 1 | 93% |
| 3 | 84% |
| **5** | **69%** |
| 7 | 51% |
| 14 | 20% |

Between day 3 and day 7 the odds of losing someone **triple**. A day-5 nudge
lifts 30-day recovery by **+7.7pp** (95% CI [5.70, 9.75]) and needs **13 sends
per recovered user** against 26–27 on every other trigger day — nudging on day 1
mostly spends sends on people who were coming back anyway.

**Read [MEMO.md](MEMO.md)** — one page, no notebook required.

---

## Schema

```
                    ┌──────────────────────┐
                    │        users         │  signup, channel, city_tier, KYC
                    └──────────┬───────────┘
                               │ user_id
        ┌──────────────────────┼───────────────────────┬─────────────────────┐
        │                      │                       │                     │
┌───────▼────────────┐ ┌───────▼────────┐ ┌────────────▼─────────┐ ┌─────────▼────────┐
│sip_daily_          │ │ nudges_sent    │ │experiment_assignments│ │ sim_user_profile │
│  transactions      │ │ sent_date      │ │ arm, assigned_at     │ │ archetype        │
│ txn_date, amount   │ │ days_missed_   │ │                      │ │ (SIMULATION      │
│ status             │ │   at_send      │ │                      │ │  GROUND TRUTH)   │
└───────┬────────────┘ └────────────────┘ └──────────────────────┘ └──────────────────┘
        │
        │  dedup + signup-validity applied once
┌───────▼──────────────┐
│ v_clean_transactions │──────┐
└───────┬──────────────┘      │
        │ gaps-and-islands    │
┌───────▼────────┐            │      ┌──────────────────────┐
│  user_streaks  │────────────┴─────▶│  v_user_consistency  │  one row per user
│ length, censored│                  └──────────────────────┘
│ recovered       │
└───────┬─────────┘
        │
┌───────▼─────────────┐        ┌─────────────────────┐
│ streak_observations │        │ data_quality_flags  │  findings + proposed fixes
│ one row per         │        └─────────────────────┘
│ streak-day          │
└─────────────────────┘
```

**Layered by meaning:** production-shaped tables are the source of truth; derived
tables are safe to truncate and rebuild; `sim_user_profile` holds simulation
ground truth in its own table so it can never be mistaken for an observed field.

---

## Run it

```bash
git clone https://github.com/Navneet-Scaler/cadence.git && cd cadence
cp .env.example .env          # fill in credentials
make setup                    # Python 3.12 venv, pinned deps, pre-commit hooks
make db-up                    # postgres:16 in Docker
make schema                   # apply the DDL
make run-sim                  # generate + load 373k transactions (~8s)
make all                      # streaks, survival, cohorts, nudge, quality, report
```

Then `make dashboard` to provision Metabase, or `make help` to see everything.

---

## How it works

### 1. Streaks — the gaps-and-islands problem
```sql
txn_date - ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY txn_date)
```
Inside an unbroken run both sides increment by 1, so the difference is constant
and *is* the streak's identity. One pass, no self-join.

The same logic is implemented twice — in SQL for production and in pandas for
testability — and [`validate_against_sql`](src/analysis/streak_builder.py)
asserts they agree on all **50,576 streaks**. A streak definition that drifts
between the dashboard and the notebook is how two teams quote different numbers
off one warehouse; here that drift is a test failure, enforced in CI's
`db-tests` job (which brings up Postgres, applies the schema, seeds data, and
runs [`tests/test_streak_builder_db.py`](tests/test_streak_builder_db.py)), not
just something you can run by hand.

### 2. Survival analysis — censoring is the whole point
A streak still running when the data ends has **not** died. Treating it as dead
biases every estimate downward. Likewise, "hasn't come back yet" is not "never
came back" — a user who lapsed three days before the window closed never had a
chance to return.

So conditional recovery is read off the fitted Kaplan-Meier curve:

```
P(returns | already missed k days) = 1 - S(horizon) / S(k)
```

The complement is named `p_no_return_within_horizon`, deliberately **not**
"never returns" — a finite window cannot support the word *never*.

### 3. Cox model — clustered, because streaks recur
50,576 streaks come from 5,000 users. Treating them as independent inflates the
effective sample size roughly tenfold and shrinks every standard error to match.
Clustering on `user_id` changed the conclusions: two covariates that looked
significant turned out to be artifacts.

### 4. The nudge experiment — matched on the risk set
Each treated break is compared only against control breaks that **also reached
the same gap length unrecovered**. A user who returned on day 1 was never
reachable by a day-3 nudge and doesn't belong in that denominator. Five
thresholds are Holm-Bonferroni corrected.

---

## Findings

**Recovery collapses between day 3 and day 7.** 84% → 51%. Day 5 is the
inflection, confirmed independently by the survival curve and the experiment.

**Consistency is bimodal, not a bell curve.** 2,435 users invest on under 10% of
their days; 1,041 on over 90%; almost nobody in between. The 30% average
describes essentially no real user — so any goal framed as "raise average
consistency" aims at a gap in the distribution. This wasn't something the
analysis went looking for.

**A user's first streak is the fragile one.** It breaks ~18% faster than the same
user's later streaks (HR 1.18, p<0.001), controlling for channel, tier, and KYC
speed. Habit formation gets easier the second time.

**Weekends cost 39% of daily activity.** Friday 1,145 active investors, Sunday 696.

**Acquisition channel doesn't predict retention.** Neither does city tier. The
forest plot shows these as non-significant rather than hiding them — a null
result is a finding.

**Conventional retention charts flatter by 19 points.** D30 reads 55% under an
account-based definition and 36% under "actually invested that day".

---

## Repository

| Path | What it is |
|---|---|
| [`sql/schema.sql`](sql/schema.sql) | Schema, commented table by table |
| [`sql/streak_construction.sql`](sql/streak_construction.sql) | Gaps-and-islands streak builder |
| [`sql/dashboard_questions.sql`](sql/dashboard_questions.sql) | All 8 dashboard cards |
| [`src/simulate/`](src/simulate/) | Behavioural data generator |
| [`src/analysis/`](src/analysis/) | Streaks, survival, cohorts, nudge, data quality |
| [`src/reporting/`](src/reporting/) | Scheduled weekly report |
| [`scripts/`](scripts/) | Cron wrapper, Metabase provisioning |
| [`tests/`](tests/) | 101 unit tests (no DB) + 1 DB-backed integration test, both run in CI |
| [`MEMO.md`](MEMO.md) | **The one-page decision memo** |
| [`ASSUMPTIONS.md`](ASSUMPTIONS.md) | What's real vs modelled |
| [`data_quality_findings.md`](data_quality_findings.md) | 6 findings with fix DDL |
| [`dashboards/metabase_notes.md`](dashboards/metabase_notes.md) | Dashboard setup |

## Stack

PostgreSQL 16 · Python 3.12 · pandas · lifelines · scipy · statsmodels ·
matplotlib · Metabase · Docker · pytest · GitHub Actions

## Engineering notes

- **No generated data in git.** The generator is the artifact; data is
  regenerable from a fixed seed, byte for byte, with a test asserting it.
- **No credentials in source.** All configuration from the environment;
  `.gitignore` landed in the first commit, before anything else was staged.
- **Reseeding is explicit.** `TRUNCATE ... RESTART IDENTITY CASCADE` would orphan
  any downstream foreign key, so it requires `--reseed` and never happens as a
  side effect of a normal run.
- **Every chart carries numbers**, and non-significant results are drawn rather
  than dropped.
