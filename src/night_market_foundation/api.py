"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .advice_service import AdviceService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          advice: AdviceService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if advice is not None:
            dispatched = _advice_routes(advice, method, parsed.path, query, body, actor_id)
            if dispatched is not None:
                return dispatched
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _advice_routes(advice: AdviceService, method: str, path: str, query: dict[str, list[str]],
                   body: dict[str, Any], actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """建议留痕与复核模块的路由；未命中返回 None。"""

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    if method == "POST" and path == "/rule-versions":
        receipt = advice.register_rule_version(actor_id=actor_id, **body)
        return (200 if receipt["replayed"] else 201), receipt
    if method == "POST" and path == "/rule-versions/retire":
        receipt = advice.retire_rule_version(actor_id=actor_id, **body)
        return 200, receipt
    if method == "GET" and path == "/rule-versions":
        rule_set_id = q("rule_set_id", "")
        version = q("version", "")
        if not rule_set_id or not version:
            raise ValidationError("rule_set_id 与 version 不能为空")
        return 200, advice.get_rule_version(actor_id, rule_set_id, version)
    if method == "POST" and path == "/consents":
        receipt = advice.grant_consent(actor_id=actor_id, **body)
        return (200 if receipt["replayed"] else 201), receipt
    if method == "POST" and path == "/consents/withdraw":
        return 200, advice.withdraw_consent(actor_id=actor_id, **body)
    if method == "POST" and path == "/consultations":
        receipt = advice.create_consultation(actor_id=actor_id, **body)
        return (200 if receipt["replayed"] else 201), receipt
    if method == "GET" and path == "/consultations":
        consultation_id = q("consultation_id", "")
        if not consultation_id:
            raise ValidationError("consultation_id 不能为空")
        return 200, advice.get_consultation(actor_id=actor_id, consultation_id=consultation_id,
                                           purpose=q("purpose", "review") or "review")
    if method == "POST" and path == "/advice-versions":
        receipt = advice.add_intake_version(actor_id=actor_id, **body)
        return (200 if receipt["replayed"] else 201), receipt
    if method == "POST" and path == "/advice-versions/publish":
        receipt = advice.publish_version(actor_id=actor_id, **body)
        return (200 if receipt["replayed"] else 201), receipt
    if method == "GET" and path == "/advice-versions":
        version_id = q("version_id", "")
        if not version_id:
            raise ValidationError("version_id 不能为空")
        return 200, advice.get_version(actor_id=actor_id, version_id=version_id,
                                      purpose=q("purpose", "review") or "review")
    if method == "POST" and path == "/advice-sections":
        return 201, advice.add_section(actor_id=actor_id, **body)
    if method == "POST" and path == "/advice-sections/sign":
        return 201, advice.sign_section(actor_id=actor_id, **body)
    if method == "GET" and path == "/advice-explain":
        section_id = q("section_id", "")
        if not section_id:
            raise ValidationError("section_id 不能为空")
        return 200, advice.explain_advice(actor_id=actor_id, section_id=section_id,
                                         purpose=q("purpose", "explain") or "explain")
    if method == "GET" and path == "/access-records":
        return 200, advice.list_access_records(actor_id=actor_id,
                                              consultation_id=q("consultation_id"))
    return None


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    advice: AdviceService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                advice=self.advice)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.advice = AdviceService(database, Handler.service.clock)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
