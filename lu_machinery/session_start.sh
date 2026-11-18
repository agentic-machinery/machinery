#!/bin/bash
# Session Start — project state at a glance
# Usage: scripts/session_start.sh
#
# Pulls from monitoring API (localhost:8765) to show:
#   1. Project totals
#   2. Per-track progress (research → content → audit → review)
#   3. Active builds
#   4. Failing modules
#   5. Active-manifest learner state
#   6. Fleet Comms inbox count
#
# Issue: #595

set -euo pipefail

API="http://localhost:8765"
# The machinery package directory holding this script, from its own (unresolved)
# location; its name spells the package in the commands below.
MACHINERY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MACHINERY_PKG="$(basename "$MACHINERY_DIR")"
BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
PURPLE='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Check API is running
# ---------------------------------------------------------------------------
if ! curl -s --max-time 2 "$API/api/state/summary" > /dev/null 2>&1; then
    echo -e "${RED}Monitoring API not running at $API${NC}"
    echo "Start it:  the project interpreter -m uvicorn ${MACHINERY_PKG}.api.main:app --host 0.0.0.0 --port 8765 --reload &"
    exit 1
fi

echo ""
echo -e "${BOLD}==============================================${NC}"
echo -e "${BOLD}  Session Start — $(date '+%Y-%m-%d %H:%M')${NC}"
echo -e "${BOLD}==============================================${NC}"
echo ""

# ---------------------------------------------------------------------------
# 1. Project totals
# ---------------------------------------------------------------------------
SUMMARY=$(curl -s "$API/api/state/summary")

total=$(echo "$SUMMARY" | jq '.totals.total')
research=$(echo "$SUMMARY" | jq '.totals.research_done')
content=$(echo "$SUMMARY" | jq '.totals.content_done')
audit=$(echo "$SUMMARY" | jq '.totals.audit_passing')
review=$(echo "$SUMMARY" | jq '.totals.final_review_done')

research_pct=$((research * 100 / total))
content_pct=$((content * 100 / total))
audit_pct=$((audit * 100 / total))
review_pct=$((review * 100 / total))

echo -e "${BOLD}PROJECT TOTALS${NC}  ($total modules)"
echo -e "  Research:     ${YELLOW}$research${NC}/$total  (${research_pct}%)"
echo -e "  Content:      ${BLUE}$content${NC}/$total  (${content_pct}%)"
echo -e "  Audit pass:   ${GREEN}$audit${NC}/$total  (${audit_pct}%)"
echo -e "  Final review: ${PURPLE}$review${NC}/$total  (${review_pct}%)"
echo ""

# ---------------------------------------------------------------------------
# 2. Per-track progress table
# ---------------------------------------------------------------------------
echo -e "${BOLD}PER-TRACK PROGRESS${NC}"
printf "  ${DIM}%-14s %5s  %5s  %5s  %5s  %5s  %s${NC}\n" \
    "Track" "Total" "Res" "Cont" "Audit" "Rev" "Profile"
echo -e "  ${DIM}$(printf '%.0s-' {1..65})${NC}"

# Sort tracks: core first, then seminar, then pro
echo "$SUMMARY" | jq -r '
  .tracks | to_entries | sort_by(
    if .value.profile == "core" then "0" + .key
    elif .value.profile == "seminar" then "1" + .key
    else "2" + .key end
  ) | .[] | [.key, .value.total, .value.research_done, .value.content_done,
              .value.audit_passing, .value.final_review_done, .value.profile] | @tsv
' | while IFS=$'\t' read -r track t r c a rv prof; do
    # Color the track name by profile
    case "$prof" in
        core)    track_color="$CYAN" ;;
        seminar) track_color="$YELLOW" ;;
        pro)     track_color="$PURPLE" ;;
        *)       track_color="$NC" ;;
    esac

    # Highlight fully-researched tracks
    if [ "$r" = "$t" ]; then
        r_disp="${GREEN}${r}${NC}"
    elif [ "$r" = "0" ]; then
        r_disp="${RED}${r}${NC}"
    else
        r_disp="${YELLOW}${r}${NC}"
    fi

    printf "  ${track_color}%-14s${NC} %5s  %5s  %5s  %5s  %5s  ${DIM}%s${NC}\n" \
        "$track" "$t" "$r" "$c" "$a" "$rv" "$prof"
done
echo ""

# ---------------------------------------------------------------------------
# 3. Active builds
# ---------------------------------------------------------------------------
ACTIVE=$(curl -s "$API/api/batch/active")
active_count=$(echo "$ACTIVE" | jq 'length')

if [ "$active_count" -gt 0 ]; then
    echo -e "${BOLD}ACTIVE BUILDS${NC}  ($active_count running)"
    echo "$ACTIVE" | jq -r '.[] | "  \(.track)/\(.slug) — \(.seconds_ago)s ago"'
else
    echo -e "${BOLD}ACTIVE BUILDS${NC}  ${DIM}none${NC}"
fi
echo ""

# ---------------------------------------------------------------------------
# 4. Fleet Comms inbox (body-free count from the Monitor API)
# ---------------------------------------------------------------------------
# Only a registered seat has an inbox: without SESSION_HANDOFF_AGENT it is unknown.
INBOX_AGENT="${SESSION_HANDOFF_AGENT:-}"
if [ -z "$INBOX_AGENT" ]; then
    echo -e "${BOLD}INBOX${NC}  ${DIM}unknown (no seat identity: set SESSION_HANDOFF_AGENT to your slot)${NC}"
    INBOX_COUNT=""
else
    INBOX_COUNT=$(curl -s --max-time 5 -G "$API/api/comms/inbox" --data-urlencode "agent=$INBOX_AGENT" \
        | jq -r 'if .probe == "ok" and (.count | type == "number") then .count else "?" end' 2>/dev/null || echo "?")
fi
if [ -z "$INBOX_AGENT" ]; then
    :
elif [ "$INBOX_COUNT" = "?" ] || [ -z "$INBOX_COUNT" ]; then
    echo -e "${BOLD}INBOX${NC}  ${DIM}unknown (${MACHINERY_PKG}.fleet_comms deliveries pending --recipient $INBOX_AGENT)${NC}"
elif [ "$INBOX_COUNT" != "0" ]; then
    echo -e "${BOLD}INBOX${NC}  ${YELLOW}$INBOX_COUNT queued for $INBOX_AGENT${NC}"
else
    echo -e "${BOLD}INBOX${NC}  ${DIM}empty${NC}"
fi

echo ""
echo -e "${DIM}Endpoints: $API/api/state/{summary,pipeline/{track},weak-points}${NC}"
echo -e "${DIM}Dashboard: $API/ | Docs: $API/docs${NC}"
echo ""
