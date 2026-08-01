-- =============================================================================
-- Cadence — schema for the daily SIP habit & streak retention engine
-- =============================================================================
--
-- Layered deliberately, because the layers mean different things:
--
--   1. PRODUCTION-SHAPED tables (users, sip_daily_transactions, nudges_sent,
--      experiment_assignments) — modelled on what a daily micro-SIP product
--      would actually persist. These are intentionally under-constrained. See
--      the "constraint gaps" note below: the missing constraints are the
--      subject of data_quality_findings.md, not an oversight.
--
--   2. GOVERNANCE table (data_quality_flags) — where automated checks record
--      what they found and what fix they propose to engineering.
--
--   3. DERIVED analytics tables (user_streaks, streak_observations) — rebuilt
--      from raw transactions by the pipeline. Always safe to TRUNCATE and
--      recompute; they hold no source-of-truth data.
--
--   4. SIMULATION GROUND TRUTH (sim_user_profile) — the archetype and decay
--      parameters each synthetic user was generated from. No production
--      equivalent exists; it is kept in its own table precisely so it can
--      never be mistaken for an observed field. Used to validate that the
--      analysis recovers the behaviour that was planted.
--
-- Idempotent: safe to re-run. Drops in FK-dependency order first.
-- =============================================================================

DROP VIEW  IF EXISTS v_daily_active_users     CASCADE;
DROP VIEW  IF EXISTS v_clean_transactions     CASCADE;
DROP TABLE IF EXISTS streak_observations      CASCADE;
DROP TABLE IF EXISTS user_streaks             CASCADE;
DROP TABLE IF EXISTS data_quality_flags       CASCADE;
DROP TABLE IF EXISTS experiment_assignments   CASCADE;
DROP TABLE IF EXISTS nudges_sent              CASCADE;
DROP TABLE IF EXISTS sip_daily_transactions   CASCADE;
DROP TABLE IF EXISTS sim_user_profile         CASCADE;
DROP TABLE IF EXISTS users                    CASCADE;


-- =============================================================================
-- 1. PRODUCTION-SHAPED TABLES
-- =============================================================================

-- One row per registered investor.
CREATE TABLE users (
    user_id             SERIAL PRIMARY KEY,
    signup_date         DATE NOT NULL,           -- date the account was created
    acquisition_channel VARCHAR(50),             -- organic | referral | paid_social | influencer | app_store
    city_tier           VARCHAR(10),             -- tier_1 | tier_2 | tier_3 (India tiering)
    kyc_status          VARCHAR(20),             -- verified | pending | rejected
    kyc_completed_at    TIMESTAMP                -- NULL while KYC is incomplete
);

COMMENT ON TABLE  users IS 'Registered investors. One row per account.';
COMMENT ON COLUMN users.kyc_completed_at IS
    'NULL when KYC never completed. Time from signup to this value is the "KYC speed" covariate in the Cox model — slow verification is a plausible early-friction churn driver.';

-- The core behavioural table: one row per attempted daily SIP contribution.
-- This is the table every streak in the product is computed from.
CREATE TABLE sip_daily_transactions (
    txn_id     SERIAL PRIMARY KEY,
    user_id    INT REFERENCES users(user_id),
    txn_date   DATE NOT NULL,                    -- the SIP day this contribution is for
    amount     NUMERIC(10,2),                    -- NULL-able on purpose; see constraint gaps
    status     VARCHAR(20) NOT NULL,             -- success | failed | skipped
    created_at TIMESTAMP DEFAULT now()
);

COMMENT ON TABLE  sip_daily_transactions IS
    'One row per attempted daily SIP contribution. Only status = ''success'' counts toward a streak: a failed autopay mandate is not a habit signal, and neither is a user-initiated skip.';
COMMENT ON COLUMN sip_daily_transactions.status IS
    'success = money moved. failed = payment/mandate error (product''s fault, not the user''s). skipped = user explicitly opted out for the day. The failed/skipped split matters: failures are recoverable with a retry, skips need a behavioural nudge.';
COMMENT ON COLUMN sip_daily_transactions.amount IS
    'Nullable at the schema level. Real ledgers should never allow this; the absence of NOT NULL here is finding DQ-01.';

-- Retention nudges actually dispatched to a user.
CREATE TABLE nudges_sent (
    nudge_id            SERIAL PRIMARY KEY,
    user_id             INT REFERENCES users(user_id),
    sent_date           DATE NOT NULL,
    days_missed_at_send INT NOT NULL,            -- consecutive missed days when the nudge fired
    nudge_type          VARCHAR(30)              -- push | sms | email
);

COMMENT ON COLUMN nudges_sent.days_missed_at_send IS
    'Consecutive missed days at dispatch time. This is the treatment "dose" — effect is segmented by this value to find the highest-leverage intervention day.';

-- Experiment arm assignment. Kept separate from users so a user can be in
-- several experiments over time without schema churn.
CREATE TABLE experiment_assignments (
    assignment_id   SERIAL PRIMARY KEY,
    user_id         INT REFERENCES users(user_id),
    experiment_name VARCHAR(60) NOT NULL,
    arm             VARCHAR(20) NOT NULL,        -- treatment | control
    assigned_at     TIMESTAMP NOT NULL,
    UNIQUE (user_id, experiment_name)
);

COMMENT ON TABLE experiment_assignments IS
    'Randomised arm assignment. Assignment happens at signup, before any outcome is observed, so the treatment/control comparison is not contaminated by selection on behaviour.';


-- =============================================================================
-- 2. GOVERNANCE
-- =============================================================================

-- Output of the automated data quality checks. Every row is something an
-- analyst would raise with engineering, with the fix already proposed.
CREATE TABLE data_quality_flags (
    flag_id      SERIAL PRIMARY KEY,
    table_name   VARCHAR(50),
    issue_type   VARCHAR(50),                    -- e.g. null_amount, duplicate_txn, txn_before_signup
    row_reference TEXT,                          -- primary key(s) of the offending row(s)
    detected_at  TIMESTAMP DEFAULT now(),
    proposed_fix TEXT,                           -- the constraint or validation that would prevent it
    severity     VARCHAR(20)                     -- high | medium | low
);

COMMENT ON TABLE data_quality_flags IS
    'Append-only log of detected data quality issues. proposed_fix is mandatory in practice: flagging a problem without a proposed fix is half a job.';


-- =============================================================================
-- 3. DERIVED ANALYTICS TABLES (rebuilt by the pipeline; safe to truncate)
-- =============================================================================

-- One row per unbroken run of consecutive successful SIP days.
-- Populated by src/analysis/streak_builder.py via the gaps-and-islands query.
CREATE TABLE user_streaks (
    streak_id       SERIAL PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES users(user_id),
    streak_index    INT NOT NULL,                -- 1 = the user's first ever streak
    streak_start    DATE NOT NULL,
    streak_end      DATE NOT NULL,
    streak_length   INT NOT NULL,                -- days, inclusive of both ends
    is_censored     BOOLEAN NOT NULL,            -- TRUE = still alive at the observation window's end
    days_to_next_streak INT,                     -- gap length before the user came back; NULL = never returned
    recovered       BOOLEAN NOT NULL,            -- TRUE = a later streak exists for this user
    UNIQUE (user_id, streak_index)
);

COMMENT ON COLUMN user_streaks.is_censored IS
    'Right-censoring indicator for Kaplan-Meier. A streak still running on the last day of data has not "died" — it is censored, and treating it as a death would bias survival estimates downward.';
COMMENT ON COLUMN user_streaks.recovered IS
    'TRUE if the user ever started another streak after this one broke. Distinguishes a permanent churn from a temporary pause — the central question of the project.';

-- Per (user, day-of-streak) observations feeding the survival and hazard models.
CREATE TABLE streak_observations (
    observation_id  SERIAL PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES users(user_id),
    streak_id       INT NOT NULL REFERENCES user_streaks(streak_id) ON DELETE CASCADE,
    day_of_streak   INT NOT NULL,                -- 1-indexed day within the streak
    obs_date        DATE NOT NULL,
    survived_to_next_day BOOLEAN NOT NULL
);

COMMENT ON TABLE streak_observations IS
    'Long-format expansion of user_streaks, one row per streak-day. Gives the discrete-time hazard at each day-of-streak directly: P(break on day d | alive at day d).';


-- =============================================================================
-- 4. SIMULATION GROUND TRUTH (no production equivalent)
-- =============================================================================

CREATE TABLE sim_user_profile (
    user_id            INT PRIMARY KEY REFERENCES users(user_id),
    archetype          VARCHAR(40) NOT NULL,     -- sticky_former | early_dropper | weekday_only | payday_spiker
    base_success_prob  NUMERIC(5,4) NOT NULL,    -- day-1 probability of investing
    decay_rate         NUMERIC(5,4) NOT NULL,    -- how fast that probability decays inside a gap
    return_propensity  NUMERIC(5,4) NOT NULL     -- baseline chance of restarting after a break
);

COMMENT ON TABLE sim_user_profile IS
    'SIMULATION ONLY — the parameters each synthetic user was generated from. Never treat as an observed field. Used to check the analysis recovers the planted behaviour, and to label segments in exploratory charts.';


-- =============================================================================
-- 5. VIEWS — the cleaning rules, written once
-- =============================================================================

-- The canonical "trustworthy transactions" view. Every downstream analysis reads
-- this rather than the raw table, so the cleaning rules live in exactly one place:
--   * de-duplicate (user_id, txn_date), keeping the earliest-created row
--   * drop transactions dated before the user's signup_date
CREATE VIEW v_clean_transactions AS
WITH deduped AS (
    SELECT t.*,
           ROW_NUMBER() OVER (
               PARTITION BY t.user_id, t.txn_date
               ORDER BY t.created_at, t.txn_id
           ) AS rn
    FROM sip_daily_transactions t
)
SELECT d.txn_id,
       d.user_id,
       d.txn_date,
       d.amount,
       d.status,
       d.created_at
FROM deduped d
JOIN users u ON u.user_id = d.user_id
WHERE d.rn = 1
  AND d.txn_date >= u.signup_date;

COMMENT ON VIEW v_clean_transactions IS
    'Deduplicated, signup-date-valid transactions. Analysis reads this; data quality checks read the raw table, so the checks can still see what the view hides.';

-- Daily active investors — the top-line habit metric the dashboard opens on.
CREATE VIEW v_daily_active_users AS
SELECT txn_date,
       COUNT(DISTINCT user_id)                       AS active_users,
       SUM(COALESCE(amount, 0))                      AS total_amount,
       ROUND(AVG(COALESCE(amount, 0)), 2)            AS avg_amount
FROM v_clean_transactions
WHERE status = 'success'
GROUP BY txn_date;


-- =============================================================================
-- 6. INDEXES
-- =============================================================================
-- The streak query partitions by user_id and orders by txn_date, so that
-- composite index carries the whole gaps-and-islands window function.
CREATE INDEX idx_txn_user_date    ON sip_daily_transactions (user_id, txn_date);
CREATE INDEX idx_txn_date         ON sip_daily_transactions (txn_date);
CREATE INDEX idx_txn_status       ON sip_daily_transactions (status);
CREATE INDEX idx_nudges_user      ON nudges_sent (user_id, sent_date);
CREATE INDEX idx_streaks_user     ON user_streaks (user_id);
CREATE INDEX idx_obs_streak       ON streak_observations (streak_id);
CREATE INDEX idx_users_signup     ON users (signup_date);


-- =============================================================================
-- CONSTRAINT GAPS — deliberate, and the subject of data_quality_findings.md
-- =============================================================================
-- The following are absent on purpose, so the seeded data can reproduce the
-- failure modes a real ledger exhibits before anyone tightens the schema:
--
--   * sip_daily_transactions.amount has no NOT NULL          -> DQ-01
--   * no UNIQUE (user_id, txn_date)                          -> DQ-02
--   * no CHECK (txn_date >= signup_date) / FK-level guard    -> DQ-03
--   * no CHECK (status IN ('success','failed','skipped'))    -> DQ-04
--   * users.kyc_status not enforced against transaction gating-> DQ-05
--
-- data_quality_findings.md carries the detection query, blast radius, and the
-- exact DDL that would close each gap.
-- =============================================================================
