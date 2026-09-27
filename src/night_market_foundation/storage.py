"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_versions (
    rule_set_id TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('effective','retired')),
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    retired_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (rule_set_id, version)
);
CREATE TABLE IF NOT EXISTS consents (
    consent_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    participant_id TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('granted','withdrawn')),
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT,
    withdrawn_at TEXT,
    withdrawal_reason TEXT
);
CREATE TABLE IF NOT EXISTS consultations (
    consultation_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    participant_id TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    consent_id TEXT NOT NULL REFERENCES consents(consent_id),
    current_version_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consultation_versions (
    version_id TEXT PRIMARY KEY,
    consultation_id TEXT NOT NULL REFERENCES consultations(consultation_id),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    trigger_text TEXT NOT NULL CHECK(trigger_text IN ('initial','supplement')),
    supplement_summary TEXT,
    intake_json TEXT,
    intake_fields_json TEXT NOT NULL,
    intake_hash TEXT NOT NULL,
    consent_id TEXT NOT NULL,
    consent_scope_hash TEXT NOT NULL,
    rule_set_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    rule_content_hash TEXT NOT NULL,
    non_diagnosis_notice TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','published','superseded','erased')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT,
    superseded_at TEXT,
    superseded_reason TEXT,
    superseded_by_version TEXT,
    content_erased_at TEXT,
    UNIQUE(consultation_id, sequence)
);
CREATE TABLE IF NOT EXISTS advice_sections (
    section_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES consultation_versions(version_id),
    expert_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('lifestyle','risk_alert')),
    title TEXT,
    content_json TEXT,
    content_hash TEXT NOT NULL,
    rule_refs_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS advice_signatures (
    signature_id TEXT PRIMARY KEY,
    section_id TEXT NOT NULL REFERENCES advice_sections(section_id),
    version_id TEXT NOT NULL REFERENCES consultation_versions(version_id),
    signer_id TEXT NOT NULL,
    signer_display_name TEXT NOT NULL,
    signature_hash TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    UNIQUE(section_id, signer_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
