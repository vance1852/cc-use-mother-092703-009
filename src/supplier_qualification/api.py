"""无第三方依赖的关键供应商资格 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import QualificationError, ValidationFailed
from .service import QualificationService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Any


class JsonApplication:
    def __init__(self, service: QualificationService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    @staticmethod
    def _query(query: Mapping[str, list[str]], key: str, default: str | None = None) -> str | None:
        values = query.get(key)
        if not values or not values[0]:
            return default
        return values[0]

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "supplier-qualification"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized) if path != "/health" else ""

            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))

            if method == "POST" and path == "/certificates":
                return Response(201, self.service.register_certificate(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "certificates":
                return Response(200, self.service.certificate(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "suppliers" and parts[2] == "certificates":
                return Response(200, {"certificates": self.service.list_certificates(parts[1])})
            if method == "POST" and len(parts) == 3 and parts[0] == "certificates" and parts[2] == "renew":
                return Response(200, self.service.renew_certificate(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "certificates" and parts[2] == "expire":
                return Response(200, self.service.expire_certificate(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "certificates" and parts[2] == "revoke":
                return Response(200, self.service.revoke_certificate(actor, parts[1], payload["reason"]))

            if method == "POST" and path == "/approved-products":
                return Response(201, self.service.register_approved_product(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "approved-products":
                return Response(200, self.service.approved_product(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "approved-products" and parts[2] == "withdraw":
                return Response(200, self.service.withdraw_approved_product(actor, parts[1], payload["reason"]))

            if method == "POST" and path == "/plants":
                return Response(201, self.service.register_plant(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "plants":
                return Response(200, self.service.plant(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "suppliers" and parts[2] == "plants":
                return Response(200, {"plants": self.service.list_plants(parts[1])})

            if method == "POST" and path == "/quality-events":
                return Response(201, self.service.open_quality_event(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "quality-events":
                return Response(200, self.service.quality_event(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "quality-events" and parts[2] == "corrective-action":
                return Response(200, self.service.update_corrective_action(actor, parts[1], payload["corrective_action"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "quality-events" and parts[2] == "close":
                return Response(200, self.service.close_quality_event(actor, parts[1], payload.get("corrective_action")))

            if method == "POST" and path == "/suspensions":
                return Response(201, self.service.suspend_supplier(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "suspensions":
                return Response(200, self.service.suspension(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "suspensions" and parts[2] == "lift":
                return Response(200, self.service.lift_suspension(actor, parts[1]))

            if method == "POST" and path == "/orders":
                return Response(201, self.service.create_order(actor, payload))
            if method == "GET" and path == "/orders":
                review = self._query(query, "review_required")
                return Response(200, {"orders": self.service.list_orders(
                    self._query(query, "supplier_id"),
                    self._query(query, "material_code"),
                    None if review is None else review == "1",
                )})
            if method == "GET" and len(parts) == 2 and parts[0] == "orders":
                return Response(200, self.service.order(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "lots":
                return Response(201, self.service.register_order_lot(actor, parts[1], payload))
            if method == "POST" and len(parts) == 5 and parts[0] == "orders" and parts[2] == "lots" and parts[4] == "inspection":
                return Response(200, self.service.record_lot_inspection(actor, parts[1], parts[3], payload["inspection_result"], payload.get("inspection_report")))
            if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "ship":
                return Response(200, self.service.ship_order(actor, parts[1], payload["batch_no"], payload.get("substitute_approval_id")))
            if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "accept":
                return Response(200, self.service.accept_order(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "cancel":
                return Response(200, self.service.cancel_order(actor, parts[1], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "review":
                return Response(200, self.service.resolve_order_review(actor, parts[1], payload["note"]))
            if method == "GET" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "compliance":
                return Response(200, self.service.order_compliance(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "impacts":
                return Response(200, self.service.order_impact_history(actor, parts[1]))

            if method == "POST" and path == "/emergencies":
                return Response(201, self.service.create_emergency_request(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "emergencies":
                return Response(200, self.service.emergency_approval(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "emergencies" and parts[2] == "approve":
                return Response(200, self.service.approve_emergency(actor, parts[1], payload["side"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "emergencies" and parts[2] == "void":
                return Response(200, self.service.void_emergency(actor, parts[1], payload["reason"]))

            if method == "GET" and path == "/alternatives":
                material_code = self._query(query, "material_code")
                if not material_code:
                    raise ValidationFailed("缺少 material_code")
                return Response(200, self.service.find_alternatives(
                    actor, material_code,
                    self._query(query, "plant_id"), self._query(query, "on_date"),
                ))
            if method == "GET" and path == "/eligibility":
                supplier_id = self._query(query, "supplier_id")
                material_code = self._query(query, "material_code")
                if not supplier_id or not material_code:
                    raise ValidationFailed("缺少 supplier_id 或 material_code")
                self.service._require(actor, "report.read")
                return Response(200, self.service.evaluate_supply(
                    supplier_id, material_code,
                    self._query(query, "plant_id"), self._query(query, "on_date"),
                ))
            if method == "GET" and len(parts) == 3 and parts[0] == "impacts":
                return Response(200, self.service.change_impacts(actor, parts[1], parts[2]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except QualificationError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SupplierQualification/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动关键供应商资格服务")
    parser.add_argument("--database", type=Path, default=Path("supplier_qualification.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(QualificationService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
