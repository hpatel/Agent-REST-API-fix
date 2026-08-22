"""
Parses OTLP payloads into ErrorEvent objects.

Supports:
  - gzip-compressed or uncompressed bodies
  - application/x-protobuf (default for otlphttp exporter)
  - application/json
"""

import gzip
import json
import logging
from typing import Generator

from .models import ErrorEvent

logger = logging.getLogger(__name__)

GZIP_MAGIC = b"\x1f\x8b"


def _decompress(body: bytes) -> bytes:
    """Decompress gzip body if needed — Collector compresses by default."""
    if body[:2] == GZIP_MAGIC:
        print("[DEBUG] Detected gzip payload — decompressing", flush=True)
        return gzip.decompress(body)
    return body


def _attrs_from_list(attribute_list) -> dict:
    result = {}
    for a in attribute_list:
        kind = a.value.WhichOneof("value")
        if kind == "string_value":
            result[a.key] = a.value.string_value
        elif kind == "int_value":
            result[a.key] = a.value.int_value
        elif kind == "bool_value":
            result[a.key] = a.value.bool_value
        elif kind == "double_value":
            result[a.key] = a.value.double_value
        else:
            result[a.key] = str(a.value)
    return result


def _parse_protobuf(body: bytes) -> Generator[ErrorEvent, None, None]:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    req = ExportTraceServiceRequest()
    req.ParseFromString(body)

    for resource_spans in req.resource_spans:
        resource_attrs = _attrs_from_list(resource_spans.resource.attributes)
        service = resource_attrs.get("service.name", "unknown")
        environment = resource_attrs.get("deployment.environment", "unknown")

        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                span_attrs = _attrs_from_list(span.attributes)
                status_code = span_attrs.get("http.status_code", 0)

                try:
                    status_code = int(status_code)
                except (ValueError, TypeError):
                    continue

                if status_code < 500:
                    continue

                exc_event = next(
                    (e for e in span.events if e.name == "exception"), None
                )
                exc_attrs = _attrs_from_list(exc_event.attributes) if exc_event else {}

                yield ErrorEvent(
                    error_id=span.trace_id.hex(),
                    span_id=span.span_id.hex(),
                    timestamp=span.end_time_unix_nano // 1_000_000_000,
                    route=span_attrs.get("http.route", span_attrs.get("http.target", "")),
                    method=span_attrs.get("http.method", ""),
                    status_code=status_code,
                    service=service,
                    environment=environment,
                    message=exc_attrs.get("exception.message", ""),
                    stack_trace=exc_attrs.get("exception.stacktrace", ""),
                    exception_type=exc_attrs.get("exception.type", ""),
                )


def _flatten_attrs_json(attr_list: list) -> dict:
    result = {}
    for item in attr_list:
        key = item.get("key", "")
        val = item.get("value", {})
        if "stringValue" in val:
            result[key] = val["stringValue"]
        elif "intValue" in val:
            result[key] = int(val["intValue"])
        elif "boolValue" in val:
            result[key] = val["boolValue"]
        elif "doubleValue" in val:
            result[key] = val["doubleValue"]
    return result


def _parse_json(body: bytes) -> Generator[ErrorEvent, None, None]:
    data = json.loads(body)

    for resource_spans in data.get("resourceSpans", []):
        resource_attrs = _flatten_attrs_json(
            resource_spans.get("resource", {}).get("attributes", [])
        )
        service = resource_attrs.get("service.name", "unknown")
        environment = resource_attrs.get("deployment.environment", "unknown")

        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                span_attrs = _flatten_attrs_json(span.get("attributes", []))
                status_code = span_attrs.get("http.status_code", 0)

                try:
                    status_code = int(status_code)
                except (ValueError, TypeError):
                    continue

                if status_code < 500:
                    continue

                exc_event = next(
                    (e for e in span.get("events", []) if e.get("name") == "exception"),
                    None,
                )
                exc_attrs = (
                    _flatten_attrs_json(exc_event.get("attributes", []))
                    if exc_event else {}
                )

                yield ErrorEvent(
                    error_id=span.get("traceId", ""),
                    span_id=span.get("spanId", ""),
                    timestamp=int(span.get("endTimeUnixNano", "0")) // 1_000_000_000,
                    route=span_attrs.get("http.route", span_attrs.get("http.target", "")),
                    method=span_attrs.get("http.method", ""),
                    status_code=status_code,
                    service=service,
                    environment=environment,
                    message=exc_attrs.get("exception.message", ""),
                    stack_trace=exc_attrs.get("exception.stacktrace", ""),
                    exception_type=exc_attrs.get("exception.type", ""),
                )


def parse_export_request(
    body: bytes, content_type: str = ""
) -> Generator[ErrorEvent, None, None]:
    """
    Decompress if needed, then parse as protobuf or JSON.
    """
    body = _decompress(body)

    is_json = "json" in content_type.lower()

    if is_json:
        print("[DEBUG] Parsing as JSON", flush=True)
        yield from _parse_json(body)
        return

    try:
        print("[DEBUG] Parsing as protobuf", flush=True)
        yield from _parse_protobuf(body)
    except Exception as proto_err:
        print(f"[DEBUG] Protobuf failed ({proto_err}), retrying as JSON", flush=True)
        yield from _parse_json(body)
