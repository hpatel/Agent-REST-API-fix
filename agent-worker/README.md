# agent-worker

Agent worker that pops error events from the Redis queue and drives
the full fix pipeline: route → clone → Claude → git → tests → PR → PagerDuty (optional).

## Project structure

```
agent-worker/
├── agent/
│   ├── models.py          # Shared dataclasses (ErrorEvent, AgentResult, etc.)
│   ├── router.py          # Resolves error route → RouteConfig from routes.yaml
│   ├── file_selector.py   # Clones repo, selects relevant source files
│   ├── claude_agent.py    # Calls Anthropic API, parses JSON response
│   ├── git_ops.py         # Applies fix, commits, pushes branch
│   ├── test_runner.py     # Runs test suite in subprocess
│   ├── pr_creator.py      # Opens GitHub PR, assigns reviewers from CODEOWNERS
│   └── pagerduty.py       # Sends PagerDuty Events API v2 alert
├── worker.py              # Main loop — BRPOP, orchestrates pipeline
├── config/
│   └── routes.yaml        # URL pattern → repo + team + PagerDuty routing key
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

## Pipeline flow

```
Redis BRPOP agent:errors
        │
        ▼
   Route resolution (routes.yaml)
        │
        ├── fallback route? → PagerDuty, stop
        │
        ▼
   Clone repo + select files (stack trace → file paths)
        │
        ▼
   Claude agent (claude-sonnet-4-6)
        │
        ├── action: escalate → PagerDuty, stop
        │
        ▼ action: fix
   Apply files → git commit → push branch
        │
        ▼
   Run tests (test_command from routes.yaml)
        │
        ▼
   Open GitHub PR (always, pass or fail)
        │
        ▼
   PagerDuty alert (optional)
        │
        ▼
   Cleanup clone dir
```

## Local dev (WSL / Linux)

```bash
# Prerequisites: Python 3.12+, Redis running locally
docker run -d -p 6379:6379 redis:7-alpine

# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your real keys

# Edit config/routes.yaml with your repos and PagerDuty keys

# Run
python -m dotenv -f .env run python worker.py
```

Scale replicas to increase throughput — each replica processes one event at a time.

## Key behaviours

**Graceful shutdown:** SIGTERM/SIGINT finishes the current job before exiting.

**Clone isolation:** Each job clones into `/tmp/autofix-clones/{error_id}/` and
the directory is always deleted in a `finally` block, even on failure.

**Tests fail → PR still opens:** A failing PR is more useful than no action.
The PR body clearly shows the test status so reviewers know to check more carefully.

**PagerDuty never raises:** Alerting failure is logged but never crashes the
worker. The pipeline result is still returned.

## Configuration reference

### routes.yaml fields

| Field | Required | Description |
|---|---|---|
| `pattern` | yes | Regex matched against `http.route` |
| `repo` | yes | `org/repo` — null for fallback routes |
| `team` | yes | Team name (display only) |
| `pagerduty_routing_key` | yes | Events API v2 integration key |
| `test_command` | yes | Shell command run inside the cloned repo |
| `language` | yes | Used in the Claude prompt and file fuzzy-matching |
| `fallback` | no | If true, skip fix attempt and alert immediately |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `ANTHROPIC_API_KEY` | — | Required |
| `GITHUB_APP_ID` | — | Required |
| `GITHUB_APP_PRIVATE_KEY` | — | Required (full PEM) |
| `GITHUB_INSTALLATION_ID` | — | Required |
| `ROUTES_CONFIG_PATH` | `./config/routes.yaml` | Path to routing config |
| `GITHUB_CLONE_DIR` | `/tmp/autofix-clones` | Base dir for repo clones |
| `TEST_TIMEOUT_SECONDS` | `300` | Max test suite duration |
