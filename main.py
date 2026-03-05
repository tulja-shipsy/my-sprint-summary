"""
Weekly Sprint Support Summary — FastAPI wrapper
Triggered by n8n Cloud via HTTP POST /run

Deploy on Render (free tier):
  - Build command: pip install -r requirements.txt
  - Start command: uvicorn main:app --host 0.0.0.0 --port 10000

Environment Variables (set in Render dashboard):
  DEVREV_PAT        = your devrev personal access token
  SLACK_BOT_TOKEN   = xoxb-your-slack-bot-token
  SLACK_CHANNEL     = @your-username  (DM for testing) or #channel-name (live)
  DEVREV_VISTA_ID   = your vista id (optional)
  API_SECRET        = any random string — used to protect the endpoint
"""

import os
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from fastapi import FastAPI, Header, HTTPException

app = FastAPI()

# ─────────────────────────────────────────────
# Config (from Render environment variables)
# ─────────────────────────────────────────────
DEVREV_PAT      = os.environ.get("DEVREV_PAT")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL   = os.environ.get("SLACK_CHANNEL", "@your-username")
DEVREV_VISTA_ID = os.environ.get("DEVREV_VISTA_ID")
API_SECRET      = os.environ.get("API_SECRET", "change-me")

DEVREV_BASE_URL = "https://api.devrev.ai"
SLACK_API_URL   = "https://slack.com/api/chat.postMessage"
CLOSED_STAGES   = {"closed", "resolved", "won't fix", "wont fix", "cancelled"}


# ─────────────────────────────────────────────
# Health check — n8n or browser can ping this
# ─────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "sprint-summary"}


# ─────────────────────────────────────────────
# Main trigger endpoint — called by n8n
# ─────────────────────────────────────────────
@app.post("/run")
def run_summary(x_api_secret: str = Header(default=None)):
    # Simple auth check
    if x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        sprint_start, sprint_end = get_sprint_window()
        issues                   = fetch_devrev_issues(sprint_start, sprint_end)
        summary                  = aggregate_issues(issues)
        message                  = format_slack_message(summary, sprint_start, sprint_end)
        post_to_slack(message)

        return {
            "status":       "ok",
            "sprint_start": sprint_start,
            "sprint_end":   sprint_end,
            "total":        summary["total"],
            "open":         summary["open"],
            "closed":       summary["closed"],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Step 1: Sprint window (last 7 days)
# ─────────────────────────────────────────────
def get_sprint_window() -> tuple[str, str]:
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    return start.isoformat(), end.isoformat()


# ─────────────────────────────────────────────
# Step 2: Fetch DevRev issues (paginated)
# ─────────────────────────────────────────────
def fetch_devrev_issues(sprint_start: str, sprint_end: str) -> list:
    headers = {
        "Authorization": f"Bearer {DEVREV_PAT}",
        "Content-Type":  "application/json",
    }

    all_issues = []
    cursor     = None

    while True:
        if DEVREV_VISTA_ID:
            endpoint = f"{DEVREV_BASE_URL}/vistas.works.list"
            payload  = {"vista": DEVREV_VISTA_ID, "limit": 100}
        else:
            endpoint = f"{DEVREV_BASE_URL}/works.list"
            payload  = {
                "type":         ["ticket"],
                "created_date": {"after": sprint_start, "before": sprint_end},
                "limit":        100,
            }

        if cursor:
            payload["cursor"] = cursor

        response = requests.post(endpoint, headers=headers, json=payload)
        response.raise_for_status()

        data  = response.json()
        works = data.get("works", [])
        all_issues.extend(works)

        cursor = data.get("next_cursor")
        if not cursor:
            break

    return all_issues


# ─────────────────────────────────────────────
# Step 3: Aggregate
# ─────────────────────────────────────────────
def aggregate_issues(issues: list) -> dict:
    summary = {
        "total":       len(issues),
        "open":        0,
        "closed":      0,
        "by_stage":    defaultdict(int),
        "by_assignee": defaultdict(int),
    }

    for issue in issues:
        stage     = issue.get("stage", {}).get("name", "Unknown")
        is_closed = stage.lower() in CLOSED_STAGES

        if is_closed:
            summary["closed"] += 1
        else:
            summary["open"] += 1

        summary["by_stage"][stage] += 1

        owners   = issue.get("owned_by", [])
        assignee = owners[0].get("display_name", "Unassigned") if owners else "Unassigned"
        summary["by_assignee"][assignee] += 1

    return summary


# ─────────────────────────────────────────────
# Step 4: Format Slack message
# ─────────────────────────────────────────────
def format_slack_message(summary: dict, sprint_start: str, sprint_end: str) -> str:
    start_fmt = datetime.fromisoformat(sprint_start).strftime("%d %b")
    end_fmt   = datetime.fromisoformat(sprint_end).strftime("%d %b %Y")

    stage_lines = "\n".join(
        f"  • {stage}: *{count}*"
        for stage, count in sorted(summary["by_stage"].items(), key=lambda x: -x[1])
    ) or "  _No stage data_"

    assignee_lines = "\n".join(
        f"  • {name}: *{count}*"
        for name, count in sorted(summary["by_assignee"].items(), key=lambda x: -x[1])
    ) or "  _No assignee data_"

    open_pct   = round(summary["open"]   / summary["total"] * 100) if summary["total"] else 0
    closed_pct = 100 - open_pct
    bar        = ("🟥" * (open_pct // 10)) + ("🟩" * (closed_pct // 10))

    source_note = (
        f"_Vista: `{DEVREV_VISTA_ID}`_" if DEVREV_VISTA_ID
        else "_Source: last 7 days_"
    )

    return f"""
:bar_chart: *Weekly Sprint Support Summary*
_Sprint window: {start_fmt} – {end_fmt}_   {source_note}

*Overview*
> Total: *{summary['total']}*  |  Open: *{summary['open']}*  |  Closed: *{summary['closed']}*
> {bar}  _{open_pct}% open_

*By Stage*
{stage_lines}

*By Assignee*
{assignee_lines}
""".strip()


# ─────────────────────────────────────────────
# Step 5: Post to Slack
# ─────────────────────────────────────────────
def post_to_slack(message: str):
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type":  "application/json",
    }
    payload = {"channel": SLACK_CHANNEL, "text": message, "mrkdwn": True}

    response = requests.post(SLACK_API_URL, headers=headers, json=payload)
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise Exception(f"Slack API error: {result.get('error')}")
