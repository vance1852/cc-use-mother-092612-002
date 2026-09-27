"""运行基础服务与建议留痕复核模块的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .advice_service import AdviceService
from .clock import FixedClock
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行登记链与建议留痕复核链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        advice = AdviceService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范活动机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="活动负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="复核员", role="reviewer", organization_id="org-001")
        service.register_actor(request_id="req-expert-life", actor_id="admin-001", new_actor_id="expert-life",
                               display_name="养生专家", role="expert", organization_id="org-001")
        service.register_actor(request_id="req-expert-risk", actor_id="admin-001", new_actor_id="expert-risk",
                               display_name="风险提示专家", role="expert", organization_id="org-001")
        service.register_actor(request_id="req-auditor", actor_id="admin-001", new_actor_id="auditor-001",
                               display_name="审计员", role="auditor", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号活动站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="organizer_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="organizer_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 规则版本：专家依据当时生效的规则出具建议。
        advice.register_rule_version(request_id="req-rule-v1", actor_id="reviewer-001",
                                     rule_set_id="tcm-life", version="2026.1",
                                     content={"items": [
                                         {"id": "dampness-diet", "text": "痰湿体质宜清淡饮食"},
                                         {"id": "risk-chest-tight", "text": "胸闷持续应及时就医"},
                                     ]})

        # 授权先行：只登记用途与最少必要字段。
        consent_one = advice.grant_consent(
            request_id="req-consent-1", actor_id="operator-001", participant_id="participant-001",
            scopes=["intake_record", "advice_generation", "advice_disclosure"],
            fields=["constitution", "symptoms"])["resource_id"]
        consultation_id = advice.create_consultation(
            request_id="req-consultation", actor_id="operator-001", site_id="site-001",
            consent_id=consent_one)["resource_id"]

        # 首版建议：两位专家分签各自段落，复核员确认闸口后发布。
        version_one = advice.add_intake_version(
            request_id="req-version-1", actor_id="operator-001", consultation_id=consultation_id,
            intake={"constitution": "痰湿质", "symptoms": "午后困倦、舌苔厚腻"})["resource_id"]
        life = advice.add_section(actor_id="expert-life", version_id=version_one, kind="lifestyle",
                                  title="饮食起居", content={"text": "清淡饮食、规律作息"},
                                  rule_refs=["dampness-diet"])
        risk = advice.add_section(actor_id="expert-risk", version_id=version_one, kind="risk_alert",
                                  title="就医提示", content={"text": "若持续胸闷请及时就医"},
                                  rule_refs=["risk-chest-tight"])
        advice.sign_section(actor_id="expert-life", section_id=life["section_id"])
        advice.sign_section(actor_id="expert-risk", section_id=risk["section_id"])
        advice.publish_version(request_id="req-publish-1", actor_id="reviewer-001",
                               version_id=version_one)

        # 审计员视角：只能看到结构与哈希，健康内容隐藏。
        auditor_view = advice.get_version(actor_id="auditor-001", version_id=version_one)
        auditor_masked = auditor_view["intake"] is None and all(
            section["content"] is None for section in auditor_view["sections"])

        # 补充过敏史：取得覆盖新字段的新授权，生成第二版，旧版留痕但失效。
        consent_two = advice.grant_consent(
            request_id="req-consent-2", actor_id="operator-001", participant_id="participant-001",
            scopes=["intake_record", "advice_generation", "advice_disclosure"],
            fields=["constitution", "symptoms", "allergy"])["resource_id"]
        version_two = advice.add_intake_version(
            request_id="req-version-2", actor_id="operator-001", consultation_id=consultation_id,
            trigger="supplement", supplement_summary="参与者补充花粉过敏史，移除芳香类起居建议",
            intake={"constitution": "痰湿质", "symptoms": "午后困倦", "allergy": "花粉"},
            consent_id=consent_two)["resource_id"]
        life_two = advice.add_section(actor_id="expert-life", version_id=version_two, kind="lifestyle",
                                      title="饮食起居（修订）",
                                      content={"text": "清淡饮食，规避芳香类过敏原"},
                                      rule_refs=["dampness-diet"])
        risk_two = advice.add_section(actor_id="expert-risk", version_id=version_two, kind="risk_alert",
                                      title="就医提示", content={"text": "胸闷或急性过敏反应请就医"},
                                      rule_refs=["risk-chest-tight"])
        advice.sign_section(actor_id="expert-life", section_id=life_two["section_id"])
        advice.sign_section(actor_id="expert-risk", section_id=risk_two["section_id"])
        advice.publish_version(request_id="req-publish-2", actor_id="reviewer-001",
                               version_id=version_two)

        old_view = advice.get_version(actor_id="reviewer-001", version_id=version_one)
        new_view = advice.get_version(actor_id="reviewer-001", version_id=version_two)
        explanation = advice.explain_advice(actor_id="reviewer-001", section_id=life["section_id"])

        # 撤回首版授权：旧版内容擦除、停止生效；访问审计事实保留。
        advice.withdraw_consent(request_id="req-withdraw", actor_id="operator-001",
                                consent_id=consent_one, reason="参与者活动结束后撤回")
        erased_view = advice.get_version(actor_id="reviewer-001", version_id=version_one)
        access = advice.list_access_records(actor_id="auditor-001", consultation_id=consultation_id)

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {
            "status": "ok",
            "records": len(records),
            "audit_events": event_count,
            "audit_valid": valid,
            "first_replayed": first.replayed,
            "second_replayed": replay.replayed,
            "auditor_sensitive_fields_masked": auditor_masked,
            "old_version_status": old_view["status"],
            "old_version_effective": old_view["effective"],
            "new_version_effective": new_view["effective"],
            "new_version_current": new_view["lifecycle"] is not None,
            "explained_old_advice_ineffective": explanation["effective"] is False,
            "replacement_reason_recorded": "过敏" in (old_view["lifecycle"]["superseded_reason"] or ""),
            "erased_after_withdrawal": erased_view["status"] == "erased"
            and erased_view["intake"] is None,
            "access_facts_retained_after_withdrawal": len(access["items"]) > 0
            and {item["consent_status_at_access"] for item in access["items"]} >= {"granted", "withdrawn"},
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (
        result["status"] == "ok"
        and result["audit_valid"]
        and result["auditor_sensitive_fields_masked"]
        and result["old_version_status"] == "superseded"
        and result["old_version_effective"] is False
        and result["new_version_effective"]
        and result["explained_old_advice_ineffective"]
        and result["replacement_reason_recorded"]
        and result["erased_after_withdrawal"]
        and result["access_facts_retained_after_withdrawal"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
