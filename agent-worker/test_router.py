"""
Tests for agent/router.py

Covers:
  - correct repo and team resolved for known routes
  - first-match-wins ordering
  - fallback route for unknown URLs
  - missing config file raises clearly
"""

import pytest
from agent.router import resolve_route, _load_routes
from agent.models import ErrorEvent


def make_event(route: str) -> ErrorEvent:
    return ErrorEvent(
        error_id="test",
        span_id="",
        timestamp=0,
        route=route,
        method="GET",
        status_code=500,
        service="test-api",
        environment="test",
    )


class TestRouteResolution:
    def test_payments_route_resolves_correctly(self, routes_yaml):
        _load_routes.cache_clear()
        event = make_event("/v2/payments/charge")
        config = resolve_route(event, routes_yaml)
        assert config.repo == "acme-org/payments-service"
        assert config.team == "payments-team"
        assert config.language == "python"
        assert config.fallback is False

    def test_auth_route_resolves_correctly(self, routes_yaml):
        _load_routes.cache_clear()
        event = make_event("/v1/auth/login")
        config = resolve_route(event, routes_yaml)
        assert config.repo == "acme-org/auth-service"
        assert config.team == "identity-team"

    def test_unknown_route_hits_fallback(self, routes_yaml):
        _load_routes.cache_clear()
        event = make_event("/unknown/endpoint")
        config = resolve_route(event, routes_yaml)
        assert config.fallback is True
        assert config.repo is None
        assert config.team == "platform-team"

    def test_first_match_wins(self, routes_yaml):
        """A route matching multiple patterns should resolve to the first one."""
        _load_routes.cache_clear()
        event = make_event("/v2/payments/auth")  # matches payments, not auth
        config = resolve_route(event, routes_yaml)
        assert config.repo == "acme-org/payments-service"

    def test_test_command_included(self, routes_yaml):
        _load_routes.cache_clear()
        event = make_event("/v2/payments/charge")
        config = resolve_route(event, routes_yaml)
        assert config.test_command == "pytest tests/"

"""     def test_pagerduty_key_included(self, routes_yaml):
        _load_routes.cache_clear()
        event = make_event("/v2/payments/charge")
        config = resolve_route(event, routes_yaml)
        assert config.pagerduty_routing_key == "key-payments" """


class TestMissingConfig:
    def test_missing_file_raises_file_not_found(self):
        _load_routes.cache_clear()
        event = make_event("/v2/payments")
        with pytest.raises(FileNotFoundError):
            resolve_route(event, "/nonexistent/routes.yaml")
