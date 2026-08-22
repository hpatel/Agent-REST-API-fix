from dataclasses import dataclass, asdict
from typing import Optional
import json


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

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @property
    def dedup_key(self) -> str:
        """Stable key for deduplication: same route + exception type = same error."""
        return f"{self.service}:{self.route}:{self.exception_type or self.status_code}"
