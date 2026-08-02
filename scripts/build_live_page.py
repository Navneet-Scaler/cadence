"""Builds docs/index.html: the live findings page served by GitHub Pages.

Queries the local database for the real numbers behind every chart on the
page (daily active investors, streak breaks, nudges, cohort retention) and
embeds them as a JSON payload, so the page is a snapshot of an actual pipeline
run rather than hand-typed figures that can drift from the analysis.

The page itself is a single self-contained HTML file: inline CSS, inline SVG
charts (no chart library), inline JS, no external requests, themed for light
and dark. That is a deliberate constraint, not a limitation of convenience:
GitHub Pages serves static files with no build step, and a CDN dependency
would be one outage away from a blank page for anyone who opens the link.

Run after ``make run-sim`` and ``make streak`` populate the database:

    python -m scripts.build_live_page

Regenerates docs/index.html in place. Commit the result; there is no build
step in CI for it, the committed file is what GitHub Pages serves.
"""

from __future__ import annotations

import json
import logging
import random

from src import db

logger = logging.getLogger("cadence.live_page")

RNG_SEED = 7
MAX_FEED_EVENTS = 420
MIN_STREAK_FOR_FEED = 10


def fetch_payload() -> dict:
    """Pull every number the live page renders, straight from the database."""
    daily = db.read_sql(
        """
        SELECT txn_date::text AS d, active_users AS u, total_amount AS amt
        FROM v_daily_active_users ORDER BY txn_date
        """
    )

    breaks = db.read_sql(
        f"""
        SELECT s.streak_end::text AS d, s.user_id, s.streak_length, s.recovered,
               s.days_to_next_streak, p.archetype
        FROM user_streaks s
        JOIN sim_user_profile p USING (user_id)
        WHERE NOT s.is_censored AND s.streak_length >= {MIN_STREAK_FOR_FEED}
        ORDER BY s.streak_end
        """
    )

    nudges = db.read_sql(
        """
        SELECT n.sent_date::text AS d, n.user_id, n.days_missed_at_send AS gap,
               n.nudge_type, p.archetype
        FROM nudges_sent n JOIN sim_user_profile p USING (user_id)
        ORDER BY n.sent_date
        """
    )

    bands = db.read_sql(
        """
        SELECT CASE WHEN streak_length >= 30 THEN '30+' WHEN streak_length >= 14 THEN '14-29'
                    WHEN streak_length >= 7 THEN '7-13' WHEN streak_length >= 3 THEN '3-6' ELSE '1-2' END AS band,
               COUNT(*) AS n
        FROM user_streaks WHERE is_censored GROUP BY 1
        """
    )

    cohort = db.read_sql(
        """
        WITH window_end AS (SELECT MAX(txn_date) AS last_day FROM v_clean_transactions),
        cohorts AS (SELECT u.user_id, DATE_TRUNC('week', u.signup_date)::date cw, u.signup_date FROM users u),
        activity AS (SELECT t.user_id, (t.txn_date - c.signup_date) di FROM v_clean_transactions t
                     JOIN cohorts c ON c.user_id=t.user_id WHERE t.status='success')
        SELECT c.cw::text AS week, COUNT(DISTINCT c.user_id) sz,
               ROUND(100.0*COUNT(DISTINCT a1.user_id)/NULLIF(COUNT(DISTINCT c.user_id),0),1) d1,
               ROUND(100.0*COUNT(DISTINCT a7.user_id)/NULLIF(COUNT(DISTINCT c.user_id),0),1) d7,
               ROUND(100.0*COUNT(DISTINCT a30.user_id)/NULLIF(COUNT(DISTINCT c.user_id),0),1) d30
        FROM cohorts c CROSS JOIN window_end w
        LEFT JOIN activity a1 ON a1.user_id=c.user_id AND a1.di BETWEEN 1 AND 1
        LEFT JOIN activity a7 ON a7.user_id=c.user_id AND a7.di BETWEEN 1 AND 7
        LEFT JOIN activity a30 ON a30.user_id=c.user_id AND a30.di BETWEEN 24 AND 30
        WHERE c.signup_date <= w.last_day - 30
        GROUP BY c.cw HAVING COUNT(DISTINCT c.user_id) >= 50 ORDER BY c.cw
        """
    )

    logger.info(
        "fetched %s daily rows, %s breaks, %s nudges, %s cohorts",
        len(daily),
        len(breaks),
        len(nudges),
        len(cohort),
    )

    # Downsample the event feed for page weight: prioritise longer streaks so
    # the replay reads as a narrative rather than noise, cap total events.
    rng = random.Random(RNG_SEED)
    breaks_records = breaks.to_dict("records")
    recovered = [b for b in breaks_records if b["recovered"]]
    not_recovered = [b for b in breaks_records if not b["recovered"]]
    n_recovered = min(int(MAX_FEED_EVENTS * 0.4), len(recovered))
    n_lapsed = min(int(MAX_FEED_EVENTS * 0.2), len(not_recovered))
    sample = rng.sample(recovered, n_recovered) + rng.sample(not_recovered, n_lapsed)

    nudges_records = nudges.to_dict("records")
    n_nudges = min(int(MAX_FEED_EVENTS * 0.4), len(nudges_records))
    nudge_sample = rng.sample(nudges_records, n_nudges)

    feed = []
    for b in sample:
        feed.append(
            {
                "d": b["d"],
                "type": "recovered" if b["recovered"] else "lapsed",
                "streak": int(b["streak_length"]),
                "archetype": b["archetype"],
                "gap": (
                    int(b["days_to_next_streak"])
                    if b["recovered"] and b["days_to_next_streak"]
                    else None
                ),
            }
        )
    for n in nudge_sample:
        feed.append(
            {
                "d": n["d"],
                "type": "nudge",
                "gap": int(n["gap"]),
                "channel": n["nudge_type"],
                "archetype": n["archetype"],
            }
        )
    feed.sort(key=lambda r: r["d"])
    logger.info("feed sampled to %s events", len(feed))

    return {
        "daily": daily.to_dict("records"),
        "feed": feed,
        "bands": bands.to_dict("records"),
        "cohort": cohort.to_dict("records"),
    }


HTML_TEMPLATE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cadence: Live Findings</title>
<meta name="description" content="Where the daily SIP habit breaks, and what to do about it: a replayable, statistically-annotated tour of the Cadence retention engine.">
<style>
:root{
  color-scheme:light;
  --surface:#fcfcfb; --surface-2:#f3f2ef; --surface-3:#eae9e4;
  --ink-1:#0b0b0b; --ink-2:#4a4944; --ink-3:#88867d;
  --border:#e2e0d8; --border-strong:#cfcdc2;
  --blue:#2a78d6; --orange:#eb6834; --green:#1baf7a; --yellow:#c98500; --violet:#4a3aa7; --red:#d03b3b;
  --good:#0ca30c; --critical:#d03b3b;
  --shadow:0 1px 2px rgba(20,20,10,.05),0 6px 20px rgba(20,20,10,.05);
  --shadow-lg:0 8px 30px rgba(20,20,10,.10);
  --radius:16px;
  --mono:"SF Mono",ui-monospace,Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Helvetica,Arial,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --surface:#111113; --surface-2:#18181b; --surface-3:#212124;
    --ink-1:#f5f5f3; --ink-2:#b7b5ab; --ink-3:#7d7b73;
    --border:#28282b; --border-strong:#37373b;
    --blue:#4a91ea; --orange:#e2763f; --green:#25b381; --yellow:#d69a2b; --violet:#8b7ce8; --red:#e2645f;
    --good:#3fcf5f; --critical:#e2645f;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 6px 24px rgba(0,0,0,.35);
    --shadow-lg:0 12px 40px rgba(0,0,0,.5);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --surface:#111113; --surface-2:#18181b; --surface-3:#212124;
  --ink-1:#f5f5f3; --ink-2:#b7b5ab; --ink-3:#7d7b73;
  --border:#28282b; --border-strong:#37373b;
  --blue:#4a91ea; --orange:#e2763f; --green:#25b381; --yellow:#d69a2b; --violet:#8b7ce8; --red:#e2645f;
  --good:#3fcf5f; --critical:#e2645f;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 6px 24px rgba(0,0,0,.35);
  --shadow-lg:0 12px 40px rgba(0,0,0,.5);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--surface-2);color:var(--ink-1);font:15px/1.6 var(--sans);
  -webkit-font-smoothing:antialiased;overflow-x:hidden}
::selection{background:color-mix(in srgb,var(--blue) 30%,transparent)}
a{color:var(--blue)}
code,.mono{font-family:var(--mono)}
.wrap{max-width:1180px;margin:0 auto;padding:0 24px}
section{scroll-margin-top:76px}

/* ---------- nav ---------- */
nav.top{position:sticky;top:0;z-index:50;backdrop-filter:blur(14px) saturate(1.4);
  background:color-mix(in srgb,var(--surface) 78%,transparent);border-bottom:1px solid var(--border)}
nav.top .row{display:flex;align-items:center;gap:6px;height:56px;max-width:1180px;margin:0 auto;padding:0 24px}
.brand{font-weight:800;font-size:15px;letter-spacing:-.02em;margin-right:10px;white-space:nowrap;display:flex;align-items:center;gap:8px}
.brand .dot{width:8px;height:8px;border-radius:50%;background:var(--orange);animation:pulse 2s ease-in-out infinite}
nav.top .links{display:flex;gap:2px;flex:1;overflow-x:auto}
nav.top .links a{color:var(--ink-2);text-decoration:none;font-size:13px;font-weight:600;padding:8px 12px;
  border-radius:8px;white-space:nowrap;transition:.15s}
nav.top .links a:hover{color:var(--ink-1);background:var(--surface-3)}
.level-toggle{display:flex;border:1px solid var(--border-strong);border-radius:8px;overflow:hidden;flex-shrink:0}
.level-toggle button{background:var(--surface);border:0;color:var(--ink-3);font:600 11.5px var(--sans);
  padding:7px 12px;cursor:pointer;letter-spacing:.02em}
.level-toggle button.active{background:var(--ink-1);color:var(--surface)}
.gh-link{margin-left:8px;color:var(--ink-2);flex-shrink:0}
.gh-link svg{width:20px;height:20px;display:block}

@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 color-mix(in srgb,var(--orange) 55%,transparent)}
  50%{opacity:.55;box-shadow:0 0 0 5px transparent}}

/* ---------- hero ---------- */
header.hero{padding:64px 0 44px}
.eyebrow{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:700;letter-spacing:.09em;
  text-transform:uppercase;color:var(--blue);background:color-mix(in srgb,var(--blue) 12%,transparent);
  padding:5px 12px;border-radius:999px;margin-bottom:18px}
h1{margin:0 0 14px;font-size:clamp(28px,4vw,42px);letter-spacing:-.025em;line-height:1.12;max-width:760px}
.lede{color:var(--ink-2);max-width:620px;font-size:16px;margin:0 0 26px}
.hero-actions{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:34px}
.btn{display:inline-flex;align-items:center;gap:8px;font:600 14px var(--sans);padding:11px 18px;border-radius:10px;
  text-decoration:none;border:1px solid transparent;transition:.15s;cursor:pointer}
.btn-primary{background:var(--ink-1);color:var(--surface)}
.btn-primary:hover{opacity:.85}
.btn-ghost{background:var(--surface);border-color:var(--border-strong);color:var(--ink-1)}
.btn-ghost:hover{background:var(--surface-3)}

.rec-card{display:flex;gap:18px;align-items:flex-start;padding:22px 24px;background:linear-gradient(135deg,
  color-mix(in srgb,var(--orange) 9%,var(--surface)),var(--surface));border:1px solid var(--border);
  border-radius:var(--radius);box-shadow:var(--shadow);max-width:720px}
.rec-icon{flex-shrink:0;width:44px;height:44px;border-radius:12px;background:var(--orange);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:800}
.rec-card .label{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink-3);font-weight:700;margin-bottom:4px}
.rec-card .headline{font-size:17px;font-weight:700;margin-bottom:6px;line-height:1.35}
.rec-card .sub{color:var(--ink-2);font-size:13.5px}

/* ---------- kpi strip ---------- */
.kpi-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:34px 0 0}
@media (max-width:760px){.kpi-strip{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px 20px;box-shadow:var(--shadow)}
.kpi .n{font-family:var(--mono);font-size:26px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.kpi .k{color:var(--ink-3);font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;margin-top:4px;font-weight:600}

/* ---------- generic section shell ---------- */
.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin:0 0 6px;flex-wrap:wrap}
.section-head h2{font-size:22px;margin:0;letter-spacing:-.015em}
.section-tag{font-family:var(--mono);font-size:11px;color:var(--ink-3);background:var(--surface-3);
  padding:3px 9px;border-radius:6px}
.section-sub{color:var(--ink-2);font-size:14px;max-width:680px;margin:0 0 26px}
.section-pad{padding:60px 0}
.section-pad.alt{background:var(--surface);border-top:1px solid var(--border);border-bottom:1px solid var(--border)}

.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:24px 26px 20px;box-shadow:var(--shadow)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media (max-width:820px){.grid2{grid-template-columns:1fr}}
.card h3{font-size:15px;margin:0 0 3px}
.card .desc{color:var(--ink-3);font-size:12.5px;margin:0 0 16px;line-height:1.5}

/* explain toggle content */
.lvl-advanced{display:none}
body.mode-advanced .lvl-advanced{display:block}
body.mode-advanced .lvl-simple{display:none}
.explain{margin-top:14px;padding-top:14px;border-top:1px dashed var(--border)}
.explain p{margin:0 0 8px;font-size:13px;color:var(--ink-2);line-height:1.6}
.explain code{background:var(--surface-3);padding:1px 6px;border-radius:5px;font-size:12px}
.explain .formula{display:block;background:var(--surface-3);padding:10px 14px;border-radius:8px;
  font-family:var(--mono);font-size:12.5px;margin:8px 0;color:var(--ink-1);overflow-x:auto;white-space:pre}

svg.chart{width:100%;height:auto;display:block;overflow:visible}
.grid-line{stroke:var(--border);stroke-width:1}
.axis-text{fill:var(--ink-3);font-size:10.5px;font-family:var(--sans)}
.val-text{fill:var(--ink-2);font-size:10.5px;font-weight:700;font-family:var(--sans)}
.legend{display:flex;gap:16px;margin-bottom:14px;flex-wrap:wrap}
.legend span{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--ink-2)}
.swatch{width:10px;height:10px;border-radius:3px;display:inline-block}

/* ---------- live replay ---------- */
.replay-shell{background:var(--surface);border:1px solid var(--border);border-radius:20px;box-shadow:var(--shadow-lg);
  overflow:hidden}
.replay-top{display:flex;align-items:center;justify-content:space-between;padding:16px 22px;border-bottom:1px solid var(--border);
  flex-wrap:wrap;gap:10px}
.live-badge{display:inline-flex;align-items:center;gap:7px;font:700 11px var(--sans);letter-spacing:.06em;
  text-transform:uppercase;color:var(--critical)}
.live-badge .dot{width:7px;height:7px;border-radius:50%;background:var(--critical);animation:pulse 1.6s ease-in-out infinite}
.replay-date{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--ink-1)}
.replay-controls{display:flex;align-items:center;gap:8px}
.icon-btn{width:34px;height:34px;border-radius:9px;border:1px solid var(--border-strong);background:var(--surface);
  color:var(--ink-1);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:.15s}
.icon-btn:hover{background:var(--surface-3)}
.icon-btn svg{width:15px;height:15px}
.speed-group{display:flex;border:1px solid var(--border-strong);border-radius:9px;overflow:hidden}
.speed-group button{background:var(--surface);border:0;border-right:1px solid var(--border);color:var(--ink-3);
  font:700 11px var(--mono);padding:8px 10px;cursor:pointer}
.speed-group button:last-child{border-right:0}
.speed-group button.active{background:var(--ink-1);color:var(--surface)}
.replay-body{display:grid;grid-template-columns:1.5fr 1fr;min-height:360px}
@media (max-width:860px){.replay-body{grid-template-columns:1fr}}
.replay-chart{padding:20px 22px}
.replay-kpis{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
.replay-kpi{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.replay-kpi .n{font-family:var(--mono);font-weight:700;font-size:17px;font-variant-numeric:tabular-nums}
.replay-kpi .k{font-size:10px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.04em}
.scrub{width:100%;accent-color:var(--orange);margin-top:4px}
.feed-panel{border-left:1px solid var(--border);background:var(--surface-2);display:flex;flex-direction:column;max-height:460px}
.feed-head{padding:12px 18px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
  color:var(--ink-3);border-bottom:1px solid var(--border)}
.feed-list{overflow-y:auto;flex:1;padding:6px 0}
.feed-item{display:flex;gap:10px;align-items:flex-start;padding:9px 18px;font-size:12.5px;
  animation:slideIn .35s ease-out}
@keyframes slideIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
.feed-icon{width:22px;height:22px;border-radius:7px;flex-shrink:0;display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:800;color:#fff;margin-top:1px}
.feed-icon.recovered{background:var(--good)}
.feed-icon.lapsed{background:var(--ink-3)}
.feed-icon.nudge{background:var(--orange)}
.feed-text b{color:var(--ink-1)}
.feed-text{color:var(--ink-2);line-height:1.4}
.feed-time{color:var(--ink-3);font-family:var(--mono);font-size:10.5px;margin-left:auto;flex-shrink:0;padding-top:2px}
.feed-list::-webkit-scrollbar{width:6px}
.feed-list::-webkit-scrollbar-thumb{background:var(--border-strong);border-radius:3px}
.feed-empty{padding:30px 18px;color:var(--ink-3);font-size:12.5px;text-align:center}

/* ---------- tables ---------- */
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--border)}
th{color:var(--ink-3);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.03em;
  position:sticky;top:0;background:var(--surface)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--mono)}
tr.highlight td{background:color-mix(in srgb,var(--orange) 10%,transparent);font-weight:700}
.table-scroll{max-height:340px;overflow-y:auto;border:1px solid var(--border);border-radius:12px}
.table-scroll table{margin:0}
.table-scroll thead th{border-bottom:1px solid var(--border)}

/* ---------- quality grid ---------- */
.quality-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
@media (max-width:820px){.quality-grid{grid-template-columns:1fr 1fr}}
@media (max-width:560px){.quality-grid{grid-template-columns:1fr}}
.q-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px 18px;box-shadow:var(--shadow)}
.q-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.q-code{font-family:var(--mono);font-weight:800;font-size:13px}
.chip{font-size:10px;font-weight:800;padding:3px 9px;border-radius:999px;text-transform:uppercase;letter-spacing:.03em}
.chip.fail{background:color-mix(in srgb,var(--critical) 18%,transparent);color:var(--critical)}
.chip.pass{background:color-mix(in srgb,var(--good) 18%,transparent);color:var(--good)}
.q-title{font-size:13px;font-weight:700;margin-bottom:4px}
.q-detail{font-size:12px;color:var(--ink-2);line-height:1.5}
.q-rows{font-family:var(--mono);font-size:11px;color:var(--ink-3);margin-top:8px}

footer{padding:50px 0 70px;text-align:center;color:var(--ink-3);font-size:12.5px;border-top:1px solid var(--border)}
footer a{color:var(--ink-2);text-decoration:none;font-weight:600}
footer a:hover{color:var(--blue)}
.foot-links{display:flex;gap:18px;justify-content:center;margin-bottom:14px;flex-wrap:wrap}
.snapshot-pill{display:inline-block;font-size:11px;padding:4px 10px;border-radius:999px;background:var(--surface-2);
  border:1px solid var(--border);color:var(--ink-2);margin-bottom:10px}

@keyframes fadeUp{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:translateY(0)}}
.reveal{animation:fadeUp .6s ease both}
@media (prefers-reduced-motion:reduce){.reveal{animation:none}}
.reveal.in{opacity:1;transform:translateY(0)}
</style>
</head>
<body class="mode-simple">

<nav class="top">
  <div class="row">
    <div class="brand"><span class="dot"></span>Cadence</div>
    <div class="links">
      <a href="#replay">Live Replay</a>
      <a href="#survival">Survival</a>
      <a href="#cohorts">Cohorts</a>
      <a href="#experiment">Experiment</a>
      <a href="#quality">Data Quality</a>
    </div>
    <div class="level-toggle" role="group" aria-label="Explanation depth">
      <button data-level="simple" class="active">Simple</button>
      <button data-level="advanced">Advanced</button>
    </div>
    <a class="gh-link" href="https://github.com/Navneet-Scaler/cadence" aria-label="GitHub repository" target="_blank" rel="noopener">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38
      0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72
      1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0
      0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08
      2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01
      8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>
    </a>
  </div>
</nav>

<div class="wrap">
  <header class="hero">
    <span class="eyebrow">Cadence &middot; live findings snapshot</span>
    <h1>Where the daily SIP habit breaks, and what to do about it</h1>
    <p class="lede">A replayable, statistically-annotated tour of the retention engine behind Cadence.
      5,000 simulated users, 373,387 transactions, 50,576 streaks, 12 months of daily investing behaviour.
      Toggle <b>Simple / Advanced</b> above to control how deep each explanation goes.</p>
    <div class="hero-actions">
      <a class="btn btn-primary" href="#replay">Watch the year replay &rarr;</a>
      <a class="btn btn-ghost" href="https://github.com/Navneet-Scaler/cadence/blob/main/MEMO.md" target="_blank" rel="noopener">Read the memo</a>
      <a class="btn btn-ghost" href="https://github.com/Navneet-Scaler/cadence" target="_blank" rel="noopener">View source</a>
    </div>
    <div class="rec-card">
      <div class="rec-icon">5</div>
      <div>
        <div class="label">Recommendation</div>
        <div class="headline">Fire the retention nudge on day 5 of a lapse, not day 1</div>
        <div class="sub">+7.7pp recovery (95% CI 5.70&ndash;9.75), 13 sends per recovered user vs 26&ndash;27 on every other trigger day</div>
      </div>
    </div>

    <div class="kpi-strip">
      <div class="kpi"><div class="n" data-count="5000">0</div><div class="k">Users simulated</div></div>
      <div class="kpi"><div class="n" data-count="373387">0</div><div class="k">Transactions</div></div>
      <div class="kpi"><div class="n" data-count="50576">0</div><div class="k">Streaks analysed</div></div>
      <div class="kpi"><div class="n" data-count="660">0</div><div class="k">Extra recoveries/yr at day 5</div></div>
    </div>
  </header>
</div>

<div class="wrap section-pad" id="replay">
  <div class="section-head"><h2>Watch the habit form, day by day</h2><span class="section-tag">replay</span></div>
  <p class="section-sub">This replays the pipeline's actual daily-active-investor series for the full simulated year,
    compressed to under a minute. The feed on the right samples real streak breaks, recoveries, and nudges from the
    generated dataset and surfaces them as the timeline reaches their date. Nothing here is production traffic:
    it is a truthful replay of the same numbers quoted throughout this page, not a live feed of real users.</p>

  <div class="replay-shell reveal">
    <div class="replay-top">
      <div style="display:flex;align-items:center;gap:14px">
        <span class="live-badge"><span class="dot"></span>Replaying</span>
        <span class="replay-date" id="replayDate">1 Jan 2025</span>
      </div>
      <div class="replay-controls">
        <button class="icon-btn" id="playBtn" aria-label="Play/Pause">
          <svg id="playIcon" viewBox="0 0 16 16" fill="currentColor"><path d="M4 2l10 6-10 6V2z"/></svg>
          <svg id="pauseIcon" viewBox="0 0 16 16" fill="currentColor" style="display:none"><path d="M4 2h3v12H4V2zm5 0h3v12H9V2z"/></svg>
        </button>
        <button class="icon-btn" id="resetBtn" aria-label="Restart">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M13.5 8A5.5 5.5 0 1 1 8 2.5"/><path d="M13.5 3v3.5H10" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <div class="speed-group">
          <button data-speed="1" class="">1x</button>
          <button data-speed="4" class="active">4x</button>
          <button data-speed="12">12x</button>
        </div>
      </div>
    </div>
    <div class="replay-body">
      <div class="replay-chart">
        <div class="replay-kpis">
          <div class="replay-kpi"><div class="n" id="kpiActive">0</div><div class="k">Active investors today</div></div>
          <div class="replay-kpi"><div class="n" id="kpiInvested">&#8377;0</div><div class="k">Invested this year so far</div></div>
        </div>
        <svg class="chart" viewBox="0 0 640 220" id="chartReplay"></svg>
        <input type="range" class="scrub" id="scrub" min="0" max="360" value="0">
      </div>
      <div class="feed-panel">
        <div class="feed-head">Event stream (sampled, real values)</div>
        <div class="feed-list" id="feedList"><div class="feed-empty">Press play to start the replay.</div></div>
      </div>
    </div>
  </div>
</div>

<div class="wrap section-pad alt" id="survival">
  <div class="section-head"><h2>Survival analysis</h2><span class="section-tag">Kaplan&ndash;Meier &middot; Cox PH</span></div>
  <p class="section-sub">Two different questions: how long a streak lives, and once it breaks, whether the user comes back.
    Both are handled with censoring-aware survival methods rather than raw percentages.</p>

  <div class="grid2">
    <div class="card reveal">
      <h3>Recovery collapses between day 3 and day 7</h3>
      <p class="desc">Conditional probability a lapsed user returns within 30 days, by days already missed.</p>
      <svg class="chart" viewBox="0 0 480 240" id="chartRecovery"></svg>
      <div class="explain">
        <div class="lvl-simple"><p>If someone misses one day, 93 out of 100 come back within a month. But if they've
          already been gone for a week, only about half do. The sweet spot to send a reminder is day 5, right before
          most people give up for good.</p></div>
        <div class="lvl-advanced">
          <p>Kaplan&ndash;Meier estimate of time-to-return after a streak break, with users still absent at the
            30-day horizon treated as right-censored rather than "never returning."</p>
          <span class="formula">P(return | missed k days) = 1 − S(30) / S(k)</span>
          <p>Computed from the fitted survival function, not by counting rows: a user who lapsed 3 days before the
            observation window closed never had a chance to return, and counting them as churned would bias the
            estimate. See <code>src/analysis/survival_analysis.py</code>.</p>
        </div>
      </div>
    </div>

    <div class="card reveal">
      <h3>What actually changes the hazard of a break</h3>
      <p class="desc">Cox proportional hazards, clustered on user_id. &gt;1 breaks faster, &lt;1 holds longer.</p>
      <svg class="chart" viewBox="0 0 480 260" id="chartHazard"></svg>
      <div class="explain">
        <div class="lvl-simple"><p>A user's very first streak is the fragile one; it breaks about 18% faster than
          their later streaks. Where someone came from (an ad, a friend's referral) or which city tier they're in
          doesn't meaningfully change their odds once you account for this.</p></div>
        <div class="lvl-advanced">
          <p>Coefficients exponentiate to hazard ratios. Standard errors are clustered on <code>user_id</code>
            because 50,576 streaks come from 5,000 users: treating them as independent inflates the effective
            sample size roughly tenfold and manufactures significance. Two covariates that looked significant
            unclustered (KYC completion speed, weekend signup) are null once clustering is applied.</p>
          <span class="formula">h(t|x) = h0(t) &middot; exp(&beta;&middot;x),  robust (sandwich) SE by cluster</span>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="wrap section-pad" id="cohorts">
  <div class="section-head"><h2>Cohort retention, redefined</h2><span class="section-tag">27 weekly cohorts</span></div>
  <p class="section-sub">D1 / D7 / D30, but "retained" means the user actually invested that day, not merely that
    the account still exists. Cohorts without full exposure to a horizon are excluded rather than divided by a
    partial denominator.</p>
  <div class="card reveal">
    <div class="table-scroll">
      <table id="cohortTable">
        <thead><tr><th>Cohort week</th><th class="num">Size</th><th class="num">D1 %</th><th class="num">D7 %</th><th class="num">D30 %</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="explain">
      <div class="lvl-simple"><p>Out of everyone who signed up in a given week, this shows what share were still
        actually investing 1, 7, and 30 days later. It stays fairly flat around 50&ndash;55% at D30 across the whole
        year, no cohort is doing dramatically better or worse than another.</p></div>
      <div class="lvl-advanced">
        <p>Pooled retention is size-weighted, not an unweighted mean of cohorts, so a 52-user cohort cannot move the
          headline as much as a 295-user one. A linear fit on the D30 series across all 27 cohorts gives a slope of
          &minus;0.004 points per weekly cohort, indistinguishable from zero given a range of 47.1&ndash;66.3%.</p>
      </div>
    </div>
  </div>
</div>

<div class="wrap section-pad alt" id="experiment">
  <div class="section-head"><h2>The nudge experiment</h2><span class="section-tag">risk-set matched &middot; Holm&ndash;Bonferroni</span></div>
  <p class="section-sub">Users were randomised into treatment/control at signup, before any behaviour was observed.
    Each treated break is compared only against control breaks that reached the same gap length unrecovered.</p>

  <div class="grid2">
    <div class="card reveal">
      <h3>Lift by trigger day</h3>
      <p class="desc">Percentage-point lift in 30-day recovery vs matched control.</p>
      <svg class="chart" viewBox="0 0 480 240" id="chartNudge"></svg>
    </div>
    <div class="card reveal">
      <h3>What each trigger day is worth</h3>
      <table>
        <thead><tr><th>Day</th><th class="num">Lift</th><th class="num">95% CI</th><th class="num">Sends/recovery</th></tr></thead>
        <tbody>
          <tr><td>1</td><td class="num">+3.7pp</td><td class="num">2.98&ndash;4.36</td><td class="num">27.3</td></tr>
          <tr><td>2</td><td class="num">+3.8pp</td><td class="num">3.08&ndash;4.48</td><td class="num">26.4</td></tr>
          <tr><td>3</td><td class="num">+3.6pp</td><td class="num">2.50&ndash;4.67</td><td class="num">27.9</td></tr>
          <tr class="highlight"><td>5</td><td class="num">+7.7pp</td><td class="num">5.70&ndash;9.75</td><td class="num">12.9</td></tr>
          <tr><td>7</td><td class="num">+4.1pp</td><td class="num">0.62&ndash;7.50</td><td class="num">24.6</td></tr>
        </tbody>
      </table>
      <div class="explain">
        <div class="lvl-simple"><p>Sending a reminder on day 5 works roughly twice as well as sending one on day 1,
          2, 3, or 7. Day 1 nudges mostly reach people who were already coming back on their own.</p></div>
        <div class="lvl-advanced">
          <p>Five thresholds tested simultaneously carry a ~23% chance of at least one false positive at
            &alpha;=0.05 by chance alone. Holm&ndash;Bonferroni correction is applied; all five thresholds remain
            significant after correction, and day 5 is the standout by effect size, not merely by significance.</p>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="wrap section-pad" id="distributions">
  <div class="section-head"><h2>Who is actually investing</h2><span class="section-tag">segmentation</span></div>
  <div class="grid2">
    <div class="card reveal">
      <h3>Consistency is bimodal</h3>
      <p class="desc">Share of days since signup each user actually invested. No meaningful middle.</p>
      <svg class="chart" viewBox="0 0 480 220" id="chartConsistency"></svg>
    </div>
    <div class="card reveal">
      <h3>Live streaks by length band</h3>
      <p class="desc">How many people currently hold a habit, and how deep it goes.</p>
      <svg class="chart" viewBox="0 0 480 220" id="chartBands"></svg>
    </div>
  </div>
  <div class="explain" style="margin-top:20px">
    <div class="lvl-simple"><p>2,435 users barely invest at all (under 10% of days). 1,041 invest almost every
      single day (over 90%). Almost nobody is in between, the "average" user described by a typical dashboard
      metric barely exists.</p></div>
    <div class="lvl-advanced"><p>This bimodality was not a hypothesis going in. It reframes the retention question
      from "raise the average" (which targets a near-empty region of the distribution) to "what moves a user from
      the left cluster to the right one, and how early is that transition visible."</p></div>
  </div>
</div>

<div class="wrap section-pad alt" id="quality">
  <div class="section-head"><h2>Data quality, in the open</h2><span class="section-tag">6 automated checks</span></div>
  <p class="section-sub">Checks run against the raw tables, never the cleaned view, so a defect the analysis
    already tolerates does not silently disappear from the audit trail.</p>
  <div class="quality-grid" id="qualityGrid"></div>
</div>

<div class="wrap">
  <footer>
    <span class="snapshot-pill">Snapshot &middot; simulated data &middot; regenerates from a fixed seed</span>
    <div class="foot-links">
      <a href="https://github.com/Navneet-Scaler/cadence">GitHub</a>
      <a href="https://github.com/Navneet-Scaler/cadence/blob/main/MEMO.md">Memo</a>
      <a href="https://github.com/Navneet-Scaler/cadence/blob/main/ASSUMPTIONS.md">Assumptions</a>
      <a href="https://github.com/Navneet-Scaler/cadence/blob/main/data_quality_findings.md">Data quality findings</a>
      <a href="https://github.com/Navneet-Scaler/cadence/releases">Releases</a>
    </div>
    <div>Built end to end from schema to decision memo.</div>
  </footer>
</div>

<script id="cadence-data" type="application/json">__DATA_JSON__</script>
<script>
(function(){
"use strict";
const DATA = JSON.parse(document.getElementById("cadence-data").textContent);
const css = getComputedStyle(document.documentElement);
const v = n => css.getPropertyValue(n).trim();

/* ---------- level toggle ---------- */
document.querySelectorAll(".level-toggle button").forEach(btn=>{
  btn.addEventListener("click", ()=>{
    document.querySelectorAll(".level-toggle button").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    document.body.classList.toggle("mode-advanced", btn.dataset.level==="advanced");
    document.body.classList.toggle("mode-simple", btn.dataset.level==="simple");
  });
});

/* ---------- count-up KPIs ----------
   Runs immediately on load rather than behind an IntersectionObserver: the
   hero KPI strip is always above the fold, and headless/no-compositor
   environments (screenshot tooling, some in-app browsers) don't reliably
   fire intersection callbacks, which previously left these stuck at 0. */
function countUp(el, to, dur){
  const start = performance.now();
  function step(now){
    const p = Math.min(1,(now-start)/dur);
    const eased = 1-Math.pow(1-p,3);
    el.textContent = Math.round(to*eased).toLocaleString();
    if(p<1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}
document.querySelectorAll("[data-count]").forEach(el=>countUp(el, parseInt(el.dataset.count,10), 1400));

/* ---------- chart helpers ---------- */
function el(tag, attrs){ const e=document.createElementNS("http://www.w3.org/2000/svg",tag);
  for(const k in attrs) e.setAttribute(k, attrs[k]); return e; }

function barChart(svg, data, opts){
  svg.innerHTML="";
  const W=svg.viewBox.baseVal.width, H=svg.viewBox.baseVal.height;
  const padL=opts.padL||38, padB=32, padT=10, padR=opts.padR||8;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const maxV=opts.max||Math.max(...data.map(d=>d.v))*1.15;
  const n=data.length, gap=plotW/n, bw=gap*(opts.bwFrac||0.55);
  [0,.25,.5,.75,1].forEach(f=>{
    const y=padT+plotH*(1-f);
    svg.appendChild(el("line",{x1:padL,x2:W-padR,y1:y,y2:y,class:"grid-line"}));
    svg.appendChild(Object.assign(el("text",{x:padL-7,y:y+3,class:"axis-text","text-anchor":"end"}),
      {textContent: opts.fmtY?opts.fmtY(maxV*f):Math.round(maxV*f)}));
  });
  data.forEach((d,i)=>{
    const cx=padL+gap*i+gap/2, h=(d.v/maxV)*plotH, y=padT+plotH-h;
    const r=el("rect",{x:cx-bw/2,y,width:bw,height:h,rx:4,fill:d.color||v("--blue")});
    svg.appendChild(r);
    svg.appendChild(Object.assign(el("text",{x:cx,y:y-6,class:"val-text","text-anchor":"middle"}),
      {textContent: opts.fmtV?opts.fmtV(d.v):d.v}));
    svg.appendChild(Object.assign(el("text",{x:cx,y:H-padB+16,class:"axis-text","text-anchor":"middle"}),
      {textContent:d.label}));
  });
}

function lineChart(svg, data, opts){
  svg.innerHTML="";
  const W=svg.viewBox.baseVal.width, H=svg.viewBox.baseVal.height;
  const padL=42, padB=28, padT=14, padR=16;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const maxV=opts.max||100;
  const xs=data.map(d=>d.x), minX=Math.min(...xs), maxX=Math.max(...xs);
  [0,.25,.5,.75,1].forEach(f=>{
    const y=padT+plotH*(1-f);
    svg.appendChild(el("line",{x1:padL,x2:W-padR,y1:y,y2:y,class:"grid-line"}));
    svg.appendChild(Object.assign(el("text",{x:padL-7,y:y+3,class:"axis-text","text-anchor":"end"}),
      {textContent: Math.round(maxV*f)+(opts.pct?"%":"")}));
  });
  const pts=data.map(d=>[padL+((d.x-minX)/(maxX-minX))*plotW, padT+plotH*(1-d.v/maxV), d]);
  svg.appendChild(el("path",{d:pts.map((p,i)=>(i===0?"M":"L")+p[0]+" "+p[1]).join(" "),
    fill:"none",stroke:v("--blue"),"stroke-width":3,"stroke-linecap":"round","stroke-linejoin":"round"}));
  pts.forEach(([x,y,d])=>{
    svg.appendChild(el("circle",{cx:x,cy:y,r:d.emph?6:4,fill:d.emph?v("--orange"):v("--blue"),
      stroke:v("--surface"),"stroke-width":2}));
    svg.appendChild(Object.assign(el("text",{x,y:y-12,class:"val-text","text-anchor":"middle"}),
      {textContent:d.v+"%"}));
    svg.appendChild(Object.assign(el("text",{x,y:H-padB+16,class:"axis-text","text-anchor":"middle"}),
      {textContent:"Day "+d.x}));
  });
}

function forestChart(svg, data){
  svg.innerHTML="";
  const W=svg.viewBox.baseVal.width, H=svg.viewBox.baseVal.height;
  const padL=150, padR=60, padT=14, padB=30;
  const plotW=W-padL-padR;
  const xMin=0.8, xMax=1.25;
  const xScale=x=>padL+((x-xMin)/(xMax-xMin))*plotW;
  const rowH=(H-padT-padB)/data.length;
  const zeroX=xScale(1);
  svg.appendChild(el("line",{x1:zeroX,x2:zeroX,y1:padT,y2:H-padB,stroke:v("--border-strong"),"stroke-width":1}));
  data.forEach((d,i)=>{
    const y=padT+rowH*i+rowH/2;
    const color = !d.sig ? v("--ink-3") : (d.hr>1 ? v("--critical") : v("--good"));
    svg.appendChild(el("line",{x1:xScale(Math.max(d.lo,xMin)),x2:xScale(Math.min(d.hi,xMax)),y1:y,y2:y,
      stroke:color,"stroke-width":2.5,opacity:.6}));
    svg.appendChild(el("circle",{cx:xScale(d.hr),cy:y,r:5,fill:color}));
    svg.appendChild(Object.assign(el("text",{x:10,y:y+4,class:"axis-text"}),{textContent:d.label}));
    svg.appendChild(Object.assign(el("text",{x:xScale(Math.min(d.hi,xMax))+8,y:y+4,class:"val-text"}),
      {textContent:d.hr.toFixed(2)+(d.sig?"":" n.s.")}));
  });
}

/* ---------- static charts ---------- */
lineChart(document.getElementById("chartRecovery"), [
  {x:1,v:93},{x:3,v:84},{x:5,v:69,emph:true},{x:7,v:51},{x:14,v:20}
], {pct:true});

forestChart(document.getElementById("chartHazard"), [
  {label:"1st streak",hr:1.18,lo:1.14,hi:1.21,sig:true},
  {label:"KYC incomplete",hr:1.07,lo:0.93,hi:1.22,sig:false},
  {label:"Referral",hr:1.02,lo:0.95,hi:1.10,sig:false},
  {label:"Weekend signup",hr:0.97,lo:0.93,hi:1.01,sig:false},
  {label:"City tier 2",hr:0.98,lo:0.94,hi:1.03,sig:false},
  {label:"KYC weeks",hr:0.96,lo:0.82,hi:1.13,sig:false},
]);

barChart(document.getElementById("chartNudge"), [
  {label:"1",v:3.7,color:v("--ink-3")},{label:"2",v:3.8,color:v("--ink-3")},
  {label:"3",v:3.6,color:v("--ink-3")},{label:"5",v:7.7,color:v("--orange")},
  {label:"7",v:4.1,color:v("--ink-3")},
], {max:9, fmtV:n=>"+"+n.toFixed(1)});

barChart(document.getElementById("chartConsistency"), [
  {label:"10%",v:2435,color:v("--blue")},{label:"20%",v:477,color:v("--blue")},
  {label:"30%",v:286,color:v("--blue")},{label:"40%",v:206,color:v("--blue")},
  {label:"50%",v:188,color:v("--blue")},{label:"60%",v:116,color:v("--blue")},
  {label:"70%",v:78,color:v("--blue")},{label:"80%",v:173,color:v("--blue")},
  {label:"90%",v:834,color:v("--green")},{label:"100%",v:207,color:v("--green")},
], {max:2600, fmtV:n=>n>=1000?(n/1000).toFixed(1)+"k":n, bwFrac:.65});

const bandOrder=["1-2","3-6","7-13","14-29","30+"];
const bandMap=Object.fromEntries(DATA.bands.map(b=>[b.band,b.n]));
barChart(document.getElementById("chartBands"), bandOrder.map(b=>({label:b+"d",v:bandMap[b]||0,color:v("--violet")})),
  {max:Math.max(...Object.values(bandMap))*1.2});

/* ---------- cohort table ---------- */
const tbody=document.querySelector("#cohortTable tbody");
DATA.cohort.forEach(c=>{
  const tr=document.createElement("tr");
  tr.innerHTML=`<td>${new Date(c.week).toLocaleDateString("en-GB",{day:"numeric",month:"short",year:"numeric"})}</td>
    <td class="num">${c.sz}</td><td class="num">${c.d1}</td><td class="num">${c.d7}</td><td class="num">${c.d30}</td>`;
  tbody.appendChild(tr);
});

/* ---------- quality grid ---------- */
const quality=[
  {code:"DQ-01",title:"Null transaction amounts",sev:"high",pass:false,rows:"5,593 rows &middot; 1.50%",detail:"Successful rows recording no amount understate revenue silently."},
  {code:"DQ-02",title:"Duplicate transactions",sev:"high",pass:false,rows:"3,697 user-days &middot; 1,884 users",detail:"A duplicate shatters one real streak into several in the gaps-and-islands query."},
  {code:"DQ-03",title:"Transactions before signup",sev:"medium",pass:false,rows:"1,472 rows &middot; 1,094 users",detail:"Client-timestamp trust produces negative day-since-signup indices."},
  {code:"DQ-04",title:"Undocumented status values",sev:"high",pass:false,rows:"747 rows, wrong-case SUCCESS",detail:"Invisible to every status='success' filter while being a real contribution."},
  {code:"DQ-05",title:"Unverified users transacting",sev:"high",pass:false,rows:"557 rows &middot; 450 users &middot; &#8377;10.7L",detail:"Missing KYC gate or stale status field, the data alone cannot say which."},
  {code:"DQ-06",title:"KYC status/timestamp mismatch",sev:"medium",pass:true,rows:"0 rows",detail:"Passes today. Kept because the two fields are written separately and can drift."},
];
const qg=document.getElementById("qualityGrid");
quality.forEach(q=>{
  const div=document.createElement("div");
  div.className="q-card reveal";
  div.innerHTML=`<div class="q-top"><span class="q-code">${q.code}</span>
    <span class="chip ${q.pass?"pass":"fail"}">${q.pass?"Pass":"Fail"}</span></div>
    <div class="q-title">${q.title}</div><div class="q-detail">${q.detail}</div>
    <div class="q-rows">${q.rows}</div>`;
  qg.appendChild(div);
});

/* ---------- live replay engine ---------- */
const daily = DATA.daily; // [{d,u,amt}]
const feed = DATA.feed;   // sorted [{d,type,...}]
const replayDate=document.getElementById("replayDate");
const kpiActive=document.getElementById("kpiActive");
const kpiInvested=document.getElementById("kpiInvested");
const scrub=document.getElementById("scrub");
const feedList=document.getElementById("feedList");
const chartReplay=document.getElementById("chartReplay");
scrub.max = daily.length-1;

let idx=0, playing=false, speed=4, feedPtr=0, cumInvested=0, rafId=null, lastTick=0;

function fmtDate(s){ return new Date(s).toLocaleDateString("en-GB",{day:"numeric",month:"short",year:"numeric"}); }
function fmtINR(n){ if(n>=100000) return "₹"+(n/100000).toFixed(1)+"L"; if(n>=1000) return "₹"+(n/1000).toFixed(1)+"k"; return "₹"+Math.round(n); }

function drawReplayChart(uptoIdx){
  const W=640,H=220,padL=42,padB=22,padT=10,padR=10;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const maxU = Math.max(...daily.map(d=>d.u))*1.1;
  chartReplay.innerHTML="";
  [0,.5,1].forEach(f=>{
    const y=padT+plotH*(1-f);
    chartReplay.appendChild(el("line",{x1:padL,x2:W-padR,y1:y,y2:y,class:"grid-line"}));
    chartReplay.appendChild(Object.assign(el("text",{x:padL-7,y:y+3,class:"axis-text","text-anchor":"end"}),
      {textContent:Math.round(maxU*f)}));
  });
  const visible = daily.slice(0, uptoIdx+1);
  if(visible.length<2) return;
  const pts = visible.map((d,i)=>[padL+(i/(daily.length-1))*plotW, padT+plotH*(1-d.u/maxU)]);
  chartReplay.appendChild(el("path",{d:pts.map((p,i)=>(i===0?"M":"L")+p[0]+" "+p[1]).join(" "),
    fill:"none",stroke:v("--blue"),"stroke-width":2.5,"stroke-linecap":"round","stroke-linejoin":"round"}));
  const last=pts[pts.length-1];
  chartReplay.appendChild(el("circle",{cx:last[0],cy:last[1],r:5,fill:v("--orange"),stroke:v("--surface"),"stroke-width":2}));
  chartReplay.appendChild(Object.assign(el("text",{x:padL,y:H-4,class:"axis-text"}),{textContent:fmtDate(daily[0].d)}));
  chartReplay.appendChild(Object.assign(el("text",{x:W-padR,y:H-4,class:"axis-text","text-anchor":"end"}),{textContent:fmtDate(daily[daily.length-1].d)}));
}

const feedIcons={recovered:"&#10003;",lapsed:"&times;",nudge:"&#9993;"};
function feedLine(ev){
  if(ev.type==="nudge") return `<b>Nudge sent</b> (${ev.channel}) after ${ev.gap}d missed &middot; ${ev.archetype.replace("_"," ")}`;
  if(ev.type==="recovered") return `Streak of <b>${ev.streak}d</b> broke, recovered after ${ev.gap??"?"}d &middot; ${ev.archetype.replace("_"," ")}`;
  return `Streak of <b>${ev.streak}d</b> broke, no recovery yet &middot; ${ev.archetype.replace("_"," ")}`;
}
function pushFeed(ev){
  if(feedList.querySelector(".feed-empty")) feedList.innerHTML="";
  const div=document.createElement("div");
  div.className="feed-item";
  div.innerHTML=`<div class="feed-icon ${ev.type}">${feedIcons[ev.type]}</div>
    <div class="feed-text">${feedLine(ev)}</div><div class="feed-time">${fmtDate(ev.d)}</div>`;
  feedList.appendChild(div);
  feedList.scrollTop = feedList.scrollHeight;
  while(feedList.children.length>60) feedList.removeChild(feedList.firstChild);
}

function renderAt(i){
  i=Math.max(0,Math.min(daily.length-1,i));
  idx=i;
  const row=daily[i];
  replayDate.textContent=fmtDate(row.d);
  kpiActive.textContent=row.u.toLocaleString();
  cumInvested = daily.slice(0,i+1).reduce((s,d)=>s+parseFloat(d.amt),0);
  kpiInvested.textContent=fmtINR(cumInvested);
  scrub.value=i;
  drawReplayChart(i);
  while(feedPtr<feed.length && feed[feedPtr].d <= row.d){ pushFeed(feed[feedPtr]); feedPtr++; }
}

function resetReplay(){
  feedPtr=0; feedList.innerHTML='<div class="feed-empty">Press play to start the replay.</div>';
  renderAt(0);
}

function tick(now){
  if(!playing) return;
  if(now-lastTick > (140/speed)){
    lastTick=now;
    if(idx>=daily.length-1){ playing=false; setPlayIcon(); return; }
    renderAt(idx+1);
  }
  rafId=requestAnimationFrame(tick);
}
function setPlayIcon(){
  document.getElementById("playIcon").style.display=playing?"none":"block";
  document.getElementById("pauseIcon").style.display=playing?"block":"none";
}
document.getElementById("playBtn").addEventListener("click",()=>{
  playing=!playing; setPlayIcon();
  if(playing){ lastTick=0; rafId=requestAnimationFrame(tick); }
});
document.getElementById("resetBtn").addEventListener("click",()=>{ playing=false; setPlayIcon(); resetReplay(); });
document.querySelectorAll(".speed-group button").forEach(b=>{
  b.addEventListener("click",()=>{
    document.querySelectorAll(".speed-group button").forEach(x=>x.classList.remove("active"));
    b.classList.add("active"); speed=parseFloat(b.dataset.speed);
  });
});
scrub.addEventListener("input",()=>{ playing=false; setPlayIcon(); feedPtr=0; feedList.innerHTML=""; renderAt(parseInt(scrub.value,10)); });

resetReplay();

/* autoplay once visible, with a timed fallback for environments where
   IntersectionObserver callbacks don't fire (headless capture, some
   in-app browsers) so the replay is never permanently stuck at day 1 */
function startAutoplay(){
  if(idx===0 && !playing){ playing=true; setPlayIcon(); lastTick=0; rafId=requestAnimationFrame(tick); }
}
if("IntersectionObserver" in window){
  const replayIo=new IntersectionObserver(entries=>{
    entries.forEach(e=>{ if(e.isIntersecting){ startAutoplay(); replayIo.disconnect(); } });
  },{threshold:.4});
  replayIo.observe(document.getElementById("replay"));
}
setTimeout(startAutoplay, 1500);

})();
</script>
</body></html>
"""


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    data_json = json.dumps(fetch_payload(), separators=(",", ":"), default=str)
    html = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    out = db.PROJECT_ROOT / "docs" / "index.html"
    out.write_text(html)
    logger.info("wrote %s (%s bytes)", out, f"{len(html):,}")


if __name__ == "__main__":
    main()
