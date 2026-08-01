-- =============================================================================
-- Streak construction — the gaps-and-islands problem
-- =============================================================================
--
-- A "streak" is a run of consecutive calendar days on which a user successfully
-- invested. Turning a list of dates into runs is the classic gaps-and-islands
-- problem, and the trick is one line:
--
--     txn_date - ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY txn_date)
--
-- Within an unbroken run, txn_date and the row number both increase by exactly 1
-- each step, so their difference is constant — that constant IS the streak's
-- identity. The moment a day is missed, txn_date jumps by more than the row
-- number and the difference shifts, starting a new group. No self-join, no
-- recursion, one pass.
--
-- Reads v_clean_transactions, not the raw table, so dedup and signup-date
-- validity are already applied. Duplicates matter here more than anywhere else:
-- two rows for the same day would each consume a row number, permanently
-- desynchronising the difference and shattering one real streak into several.
--
-- Only status = 'success' counts. A failed mandate or an explicit skip is not a
-- day the habit held. Note this also excludes the seeded 'SUCCESS' (wrong-case)
-- rows — see DQ-04, whose blast radius is exactly these phantom streak breaks.
--
-- Idempotent: truncates and rebuilds both derived tables.
-- =============================================================================

TRUNCATE streak_observations, user_streaks RESTART IDENTITY;

WITH observation_window AS (
    -- The last day of data. A streak still running on this day has not ended;
    -- it is right-censored, and Kaplan-Meier must be told so.
    SELECT MAX(txn_date) AS last_observed_day
    FROM v_clean_transactions
),
ranked AS (
    SELECT user_id,
           txn_date,
           txn_date - (ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY txn_date))::int
               AS streak_group
    FROM v_clean_transactions
    WHERE status = 'success'
),
collapsed AS (
    SELECT user_id,
           streak_group,
           MIN(txn_date) AS streak_start,
           MAX(txn_date) AS streak_end,
           COUNT(*)::int AS streak_length
    FROM ranked
    GROUP BY user_id, streak_group
),
sequenced AS (
    SELECT c.*,
           ROW_NUMBER() OVER (PARTITION BY c.user_id ORDER BY c.streak_start)::int
               AS streak_index,
           LEAD(c.streak_start) OVER (PARTITION BY c.user_id ORDER BY c.streak_start)
               AS next_streak_start
    FROM collapsed c
)
INSERT INTO user_streaks (
    user_id, streak_index, streak_start, streak_end, streak_length,
    is_censored, days_to_next_streak, recovered
)
SELECT s.user_id,
       s.streak_index,
       s.streak_start,
       s.streak_end,
       s.streak_length,
       s.streak_end = w.last_observed_day                       AS is_censored,
       (s.next_streak_start - s.streak_end)::int                AS days_to_next_streak,
       s.next_streak_start IS NOT NULL                          AS recovered
FROM sequenced s
CROSS JOIN observation_window w
ORDER BY s.user_id, s.streak_index;


-- Long-format expansion: one row per streak-day. This is what gives the
-- discrete-time hazard P(break on day d | still alive on day d) directly,
-- without having to re-derive it from streak lengths every time.
INSERT INTO streak_observations (user_id, streak_id, day_of_streak, obs_date, survived_to_next_day)
SELECT s.user_id,
       s.streak_id,
       d.day_of_streak,
       s.streak_start + (d.day_of_streak - 1) AS obs_date,
       -- The final day of an uncensored streak is the day the habit broke.
       -- On a censored streak the final day is simply where the data ran out,
       -- so it is not counted as a break.
       (d.day_of_streak < s.streak_length) OR s.is_censored AS survived_to_next_day
FROM user_streaks s
CROSS JOIN LATERAL generate_series(1, s.streak_length) AS d(day_of_streak);
