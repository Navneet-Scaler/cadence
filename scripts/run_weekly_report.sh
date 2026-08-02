#!/usr/bin/env bash
#
# Cron wrapper for the weekly retention report.
#
# Install (Mondays at 06:00):
#   crontab -e
#   0 6 * * 1 /path/to/cadence/scripts/run_weekly_report.sh >> /tmp/cadence-cron.log 2>&1
#
# Cron runs with a near-empty environment and a working directory that is not
# the repo, which is the single most common reason a scheduled job that works
# by hand fails at 6am. This wrapper fixes both, then branches on the report's
# exit code so alerts can be routed somewhere a human will see them.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON="${PROJECT_ROOT}/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "venv not found at ${PYTHON} — run 'make setup' first" >&2
    exit 2
fi

set +e
"$PYTHON" -m src.reporting.generate_weekly_report "$@"
STATUS=$?
set -e

case "$STATUS" in
    0)
        echo "cadence: report generated, no alerts"
        ;;
    1)
        echo "cadence: report generated WITH ALERTS — see reports/weekly_report_latest.md"
        # Route to Slack/email/PagerDuty here. Kept as a comment rather than a
        # broken curl so the script is runnable as-is:
        # curl -sS -X POST "$SLACK_WEBHOOK_URL" \
        #     -H 'Content-Type: application/json' \
        #     -d "$(jq -Rn --rawfile r reports/weekly_report_latest.md '{text: $r}')"
        ;;
    *)
        echo "cadence: report FAILED (exit ${STATUS}) — see reports/logs/weekly_report.log" >&2
        ;;
esac

exit "$STATUS"
