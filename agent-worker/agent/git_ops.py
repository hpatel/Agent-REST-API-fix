"""
Applies the agent's file changes to the cloned repo, creates a branch,
commits, and pushes to GitHub.

Branch naming: autofix/{error_id[:8]}-{unix_timestamp}
"""

import logging
import os
import time
from pathlib import Path

from git import Repo, GitCommandError

from .models import AgentResult, ErrorContext

logger = logging.getLogger(__name__)


def apply_and_push(
    context: ErrorContext,
    agent_result: AgentResult,
    repo_local_path: str,
) -> str:
    """
    Writes the agent's file changes, creates a branch, commits, and pushes.

    Returns the branch name.
    Raises GitCommandError on push failure — caller should alert and skip PR.
    """
    event = context.event
    repo = Repo(repo_local_path)

    branch_name = f"autofix/{event.error_id[:8]}-{int(time.time())}"
    repo.git.checkout("-b", branch_name)

    # Write each changed file
    root = Path(repo_local_path)
    for agent_file in agent_result.files:
        dest = root / agent_file.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(agent_file.content, encoding="utf-8")
        logger.debug(f"Wrote {agent_file.path}")

    # Stage and commit
    repo.git.add("--all")

    short_diagnosis = agent_result.diagnosis[:72]  # subject line limit
    commit_message = (
        f"[autofix] {short_diagnosis}\n\n"
        f"Automated fix generated for error {event.error_id}.\n"
        f"Route: {event.method} {event.route}\n"
        f"Exception: {event.exception_type}: {event.message[:120]}\n\n"
        f"Diagnosis: {agent_result.diagnosis}"
    )
    repo.index.commit(commit_message)

    # Push — the clone URL already contains the GitHub App token
    repo.git.push("origin", branch_name)

    logger.info(
        "Pushed fix branch",
        extra={
            "error_id": event.error_id,
            "branch": branch_name,
            "repo": context.route_config.repo,
            "files": [f.path for f in agent_result.files],
        },
    )

    return branch_name
