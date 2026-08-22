"""
Tests for app/otlp_parser.py

Covers:
  - gzip decompression
  - JSON and protobuf format detection
  - 5xx filtering
  - exception event extraction
  - attribute type handling (int vs string status codes)
"""

import gzip
import json
import pytest

from app.otlp_parser import parse_export_request
from conftest import make_otlp_json


class TestGzipDecompression:
    def test_compressed_payload_is_decompressed(self):
        body, ct = make_otlp_json(500, compressed=True)
        events = list(parse_export_request(body, ct))
        assert len(events) == 1

    def test_uncompressed_payload_still_works(self):
        body, ct = make_otlp_json(500, compressed=False)
        events = list(parse_export_request(body, ct))
        assert len(events) == 1


class TestStatusCodeFiltering:
    def test_500_is_included(self):
        body, ct = make_otlp_json(500)
        events = list(parse_export_request(body, ct))
        assert len(events) == 1

    def test_503_is_included(self):
        body, ct = make_otlp_json(503)
        events = list(parse_export_request(body, ct))
        assert len(events) == 1

    def test_200_is_excluded(self):
        body, ct = make_otlp_json(200)
        events = list(parse_export_request(body, ct))
        assert len(events) == 0

    def test_404_is_excluded(self):
        body, ct = make_otlp_json(404)
        events = list(parse_export_request(body, ct))
        assert len(events) == 0

    def test_499_is_excluded(self):
        body, ct = make_otlp_json(499)
        events = list(parse_export_request(body, ct))
        assert len(events) == 0


class TestFieldExtraction:
    def test_route_extracted(self):
        body, ct = make_otlp_json(500, route="/v1/auth/login")
        events = list(parse_export_request(body, ct))
        assert events[0].route == "/v1/auth/login"

    def test_method_extracted(self):
        body, ct = make_otlp_json(500, method="DELETE")
        events = list(parse_export_request(body, ct))
        assert events[0].method == "DELETE"

    def test_service_name_extracted(self):
        body, ct = make_otlp_json(500, service="auth-service")
        events = list(parse_export_request(body, ct))
        assert events[0].service == "auth-service"

    def test_environment_extracted(self):
        body, ct = make_otlp_json(500, environment="production")
        events = list(parse_export_request(body, ct))
        assert events[0].environment == "production"

    def test_exception_type_extracted(self):
        body, ct = make_otlp_json(500, exception_type="ValueError")
        events = list(parse_export_request(body, ct))
        assert events[0].exception_type == "ValueError"

    def test_exception_message_extracted(self):
        body, ct = make_otlp_json(500, exception_message="bad input")
        events = list(parse_export_request(body, ct))
        assert events[0].message == "bad input"

    def test_stack_trace_extracted(self):
        trace = 'File "app/handler.py", line 99, in process'
        body, ct = make_otlp_json(500, stack_trace=trace)
        events = list(parse_export_request(body, ct))
        assert events[0].stack_trace == trace

    def test_status_code_as_int(self):
        body, ct = make_otlp_json(500)
        events = list(parse_export_request(body, ct))
        assert events[0].status_code == 500
        assert isinstance(events[0].status_code, int)


class TestMissingExceptionEvent:
    def test_span_without_exception_event_still_yields(self):
        """A 500 with no exception event should still be enqueued — agent will escalate."""
        body, ct = make_otlp_json(500, with_exception=False)
        events = list(parse_export_request(body, ct))
        assert len(events) == 1
        assert events[0].stack_trace == ""
        assert events[0].exception_type == ""

    def test_missing_fields_default_to_empty_string(self):
        body, ct = make_otlp_json(500, with_exception=False)
        events = list(parse_export_request(body, ct))
        assert events[0].message == ""
        assert events[0].exception_type == ""


class TestMultipleSpans:
    def test_multiple_5xx_spans_all_yielded(self):
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "api"}},
                    {"key": "deployment.environment", "value": {"stringValue": "test"}},
                ]},
                "scopeSpans": [{"spans": [
                    {
                        "traceId": "aaa", "spanId": "111",
                        "endTimeUnixNano": "1000000000000",
                        "attributes": [
                            {"key": "http.route",       "value": {"stringValue": "/a"}},
                            {"key": "http.method",      "value": {"stringValue": "GET"}},
                            {"key": "http.status_code", "value": {"intValue": 500}},
                        ],
                        "events": [],
                    },
                    {
                        "traceId": "bbb", "spanId": "222",
                        "endTimeUnixNano": "2000000000000",
                        "attributes": [
                            {"key": "http.route",       "value": {"stringValue": "/b"}},
                            {"key": "http.method",      "value": {"stringValue": "POST"}},
                            {"key": "http.status_code", "value": {"intValue": 503}},
                        ],
                        "events": [],
                    },
                    {
                        "traceId": "ccc", "spanId": "333",
                        "endTimeUnixNano": "3000000000000",
                        "attributes": [
                            {"key": "http.route",       "value": {"stringValue": "/c"}},
                            {"key": "http.method",      "value": {"stringValue": "GET"}},
                            {"key": "http.status_code", "value": {"intValue": 200}},
                        ],
                        "events": [],
                    },
                ]}]
            }]
        }
        body = gzip.compress(json.dumps(payload).encode())
        events = list(parse_export_request(body, "application/json"))
        assert len(events) == 2
        routes = {e.route for e in events}
        assert routes == {"/a", "/b"}


class TestInvalidPayload:
    def test_empty_body_raises(self):
        with pytest.raises(Exception):
            list(parse_export_request(b"", "application/json"))

    def test_garbage_body_raises(self):
        with pytest.raises(Exception):
            list(parse_export_request(b"not valid protobuf or json", "application/x-protobuf"))
