"""建议留痕与复核领域服务。

在基础服务的权限、幂等、事务与哈希审计边界之上，实现：
授权最少必要留痕、规则版本快照、复核版本链、专家分签、
发布闸口、授权撤回后的擦除与访问事实保留、按角色脱敏的可复核查询。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .storage import Database

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 专家/专家管理员可以起草规则；现场操作员只能在授权下问询；复核人负责发布闸口；审计员只读脱敏。
RULE_ROLES = frozenset({"admin", "reviewer"})
CONSENT_ROLES = frozenset({"admin", "operator"})
DRAFT_ROLES = frozenset({"admin", "operator"})
EXPERT_ROLES = frozenset({"admin", "expert"})
PUBLISH_ROLES = frozenset({"admin", "reviewer"})
READ_ROLES = frozenset({"admin", "operator", "reviewer", "expert", "auditor"})

SCOPES = frozenset({"intake_record", "advice_generation", "advice_disclosure"})

NON_DIAGNOSIS_NOTICE = (
    "本内容为中医药文化体验中的生活方式参考，不构成疾病诊断或治疗方案；"
    "如症状持续或加重，请前往正规医疗机构就诊。"
)

# 各角色可见的敏感字段类别。审计员只能核对结构与哈希，不能看健康内容。
SENSITIVE_INTAKE_ROLES = frozenset({"admin", "operator", "reviewer", "expert"})
SENSITIVE_ADVICE_ROLES = frozenset({"admin", "operator", "reviewer", "expert"})
MASKED = "***"


class AdviceService:
    """协调授权、规则快照、版本、签署、发布、擦除与脱敏查询。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _parse_ts(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            text = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError("时间必须是 ISO 8601 格式") from exc
        if parsed.tzinfo is None:
            raise ValidationError("时间必须包含时区")
        return parsed.astimezone(timezone.utc)

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 2000) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _same_org(self, actor, organization_id: str) -> None:
        if actor["role"] != "admin" and actor["organization_id"] != organization_id:
            raise PermissionDenied("不能访问其他机构的数据")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    # ------------------------------------------------------------------ 规则版本

    def register_rule_version(self, *, request_id: str, actor_id: str, rule_set_id: str,
                              version: str, content: dict[str, Any],
                              effective_from: str | None = None) -> dict[str, Any]:
        """登记一套规则的新生效版本；同规则集旧生效版本自动失效。"""

        if not isinstance(content, dict) or not content:
            raise ValidationError("content 必须是非空对象")
        items = content.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError("规则 content 必须包含非空 items 列表")
        item_ids: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or not str(item.get("id", "")).strip() or not str(item.get("text", "")).strip():
                raise ValidationError("每条规则必须包含非空 id 与 text")
            if item["id"] in item_ids:
                raise ValidationError(f"规则 id 重复: {item['id']}")
            item_ids.add(item["id"])
        effective_from_text = effective_from or self._now()
        self._parse_ts(effective_from_text)
        payload = {"actor_id": actor_id, "rule_set_id": rule_set_id, "version": version,
                   "content": content, "effective_from": effective_from_text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *RULE_ROLES)
            rule_set_id = self._identifier(rule_set_id, "rule_set_id")
            version = self._identifier(version, "version")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM rule_versions WHERE rule_set_id=? AND version=?",
                    (rule_set_id, version),
                ).fetchone()
                if existing:
                    raise ConflictError("规则版本已经存在")
                content_hash = digest(content)
                connection.execute(
                    "INSERT INTO rule_versions(rule_set_id,version,status,content_json,content_hash,"
                    "effective_from,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (rule_set_id, version, "effective", canonical_json(content), content_hash,
                     effective_from_text, actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE rule_versions SET status='retired', retired_at=? "
                    "WHERE rule_set_id=? AND status='effective' AND version<>?",
                    (self._now(), rule_set_id, version),
                )
                self._audit(connection, actor_id=actor_id, action="rule_version.registered",
                            resource_type="rule_version", resource_id=f"{rule_set_id}:{version}",
                            detail={"rule_set_id": rule_set_id, "version": version,
                                    "content_hash": content_hash, "effective_from": effective_from_text})
                return "rule_version", f"{rule_set_id}:{version}", {
                    "rule_set_id": rule_set_id, "version": version, "content_hash": content_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_rule_version", payload=payload, create=create)

    def retire_rule_version(self, *, request_id: str, actor_id: str,
                            rule_set_id: str, version: str) -> dict[str, Any]:
        """宣布某规则版本失效；此后引用该快照的草稿不能再发布。"""

        payload = {"actor_id": actor_id, "rule_set_id": rule_set_id, "version": version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *RULE_ROLES)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM rule_versions WHERE rule_set_id=? AND version=?",
                    (rule_set_id, version),
                ).fetchone()
                if row is None:
                    raise NotFoundError("规则版本不存在")
                if row["status"] != "effective":
                    raise ConflictError("规则版本已经失效")
                connection.execute(
                    "UPDATE rule_versions SET status='retired', retired_at=? WHERE rule_set_id=? AND version=?",
                    (self._now(), rule_set_id, version),
                )
                self._audit(connection, actor_id=actor_id, action="rule_version.retired",
                            resource_type="rule_version", resource_id=f"{rule_set_id}:{version}",
                            detail={"rule_set_id": rule_set_id, "version": version})
                return "rule_version", f"{rule_set_id}:{version}", {"status": "retired"}

            return self._idempotent(connection, request_id=request_id,
                                    action="retire_rule_version", payload=payload, create=create)

    def _effective_rule(self, connection, rule_set_id: str):
        row = connection.execute(
            "SELECT * FROM rule_versions WHERE rule_set_id=? AND status='effective' "
            "ORDER BY effective_from DESC, rowid DESC LIMIT 1",
            (rule_set_id,),
        ).fetchone()
        if row is None:
            raise ValidationError("该规则集当前没有生效版本")
        return row

    def get_rule_version(self, actor_id: str, rule_set_id: str, version: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *READ_ROLES)
            row = connection.execute(
                "SELECT * FROM rule_versions WHERE rule_set_id=? AND version=?",
                (rule_set_id, version),
            ).fetchone()
            if row is None:
                raise NotFoundError("规则版本不存在")
            result = {
                "rule_set_id": row["rule_set_id"], "version": row["version"], "status": row["status"],
                "content_hash": row["content_hash"], "effective_from": row["effective_from"],
                "retired_at": row["retired_at"],
            }
            if actor["role"] in RULE_ROLES or actor["role"] == "expert":
                result["content"] = json.loads(row["content_json"])
            return result

    # ------------------------------------------------------------------ 授权

    def grant_consent(self, *, request_id: str, actor_id: str, participant_id: str,
                      scopes: list[str], fields: list[str], expires_at: str | None = None) -> dict[str, Any]:
        """登记参与者授权：只记录用途范围与最少必要字段清单。"""

        participant_id = self._identifier(participant_id, "participant_id")
        if not isinstance(scopes, list) or not scopes or any(s not in SCOPES for s in scopes):
            raise ValidationError(f"scopes 必须是 {sorted(SCOPES)} 的非空子集")
        if not isinstance(fields, list) or not fields:
            raise ValidationError("fields 必须是非空数组（最少必要字段清单）")
        fields = tuple(sorted({self._identifier(f, "field") for f in fields}))
        scopes = tuple(sorted(scopes))
        expires = self._parse_ts(expires_at)
        if expires is not None and expires <= self.clock.now():
            raise ValidationError("授权到期时间必须晚于当前时间")
        payload = {"actor_id": actor_id, "participant_id": participant_id,
                   "scopes": scopes, "fields": fields, "expires_at": expires_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONSENT_ROLES)

            def create() -> tuple[str, str, dict[str, Any]]:
                consent_id = uuid.uuid4().hex
                scope_hash = digest({"participant_id": participant_id, "scopes": scopes, "fields": fields})
                connection.execute(
                    "INSERT INTO consents(consent_id,organization_id,participant_id,scope_json,fields_json,"
                    "scope_hash,status,granted_by,granted_at,expires_at) VALUES(?,?,?,?,?,?,'granted',?,?,?)",
                    (consent_id, actor["organization_id"], participant_id, canonical_json(list(scopes)),
                     canonical_json(list(fields)), scope_hash, actor_id, self._now(), expires_at),
                )
                self._audit(connection, actor_id=actor_id, action="consent.granted",
                            resource_type="consent", resource_id=consent_id,
                            detail={"participant_id": participant_id, "scopes": list(scopes),
                                    "fields": list(fields), "scope_hash": scope_hash,
                                    "expires_at": expires_at})
                return "consent", consent_id, {"consent_id": consent_id, "scope_hash": scope_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="grant_consent", payload=payload, create=create)

    def withdraw_consent(self, *, request_id: str, actor_id: str, consent_id: str,
                         reason: str | None = None) -> dict[str, Any]:
        """撤回授权：立即擦除受权内容并禁止再用，但保留访问与签署的审计事实。"""

        if reason is not None:
            reason = self._text(reason, "reason", 500)
        payload = {"actor_id": actor_id, "consent_id": consent_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONSENT_ROLES)

            def create() -> tuple[str, str, dict[str, Any]]:
                consent = connection.execute("SELECT * FROM consents WHERE consent_id=?", (consent_id,)).fetchone()
                if consent is None:
                    raise NotFoundError("授权不存在")
                self._same_org(actor, consent["organization_id"])
                if consent["status"] == "withdrawn":
                    raise ConflictError("授权已经撤回")
                now = self._now()
                connection.execute(
                    "UPDATE consents SET status='withdrawn', withdrawn_at=?, withdrawal_reason=? WHERE consent_id=?",
                    (now, reason, consent_id),
                )
                erased_versions = self._erase_consent_content(connection, consent_id, now)
                self._audit(connection, actor_id=actor_id, action="consent.withdrawn",
                            resource_type="consent", resource_id=consent_id,
                            detail={"participant_id": consent["participant_id"], "reason": reason,
                                    "erased_versions": erased_versions})
                return "consent", consent_id, {"status": "withdrawn", "erased_versions": erased_versions}

            return self._idempotent(connection, request_id=request_id,
                                    action="consent_withdrawn", payload=payload, create=create)

    def _erase_consent_content(self, connection, consent_id: str, now: str) -> list[str]:
        """擦除该授权下所有版本的问询摘要与建议正文；签名哈希作为审计事实保留。"""

        rows = connection.execute(
            "SELECT version_id FROM consultation_versions WHERE consent_id=?", (consent_id,)
        ).fetchall()
        version_ids = [row["version_id"] for row in rows]
        if version_ids:
            marks = ",".join("?" for _ in version_ids)
            connection.execute(
                f"UPDATE consultation_versions SET status='erased', intake_json=NULL, "
                f"content_erased_at=? WHERE version_id IN ({marks})",
                (now, *version_ids),
            )
            connection.execute(
                f"UPDATE advice_sections SET content_json=NULL, title=NULL WHERE version_id IN ({marks})",
                version_ids,
            )
        return version_ids

    def _consent_valid(self, connection, consent_row, *, require_scopes: set[str] | None = None) -> list[str]:
        """返回当前仍然有效的授权原因；无效时返回失效原因列表（空列表表示有效）。"""

        reasons: list[str] = []
        if consent_row["status"] != "granted":
            reasons.append("授权已撤回")
            return reasons
        if consent_row["expires_at"]:
            if self._parse_ts(consent_row["expires_at"]) <= self.clock.now():
                reasons.append("授权已过期")
        if require_scopes:
            granted = set(json.loads(consent_row["scope_json"]))
            missing = require_scopes - granted
            if missing:
                reasons.append(f"授权用途缺少: {sorted(missing)}")
        return reasons

    def _load_consent(self, connection, consent_id: str):
        row = connection.execute("SELECT * FROM consents WHERE consent_id=?", (consent_id,)).fetchone()
        if row is None:
            raise NotFoundError("授权不存在")
        return row

    # ------------------------------------------------------------------ 辨识会话与版本

    def create_consultation(self, *, request_id: str, actor_id: str, site_id: str,
                            consent_id: str) -> dict[str, Any]:
        """在有效授权下开启一次体质辨识会话。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "consent_id": consent_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DRAFT_ROLES)
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            self._same_org(actor, site["organization_id"])
            consent = self._load_consent(connection, consent_id)
            self._same_org(actor, consent["organization_id"])
            invalid = self._consent_valid(connection, consent)
            if invalid:
                raise PermissionDenied(f"授权不可用: {'; '.join(invalid)}")

            def create() -> tuple[str, str, dict[str, Any]]:
                consultation_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO consultations(consultation_id,organization_id,participant_id,site_id,"
                    "consent_id,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (consultation_id, site["organization_id"], consent["participant_id"], site_id,
                     consent_id, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="consultation.created",
                            resource_type="consultation", resource_id=consultation_id,
                            detail={"site_id": site_id, "consent_id": consent_id,
                                    "participant_id": consent["participant_id"]})
                return "consultation", consultation_id, {"consultation_id": consultation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_consultation", payload=payload, create=create)

    def add_intake_version(self, *, request_id: str, actor_id: str, consultation_id: str,
                           intake: dict[str, Any], trigger: str = "initial",
                           supplement_summary: str | None = None,
                           rule_set_id: str | None = None,
                           consent_id: str | None = None) -> dict[str, Any]:
        """记录问询摘要并生成新的复核版本草稿；补充信息不覆盖旧版本。

        首版 trigger=initial；补充信息 trigger=supplement，必须给出 supplement_summary
        说明本次补充了什么，该说明同时成为旧版本被替换时可查的原因。
        补充信息若涉及新字段，必须先取得覆盖这些字段的新授权，并以 consent_id 指定；
        每个版本各自冻结其记录时所依据的授权范围快照。
        """

        if not isinstance(intake, dict) or not intake:
            raise ValidationError("intake 必须是非空对象（问询摘要）")
        if trigger not in ("initial", "supplement"):
            raise ValidationError("trigger 只能是 initial 或 supplement")
        if trigger == "supplement":
            supplement_summary = self._text(supplement_summary or "", "supplement_summary", 500)
        elif supplement_summary:
            raise ValidationError("首版问询不应附带补充说明")
        payload = {"actor_id": actor_id, "consultation_id": consultation_id, "intake": intake,
                   "trigger": trigger, "supplement_summary": supplement_summary,
                   "rule_set_id": rule_set_id, "consent_id": consent_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *DRAFT_ROLES)
            consultation = connection.execute(
                "SELECT * FROM consultations WHERE consultation_id=?", (consultation_id,)
            ).fetchone()
            if consultation is None:
                raise NotFoundError("辨识会话不存在")
            self._same_org(actor, consultation["organization_id"])
            version_consent_id = consent_id or consultation["consent_id"]
            consent = self._load_consent(connection, version_consent_id)
            if consent["participant_id"] != consultation["participant_id"] or \
                    consent["organization_id"] != consultation["organization_id"]:
                raise ValidationError("补充授权必须属于同一位参与者与同一机构")
            invalid = self._consent_valid(connection, consent, require_scopes={"intake_record", "advice_generation"})
            if invalid:
                raise PermissionDenied(f"授权不可用: {'; '.join(invalid)}")

            last = connection.execute(
                "SELECT sequence, status FROM consultation_versions WHERE consultation_id=? "
                "ORDER BY sequence DESC LIMIT 1", (consultation_id,),
            ).fetchone()

            allowed_fields = set(json.loads(consent["fields_json"]))
            intake_fields = sorted(intake.keys())
            overflow = [f for f in intake_fields if f not in allowed_fields]
            if overflow:
                raise PermissionDenied(f"问询字段超出授权的最少必要范围: {overflow}")

            chosen_rule_set = rule_set_id
            if chosen_rule_set is None:
                chosen_rule_set = self._default_rule_set(connection)
            chosen_rule_set = self._identifier(chosen_rule_set, "rule_set_id")
            rule = self._effective_rule(connection, chosen_rule_set)

            def create() -> tuple[str, str, dict[str, Any]]:
                # 状态冲突检查放在幂等判定之后：同一 request_id 的重试应回放原回执。
                if trigger == "initial" and last is not None:
                    raise ConflictError("该会话已经存在首版问询，补充信息必须以 supplement 生成新版本")
                if trigger == "supplement" and last is None:
                    raise ValidationError("补充版本必须基于已有版本")
                open_draft = connection.execute(
                    "SELECT 1 FROM consultation_versions WHERE consultation_id=? AND status='draft' LIMIT 1",
                    (consultation_id,),
                ).fetchone()
                if open_draft:
                    raise ConflictError("仍有未发布的草稿版本，请先发布或放弃后再补充信息")
                sequence = (last["sequence"] + 1) if last else 1
                version_id = uuid.uuid4().hex
                intake_hash = digest(intake)
                connection.execute(
                    "INSERT INTO consultation_versions(version_id,consultation_id,sequence,trigger_text,"
                    "supplement_summary,intake_json,intake_fields_json,intake_hash,consent_id,"
                    "consent_scope_hash,rule_set_id,rule_version,rule_content_hash,non_diagnosis_notice,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)",
                    (version_id, consultation_id, sequence, trigger, supplement_summary,
                     canonical_json(intake), canonical_json(intake_fields), intake_hash,
                     consent["consent_id"], consent["scope_hash"], chosen_rule_set, rule["version"],
                     rule["content_hash"], NON_DIAGNOSIS_NOTICE, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="advice_version.drafted",
                            resource_type="advice_version", resource_id=version_id,
                            detail={"consultation_id": consultation_id, "sequence": sequence,
                                    "trigger": trigger, "supplement_summary": supplement_summary,
                                    "intake_fields": intake_fields, "intake_hash": intake_hash,
                                    "consent_id": consent["consent_id"],
                                    "consent_scope_hash": consent["scope_hash"],
                                    "rule_set_id": chosen_rule_set, "rule_version": rule["version"],
                                    "rule_content_hash": rule["content_hash"]})
                return "advice_version", version_id, {"version_id": version_id, "sequence": sequence}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_intake_version", payload=payload, create=create)

    def _default_rule_set(self, connection) -> str:
        row = connection.execute(
            "SELECT rule_set_id FROM rule_versions WHERE status='effective' "
            "ORDER BY effective_from DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValidationError("当前没有任何生效规则集，请指定 rule_set_id")
        return row["rule_set_id"]

    # ------------------------------------------------------------------ 专家分签

    def _signature_material(self, section_row, version_row) -> dict[str, Any]:
        return {
            "section_id": section_row["section_id"],
            "version_id": section_row["version_id"],
            "expert_id": section_row["expert_id"],
            "kind": section_row["kind"],
            "content_hash": section_row["content_hash"],
            "rule_refs": json.loads(section_row["rule_refs_json"]),
            "intake_hash": version_row["intake_hash"],
            "rule_set_id": version_row["rule_set_id"],
            "rule_version": version_row["rule_version"],
            "rule_content_hash": version_row["rule_content_hash"],
            "consent_scope_hash": version_row["consent_scope_hash"],
            "non_diagnosis_notice": version_row["non_diagnosis_notice"],
        }

    def add_section(self, *, actor_id: str, version_id: str, kind: str,
                    content: dict[str, Any], rule_refs: list[str],
                    title: str | None = None) -> dict[str, Any]:
        """专家在草稿上添加本人负责的建议段（生活方式建议或就医风险提示）。"""

        if kind not in ("lifestyle", "risk_alert"):
            raise ValidationError("kind 只能是 lifestyle 或 risk_alert")
        if not isinstance(content, dict) or not content:
            raise ValidationError("content 必须是非空对象")
        if not isinstance(rule_refs, list) or not rule_refs:
            raise ValidationError("rule_refs 必须指明本段落依据的规则条目")
        if title is not None:
            title = self._text(title, "title", 200)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EXPERT_ROLES)
            version = self._load_version(connection, version_id)
            consultation = connection.execute(
                "SELECT * FROM consultations WHERE consultation_id=?", (version["consultation_id"],)
            ).fetchone()
            self._same_org(actor, consultation["organization_id"])
            consent = self._load_consent(connection, version["consent_id"])
            invalid = self._consent_valid(connection, consent, require_scopes={"advice_generation"})
            if invalid:
                raise PermissionDenied(f"授权不可用: {'; '.join(invalid)}")
            if version["status"] != "draft":
                raise ConflictError("只能在草稿版本上添加建议")
            rule = connection.execute(
                "SELECT * FROM rule_versions WHERE rule_set_id=? AND version=?",
                (version["rule_set_id"], version["rule_version"]),
            ).fetchone()
            known_refs = {item["id"] for item in json.loads(rule["content_json"])["items"]}
            refs = sorted(set(rule_refs))
            unknown = [ref for ref in refs if ref not in known_refs]
            if unknown:
                raise ValidationError(f"规则条目在冻结的规则快照中不存在: {unknown}")
            content_hash = digest(content)
            section_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO advice_sections(section_id,version_id,expert_id,kind,title,content_json,"
                "content_hash,rule_refs_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (section_id, version_id, actor_id, kind, title, canonical_json(content),
                 content_hash, canonical_json(refs), self._now()),
            )
            self._audit(connection, actor_id=actor_id, action="advice_section.added",
                        resource_type="advice_section", resource_id=section_id,
                        detail={"version_id": version_id, "kind": kind, "expert_id": actor_id,
                                "content_hash": content_hash, "rule_refs": refs})
            return {"section_id": section_id, "content_hash": content_hash}

    def sign_section(self, *, actor_id: str, section_id: str) -> dict[str, Any]:
        """专家签署本人撰写的段落；签名绑定内容、规则快照与问询摘要。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *EXPERT_ROLES)
            section = connection.execute("SELECT * FROM advice_sections WHERE section_id=?", (section_id,)).fetchone()
            if section is None:
                raise NotFoundError("建议段落不存在")
            version = self._load_version(connection, section["version_id"])
            consultation = connection.execute(
                "SELECT * FROM consultations WHERE consultation_id=?", (version["consultation_id"],)
            ).fetchone()
            self._same_org(actor, consultation["organization_id"])
            if version["status"] != "draft":
                raise ConflictError("版本已经结束，不能再签署")
            if section["expert_id"] != actor_id:
                raise PermissionDenied("专家只能签署自己负责撰写的段落")
            duplicate = connection.execute(
                "SELECT 1 FROM advice_signatures WHERE section_id=? AND signer_id=?",
                (section_id, actor_id),
            ).fetchone()
            if duplicate:
                raise ConflictError("已经签署过该段落")
            consent = self._load_consent(connection, version["consent_id"])
            invalid = self._consent_valid(connection, consent, require_scopes={"advice_generation"})
            if invalid:
                raise PermissionDenied(f"授权不可用: {'; '.join(invalid)}")
            material = self._signature_material(section, version)
            signature_hash = digest(material)
            signature_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO advice_signatures(signature_id,section_id,version_id,signer_id,"
                "signer_display_name,signature_hash,signed_at) VALUES(?,?,?,?,?,?,?)",
                (signature_id, section_id, version["version_id"], actor_id,
                 actor["display_name"], signature_hash, self._now()),
            )
            self._audit(connection, actor_id=actor_id, action="advice_section.signed",
                        resource_type="advice_signature", resource_id=signature_id,
                        detail={"section_id": section_id, "version_id": version["version_id"],
                                "signer_id": actor_id, "signature_hash": signature_hash,
                                "content_hash": section["content_hash"]})
            return {"signature_id": signature_id, "signature_hash": signature_hash}

    # ------------------------------------------------------------------ 发布闸口

    def publish_version(self, *, request_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        """发布前一次性确认：授权范围、规则快照、全部签署当前均有效。"""

        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *PUBLISH_ROLES)
            version = self._load_version(connection, version_id)
            consultation = connection.execute(
                "SELECT * FROM consultations WHERE consultation_id=?", (version["consultation_id"],)
            ).fetchone()
            self._same_org(actor, consultation["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if version["status"] != "draft":
                    raise ConflictError(f"版本当前状态为 {version['status']}，不能发布")
                failures = self._publish_check_failures(connection, version)
                if failures:
                    raise ConflictError("发布校验未通过: " + "；".join(failures))
                now = self._now()
                previous = connection.execute(
                    "SELECT version_id, supplement_summary FROM consultation_versions "
                    "WHERE consultation_id=? AND status='published' ORDER BY sequence DESC LIMIT 1",
                    (version["consultation_id"],),
                ).fetchone()
                connection.execute(
                    "UPDATE consultation_versions SET status='published', published_at=?, published_by=? "
                    "WHERE version_id=?",
                    (now, actor_id, version_id),
                )
                superseded_id = None
                if previous:
                    superseded_id = previous["version_id"]
                    reason = version["supplement_summary"] or (
                        f"被第 {version['sequence']} 版复核结论替换（依据更新后的问询信息）"
                    )
                    connection.execute(
                        "UPDATE consultation_versions SET status='superseded', superseded_at=?, "
                        "superseded_reason=?, superseded_by_version=? WHERE version_id=?",
                        (now, reason, version_id, superseded_id),
                    )
                connection.execute(
                    "UPDATE consultations SET current_version_id=? WHERE consultation_id=?",
                    (version_id, version["consultation_id"]),
                )
                self._audit(connection, actor_id=actor_id, action="advice_version.published",
                            resource_type="advice_version", resource_id=version_id,
                            detail={"consultation_id": version["consultation_id"],
                                    "sequence": version["sequence"],
                                    "rule_set_id": version["rule_set_id"],
                                    "rule_version": version["rule_version"],
                                    "rule_content_hash": version["rule_content_hash"],
                                    "consent_scope_hash": version["consent_scope_hash"],
                                    "superseded_version": superseded_id})
                return "advice_version", version_id, {
                    "version_id": version_id, "status": "published", "superseded_version": superseded_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_advice_version", payload=payload, create=create)

    def _publish_check_failures(self, connection, version) -> list[str]:
        failures: list[str] = []
        consent = self._load_consent(connection, version["consent_id"])
        invalid = self._consent_valid(
            connection, consent,
            require_scopes={"intake_record", "advice_generation", "advice_disclosure"},
        )
        failures.extend(invalid)
        if consent["scope_hash"] != version["consent_scope_hash"]:
            failures.append("授权范围快照与发布时授权不一致")
        rule = connection.execute(
            "SELECT * FROM rule_versions WHERE rule_set_id=? AND version=?",
            (version["rule_set_id"], version["rule_version"]),
        ).fetchone()
        if rule is None:
            failures.append("冻结的规则版本不存在")
        elif rule["content_hash"] != version["rule_content_hash"]:
            failures.append("规则内容在起草后发生变化，规则快照已失效")
        elif rule["status"] != "effective":
            failures.append(f"规则版本 {rule['version']} 已失效，须依据现行规则重新出具版本")
        sections = connection.execute(
            "SELECT * FROM advice_sections WHERE version_id=? ORDER BY rowid", (version["version_id"],)
        ).fetchall()
        if not sections:
            failures.append("版本中没有任何专家建议段")
        for section in sections:
            signatures = connection.execute(
                "SELECT * FROM advice_signatures WHERE section_id=? ORDER BY rowid",
                (section["section_id"],),
            ).fetchall()
            if not signatures:
                failures.append(f"建议段 {section['section_id']} 尚未获得签署")
                continue
            expected_material = self._signature_material(section, version)
            expected_hash = digest(expected_material)
            for signature in signatures:
                if signature["signer_id"] != section["expert_id"]:
                    failures.append(f"建议段 {section['section_id']} 存在非负责专家的签署")
                if signature["signature_hash"] != expected_hash:
                    failures.append(f"建议段 {section['section_id']} 内容与签署时不一致")
                signer = connection.execute(
                    "SELECT active FROM actors WHERE actor_id=?", (signature["signer_id"],)
                ).fetchone()
                if signer is None or not signer["active"]:
                    failures.append(f"签署人 {signature['signer_id']} 已停用，签署不再有效")
        return failures

    def _load_version(self, connection, version_id: str):
        row = connection.execute(
            "SELECT * FROM consultation_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("建议版本不存在")
        return row

    # ------------------------------------------------------------------ 可复核查询

    def _load_sections(self, connection, version_id: str) -> list:
        return connection.execute(
            "SELECT * FROM advice_sections WHERE version_id=? ORDER BY rowid", (version_id,)
        ).fetchall()

    def _load_signatures(self, connection, section_id: str) -> list:
        return connection.execute(
            "SELECT * FROM advice_signatures WHERE section_id=? ORDER BY rowid", (section_id,)
        ).fetchall()

    def _render_section(self, connection, actor, section_row, version_row, advice_visible: bool) -> dict[str, Any]:
        signatures = []
        for sig in self._load_signatures(connection, section_row["section_id"]):
            signer = connection.execute(
                "SELECT active FROM actors WHERE actor_id=?", (sig["signer_id"],)
            ).fetchone()
            signatures.append({
                "signer_id": sig["signer_id"], "signer_display_name": sig["signer_display_name"],
                "signed_at": sig["signed_at"], "signature_hash": sig["signature_hash"],
                "signer_active": bool(signer and signer["active"]),
            })
        rendered = {
            "section_id": section_row["section_id"], "kind": section_row["kind"],
            "expert_id": section_row["expert_id"],
            "rule_refs": json.loads(section_row["rule_refs_json"]),
            "content_hash": section_row["content_hash"],
            "signatures": signatures,
        }
        if advice_visible:
            rendered["title"] = section_row["title"]
            rendered["content"] = json.loads(section_row["content_json"]) if section_row["content_json"] else None
        else:
            rendered["title"] = MASKED
            rendered["content"] = None
            rendered["hidden_reason"] = (
                "建议正文已因授权撤回被擦除" if version_row["status"] == "erased"
                else "当前角色无权查看建议正文"
            )
        return rendered

    def _version_effectiveness(self, connection, version_row, consultation_row) -> tuple[bool, list[str], list[str]]:
        """返回（是否当前生效，生效原因，失效原因）。"""

        effective_reasons: list[str] = []
        ineffective_reasons: list[str] = []
        status = version_row["status"]
        if status == "draft":
            ineffective_reasons.append("版本仍是草稿，尚未通过发布闸口")
        elif status == "superseded":
            ineffective_reasons.append(
                f"已被新版本 {version_row['superseded_by_version']} 替换；替换原因："
                f"{version_row['superseded_reason']}"
            )
        elif status == "erased":
            ineffective_reasons.append("授权撤回后内容已擦除，不再作为当前结论（历史访问事实保留在审计链中）")
        # 草稿同样逐条预检授权、规则快照与签署，便于发布前复核；
        # 已发布版本则据这些条件判定其当前是否仍然有效。
        if status in ("draft", "published"):
            consent = self._load_consent(connection, version_row["consent_id"])
            invalid = self._consent_valid(connection, consent)
            if invalid:
                ineffective_reasons.extend(invalid)
            elif status == "published":
                effective_reasons.append("参与者授权当前仍在有效期内")
            rule = connection.execute(
                "SELECT * FROM rule_versions WHERE rule_set_id=? AND version=?",
                (version_row["rule_set_id"], version_row["rule_version"]),
            ).fetchone()
            if rule is None:
                ineffective_reasons.append("冻结的规则版本已不存在")
            elif rule["status"] != "effective" or rule["content_hash"] != version_row["rule_content_hash"]:
                ineffective_reasons.append(
                    f"依据的规则快照 {version_row['rule_set_id']}:{version_row['rule_version']} 已失效"
                )
            elif status == "published":
                effective_reasons.append(
                    f"依据的规则快照 {version_row['rule_set_id']}:{version_row['rule_version']} 当前仍有效"
                )
            sections = self._load_sections(connection, version_row["version_id"])
            unsigned = []
            for section in sections:
                sig = connection.execute(
                    "SELECT COUNT(*) AS count FROM advice_signatures WHERE section_id=?",
                    (section["section_id"],),
                ).fetchone()["count"]
                if not sig:
                    unsigned.append(section["section_id"])
            if unsigned:
                ineffective_reasons.append(f"存在未签署段落: {unsigned}")
            elif sections and status == "published":
                effective_reasons.append("每段建议均由负责专家本人签署")
        if status == "published":
            if consultation_row["current_version_id"] != version_row["version_id"]:
                ineffective_reasons.append("版本虽已发布，但不是会话当前版本")
            else:
                effective_reasons.append("版本已通过发布闸口且是该会话当前版本")
        return (not ineffective_reasons and bool(effective_reasons)), effective_reasons, ineffective_reasons

    def _render_version(self, connection, actor, version_row, *, record_access: bool,
                        purpose: str) -> dict[str, Any]:
        consultation = connection.execute(
            "SELECT * FROM consultations WHERE consultation_id=?", (version_row["consultation_id"],)
        ).fetchone()
        self._same_org(actor, consultation["organization_id"])
        consent = self._load_consent(connection, version_row["consent_id"])

        role = actor["role"]
        intake_visible = role in SENSITIVE_INTAKE_ROLES and consent["status"] == "granted" \
            and version_row["status"] != "erased"
        advice_visible = role in SENSITIVE_ADVICE_ROLES and consent["status"] == "granted" \
            and version_row["status"] != "erased"

        intake_fields = json.loads(version_row["intake_fields_json"])
        if intake_visible:
            intake_value = json.loads(version_row["intake_json"]) if version_row["intake_json"] else None
        else:
            intake_value = None
        intake_hidden_reason = None
        if not intake_visible:
            if version_row["status"] == "erased" or consent["status"] != "granted":
                intake_hidden_reason = "问询摘要已因授权撤回而擦除"
            elif role == "auditor":
                intake_hidden_reason = "审计员角色只能核对结构与哈希，不展示健康信息"
            else:
                intake_hidden_reason = "当前角色无权查看问询摘要"

        sections = [self._render_section(connection, actor, row, version_row, advice_visible)
                    for row in self._load_sections(connection, version_row["version_id"])]
        is_effective, eff_reasons, ineff_reasons = self._version_effectiveness(
            connection, version_row, consultation)

        disclosed_fields: list[str] = []
        if intake_visible:
            disclosed_fields.extend(f"intake.{f}" for f in intake_fields)
        if advice_visible:
            disclosed_fields.extend(f"advice.{s['section_id']}" for s in sections)
        if record_access:
            self._audit(connection, actor_id=actor["actor_id"], action="advice.accessed",
                        resource_type="advice_version", resource_id=version_row["version_id"],
                        detail={"consultation_id": version_row["consultation_id"],
                                "version_id": version_row["version_id"],
                                "purpose": purpose, "fields_disclosed": disclosed_fields,
                                "consent_id": consent["consent_id"],
                                "consent_status_at_access": consent["status"]})

        return {
            "version_id": version_row["version_id"],
            "consultation_id": version_row["consultation_id"],
            "sequence": version_row["sequence"],
            "trigger": version_row["trigger_text"],
            "supplement_summary": version_row["supplement_summary"],
            "status": version_row["status"],
            "non_diagnosis_notice": version_row["non_diagnosis_notice"],
            "intake": intake_value,
            "intake_fields": intake_fields,
            "intake_hash": version_row["intake_hash"],
            "intake_hidden_reason": intake_hidden_reason,
            "consent": {
                "consent_id": consent["consent_id"], "status": consent["status"],
                "scope_hash": consent["scope_hash"],
                "withdrawn_at": consent["withdrawn_at"],
                "withdrawal_reason": consent["withdrawal_reason"],
            },
            "rule_snapshot": {
                "rule_set_id": version_row["rule_set_id"], "version": version_row["rule_version"],
                "content_hash": version_row["rule_content_hash"],
            },
            "sections": sections,
            "lifecycle": {
                "created_by": version_row["created_by"], "created_at": version_row["created_at"],
                "published_at": version_row["published_at"], "published_by": version_row["published_by"],
                "superseded_at": version_row["superseded_at"],
                "superseded_reason": version_row["superseded_reason"],
                "superseded_by_version": version_row["superseded_by_version"],
                "content_erased_at": version_row["content_erased_at"],
            },
            "effective": is_effective,
            "effective_reasons": eff_reasons,
            "ineffective_reasons": ineff_reasons,
            "viewer_role": role,
        }

    def get_version(self, *, actor_id: str, version_id: str, purpose: str = "review") -> dict[str, Any]:
        purpose = self._text(purpose, "purpose", 100)
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *READ_ROLES)
            version = self._load_version(connection, version_id)
            return self._render_version(connection, actor, version, record_access=True, purpose=purpose)

    def get_consultation(self, *, actor_id: str, consultation_id: str, purpose: str = "review") -> dict[str, Any]:
        purpose = self._text(purpose, "purpose", 100)
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *READ_ROLES)
            consultation = connection.execute(
                "SELECT * FROM consultations WHERE consultation_id=?", (consultation_id,)
            ).fetchone()
            if consultation is None:
                raise NotFoundError("辨识会话不存在")
            self._same_org(actor, consultation["organization_id"])
            consent = self._load_consent(connection, consultation["consent_id"])
            versions = connection.execute(
                "SELECT * FROM consultation_versions WHERE consultation_id=? ORDER BY sequence",
                (consultation_id,),
            ).fetchall()
            rendered_versions = [
                self._render_version(connection, actor, row, record_access=True, purpose=purpose)
                for row in versions
            ]
            self._audit(connection, actor_id=actor["actor_id"], action="consultation.accessed",
                        resource_type="consultation", resource_id=consultation_id,
                        detail={"purpose": purpose, "version_count": len(versions),
                                "consent_id": consent["consent_id"],
                                "consent_status_at_access": consent["status"]})
            participant = consultation["participant_id"]
            if actor["role"] == "auditor":
                participant = MASKED
            return {
                "consultation_id": consultation_id,
                "organization_id": consultation["organization_id"],
                "participant_id": participant,
                "site_id": consultation["site_id"],
                "consent_id": consultation["consent_id"],
                "current_version_id": consultation["current_version_id"],
                "created_at": consultation["created_at"],
                "versions": rendered_versions,
                "viewer_role": actor["role"],
            }

    def explain_advice(self, *, actor_id: str, section_id: str, purpose: str = "explain") -> dict[str, Any]:
        """说明某条建议为什么生效或失效（授权、规则快照、签署、版本生命周期）。"""

        purpose = self._text(purpose, "purpose", 100)
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *READ_ROLES)
            section = connection.execute(
                "SELECT * FROM advice_sections WHERE section_id=?", (section_id,)
            ).fetchone()
            if section is None:
                raise NotFoundError("建议段落不存在")
            version = self._load_version(connection, section["version_id"])
            consultation = connection.execute(
                "SELECT * FROM consultations WHERE consultation_id=?", (version["consultation_id"],)
            ).fetchone()
            self._same_org(actor, consultation["organization_id"])
            consent = self._load_consent(connection, version["consent_id"])
            rule = connection.execute(
                "SELECT * FROM rule_versions WHERE rule_set_id=? AND version=?",
                (version["rule_set_id"], version["rule_version"]),
            ).fetchone()
            sigs = self._load_signatures(connection, section_id)
            expected_hash = digest(self._signature_material(section, version))
            signatures = [{
                "signer_id": s["signer_id"], "signed_at": s["signed_at"],
                "signature_hash": s["signature_hash"],
                "signature_matches_current_content": s["signature_hash"] == expected_hash,
            } for s in sigs]
            is_effective, eff_reasons, ineff_reasons = self._version_effectiveness(
                connection, version, consultation)
            section_effective = is_effective and bool(signatures_match(sigs, expected_hash))
            if is_effective and not signatures_match(sigs, expected_hash):
                ineff_reasons = ineff_reasons + ["该段落签名与当前内容不匹配"]
            self._audit(connection, actor_id=actor["actor_id"], action="advice.explained",
                        resource_type="advice_section", resource_id=section_id,
                        detail={"version_id": version["version_id"], "purpose": purpose,
                                "consent_status_at_access": consent["status"]})
            return {
                "section_id": section_id,
                "kind": section["kind"],
                "expert_id": section["expert_id"],
                "content_hash": section["content_hash"],
                "rule_refs": json.loads(section["rule_refs_json"]),
                "effective": section_effective,
                "effective_reasons": eff_reasons,
                "ineffective_reasons": ineff_reasons,
                "version": {
                    "version_id": version["version_id"], "sequence": version["sequence"],
                    "status": version["status"],
                    "superseded_reason": version["superseded_reason"],
                    "superseded_by_version": version["superseded_by_version"],
                    "content_erased_at": version["content_erased_at"],
                },
                "rule_snapshot": {
                    "rule_set_id": version["rule_set_id"], "version": version["rule_version"],
                    "content_hash": version["rule_content_hash"],
                    "rule_status_now": rule["status"] if rule else "missing",
                    "content_hash_matches": bool(rule and rule["content_hash"] == version["rule_content_hash"]),
                    "retired_at": rule["retired_at"] if rule else None,
                },
                "consent_status": {
                    "consent_id": consent["consent_id"], "status": consent["status"],
                    "scope_hash": consent["scope_hash"],
                    "scope_hash_matches_version": consent["scope_hash"] == version["consent_scope_hash"],
                    "withdrawn_at": consent["withdrawn_at"],
                },
                "signatures": signatures,
                "viewer_role": actor["role"],
            }

    # ------------------------------------------------------------------ 访问留痕

    def list_access_records(self, *, actor_id: str, consultation_id: str | None = None) -> dict[str, Any]:
        """从审计链提取敏感信息访问事实；撤回授权后这些事实仍然可查。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            events = connection.execute(
                "SELECT * FROM audit_events WHERE action IN ('advice.accessed','consultation.accessed') "
                "ORDER BY sequence"
            ).fetchall()
            items = []
            for event in events:
                detail = json.loads(event["detail_json"])
                if consultation_id and detail.get("consultation_id") != consultation_id:
                    continue
                if actor["role"] != "admin":
                    consultation = connection.execute(
                        "SELECT organization_id FROM consultations WHERE consultation_id=?",
                        (detail.get("consultation_id"),),
                    ).fetchone()
                    if consultation is None or consultation["organization_id"] != actor["organization_id"]:
                        continue
                items.append({
                    "sequence": event["sequence"], "actor_id": event["actor_id"],
                    "action": event["action"], "resource_id": event["resource_id"],
                    "consultation_id": detail.get("consultation_id"),
                    "version_id": event["resource_id"] if event["action"] == "advice.accessed"
                    else detail.get("version_id"),
                    "purpose": detail.get("purpose"),
                    "fields_disclosed": detail.get("fields_disclosed", []),
                    "consent_status_at_access": detail.get("consent_status_at_access"),
                    "accessed_at": event["occurred_at"],
                })
            return {"items": items}


def signatures_match(signatures, expected_hash: str) -> bool:
    return bool(signatures) and all(s["signature_hash"] == expected_hash for s in signatures)
