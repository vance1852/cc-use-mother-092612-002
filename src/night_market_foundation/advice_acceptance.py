"""建议留痕服务的离线端到端验收。

在临时 SQLite 数据库中走通完整生命周期，并对关键可复核性质做断言：
授权闸门、规则快照、分段签署、发布重校验、版本留痕、角色红acted、
撤回后停用内容但保留审计事实。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .advice import AdviceService
from .clock import FixedClock
from .storage import Database


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "advice_acceptance.sqlite3")
        service = AdviceService(database, FixedClock(datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)))

        # 基础建档
        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="org-001", name="夜市示范机构")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="op", actor_id="admin-001", new_actor_id="op-001",
                               display_name="现场操作员", role="operator", organization_id="org-001")
        service.register_actor(request_id="rv-a", actor_id="admin-001", new_actor_id="rv-a",
                               display_name="养生专家甲", role="reviewer", organization_id="org-001")
        service.register_actor(request_id="rv-b", actor_id="admin-001", new_actor_id="rv-b",
                               display_name="风险评估专家乙", role="reviewer", organization_id="org-001")
        service.register_actor(request_id="au", actor_id="admin-001", new_actor_id="au-001",
                               display_name="审计员", role="auditor", organization_id="org-001")
        service.register_site(request_id="site", actor_id="op-001", site_id="site-001",
                              organization_id="org-001", name="体质辨识摊位",
                              timezone_name="Asia/Shanghai")

        # 规则版本 v1 生效
        service.register_rule_version(
            request_id="rule-v1", actor_id="admin-001", rule_version_id="rule-2026-1",
            site_id="site-001", version_label="2026 年三季度版",
            snapshot={"entries": {"tanshi": "少食生冷油腻，规律作息"},
                      "notice": "文化体验提示口径"},
            effective_from="2026-07-01T00:00:00Z")

        # 参与者授权：只授权最少必要字段
        service.grant_consent(
            request_id="consent", actor_id="op-001", consent_id="consent-001",
            site_id="site-001", participant_ref="visitor-001",
            scope={"fields": ["constitution", "sleep", "appetite"],
                   "purposes": ["constitution_advice"]})
        service.create_case(request_id="case", actor_id="op-001", case_id="case-001",
                            site_id="site-001", participant_ref="visitor-001",
                            consent_id="consent-001")

        # v1 起草、两位专家分段签署、发布
        service.draft_version(
            request_id="v1-draft", actor_id="rv-a", case_id="case-001",
            inquiry_summary={"constitution": "偏痰湿", "sleep": "入睡偏晚"},
            rule_version_id="rule-2026-1")
        service.add_section(request_id="v1-life", actor_id="rv-a", case_id="case-001",
                            version_no=1, kind="lifestyle",
                            content="建议少食生冷油腻，晚上 11 点前休息，适度活动。")
        service.add_section(request_id="v1-risk", actor_id="rv-b", case_id="case-001",
                            version_no=1, kind="medical_risk",
                            content="若疲倦、胸闷持续两周以上，请至正规医疗机构就诊。")
        life_id = _section_id(database, "case-001", 1, "lifestyle")
        risk_id = _section_id(database, "case-001", 1, "medical_risk")
        service.sign_section(request_id="v1-sign-life", actor_id="rv-a", case_id="case-001",
                             version_no=1, section_id=life_id)
        service.sign_section(request_id="v1-sign-risk", actor_id="rv-b", case_id="case-001",
                             version_no=1, section_id=risk_id)
        service.publish_version(request_id="v1-publish", actor_id="rv-a",
                                case_id="case-001", version_no=1)

        # 不同角色视角
        expert_view = service.view_version(actor_id="rv-a", case_id="case-001", version_no=1)
        operator_view = service.view_version(actor_id="op-001", case_id="case-001", version_no=1)
        auditor_view = service.view_version(actor_id="au-001", case_id="case-001", version_no=1)
        assert expert_view["redaction_level"] == "full"
        assert operator_view["redaction_level"] == "inquiry_redacted"
        assert auditor_view["redaction_level"] == "metadata_only"
        assert expert_view["nature"] == "cultural_experience"
        assert "不属于医学诊断" in expert_view["diagnostic_boundary"]

        # 次日规则换版；参与者补充过敏史 → 生成复核版本 v2
        service.register_rule_version(
            request_id="rule-v2", actor_id="admin-001", rule_version_id="rule-2026-2",
            site_id="site-001", version_label="2026 年四季度版",
            snapshot={"entries": {"tanshi": "少食生冷油腻，关注食材过敏"},
                      "notice": "文化体验提示口径"})
        service.supersede_rule_version(
            request_id="rule-v1-off", actor_id="admin-001",
            rule_version_id="rule-2026-1", effective_to="2026-10-01T00:00:00Z")
        service.draft_version(
            request_id="v2-draft", actor_id="rv-a", case_id="case-001",
            inquiry_summary={"constitution": "痰湿为主", "sleep": "入睡偏晚",
                             "appetite": "一般，食芒果后口唇发痒"},
            rule_version_id="rule-2026-2", change_reason_code="supplementary_info",
            change_reason="参与者补充食欲及芒果过敏史")
        service.add_section(request_id="v2-life", actor_id="rv-a", case_id="case-001",
                            version_no=2, kind="lifestyle",
                            content="延续作息建议；芒果及易致敏食材先少量尝试。")
        service.add_section(request_id="v2-risk", actor_id="rv-b", case_id="case-001",
                            version_no=2, kind="medical_risk",
                            content="若食用后出现皮疹、呼吸困难，立即停用并急诊就医。")
        life2 = _section_id(database, "case-001", 2, "lifestyle")
        risk2 = _section_id(database, "case-001", 2, "medical_risk")
        service.sign_section(request_id="v2-sign-life", actor_id="rv-a", case_id="case-001",
                             version_no=2, section_id=life2)
        service.sign_section(request_id="v2-sign-risk", actor_id="rv-b", case_id="case-001",
                             version_no=2, section_id=risk2)
        service.publish_version(request_id="v2-publish", actor_id="rv-a",
                                case_id="case-001", version_no=2)

        case = service.get_case(actor_id="rv-a", case_id="case-001")
        assert case["current_version_no"] == 2
        old_explain = service.explain_version(actor_id="au-001", case_id="case-001", version_no=1)
        new_explain = service.explain_version(actor_id="au-001", case_id="case-001", version_no=2)
        assert not old_explain["effective"]
        assert old_explain["ineffective_reason"]["code"] == "superseded_by_new_review"
        assert old_explain["ineffective_reason"]["by_version"] == 2
        assert new_explain["effective"]
        assert new_explain["rule_basis"]["rule_version_id"] == "rule-2026-2"

        # 参与者撤回授权：当前版本立即作废，内容全部红acted
        service.withdraw_consent(request_id="withdraw", actor_id="op-001",
                                 consent_id="consent-001", reason="参与者活动结束后撤回授权")
        invalidated = service.explain_version(actor_id="au-001", case_id="case-001",
                                              version_no=2)
        assert invalidated["status"] == "invalidated"
        assert invalidated["ineffective_reason"]["code"] == "consent_withdrawn"
        for viewer in ("rv-a", "op-001", "au-001"):
            view = service.view_version(actor_id=viewer, case_id="case-001", version_no=2)
            assert view["redaction_level"] == "withdrawn"
            assert all(section["content"] is None for section in view["sections"])

        # 审计事实保留且哈希链完整（含撤回前的访问记录）
        events = service.audit_events()
        actions = {event["action"] for event in events}
        assert {"advice.accessed", "advice.published", "advice.version_superseded",
                "advice.version_invalidated", "consent.granted",
                "consent.withdrawn"} <= actions
        valid, event_count = service.verify_audit()
        assert valid

        database.close()
        return {"status": "ok", "current_version": case["current_version_no"],
                "history_versions": len(case["versions"]),
                "redaction_levels": {"expert": expert_view["redaction_level"],
                                     "operator": operator_view["redaction_level"],
                                     "auditor": auditor_view["redaction_level"]},
                "audit_events": event_count, "audit_valid": valid}


def _section_id(database: Database, case_id: str, version_no: int, kind: str) -> str:
    return database.connection.execute(
        "SELECT section_id FROM advice_sections WHERE case_id=? AND version_no=? AND kind=?",
        (case_id, version_no, kind)).fetchone()["section_id"]


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
