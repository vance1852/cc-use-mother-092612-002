"""体质辨识建议的留痕、复核与授权留痕服务。

本模块建立在基础服务的稳定边界之上：复用同一 SQLite 连接、角色权限、
request_id 幂等写入以及哈希串联审计，另建建议业务自己的表。

核心约束：

* 只有在参与者授权（含字段范围与用途）生效后，才允许落盘最少必要的问询摘要；
* 建议版本绑定专家出具建议时有效的规则版本快照，事后规则更新不会改写历史；
* 补充信息只能生成新的复核版本，历史版本继续可查但不再作为当前结论；
* 多人会诊时每位专家仅能签署自己创建的内容，发布前重算全部签名；
* 发布动作必须重新确认授权范围、规则快照与签名仍然有效；
* 撤回授权后内容停止使用（对所有角色红acted），但此前访问等审计事实保留；
* 文化体验与疾病诊断由服务恒定区分，不依赖专家自行声明。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService

#: 建议的业务性质：中医药文化体验，不是疾病诊断。
ADVICE_NATURE = "cultural_experience"
DIAGNOSTIC_BOUNDARY = (
    "本结果为中医药文化体验活动中的体质辨识参考，不属于医学诊断，"
    "不替代专业医疗机构的诊断与治疗；如有持续不适或明显症状，请及时就医。"
)

#: 允许专家出具的内容类别：生活方式建议、就医风险提示。
SECTION_KINDS = frozenset({"lifestyle", "medical_risk"})

#: 复核版本允许的变更原因代码。
CHANGE_REASON_CODES = frozenset({"supplementary_info", "correction", "expert_rereview"})

ADVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS advice_consents (
    consent_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    participant_ref TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('granted','withdrawn')),
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdrawn_by TEXT,
    withdrawal_reason TEXT
);
CREATE TABLE IF NOT EXISTS advice_rule_versions (
    rule_version_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    version_label TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, version_label)
);
CREATE TABLE IF NOT EXISTS advice_cases (
    case_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    participant_ref TEXT NOT NULL,
    consent_id TEXT NOT NULL REFERENCES advice_consents(consent_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS advice_versions (
    case_id TEXT NOT NULL REFERENCES advice_cases(case_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft','published','superseded','invalidated')),
    inquiry_json TEXT NOT NULL,
    inquiry_hash TEXT NOT NULL,
    rule_version_id TEXT NOT NULL,
    rule_snapshot_hash TEXT NOT NULL,
    change_reason_code TEXT,
    change_reason TEXT,
    status_reason_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT,
    PRIMARY KEY(case_id, version_no)
);
CREATE TABLE IF NOT EXISTS advice_sections (
    section_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('lifestyle','medical_risk')),
    content TEXT NOT NULL,
    created_by TEXT NOT NULL,
    signed_at TEXT,
    signature_hash TEXT,
    FOREIGN KEY(case_id, version_no) REFERENCES advice_versions(case_id, version_no),
    UNIQUE(case_id, version_no, position)
);
"""


class AdviceService(DomainService):
    """在基础领域服务之上提供建议授权、版本、签署与复核能力。"""

    def __init__(self, database, clock=None) -> None:
        super().__init__(database, clock)
        database.connection.executescript(ADVICE_SCHEMA)

    # ------------------------------------------------------------------ 内部工具

    def _timestamp(self, value: str | None, field: str) -> str:
        if value is None:
            return self._now()
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _same_org(self, actor, site_row) -> None:
        if actor.organization_id != site_row["organization_id"]:
            raise PermissionDenied("不能访问其他组织的建议档案")

    def _consent_row(self, connection, consent_id: str):
        row = connection.execute(
            "SELECT * FROM advice_consents WHERE consent_id=?", (consent_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("授权记录不存在")
        return row

    def _case_row(self, connection, case_id: str):
        row = connection.execute("SELECT * FROM advice_cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("建议档案不存在")
        return row

    def _version_row(self, connection, case_id: str, version_no: int):
        row = connection.execute(
            "SELECT * FROM advice_versions WHERE case_id=? AND version_no=?",
            (case_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFoundError("建议版本不存在")
        return row

    def _rule_row(self, connection, rule_version_id: str):
        row = connection.execute(
            "SELECT * FROM advice_rule_versions WHERE rule_version_id=?", (rule_version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return row

    @staticmethod
    def _rule_effective(rule_row, at: str) -> bool:
        if rule_row["effective_from"] > at:
            return False
        return rule_row["effective_to"] is None or rule_row["effective_to"] > at

    @staticmethod
    def _scope(consent_row) -> dict[str, Any]:
        return json.loads(consent_row["scope_json"])

    def _validate_summary(self, summary: Any, scope: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(summary, dict) or not summary:
            raise ValidationError("inquiry_summary 必须是非空对象")
        if len(summary) > 20:
            raise ValidationError("问询摘要最多包含 20 个字段")
        allowed = set(scope.get("fields", []))
        for key, value in summary.items():
            if not isinstance(key, str) or not key.strip() or len(key) > 40:
                raise ValidationError("问询字段名不能为空且不能超过 40 个字符")
            if key not in allowed:
                raise PermissionDenied(f"字段 {key} 不在参与者授权范围内，禁止记录")
            if isinstance(value, str):
                if not value.strip() or len(value) > 500:
                    raise ValidationError("问询字段值不能为空且不能超过 500 个字符")
            elif not isinstance(value, (bool, int, float)):
                raise ValidationError("问询字段值只能是文本或数值")
        return summary

    def _signature_material(self, *, section_row, rule_snapshot_hash: str) -> dict[str, Any]:
        return {
            "section_id": section_row["section_id"],
            "case_id": section_row["case_id"],
            "version_no": section_row["version_no"],
            "kind": section_row["kind"],
            "content": section_row["content"],
            "rule_snapshot_hash": rule_snapshot_hash,
            "signed_by": section_row["created_by"],
            "signed_at": section_row["signed_at"],
        }

    # ------------------------------------------------------------------ 授权

    def grant_consent(self, *, request_id: str, actor_id: str, consent_id: str, site_id: str,
                      participant_ref: str, scope: dict[str, Any]) -> Any:
        payload = {"actor_id": actor_id, "consent_id": consent_id, "site_id": site_id,
                   "participant_ref": participant_ref, "scope": scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            site = self._site(connection, site_id)
            self._same_org(actor, site)
            consent_id = self._identifier(consent_id, "consent_id")
            participant_ref = self._identifier(participant_ref, "participant_ref")
            if not isinstance(scope, dict):
                raise ValidationError("scope 必须是对象")
            fields = scope.get("fields")
            purposes = scope.get("purposes")
            if not isinstance(fields, list) or not fields or not all(
                isinstance(f, str) and f.strip() and len(f) <= 40 for f in fields
            ):
                raise ValidationError("scope.fields 必须是非空字符串数组")
            if not isinstance(purposes, list) or not purposes or not all(
                isinstance(p, str) and p.strip() for p in purposes
            ):
                raise ValidationError("scope.purposes 必须是非空字符串数组")
            scope = {"fields": list(fields), "purposes": list(purposes)}
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO advice_consents(consent_id,site_id,participant_ref,scope_json,scope_hash,"
                        "status,granted_by,granted_at) VALUES(?,?,?,?,?,'granted',?,?)",
                        (consent_id, site_id, participant_ref, canonical_json(scope), digest(scope),
                         actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("授权编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="consent.granted",
                             resource_type="consent", resource_id=consent_id,
                             detail={"site_id": site_id, "participant_ref": participant_ref,
                                     "scope_hash": digest(scope), "field_count": len(fields)},
                             occurred_at=now)
                return "consent", consent_id, {"consent_id": consent_id, "status": "granted"}

            return self._idempotent(connection, request_id=request_id, action="grant_consent",
                                    payload=payload, create=create)

    def withdraw_consent(self, *, request_id: str, actor_id: str, consent_id: str,
                         reason: str) -> Any:
        reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "consent_id": consent_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            consent = self._consent_row(connection, consent_id)
            site = self._site(connection, consent["site_id"])
            self._same_org(actor, site)
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                if consent["status"] == "withdrawn":
                    raise ConflictError("授权已经处于撤回状态")
                connection.execute(
                    "UPDATE advice_consents SET status='withdrawn',withdrawn_at=?,withdrawn_by=?,"
                    "withdrawal_reason=? WHERE consent_id=?",
                    (now, actor_id, reason, consent_id),
                )
                append_event(connection, actor_id=actor_id, action="consent.withdrawn",
                             resource_type="consent", resource_id=consent_id,
                             detail={"site_id": site["site_id"], "scope_hash": consent["scope_hash"]},
                             occurred_at=now)
                # 当前仍生效的建议版本立即作废；草稿不单独作废，但发布时会被拒绝。
                current = connection.execute(
                    "SELECT * FROM advice_versions WHERE case_id IN "
                    "(SELECT case_id FROM advice_cases WHERE consent_id=?) AND status='published'",
                    (consent_id,),
                ).fetchall()
                invalidated = []
                for version in current:
                    status_reason = {"code": "consent_withdrawn", "at": now}
                    connection.execute(
                        "UPDATE advice_versions SET status='invalidated',status_reason_json=? "
                        "WHERE case_id=? AND version_no=?",
                        (canonical_json(status_reason), version["case_id"], version["version_no"]),
                    )
                    append_event(connection, actor_id=actor_id, action="advice.version_invalidated",
                                 resource_type="advice_version",
                                 resource_id=f"{version['case_id']}#v{version['version_no']}",
                                 detail={"case_id": version["case_id"],
                                         "version_no": version["version_no"],
                                         "reason_code": "consent_withdrawn",
                                         "inquiry_hash": version["inquiry_hash"]},
                                 occurred_at=now)
                    invalidated.append(version["version_no"])
                return "consent", consent_id, {"consent_id": consent_id, "status": "withdrawn",
                                               "invalidated_versions": invalidated}

            return self._idempotent(connection, request_id=request_id, action="withdraw_consent",
                                    payload=payload, create=create)

    def get_consent(self, *, actor_id: str, consent_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            consent = self._consent_row(connection, consent_id)
            site = self._site(connection, consent["site_id"])
            self._same_org(actor, site)
            if consent["status"] == "withdrawn":
                # 撤回后不再暴露字段名，只保留授权范围的摘要作为事实凭证
                scope: Any = {"redacted": True, "reason": "consent_withdrawn"}
            else:
                scope = self._scope(consent)
            return {"consent_id": consent["consent_id"], "site_id": consent["site_id"],
                    "participant_ref": consent["participant_ref"],
                    "scope": scope, "scope_hash": consent["scope_hash"],
                    "status": consent["status"],
                    "granted_at": consent["granted_at"], "withdrawn_at": consent["withdrawn_at"]}

    # ------------------------------------------------------------------ 规则版本

    def register_rule_version(self, *, request_id: str, actor_id: str, rule_version_id: str,
                              site_id: str, version_label: str, snapshot: dict[str, Any],
                              effective_from: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "rule_version_id": rule_version_id, "site_id": site_id,
                   "version_label": version_label, "snapshot": snapshot,
                   "effective_from": effective_from}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            site = self._site(connection, site_id)
            self._same_org(actor, site)
            rule_version_id = self._identifier(rule_version_id, "rule_version_id")
            version_label = self._text(version_label, "version_label", 80)
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("entries"), dict) \
                    or not snapshot["entries"]:
                raise ValidationError("snapshot 必须包含非空 entries 对象")
            start = self._timestamp(effective_from, "effective_from")
            now = self._now()
            snapshot_hash = digest(snapshot)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO advice_rule_versions(rule_version_id,site_id,version_label,"
                        "snapshot_json,snapshot_hash,effective_from,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (rule_version_id, site_id, version_label, canonical_json(snapshot),
                         snapshot_hash, start, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("规则版本编号或同站点版本标签已经存在") from exc
                append_event(connection, actor_id=actor_id, action="rule_version.registered",
                             resource_type="rule_version", resource_id=rule_version_id,
                             detail={"site_id": site_id, "version_label": version_label,
                                     "snapshot_hash": snapshot_hash, "effective_from": start},
                             occurred_at=now)
                return "rule_version", rule_version_id, {"rule_version_id": rule_version_id,
                                                         "snapshot_hash": snapshot_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_rule_version", payload=payload, create=create)

    def supersede_rule_version(self, *, request_id: str, actor_id: str, rule_version_id: str,
                               effective_to: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "rule_version_id": rule_version_id,
                   "effective_to": effective_to}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            rule = self._rule_row(connection, rule_version_id)
            site = self._site(connection, rule["site_id"])
            self._same_org(actor, site)
            end = self._timestamp(effective_to, "effective_to")

            def create() -> tuple[str, str, dict[str, Any]]:
                if rule["effective_to"] is not None:
                    raise ConflictError("规则版本已经被新版本取代")
                if end <= rule["effective_from"]:
                    raise ValidationError("失效时间必须晚于生效时间")
                connection.execute(
                    "UPDATE advice_rule_versions SET effective_to=? WHERE rule_version_id=?",
                    (end, rule_version_id),
                )
                append_event(connection, actor_id=actor_id, action="rule_version.superseded",
                             resource_type="rule_version", resource_id=rule_version_id,
                             detail={"site_id": site["site_id"], "snapshot_hash": rule["snapshot_hash"],
                                     "effective_to": end}, occurred_at=self._now())
                return "rule_version", rule_version_id, {"rule_version_id": rule_version_id,
                                                          "effective_to": end}

            return self._idempotent(connection, request_id=request_id,
                                    action="supersede_rule_version", payload=payload, create=create)

    def get_rule_version(self, *, actor_id: str, rule_version_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            rule = self._rule_row(connection, rule_version_id)
            site = self._site(connection, rule["site_id"])
            self._same_org(actor, site)
            return {"rule_version_id": rule["rule_version_id"], "site_id": rule["site_id"],
                    "version_label": rule["version_label"],
                    "snapshot": json.loads(rule["snapshot_json"]),
                    "snapshot_hash": rule["snapshot_hash"],
                    "effective_from": rule["effective_from"], "effective_to": rule["effective_to"],
                    "effective_now": self._rule_effective(rule, self._now())}

    # ------------------------------------------------------------------ 建档与版本

    def create_case(self, *, request_id: str, actor_id: str, case_id: str, site_id: str,
                    participant_ref: str, consent_id: str) -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "site_id": site_id,
                   "participant_ref": participant_ref, "consent_id": consent_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            site = self._site(connection, site_id)
            self._same_org(actor, site)
            case_id = self._identifier(case_id, "case_id")
            participant_ref = self._identifier(participant_ref, "participant_ref")
            try:
                consent = self._consent_row(connection, consent_id)
            except NotFoundError as exc:
                # 不泄漏授权编号是否存在：没有有效授权即拒绝建档
                raise PermissionDenied("未取得参与者授权，不能建立建议档案") from exc
            if consent["site_id"] != site_id or consent["participant_ref"] != participant_ref:
                raise ValidationError("授权与站点或参与者不匹配")
            if consent["status"] != "granted":
                raise PermissionDenied("授权已撤回，不能建立建议档案")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO advice_cases(case_id,site_id,participant_ref,consent_id,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (case_id, site_id, participant_ref, consent_id, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("建议档案编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="advice_case.created",
                             resource_type="advice_case", resource_id=case_id,
                             detail={"site_id": site_id, "participant_ref": participant_ref,
                                     "consent_id": consent_id, "scope_hash": consent["scope_hash"]},
                             occurred_at=now)
                return "advice_case", case_id, {"case_id": case_id}

            return self._idempotent(connection, request_id=request_id, action="create_case",
                                    payload=payload, create=create)

    def draft_version(self, *, request_id: str, actor_id: str, case_id: str,
                      inquiry_summary: dict[str, Any], rule_version_id: str,
                      change_reason_code: str | None = None,
                      change_reason: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "inquiry_summary": inquiry_summary,
                   "rule_version_id": rule_version_id, "change_reason_code": change_reason_code,
                   "change_reason": change_reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            case = self._case_row(connection, case_id)
            site = self._site(connection, case["site_id"])
            self._same_org(actor, site)
            consent = self._consent_row(connection, case["consent_id"])
            if consent["status"] != "granted":
                raise PermissionDenied("授权已撤回，不能继续记录或使用问询信息")
            summary = self._validate_summary(inquiry_summary, self._scope(consent))
            rule = self._rule_row(connection, rule_version_id)
            if rule["site_id"] != case["site_id"]:
                raise ValidationError("规则版本与建议档案不属于同一场所")
            now = self._now()
            if not self._rule_effective(rule, now):
                raise ConflictError("规则版本在当前时间不在生效窗口内，不能作为出具依据")
            latest = connection.execute(
                "SELECT MAX(version_no) AS version_no FROM advice_versions WHERE case_id=?",
                (case_id,),
            ).fetchone()
            next_no = (latest["version_no"] or 0) + 1
            if next_no >= 2:
                latest_row = self._version_row(connection, case_id, next_no - 1)
                if latest_row["status"] == "draft":
                    raise ConflictError("已有待发布的复核版本，请先发布或放弃该版本")
                if change_reason_code not in CHANGE_REASON_CODES:
                    raise ValidationError("复核版本必须提供 change_reason_code")
                change_reason = self._text(change_reason or "", "change_reason")
            elif change_reason_code is not None or change_reason is not None:
                raise ValidationError("首个版本不能携带复核原因")
            inquiry_json = canonical_json(summary)
            inquiry_hash = digest(summary)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO advice_versions(case_id,version_no,status,inquiry_json,inquiry_hash,"
                    "rule_version_id,rule_snapshot_hash,change_reason_code,change_reason,"
                    "created_by,created_at) VALUES(?,?,'draft',?,?,?,?,?,?,?,?)",
                    (case_id, next_no, inquiry_json, inquiry_hash, rule_version_id,
                     rule["snapshot_hash"], change_reason_code, change_reason, actor_id, now),
                )
                detail = {"case_id": case_id, "version_no": next_no,
                          "inquiry_hash": inquiry_hash, "field_count": len(summary),
                          "rule_version_id": rule_version_id,
                          "rule_snapshot_hash": rule["snapshot_hash"]}
                if next_no >= 2:
                    detail["change_reason_code"] = change_reason_code
                append_event(connection, actor_id=actor_id,
                             action="advice.version_drafted", resource_type="advice_version",
                             resource_id=f"{case_id}#v{next_no}", detail=detail, occurred_at=now)
                return "advice_version", f"{case_id}#v{next_no}", {"case_id": case_id,
                                                                   "version_no": next_no}

            return self._idempotent(connection, request_id=request_id, action="draft_version",
                                    payload=payload, create=create)

    def add_section(self, *, request_id: str, actor_id: str, case_id: str, version_no: int,
                    kind: str, content: str) -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "version_no": version_no,
                   "kind": kind, "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            case = self._case_row(connection, case_id)
            site = self._site(connection, case["site_id"])
            self._same_org(actor, site)
            version = self._version_row(connection, case_id, version_no)
            if version["status"] != "draft":
                raise ConflictError("只有草稿版本可以增加内容")
            if kind not in SECTION_KINDS:
                raise ValidationError("kind 只能是 lifestyle 或 medical_risk")
            content = self._text(content, "content", 1000)
            now = self._now()
            position_row = connection.execute(
                "SELECT MAX(position) AS position FROM advice_sections WHERE case_id=? AND version_no=?",
                (case_id, version_no),
            ).fetchone()
            position = (position_row["position"] or 0) + 1
            section_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO advice_sections(section_id,case_id,version_no,position,kind,content,"
                    "created_by) VALUES(?,?,?,?,?,?,?)",
                    (section_id, case_id, version_no, position, kind, content, actor_id),
                )
                append_event(connection, actor_id=actor_id, action="advice.section_added",
                             resource_type="advice_section", resource_id=section_id,
                             detail={"case_id": case_id, "version_no": version_no, "kind": kind,
                                     "rule_snapshot_hash": version["rule_snapshot_hash"]},
                             occurred_at=now)
                return "advice_section", section_id, {"section_id": section_id, "signed": False}

            return self._idempotent(connection, request_id=request_id, action="add_section",
                                    payload=payload, create=create)

    def sign_section(self, *, request_id: str, actor_id: str, case_id: str, version_no: int,
                     section_id: str) -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "version_no": version_no,
                   "section_id": section_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            case = self._case_row(connection, case_id)
            site = self._site(connection, case["site_id"])
            self._same_org(actor, site)
            version = self._version_row(connection, case_id, version_no)
            if version["status"] != "draft":
                raise ConflictError("只有草稿版本可以签署")
            section = connection.execute(
                "SELECT * FROM advice_sections WHERE section_id=?", (section_id,)
            ).fetchone()
            if section is None:
                raise NotFoundError("建议内容段不存在")
            if section["case_id"] != case_id or section["version_no"] != version_no:
                raise NotFoundError("建议内容段不属于该版本")
            if section["created_by"] != actor_id:
                raise PermissionDenied("专家只能签署自己负责出具的内容")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                if section["signed_at"] is not None:
                    raise ConflictError("该内容已经签署")
                material = {
                    "section_id": section["section_id"],
                    "case_id": section["case_id"],
                    "version_no": section["version_no"],
                    "kind": section["kind"],
                    "content": section["content"],
                    "rule_snapshot_hash": version["rule_snapshot_hash"],
                    "signed_by": actor_id,
                    "signed_at": now,
                }
                signature_hash = digest(material)
                connection.execute(
                    "UPDATE advice_sections SET signed_at=?,signature_hash=? WHERE section_id=?",
                    (now, signature_hash, section_id),
                )
                append_event(connection, actor_id=actor_id, action="advice.section_signed",
                             resource_type="advice_section", resource_id=section_id,
                             detail={"case_id": case_id, "version_no": version_no,
                                     "signature_hash": signature_hash,
                                     "rule_snapshot_hash": version["rule_snapshot_hash"]},
                             occurred_at=now)
                return "advice_section", section_id, {"section_id": section_id, "signed": True,
                                                      "signature_hash": signature_hash}

            return self._idempotent(connection, request_id=request_id, action="sign_section",
                                    payload=payload, create=create)

    def publish_version(self, *, request_id: str, actor_id: str, case_id: str,
                        version_no: int) -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "version_no": version_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            case = self._case_row(connection, case_id)
            site = self._site(connection, case["site_id"])
            self._same_org(actor, site)
            version = self._version_row(connection, case_id, version_no)

            def create() -> tuple[str, str, dict[str, Any]]:
                if version["status"] != "draft":
                    raise ConflictError("只有草稿版本可以发布")
                # 1) 授权范围仍然有效
                consent = self._consent_row(connection, case["consent_id"])
                if consent["status"] != "granted":
                    raise PermissionDenied("授权已撤回，不能发布建议")
                self._validate_summary(json.loads(version["inquiry_json"]), self._scope(consent))
                # 2) 规则快照仍存在、未被篡改且仍在生效窗口
                rule = self._rule_row(connection, version["rule_version_id"])
                if rule["snapshot_hash"] != version["rule_snapshot_hash"]:
                    raise ConflictError("规则快照与发布时记录不一致")
                if not self._rule_effective(rule, self._now()):
                    raise ConflictError("规则版本已过生效窗口，不能据此发布；请按现行规则新建复核版本")
                # 3) 全部签署仍然有效，且专家只签自己的内容
                sections = connection.execute(
                    "SELECT * FROM advice_sections WHERE case_id=? AND version_no=? ORDER BY position",
                    (case_id, version_no),
                ).fetchall()
                if not sections:
                    raise ConflictError("没有任何已签署的建议内容，不能发布")
                kinds = {section["kind"] for section in sections}
                if "lifestyle" not in kinds or "medical_risk" not in kinds:
                    raise ConflictError("发布内容必须同时包含生活方式建议和就医风险提示")
                signers = []
                for section in sections:
                    if section["signed_at"] is None or not section["signature_hash"]:
                        raise ConflictError("存在尚未签署的建议内容，不能发布")
                    if section["created_by"] not in signers:
                        signer = self._actor(connection, section["created_by"])
                        self._require(signer, "reviewer")
                        if signer.organization_id != site["organization_id"]:
                            raise PermissionDenied("签署专家不属于本组织，签署无效")
                        signers.append(section["created_by"])
                    expected = digest(self._signature_material(
                        section_row=section, rule_snapshot_hash=version["rule_snapshot_hash"]))
                    if expected != section["signature_hash"]:
                        raise ConflictError(f"内容段 {section['section_id']} 签名校验失败")
                now = self._now()
                connection.execute(
                    "UPDATE advice_versions SET status='published',published_at=?,published_by=? "
                    "WHERE case_id=? AND version_no=?",
                    (now, actor_id, case_id, version_no),
                )
                superseded = None
                if version_no >= 2:
                    prior = self._version_row(connection, case_id, version_no - 1)
                    if prior["status"] == "published":
                        reason = {"code": "superseded_by_new_review", "by_version": version_no,
                                  "at": now,
                                  "change_reason_code": version["change_reason_code"]}
                        connection.execute(
                            "UPDATE advice_versions SET status='superseded',status_reason_json=? "
                            "WHERE case_id=? AND version_no=?",
                            (canonical_json(reason), case_id, version_no - 1),
                        )
                        superseded = version_no - 1
                        append_event(connection, actor_id=actor_id,
                                     action="advice.version_superseded",
                                     resource_type="advice_version",
                                     resource_id=f"{case_id}#v{version_no - 1}",
                                     detail={"case_id": case_id, "version_no": version_no - 1,
                                             "by_version": version_no,
                                             "reason_code": "superseded_by_new_review",
                                             "change_reason_code": version["change_reason_code"]},
                                     occurred_at=now)
                append_event(connection, actor_id=actor_id, action="advice.published",
                             resource_type="advice_version", resource_id=f"{case_id}#v{version_no}",
                             detail={"case_id": case_id, "version_no": version_no,
                                     "consent_id": case["consent_id"],
                                     "scope_hash": consent["scope_hash"],
                                     "rule_version_id": version["rule_version_id"],
                                     "rule_snapshot_hash": version["rule_snapshot_hash"],
                                     "signers": signers, "superseded_version": superseded},
                             occurred_at=now)
                return "advice_version", f"{case_id}#v{version_no}", {
                    "case_id": case_id, "version_no": version_no, "effective": True,
                    "superseded_version": superseded}

            return self._idempotent(connection, request_id=request_id, action="publish_version",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 查询与复核

    def get_case(self, *, actor_id: str, case_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            case = self._case_row(connection, case_id)
            site = self._site(connection, case["site_id"])
            self._same_org(actor, site)
            versions = []
            rows = connection.execute(
                "SELECT * FROM advice_versions WHERE case_id=? ORDER BY version_no", (case_id,)
            ).fetchall()
            current_no = next((r["version_no"] for r in rows if r["status"] == "published"), None)
            for row in rows:
                versions.append({"version_no": row["version_no"], "status": row["status"],
                                 "is_current": row["status"] == "published",
                                 "rule_version_id": row["rule_version_id"],
                                 "created_at": row["created_at"],
                                 "published_at": row["published_at"]})
            return {"case_id": case_id, "site_id": case["site_id"],
                    "participant_ref": case["participant_ref"], "consent_id": case["consent_id"],
                    "current_version_no": current_no, "versions": versions}

    def _redaction_level(self, actor, consent_row) -> str:
        if consent_row["status"] == "withdrawn":
            return "withdrawn"
        if actor.role == "reviewer":
            return "full"
        if actor.role in ("operator", "admin"):
            return "inquiry_redacted"
        return "metadata_only"  # auditor 及其他只读角色

    def view_version(self, *, actor_id: str, case_id: str, version_no: int) -> dict[str, Any]:
        """返回按角色红acted后的建议版本，并把本次访问记入审计。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            case = self._case_row(connection, case_id)
            site = self._site(connection, case["site_id"])
            self._same_org(actor, site)
            version = self._version_row(connection, case_id, version_no)
            consent = self._consent_row(connection, case["consent_id"])
            rule = self._rule_row(connection, version["rule_version_id"])
            level = self._redaction_level(actor, consent)
            inquiry = json.loads(version["inquiry_json"])
            if level == "full":
                inquiry_view: Any = inquiry
            elif level == "withdrawn":
                inquiry_view = {"redacted": True, "reason": "consent_withdrawn"}
            else:
                inquiry_view = {"redacted": True, "reason": "role_not_authorized",
                                "fields": sorted(inquiry)}
            sections = []
            for row in connection.execute(
                "SELECT * FROM advice_sections WHERE case_id=? AND version_no=? ORDER BY position",
                (case_id, version_no),
            ):
                item = {"section_id": row["section_id"], "kind": row["kind"],
                        "signed_by": row["created_by"], "signed_at": row["signed_at"],
                        "signature_hash": row["signature_hash"]}
                if level in ("full", "inquiry_redacted"):
                    item["content"] = row["content"]
                else:
                    item["content"] = None
                    item["content_redacted"] = True
                sections.append(item)
            view = {
                "case_id": case_id, "version_no": version_no,
                "status": version["status"], "is_current": version["status"] == "published",
                "nature": ADVICE_NATURE, "diagnostic_boundary": DIAGNOSTIC_BOUNDARY,
                "redaction_level": level,
                "basis": {"rule_version_id": version["rule_version_id"],
                          "rule_version_label": rule["version_label"],
                          "rule_snapshot_hash": version["rule_snapshot_hash"]},
                "consent_status": consent["status"],
                "inquiry_summary": inquiry_view,
                "sections": sections,
                "created_at": version["created_at"], "published_at": version["published_at"],
            }
            append_event(connection, actor_id=actor_id, action="advice.accessed",
                         resource_type="advice_version", resource_id=f"{case_id}#v{version_no}",
                         detail={"case_id": case_id, "version_no": version_no,
                                 "viewer_role": actor.role, "redaction_level": level,
                                 "consent_status": consent["status"]},
                         occurred_at=self._now())
            return view

    def explain_version(self, *, actor_id: str, case_id: str, version_no: int) -> dict[str, Any]:
        """说明一个版本为何生效或失效，不返回任何健康内容。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            case = self._case_row(connection, case_id)
            site = self._site(connection, case["site_id"])
            self._same_org(actor, site)
            version = self._version_row(connection, case_id, version_no)
            consent = self._consent_row(connection, case["consent_id"])
            rule = self._rule_row(connection, version["rule_version_id"])
            # 签名逐条重算，说明签署是否仍然可验证
            signatures = []
            for row in connection.execute(
                "SELECT * FROM advice_sections WHERE case_id=? AND version_no=? ORDER BY position",
                (case_id, version_no),
            ):
                signer_row = connection.execute(
                    "SELECT active,role,organization_id FROM actors WHERE actor_id=?",
                    (row["created_by"],),
                ).fetchone()
                hash_valid = bool(row["signature_hash"]) and digest(self._signature_material(
                    section_row=row, rule_snapshot_hash=version["rule_snapshot_hash"])) == \
                    row["signature_hash"]
                signatures.append({
                    "section_id": row["section_id"], "kind": row["kind"],
                    "signed_by": row["created_by"], "signed_at": row["signed_at"],
                    "hash_valid": hash_valid,
                    "signer_active": bool(signer_row["active"]) if signer_row else False,
                    "signer_role_valid": signer_row is not None and signer_row["role"] == "reviewer"
                    and signer_row["organization_id"] == site["organization_id"],
                })
            withdrawn = consent["status"] == "withdrawn"
            effective_reason = None
            ineffective_reason = None
            if version["status"] == "published":
                effective_reason = {
                    "code": "published",
                    "at": version["published_at"], "by": version["published_by"],
                    "rule_version_id": version["rule_version_id"],
                    "rule_snapshot_hash": version["rule_snapshot_hash"],
                    "consent_id": case["consent_id"], "scope_hash": consent["scope_hash"],
                    "all_signatures_valid": all(s["hash_valid"] and s["signer_role_valid"]
                                                for s in signatures),
                }
            elif version["status"] == "draft":
                ineffective_reason = {"code": "not_published",
                                      "message": "草稿尚未通过发布校验，未生效"}
            else:
                reason = json.loads(version["status_reason_json"]) if version["status_reason_json"] else {}
                ineffective_reason = {"code": reason.get("code", version["status"]),
                                      "at": reason.get("at")}
                if reason.get("code") == "superseded_by_new_review":
                    new_version = self._version_row(connection, case_id, reason["by_version"])
                    ineffective_reason["by_version"] = reason["by_version"]
                    ineffective_reason["change_reason_code"] = reason.get("change_reason_code")
                    if not withdrawn:
                        ineffective_reason["change_reason"] = new_version["change_reason"]
                elif reason.get("code") == "consent_withdrawn":
                    ineffective_reason["message"] = "参与者已撤回授权，建议停止使用"
            timeline = [{"event": "drafted", "at": version["created_at"], "by": version["created_by"],
                         "change_reason_code": version["change_reason_code"]}]
            if version["published_at"]:
                timeline.append({"event": "published", "at": version["published_at"],
                                 "by": version["published_by"]})
            if version["status"] in ("superseded", "invalidated") and version["status_reason_json"]:
                reason = json.loads(version["status_reason_json"])
                timeline.append({"event": version["status"], "at": reason.get("at"),
                                 "reason_code": reason.get("code"),
                                 "by_version": reason.get("by_version")})
            explanation = {
                "case_id": case_id, "version_no": version_no, "status": version["status"],
                "is_current": version["status"] == "published",
                "effective": version["status"] == "published",
                "nature": ADVICE_NATURE, "diagnostic_boundary": DIAGNOSTIC_BOUNDARY,
                "effective_reason": effective_reason,
                "ineffective_reason": ineffective_reason,
                "rule_basis": {"rule_version_id": version["rule_version_id"],
                               "version_label": rule["version_label"],
                               "snapshot_hash": version["rule_snapshot_hash"],
                               "effective_now": self._rule_effective(rule, self._now())},
                "consent": {"consent_id": case["consent_id"], "status": consent["status"],
                            "scope_hash": consent["scope_hash"],
                            "withdrawn_at": consent["withdrawn_at"]},
                "signatures": signatures,
                "timeline": timeline,
            }
            append_event(connection, actor_id=actor_id, action="advice.explained",
                         resource_type="advice_version", resource_id=f"{case_id}#v{version_no}",
                         detail={"case_id": case_id, "version_no": version_no,
                                 "viewer_role": actor.role, "version_status": version["status"]},
                         occurred_at=self._now())
            return explanation
