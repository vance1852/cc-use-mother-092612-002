"""建议留痕服务的 HTTP/JSON 路由，风格与基础层 api 保持一致。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError


def route_advice(service, method: str, path: str, body: dict[str, Any] | None,
                 headers: dict[str, str] | None) -> tuple[int, dict[str, Any]] | None:
    """处理 /advice 前缀的请求；不匹配时返回 None 交回基础路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    if not parsed.path.startswith("/advice"):
        return None
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def receipt_status(receipt) -> tuple[int, dict[str, Any]]:
        return 200 if receipt.replayed else 201, receipt.__dict__

    try:
        if method == "POST" and parsed.path == "/advice/consents":
            return receipt_status(service.grant_consent(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advice/consents/withdraw":
            return receipt_status(service.withdraw_consent(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/advice/consents":
            consent_id = query.get("consent_id", [""])[0]
            if not consent_id:
                raise ValidationError("consent_id 不能为空")
            return 200, service.get_consent(actor_id=actor_id, consent_id=consent_id)
        if method == "POST" and parsed.path == "/advice/rule-versions":
            return receipt_status(service.register_rule_version(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advice/rule-versions/supersede":
            return receipt_status(service.supersede_rule_version(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/advice/rule-versions":
            rule_version_id = query.get("rule_version_id", [""])[0]
            if not rule_version_id:
                raise ValidationError("rule_version_id 不能为空")
            return 200, service.get_rule_version(actor_id=actor_id, rule_version_id=rule_version_id)
        if method == "POST" and parsed.path == "/advice/cases":
            return receipt_status(service.create_case(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/advice/cases":
            case_id = query.get("case_id", [""])[0]
            if not case_id:
                raise ValidationError("case_id 不能为空")
            return 200, service.get_case(actor_id=actor_id, case_id=case_id)
        if method == "POST" and parsed.path == "/advice/versions":
            return receipt_status(service.draft_version(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advice/versions/publish":
            return receipt_status(service.publish_version(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/advice/versions":
            case_id = query.get("case_id", [""])[0]
            version_no = query.get("version_no", [""])[0]
            if not case_id or not version_no:
                raise ValidationError("case_id 与 version_no 不能为空")
            return 200, service.view_version(actor_id=actor_id, case_id=case_id,
                                            version_no=int(version_no))
        if method == "GET" and parsed.path == "/advice/versions/explain":
            case_id = query.get("case_id", [""])[0]
            version_no = query.get("version_no", [""])[0]
            if not case_id or not version_no:
                raise ValidationError("case_id 与 version_no 不能为空")
            return 200, service.explain_version(actor_id=actor_id, case_id=case_id,
                                               version_no=int(version_no))
        if method == "POST" and parsed.path == "/advice/sections":
            return receipt_status(service.add_section(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/advice/sections/sign":
            return receipt_status(service.sign_section(actor_id=actor_id, **body))
        return 404, {"error": "route_not_found", "message": "建议接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
