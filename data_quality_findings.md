# Data Quality Findings

Six automated checks run against the **raw** tables on every pipeline run
(`make quality`, and inside the weekly report). Findings are written to
`data_quality_flags` so they're queryable from the dashboard rather than trapped
in a notebook cell.

Every finding below carries three things, because a bug report with only the
first is work handed to someone else rather than work done:

1. **What is wrong**, a detection query anyone can re-run.
2. **What it costs**, blast radius in a metric someone cares about.
3. **How to fix it**, the DDL or validation rule that prevents recurrence.

> **On the checks reading raw tables:** `v_clean_transactions` already
> de-duplicates and drops pre-signup rows, so the *analysis* is protected from
> DQ-02 and DQ-03 today. Running these checks through that view would report a
> clean bill of health on a database that still has the problem. The view
> protects the analysis; these checks protect the ledger.

## Summary: run of 2 Aug 2026, 373,387 raw transactions / 5,000 users

| Code | Table | Issue | Severity | Rows | Status |
|---|---|---|---|---|---|
| DQ-01 | sip_daily_transactions | `null_amount` | high | 5,593 | ❌ FAIL |
| DQ-02 | sip_daily_transactions | `duplicate_txn` | high | 3,697 | ❌ FAIL |
| DQ-03 | sip_daily_transactions | `txn_before_signup` | medium | 1,472 | ❌ FAIL |
| DQ-04 | sip_daily_transactions | `invalid_status` | high | 747 | ❌ FAIL |
| DQ-05 | users | `unverified_user_transacting` | high | 557 | ❌ FAIL |
| DQ-06 | users | `kyc_status_timestamp_mismatch` | medium | 0 | ✅ PASS |

DQ-06 passing matters: it shows the suite distinguishes states rather than
flagging everything it looks at.

---

## DQ-01: NULL transaction amounts · **high**

**5,593 rows (1.50% of all transactions), of which 5,342 have `status = 'success'`.**

A row asserting that money moved while recording no sum. Every revenue and AUM
figure silently under-counts, and the under-count is invisible because the rows
are present and look successful.

```sql
SELECT txn_id FROM sip_daily_transactions WHERE amount IS NULL;
```

**Proposed fix**, backfill from the payment gateway ledger first, then:

```sql
ALTER TABLE sip_daily_transactions ALTER COLUMN amount SET NOT NULL;
ALTER TABLE sip_daily_transactions
    ADD CONSTRAINT chk_amount_positive CHECK (amount > 0);
```

The writer should fail loudly rather than insert a partial row. A NULL here is
the application saying "I don't know how much" and persisting it anyway.

---

## DQ-02: Duplicate transactions for the same user-day · **high**

**3,697 duplicated user-days across 1,884 users (3,697 excess rows).**

Almost certainly a retried write with no idempotency key.

**This is the most dangerous defect in the list**, and the reason is not obvious:
the streak query identifies runs using `txn_date - ROW_NUMBER()`. A duplicate
consumes an extra row number without advancing the date, so the difference shifts
and **one real streak is shattered into several**. Left unhandled it would
manufacture streak breaks that never happened, inflating churn and corrupting
every survival estimate downstream.

```sql
SELECT user_id, txn_date, COUNT(*)
FROM sip_daily_transactions
GROUP BY user_id, txn_date
HAVING COUNT(*) > 1;
```

**Proposed fix**, de-duplicate keeping the earliest `created_at`, then:

```sql
ALTER TABLE sip_daily_transactions
    ADD CONSTRAINT uq_user_day UNIQUE (user_id, txn_date);
```

and give the payment writer an idempotency key so a retry updates rather than
inserts.

---

## DQ-03: Transactions dated before signup · **medium**

**1,472 rows across 1,094 users, between 1 and 29 days before the account existed.**

The classic symptom of trusting a client-supplied timestamp over server time.
These produce negative day-since-signup indices, which quietly corrupt cohort
retention: a transaction at day −3 lands in no cohort bucket and simply vanishes
from the denominator.

```sql
SELECT t.txn_id
FROM sip_daily_transactions t
JOIN users u ON u.user_id = t.user_id
WHERE t.txn_date < u.signup_date;
```

**Proposed fix**, stamp `txn_date` server-side from the settlement event rather
than the client payload. PostgreSQL cannot express a cross-table `CHECK`, so
enforce with a trigger, or denormalise `signup_date` onto the transaction row and
constrain it there.

---

## DQ-04: Undocumented status values · **high**

**747 rows across 611 users, all with `status = 'SUCCESS'` (wrong case).**

Every downstream query filters `status = 'success'`. A wrong-case value is
therefore **invisible to the analysis while being a real contribution**, the
user did invest, the ledger recorded it, and the streak builder counts it as a
missed day. It manufactures phantom streak breaks in exactly the population that
was behaving well.

```sql
SELECT txn_id FROM sip_daily_transactions
WHERE status NOT IN ('success', 'failed', 'skipped');
```

**Proposed fix**, normalise to lower case, then constrain:

```sql
ALTER TABLE sip_daily_transactions
    ADD CONSTRAINT chk_status CHECK (status IN ('success', 'failed', 'skipped'));
```

Better still, promote `status` to an `ENUM` so the database rejects anything
undocumented at write time instead of trusting every caller to spell it right.

---

## DQ-05: Users transacting without completed KYC · **high**

**450 users with `kyc_status = 'pending'` and 34,323 successful transactions
between them, totalling ₹10,74,196.**

This is the one finding here that is **not merely a data defect**. Either:

- the KYC gate is not enforced at the payment path, a compliance exposure, or
- `kyc_status` is not updated on completion, a data defect with a clean fix.

The two have completely different remediations and the data alone cannot
distinguish them, so this needs a conversation with engineering rather than a
patch. Flagging it with the value at risk attached is the point: ₹10.7 lakh
moved through accounts the system believes are unverified.

```sql
SELECT u.kyc_status, COUNT(DISTINCT u.user_id), COUNT(*), SUM(t.amount)
FROM users u
JOIN sip_daily_transactions t ON t.user_id = u.user_id
WHERE u.kyc_status <> 'verified' AND t.status = 'success'
GROUP BY u.kyc_status;
```

**Proposed fix**, determine which failure it is first. If the gate is missing,
block at the payment service. If the status field is stale, make KYC completion
write `kyc_status` and `kyc_completed_at` in one transaction, see DQ-06.

---

## DQ-06: `kyc_status` / `kyc_completed_at` disagreement · **medium** · ✅ passing

**0 rows.** No drift at present.

The check is kept because the two fields encode one fact and are written
separately, so they *can* drift, a verified user with no completion timestamp,
or an unverified user carrying one. Any KYC-speed analysis (including the Cox
covariate) becomes unreliable the moment they disagree, and the failure is silent.

**Proposed fix (preventive)**, derive `kyc_status` from `kyc_completed_at` as a
generated column, or write both in one transaction. Two columns encoding one fact
will drift eventually.

---

## What this would change in the schema

The current schema deliberately omits these constraints so the defects above can
be reproduced and demonstrated. Applied together, DQ-01 through DQ-04 close at the
database layer:

```sql
ALTER TABLE sip_daily_transactions
    ALTER COLUMN amount SET NOT NULL,
    ADD CONSTRAINT chk_amount_positive CHECK (amount > 0),
    ADD CONSTRAINT uq_user_day UNIQUE (user_id, txn_date),
    ADD CONSTRAINT chk_status CHECK (status IN ('success', 'failed', 'skipped'));
```

Note the ordering dependency: each requires its backfill or de-duplication to run
first, or the `ALTER` fails against existing rows. That is a feature, it forces
the historical mess to be dealt with rather than fenced off.
