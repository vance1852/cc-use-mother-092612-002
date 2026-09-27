"""建议留痕与复核模块在边界上使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuleVersion:
    """一份在指定时间生效的养生规则版本。"""

    rule_set_id: str
    version: str
    status: str
    content: dict[str, Any]
    content_hash: str
    effective_from: str
    retired_at: str | None
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Consent:
    """参与者授权的不可变事实（撤回只改状态，不删除记录）。"""

    consent_id: str
    organization_id: str
    participant_id: str
    scopes: tuple[str, ...]
    fields: tuple[str, ...]
    scope_hash: str
    status: str
    granted_by: str
    granted_at: str
    expires_at: str | None
    withdrawn_at: str | None
    withdrawal_reason: str | None


@dataclass(frozen=True)
class AdviceSection:
    """某位专家负责撰写的一段建议。"""

    section_id: str
    version_id: str
    expert_id: str
    kind: str
    title: str
    content: dict[str, Any]
    content_hash: str
    rule_refs: tuple[str, ...]
    signatures: tuple["AdviceSignature", ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AdviceSignature:
    """专家对本人负责段落的签署事实。"""

    signature_id: str
    section_id: str
    signer_id: str
    signer_display_name: str
    signature_hash: str
    signed_at: str


@dataclass(frozen=True)
class AdviceVersion:
    """一次可复核的建议版本；历史版本只读保留。"""

    version_id: str
    consultation_id: str
    sequence: int
    trigger: str
    supplement_summary: str | None
    intake: dict[str, Any] | None
    intake_fields: tuple[str, ...]
    intake_hash: str
    consent_id: str
    consent_scope_hash: str
    rule_set_id: str
    rule_version: str
    rule_content_hash: str
    non_diagnosis_notice: str
    status: str
    created_by: str
    created_at: str
    published_at: str | None
    published_by: str | None
    superseded_at: str | None
    superseded_reason: str | None
    superseded_by_version: str | None
    content_erased_at: str | None
    sections: tuple[AdviceSection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Consultation:
    """同一位参与者一次体质辨识会话及其版本链。"""

    consultation_id: str
    organization_id: str
    participant_id: str
    site_id: str
    consent_id: str
    current_version_id: str | None
    created_by: str
    created_at: str
    versions: tuple[AdviceVersion, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AccessRecord:
    """一次敏感信息访问的留痕事实，撤回授权后仍然保留。"""

    access_id: str
    consultation_id: str
    version_id: str | None
    actor_id: str
    purpose: str
    scope: str
    fields: tuple[str, ...]
    consent_status_at_access: str
    consent_id: str
    accessed_at: str
