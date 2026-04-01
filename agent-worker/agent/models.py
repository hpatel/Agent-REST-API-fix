from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ErrorEvent:
    error_id: str
    timestamp: int
    route: str
    method: str
    status_code: int
    service: str
    environment: str
    message: str = ""
    stack_trace: str = ""
    exception_type: str = ""
    span_id: str = ""


@dataclass
class RouteConfig:
    pattern: str
    repo: Optional[str]          # e.g. "acme-org/payments-service"
    team: str
    #pagerduty_routing_key: str
    test_command: str
    language: str
    fallback: bool = False


@dataclass
class ErrorContext:
    """ErrorEvent + resolved routing config — passed through the whole pipeline."""
    event: ErrorEvent
    route_config: RouteConfig


@dataclass
class AgentFile:
    path: str
    content: str


@dataclass
class AgentResult:
    action: str                  # "fix" | "escalate"
    diagnosis: str
    reason: str = ""
    files: list[AgentFile] = field(default_factory=list)
    test_notes: str = ""


@dataclass
class PipelineResult:
    context: ErrorContext
    agent_result: AgentResult
    branch_name: str = ""
    pr_url: str = ""
    pr_number: int = 0
    test_passed: Optional[bool] = None
    test_output: str = ""
