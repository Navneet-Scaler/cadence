-- =============================================================================
-- Metabase dashboard questions
-- =============================================================================
--
-- Every card on the Cadence dashboard, as a standalone query. Kept in the repo
-- rather than only inside Metabase for three reasons:
--
--   * Metabase questions are not version controlled. A query that exists only in
--     the app cannot be reviewed, diffed, or restored after someone edits it.
--   * They are runnable in psql, so a card can be debugged without the app.
--   * The dashboard and the analysis must agree. These read the same views the
--     Python modules read, so a change to the streak definition moves both.
--
-- Each block is one card. Copy into Metabase as a native SQL question, or use
-- them as the reference when rebuilding the dashboard from scratch.
-- =============================================================================


-- CARD 1 — Daily active investors (line, trailing 90 days)
-- The top-line habit metric. Anything else on this dashboard explains a move here.
SELECT txn_date          AS "Date",
       active_users      AS "Active investors",
       total_amount      AS "Total invested"
FROM v_daily_active_users
WHERE txn_date >= (SELECT MAX(txn_date) - 90 FROM v_daily_active_users)
ORDER BY txn_date;


-- CARD 2 — Live streaks by length band (bar)
-- How many people currently hold a habit, and how deep it goes. Only censored
-- streaks are live: an uncensored streak has already ended.
SELECT CASE
           WHEN streak_length >= 30 THEN '30+ days'
           WHEN streak_length >= 14 THEN '14-29 days'
           WHEN streak_length >= 7  THEN '7-13 days'
           WHEN streak_length >= 3  THEN '3-6 days'
           ELSE '1-2 days'
       END                                    AS "Streak length",
       COUNT(*)                               AS "Live streaks"
FROM user_streaks
WHERE is_censored
GROUP BY 1
ORDER BY MIN(streak_length);


-- CARD 3 — Recovery rate by days missed (bar) — THE decision card
-- Reads straight off the risk set: of the breaks that reached N days without
-- returning, what share came back within 30 days. This is the card the
-- intervention day is chosen from.
WITH window_end AS (
    SELECT MAX(txn_date) AS last_day FROM v_clean_transactions
),
breaks AS (
    SELECT s.days_to_next_streak,
           COALESCE(s.days_to_next_streak, (w.last_day - s.streak_end)) AS gap_reached
    FROM user_streaks s
    CROSS JOIN window_end w
    WHERE NOT s.is_censored
      AND s.streak_end <= w.last_day - 30   -- full recovery window only
),
milestones(days_missed) AS (
    VALUES (1), (2), (3), (5), (7), (14)
)
SELECT m.days_missed                                        AS "Days missed",
       COUNT(*)                                             AS "Users at risk",
       ROUND(100.0 * AVG(
           (b.days_to_next_streak IS NOT NULL
            AND b.days_to_next_streak <= 30)::int
       ), 1)                                                AS "Recovered within 30d %"
FROM milestones m
JOIN breaks b ON b.gap_reached >= m.days_missed
GROUP BY m.days_missed
ORDER BY m.days_missed;


-- CARD 4 — Nudge effect: treatment vs control (grouped bar)
-- The experiment result, at the grain the product team would act on.
WITH window_end AS (
    SELECT MAX(txn_date) AS last_day FROM v_clean_transactions
),
breaks AS (
    SELECT s.user_id,
           s.streak_end,
           e.arm,
           s.days_to_next_streak,
           COALESCE(s.days_to_next_streak, (w.last_day - s.streak_end)) AS gap_reached,
           n.days_missed_at_send
    FROM user_streaks s
    JOIN experiment_assignments e ON e.user_id = s.user_id
    CROSS JOIN window_end w
    LEFT JOIN LATERAL (
        SELECT ns.days_missed_at_send
        FROM nudges_sent ns
        WHERE ns.user_id = s.user_id
          AND ns.sent_date > s.streak_end
          AND ns.sent_date <= s.streak_end + 30
        ORDER BY ns.sent_date
        LIMIT 1
    ) n ON TRUE
    WHERE NOT s.is_censored
      AND s.streak_end <= w.last_day - 30
),
milestones(threshold) AS (
    VALUES (1), (2), (3), (5), (7)
)
SELECT m.threshold                                          AS "Nudge day",
       CASE WHEN b.arm = 'treatment' AND b.days_missed_at_send = m.threshold
            THEN 'nudged' ELSE 'not nudged' END             AS "Group",
       COUNT(*)                                             AS "Breaks",
       ROUND(100.0 * AVG(
           (b.days_to_next_streak IS NOT NULL
            AND b.days_to_next_streak <= 30)::int
       ), 1)                                                AS "Recovered %"
FROM milestones m
JOIN breaks b ON b.gap_reached >= m.threshold
WHERE b.arm = 'control'
   OR b.days_missed_at_send = m.threshold
GROUP BY 1, 2
ORDER BY 1, 2;


-- CARD 5 — Weekly cohort retention (table / pivot)
-- Retention defined as "still transacting", not "still has an account".
WITH window_end AS (
    SELECT MAX(txn_date) AS last_day FROM v_clean_transactions
),
cohorts AS (
    SELECT u.user_id,
           DATE_TRUNC('week', u.signup_date)::date AS cohort_week,
           u.signup_date
    FROM users u
),
activity AS (
    SELECT t.user_id, (t.txn_date - c.signup_date) AS day_index
    FROM v_clean_transactions t
    JOIN cohorts c ON c.user_id = t.user_id
    WHERE t.status = 'success'
)
SELECT c.cohort_week                                         AS "Cohort week",
       COUNT(DISTINCT c.user_id)                             AS "Cohort size",
       ROUND(100.0 * COUNT(DISTINCT a1.user_id)
             / NULLIF(COUNT(DISTINCT c.user_id), 0), 1)      AS "D1 %",
       ROUND(100.0 * COUNT(DISTINCT a7.user_id)
             / NULLIF(COUNT(DISTINCT c.user_id), 0), 1)      AS "D7 %",
       ROUND(100.0 * COUNT(DISTINCT a30.user_id)
             / NULLIF(COUNT(DISTINCT c.user_id), 0), 1)      AS "D30 %"
FROM cohorts c
CROSS JOIN window_end w
LEFT JOIN activity a1  ON a1.user_id  = c.user_id AND a1.day_index  BETWEEN 1  AND 1
LEFT JOIN activity a7  ON a7.user_id  = c.user_id AND a7.day_index  BETWEEN 1  AND 7
LEFT JOIN activity a30 ON a30.user_id = c.user_id AND a30.day_index BETWEEN 24 AND 30
-- Only cohorts with full exposure to D30; otherwise the newest weeks show a
-- fake cliff caused by the data ending, not by users leaving.
WHERE c.signup_date <= w.last_day - 30
GROUP BY c.cohort_week
HAVING COUNT(DISTINCT c.user_id) >= 50
ORDER BY c.cohort_week;


-- CARD 6 — Consistency distribution (histogram)
-- Per-user active-day ratio. The long left tail is the churn problem made visual.
SELECT WIDTH_BUCKET(active_day_ratio, 0, 1, 10) * 10 || '%' AS "Active-day ratio",
       COUNT(*)                                             AS "Users"
FROM v_user_consistency
GROUP BY 1
ORDER BY MIN(active_day_ratio);


-- CARD 7 — Day-of-week effect (bar)
-- Confirms the weekend softness the simulation encodes and real fintech shows.
SELECT TO_CHAR(txn_date, 'Dy')                              AS "Day",
       ROUND(AVG(active_users))                             AS "Avg active investors"
FROM v_daily_active_users
-- ISODOW must be grouped on the row value, not an aggregate of it: grouping by
-- MIN(txn_date) is rejected, since the sort key has to be fixed per group.
GROUP BY TO_CHAR(txn_date, 'Dy'), EXTRACT(ISODOW FROM txn_date)
ORDER BY EXTRACT(ISODOW FROM txn_date);


-- CARD 8 — Open data quality flags (table)
-- Governance on the same dashboard as the metrics, so a number nobody trusts is
-- visible next to the reason not to trust it.
SELECT severity      AS "Severity",
       table_name    AS "Table",
       issue_type    AS "Issue",
       row_reference AS "Detail",
       proposed_fix  AS "Proposed fix",
       detected_at   AS "Detected"
FROM data_quality_flags
ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
         detected_at DESC;
