# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0]: 2026-08-02

First complete release: raw transactions to a decision memo, end to end.

### Added

**Data layer**
- PostgreSQL schema layered by meaning, production-shaped tables, a governance
  log, derived analytics tables, and simulation ground truth kept separate so it
  can never be mistaken for an observed field.
- `v_clean_transactions` defining dedup and signup-validity rules once.
- `v_user_consistency` at per-user grain, owning the definition of "investing
  consistency" so consumers cannot disagree about it.
- Behavioural data generator: a two-state machine with exponential gap decay,
  four archetypes, day-of-week and payday seasonality, and signup-time experiment
  randomisation. 5,000 users and 373,387 transactions in under 8 seconds.
- Deliberately seeded data quality defects, applied as a corruption layer after
  generation so the behaviour model stays clean.

**Analysis**
- Gaps-and-islands streak construction in SQL, mirrored in pandas, with the two
  asserted equal across all 50,576 streaks.
- Kaplan-Meier survival on streak length and on time-to-return, with
  right-censoring handled throughout.
- Conditional recovery computed from the fitted curve rather than by counting
  rows, so "hasn't returned yet" is never mistaken for "never returned".
- Cox proportional hazards clustered on `user_id`, because streaks are a
  recurrent event.
- Cohort retention under three definitions, with under-exposed cohorts excluded
  rather than divided by a partial denominator.
- Nudge experiment analysis with risk-set matching, Holm-Bonferroni correction,
  and number-needed-to-treat.
- Six automated data quality checks, each with a detection query, a blast radius,
  and the DDL that prevents recurrence.

**Operations**
- Weekly report generator with rotating file logs, meaningful exit codes, and
  alert thresholds, plus a cron wrapper.
- Metabase dashboard provisioned from version-controlled SQL, idempotently.
- GitHub Actions CI running `black`, `ruff`, and `pytest` on every pull request.
- 101 tests, none requiring a database.

**Documentation**
- `MEMO.md`: the one-page decision memo.
- `ASSUMPTIONS.md`: what is real and what is modelled.
- `data_quality_findings.md`: six findings with fixes.
- `dashboards/metabase_notes.md`: dashboard setup and the traps in it.

### Findings
- Recovery after a lapse collapses between day 3 (84%) and day 7 (51%); day 5 is
  the inflection.
- A day-5 nudge lifts 30-day recovery +7.7pp (95% CI [5.70, 9.75], NNT 12.9),
  roughly twice as efficient as any other trigger day.
- Consistency is bimodal, not normally distributed: 2,435 users below a 10%
  active-day ratio, 1,041 above 90%, and almost nobody between.
- A user's first streak breaks ~18% faster than their later ones (HR 1.18).
- Acquisition channel and city tier show no resolvable effect on streak survival
  once recurrent streaks are accounted for.

### Fixed
- Card 7 of the dashboard grouped by an aggregate (`EXTRACT(ISODOW FROM
  MIN(txn_date))`), which PostgreSQL rejects, caught by running every card
  rather than assuming they worked.
- Reseeding no longer happens as a side effect of `make run-sim`: `TRUNCATE ...
  RESTART IDENTITY CASCADE` would orphan any downstream table with a foreign key
  to `users`, so it now requires an explicit `--reseed`.
- Cox standard errors were understated before clustering was added; two
  covariates that appeared significant were artifacts of the independence
  assumption.

### Security
- `.gitignore` established in the first commit, before any other file was staged.
- No credentials in source; all configuration read from the environment.
- Pre-commit hooks include `detect-private-key` and `nbstripout`.
- No generated data, notebook outputs, or database dumps in version history.

[1.0.0]: https://github.com/Navneet-Scaler/cadence/releases/tag/v1.0.0
