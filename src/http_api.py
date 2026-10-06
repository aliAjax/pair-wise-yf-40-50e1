import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .domain import (
    ConflictError,
    DomainError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
    Actor,
)


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def create_handler(service, rules, static_dir):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularPython/1.0"

        def log_message(self, format, *args):
            return

        def _send(self, status, payload):
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status, body):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _actor(self):
            return Actor.from_headers(self.headers)

        def _body(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValidationError("request body must be valid JSON")
            if not isinstance(value, dict):
                raise ValidationError("request body must be a JSON object")
            return value

        def _fail(self, exc):
            if isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, (ConflictError, InvalidTransition)):
                status = 409
            elif isinstance(exc, ValidationError):
                status = 400
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._send(status, {"error": str(exc), "type": type(exc).__name__})

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                if parsed.path == "/health":
                    return self._send(200, service.health())
                if parsed.path == "/":
                    index = os.path.join(static_dir, "index.html")
                    with open(index, "r", encoding="utf-8") as handle:
                        return self._send_html(200, handle.read())
                if parts == ["api", "audit"]:
                    return self._send(200, {"items": service.audit_log()})
                if parts[:2] == ["api", "links"]:
                    if len(parts) == 3:
                        return self._send(200, service.list_links(upstream=parts[2]))
                    return self._send(200, {"items": service.list_links()})
                if parts == ["api", "lab"]:
                    query = parse_qs(parsed.query)
                    return self._send(200, {"items": service.list_lab_results(
                        sample_id=query.get("sample_id", [None])[0],
                        status=query.get("status", [None])[0],
                    )})
                if parts[:2] == ["api", "runs"]:
                    if len(parts) == 4 and parts[3] == "resume":
                        return self._send(200, service.resume_run(parts[2], actor))
                    if len(parts) == 3:
                        return self._send(200, service.get_run_detail(parts[2]))
                    query = parse_qs(parsed.query)
                    return self._send(200, {"items": service.list_runs(
                        status=query.get("status", [None])[0]
                    )})
                if parts == ["api", "notifications"]:
                    query = parse_qs(parsed.query)
                    include_void = query.get("include_void", ["true"])[0] != "false"
                    return self._send(200, {"items": service.list_notifications(
                        status=query.get("status", [None])[0],
                        include_void=include_void,
                    )})
                if len(parts) == 4 and parts[0] == "api" and parts[1] == "consignments":
                    query = parse_qs(parsed.query)
                    if parts[3] == "lab":
                        return self._send(200, {"items": service.list_lab_results(
                            consignment_id=parts[2]
                        )})
                    if parts[3] == "runs":
                        return self._send(200, {"items": service.list_runs(parts[2])})
                    if parts[3] == "notifications":
                        include_void = query.get("include_void", ["true"])[0] != "false"
                        return self._send(200, {"items": service.list_notifications(
                            consignment_id=parts[2], include_void=include_void
                        )})
                    if parts[3] == "trace-preview":
                        return self._send(200, service.preview_trace(
                            parts[2], query.get("effective_at", [None])[0]
                        ))
                    raise NotFoundError("not found")
                if len(parts) == 3 and parts[:2] == ["api", "entities"]:
                    return self._send(200, service.get(parts[2]))
                if len(parts) >= 2 and parts[0] == "api":
                    if parts[1] == "entities":
                        raise NotFoundError("not found")
                    if len(parts) == 3:
                        return self._send(200, service.get(parts[2]))
                    query = parse_qs(parsed.query)
                    status = query.get("status", [None])[0]
                    return self._send(
                        200,
                        {"items": service.list(parts[1], status=status)},
                    )
                raise NotFoundError("not found")
            except Exception as exc:
                self._fail(exc)

        def do_POST(self):
            try:
                parsed = urlparse(self.path)
                parts = [part for part in parsed.path.split("/") if part]
                actor = self._actor()
                if parts == ["api", "links"]:
                    return self._send(201, service.register_link(actor, self._body()))
                if parts == ["api", "links", "backfill"]:
                    return self._send(200, service.backfill_links(actor))
                if len(parts) == 4 and parts[0] == "api" and parts[1] == "consignments" and parts[3] == "lab":
                    return self._send(201, service.submit_lab_result(actor, parts[2], self._body()))
                if len(parts) == 4 and parts[0] == "api" and parts[1] == "consignments" and parts[3] == "recompute":
                    return self._send(200, service.recompute_for_consignment(parts[2], actor=actor))
                if len(parts) == 4 and parts[0] == "api" and parts[1] == "notifications" and parts[3] == "acknowledge":
                    body = self._body()
                    return self._send(200, service.acknowledge_notification(
                        actor, parts[2], body.get("note")
                    ))
                if len(parts) == 3 and parts[:2] == ["api", "entities"]:
                    body = self._body()
                    action = body.pop("action", None)
                    if not action:
                        raise ValidationError("action is required")
                    data = body.pop("data", body)
                    expected = body.pop("expected_version", None)
                    return self._send(
                        200,
                        service.transition(actor, parts[2], action, data, expected),
                    )
                if len(parts) == 4 and parts[0] == "api" and parts[3] == "actions":
                    body = self._body()
                    action = body.pop("action", None)
                    if not action:
                        raise ValidationError("action is required")
                    return self._send(
                        200,
                        service.transition(
                            actor,
                            parts[2],
                            action,
                            body.pop("data", body),
                            body.pop("expected_version", None),
                        ),
                    )
                if len(parts) == 5 and parts[0] == "api" and parts[4] == "actions":
                    return self._send(
                        200,
                        service.transition(actor, parts[2], parts[3], self._body(), None),
                    )
                if len(parts) == 2 and parts[0] == "api":
                    body = self._body()
                    idem = self.headers.get("Idempotency-Key")
                    return self._send(
                        201,
                        service.create(actor, parts[1], body, idem),
                    )
                raise NotFoundError("not found")
            except Exception as exc:
                self._fail(exc)

    return Handler


def create_server(host, port, service, rules, static_dir):
    handler = create_handler(service, rules, static_dir)
    return ThreadingHTTPServer((host, int(port)), handler)
