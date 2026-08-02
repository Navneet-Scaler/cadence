# Where the daily SIP habit breaks, and what to do about it

**To:** Growth · **From:** Navneet · **Date:** 2 August 2026
**Basis:** 5,000 users, 373,387 transactions, 50,576 streaks, 12 months

---

## The recommendation

**Fire the retention nudge on day 5 of a lapse. Not day 1, not day 7.**

Today the obvious instinct is to nudge immediately — someone missed a day, remind
them. The data says that mostly spends sends on people who were coming back
anyway, and that the users who actually need reaching are ignored until it's too
late.

---

## Why day 5

**A lapse is not churn until about day 5. After that it mostly is.**

Of users who miss one day, 93% come back within a month. That number holds up
through day 3 (84%), then falls off a cliff:

| Days missed | Come back within 30 days | Still gone |
|---|---|---|
| 1 | 93% | 7% |
| 3 | 84% | 16% |
| **5** | **69%** | **31%** |
| 7 | 51% | 49% |
| 14 | 20% | 80% |

**Between day 3 and day 7 the odds of losing someone triple.** Day 5 sits in the
middle of that collapse — the last point where most users are still reachable,
and the first point where a meaningful share is genuinely at risk.

The experiment agrees, from completely separate arithmetic. Nudges were randomised
at signup and fired at different lapse lengths:

| Nudge fired on | Lift in 30-day recovery | Nudges per extra recovery |
|---|---|---|
| day 1 | +3.7pp | 27 |
| day 2 | +3.8pp | 26 |
| day 3 | +3.6pp | 28 |
| **day 5** | **+7.7pp** | **13** |
| day 7 | +4.1pp | 25 |

All significant after correcting for testing five thresholds. **A day-5 nudge is
roughly twice as efficient as any other trigger** — 13 sends per recovered user
against 26 or 27. Day 1 and day 2 nudges land on users who were already at a 91%
baseline recovery rate; there is very little left to win.

---

## What this is worth

Around 49,000 streak breaks a year in a 5,000-user base. Of those, roughly 8,600
reach day 5 still unrecovered — the addressable population.

At +7.7pp, moving the trigger to day 5 recovers about **660 additional users per
year** who would otherwise have been lost, for ~8,600 sends. Moving the same
volume of sends to day 1 would recover about 320.

**Same send budget, roughly double the return.**

---

## The finding nobody was looking for

**The user base is not a bell curve. It's two separate populations.**

Plotting how often each user actually invests, there's no meaningful middle:

- **2,435 users** invest on under 10% of their days
- **1,041 users** invest on over 90% of their days
- Almost nobody sits between

The average — 30% of days — describes essentially no real user. Any target framed
as "raise average consistency" is aiming at a gap in the distribution.

The real question is not how to make moderate investors better. It is **what moves
someone from the left cluster into the right one**, and whether that transition
happens early enough to influence. It is worth designing the next experiment
around that, rather than around the average.

---

## Two more things worth knowing

**A user's first streak is the fragile one.** Controlling for channel, city tier
and KYC speed, a first streak breaks about 18% faster than the same user's later
streaks (hazard ratio 1.18). Habit formation genuinely gets easier on the second
attempt — which argues for concentrating onboarding effort on surviving the first
break, not on preventing it.

**Weekends cost 39% of daily activity.** Friday averages 1,145 active investors,
Sunday 696. This is a pattern, not a problem — but any campaign measured
Monday-to-Monday will read very differently from one measured Thursday-to-Thursday,
and comparisons that ignore it will find effects that aren't there.

**Acquisition channel does not predict retention.** Neither does city tier. Once
recurring streaks are accounted for properly, none of these reach significance.
Spending more to acquire from a "better" channel is not supported by this data.

---

## What I'd do next

1. **Move the nudge trigger to day 5.** Single config change, ~660 users a year.
2. **Instrument the day-3 to day-7 window properly.** That is where retention is
   actually decided and it currently has the least visibility.
3. **Design the next experiment around the bimodal split** — what distinguishes a
   user heading for the 90% cluster in their first two weeks?

---

## What would change my mind

This runs on simulated data built to resemble the product, not on production
records — the *method* is the deliverable, the numbers would need re-running
against real data before anyone acts on them. See [ASSUMPTIONS.md](ASSUMPTIONS.md).

Two caveats I'd want closed even then:

- **The nudge model assumes a fixed uplift** regardless of how many nudges a user
  has already had. Real notification fatigue would flatten the day-5 advantage,
  and nothing here tests for it.
- **₹10.7 lakh moved through 450 accounts the system believes are unverified.**
  That's either a missing KYC gate or a stale status field — the data can't tell
  which, and it needs an answer from engineering before any of these numbers get
  quoted externally. See [data_quality_findings.md](data_quality_findings.md).
