"""
Loads routes.yaml and resolves an ErrorEvent's route to a RouteConfig.

routes.yaml example:
  routes:
    - pattern: "^/v2/payments"
      repo: "acme-org/payments-service"
      team: "payments-team"
      pagerduty_routing_key: "abc123..."
      test_command: "pytest tests/"
      language: "python"
    - pattern: ".*"
      repo: null
      team: "platform-team"
      pagerduty_routing_key: "xyz789..."
      test_command: ""
      language: ""
      fallback: true
"""

import re
import logging
from functools import lru_cache
from pathlib import Path

import yaml

from .models import ErrorEvent, RouteConfig

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_routes(config_path: str) -> list[RouteConfig]:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Routes config not found: {config_path}")

    with path.open() as f:
        data = yaml.safe_load(f)

    routes = []
    for entry in data.get("routes", []):
        routes.append(RouteConfig(
            pattern=entry["pattern"],
            repo=entry.get("repo"),
            team=entry["team"],
            #pagerduty_routing_key=entry["pagerduty_routing_key"],
            test_command=entry.get("test_command", ""),
            language=entry.get("language", ""),
            fallback=entry.get("fallback", False),
        ))

    logger.info(f"Loaded {len(routes)} route(s) from {config_path}")
    return routes


def resolve_route(event: ErrorEvent, config_path: str) -> RouteConfig:
    """
    Match event.route against patterns in order. Returns the first match.
    Always returns something — the last route should be a catch-all fallback.
    Raises if no route matches at all (misconfigured routes.yaml).
    """
    routes = _load_routes(config_path)

    for route in routes:
        if re.match(route.pattern, event.route):
            logger.info(
                "Routed error event",
                extra={
                    "error_id": event.error_id,
                    "route": event.route,
                    "matched_pattern": route.pattern,
                    "repo": route.repo,
                    "team": route.team,
                    "fallback": route.fallback,
                },
            )
            return route

    raise ValueError(
        f"No route matched '{event.route}' and no fallback defined in routes.yaml"
    )
