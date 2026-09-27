"""建议留痕与复核领域服务的自动化测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from night_market_foundation.advice_service import AdviceService
from night_market_foundation.clock import Clock
from night_market_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

RULES = {"items": [
    {"id": "dampness-diet", "text": "痰湿体质宜清淡饮食"},
    {"id": "dampness-sleep", "text": "建议规律作息，避免熬夜"},
    {"id": "risk-chest-tight", "text": "胸闷持续应及时就医"},
]}
SCOPES_ALL = ["intake_record", "advice_generation", "advice_disclosure"]


class MutableClock(Clock):
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


class AdviceFixture(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.advice = AdviceService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="夜市机构")
        for key, aid, name, role in [
            ("admin", "a1", "管理员", "admin"),
            ("op", "op1", "操作员", "operator"),
            ("rv", "rv1", "复核员", "reviewer"),
            ("ex1", "e1", "养生专家", "expert"),
            ("ex2", "e2", "风险专家", "expert"),
            ("au", "au1", "审计员", "auditor"),
        ]:
            self.service.register_actor(request_id=key, actor_id="bootstrap" if key == "admin" else "a1",
                                        new_actor_id=aid, display_name=name, role=role, organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="站点", timezone_name="Asia/Shanghai")
        self.advice.register_rule_version(request_id="rule1", actor_id="rv1",
                                          rule_set_id="tcm-life", version="2026.1", content=RULES)

    def tearDown(self):
        self.database.close()

    def grant(self, request_id="c1", fields=("constitution", "symptoms"), scopes=None, participant="p-001"):
        return self.advice.grant_consent(
            request_id=request_id, actor_id="op1", participant_id=participant,
            scopes=scopes or list(SCOPES_ALL), fields=list(fields),
        )["resource_id"]

    def open_consultation(self, consent_id, request_id="con1"):
        return self.advice.create_consultation(
            request_id=request_id, actor_id="op1", site_id="s1", consent_id=consent_id
        )["resource_id"]

    def draft_version(self, con_id, request_id="iv1", **kwargs):
        params = {"intake": {"constitution": "痰湿质", "symptoms": "困倦"}}
        params.update(kwargs)
        return self.advice.add_intake_version(
            request_id=request_id, actor_id="op1", consultation_id=con_id, **params
        )["resource_id"]

    def sign_full_version(self, version_id):
        life = self.advice.add_section(
            actor_id="e1", version_id=version_id, kind="lifestyle", title="饮食起居",
            content={"text": "清淡饮食、规律作息"}, rule_refs=["dampness-diet", "dampness-sleep"])
        risk = self.advice.add_section(
            actor_id="e2", version_id=version_id, kind="risk_alert", title="就医提示",
            content={"text": "持续胸闷请就医"}, rule_refs=["risk-chest-tight"])
        self.advice.sign_section(actor_id="e1", section_id=life["section_id"])
        self.advice.sign_section(actor_id="e2", section_id=risk["section_id"])
        return life["section_id"], risk["section_id"]


class AuthorizationTest(AdviceFixture):
    def test_only_operator_can_record_consent(self):
        for actor in ("e1", "au1", "rv1"):
            with self.assertRaises(PermissionDenied):
                self.advice.grant_consent(request_id=f"c-{actor}", actor_id=actor, participant_id="px",
                                          scopes=SCOPES_ALL, fields=["constitution"])

    def test_consent_scopes_and_fields_validated(self):
        with self.assertRaises(ValidationError):
            self.advice.grant_consent(request_id="bad-scope", actor_id="op1", participant_id="px",
                                      scopes=["unknown_scope"], fields=["constitution"])
        with self.assertRaises(ValidationError):
            self.advice.grant_consent(request_id="bad-fields", actor_id="op1", participant_id="px",
                                      scopes=SCOPES_ALL, fields=[])

    def test_intake_field_beyond_consent_is_rejected(self):
        consent_id = self.grant(fields=("constitution",))
        con_id = self.open_consultation(consent_id)
        with self.assertRaises(PermissionDenied):
            self.draft_version(con_id, intake={"constitution": "痰湿质", "symptoms": "困倦"})

    def test_withdrawn_consent_blocks_new_use(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        self.advice.withdraw_consent(request_id="w1", actor_id="op1", consent_id=consent_id)
        with self.assertRaises(PermissionDenied):
            self.advice.create_consultation(request_id="con2", actor_id="op1",
                                            site_id="s1", consent_id=consent_id)
        with self.assertRaises(PermissionDenied):
            self.advice.add_intake_version(request_id="iv-late", actor_id="op1",
                                           consultation_id=con_id,
                                           intake={"constitution": "x"})

    def test_expired_consent_blocks_recording(self):
        expires = (self.clock.now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        consent_id = self.advice.grant_consent(
            request_id="c-exp", actor_id="op1", participant_id="p-x",
            scopes=SCOPES_ALL, fields=["constitution"], expires_at=expires)["resource_id"]
        self.clock.advance(days=2)
        with self.assertRaises(PermissionDenied):
            self.open_consultation(consent_id, request_id="con-exp")

    def test_cannot_grant_already_expired_consent(self):
        past = (self.clock.now() - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        with self.assertRaises(ValidationError):
            self.advice.grant_consent(request_id="c-past", actor_id="op1", participant_id="p-y",
                                      scopes=SCOPES_ALL, fields=["constitution"], expires_at=past)


class RuleVersionTest(AdviceFixture):
    def test_only_reviewer_registers_rules(self):
        with self.assertRaises(PermissionDenied):
            self.advice.register_rule_version(request_id="r2bad", actor_id="op1",
                                              rule_set_id="x", version="1", content=RULES)
        with self.assertRaises(PermissionDenied):
            self.advice.register_rule_version(request_id="r3bad", actor_id="e1",
                                              rule_set_id="x", version="1", content=RULES)

    def test_new_version_retires_previous(self):
        self.advice.register_rule_version(request_id="r2", actor_id="rv1",
                                          rule_set_id="tcm-life", version="2026.2", content=RULES)
        old = self.advice.get_rule_version("rv1", "tcm-life", "2026.1")
        new = self.advice.get_rule_version("rv1", "tcm-life", "2026.2")
        self.assertEqual("retired", old["status"])
        self.assertEqual("effective", new["status"])

    def test_duplicate_version_conflicts(self):
        with self.assertRaises(ConflictError):
            self.advice.register_rule_version(request_id="r-dup", actor_id="rv1",
                                              rule_set_id="tcm-life", version="2026.1", content=RULES)

    def test_rule_reference_must_exist_in_snapshot(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        version_id = self.draft_version(con_id)
        with self.assertRaises(ValidationError):
            self.advice.add_section(actor_id="e1", version_id=version_id, kind="lifestyle",
                                    content={"text": "x"}, rule_refs=["does-not-exist"])

    def test_retired_rule_snapshot_blocks_publish_and_explains(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        version_id = self.draft_version(con_id)
        life, risk = self.sign_full_version(version_id)
        self.advice.retire_rule_version(request_id="rret", actor_id="rv1",
                                        rule_set_id="tcm-life", version="2026.1")
        with self.assertRaises(ConflictError):
            self.advice.publish_version(request_id="pub-blocked", actor_id="rv1",
                                        version_id=version_id)
        explanation = self.advice.explain_advice(actor_id="rv1", section_id=life)
        self.assertFalse(explanation["effective"])
        self.assertEqual("retired", explanation["rule_snapshot"]["rule_status_now"])
        self.assertTrue(any("规则快照" in reason for reason in explanation["ineffective_reasons"]))


class VersionChainTest(AdviceFixture):
    def _published_pair(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        v1 = self.draft_version(con_id, request_id="iv1")
        life1, risk1 = self.sign_full_version(v1)
        self.advice.publish_version(request_id="pub1", actor_id="rv1", version_id=v1)
        v2 = self.advice.add_intake_version(
            request_id="iv2", actor_id="op1", consultation_id=con_id, trigger="supplement",
            supplement_summary="参与者补充花粉过敏史，剔除芳香类建议",
            intake={"constitution": "痰湿质", "symptoms": "困倦", "allergy": "花粉"},
            consent_id=self.grant(request_id="c2", fields=("constitution", "symptoms", "allergy")),
        )["resource_id"]
        self.sign_full_version(v2)
        self.advice.publish_version(request_id="pub2", actor_id="rv1", version_id=v2)
        return con_id, v1, v2, life1

    def test_supplement_requires_existing_version_and_summary(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        with self.assertRaises(ValidationError):
            self.advice.add_intake_version(request_id="sup-first", actor_id="op1",
                                           consultation_id=con_id, trigger="supplement",
                                           supplement_summary="x",
                                           intake={"constitution": "痰湿质"})
        self.draft_version(con_id)
        with self.assertRaises(ConflictError):
            self.draft_version(con_id, request_id="iv-again")
        with self.assertRaises(ValidationError):
            self.advice.add_intake_version(request_id="sup-nosum", actor_id="op1",
                                           consultation_id=con_id, trigger="supplement",
                                           intake={"constitution": "x"})

    def test_supplement_creates_new_version_and_supersedes_old(self):
        con_id, v1, v2, _ = self._published_pair()
        old = self.advice.get_version(actor_id="rv1", version_id=v1)
        new = self.advice.get_version(actor_id="rv1", version_id=v2)
        self.assertEqual("superseded", old["status"])
        self.assertFalse(old["effective"])
        self.assertEqual(v2, old["lifecycle"]["superseded_by_version"])
        self.assertIn("过敏", old["lifecycle"]["superseded_reason"])
        self.assertEqual("published", new["status"])
        self.assertTrue(new["effective"])
        summary = self.advice.get_consultation(actor_id="rv1", consultation_id=con_id)
        self.assertEqual(v2, summary["current_version_id"])
        self.assertEqual([1, 2], [v["sequence"] for v in summary["versions"]])

    def test_historical_versions_remain_queryable(self):
        con_id, v1, v2, _ = self._published_pair()
        old = self.advice.get_version(actor_id="rv1", version_id=v1)
        self.assertEqual("痰湿质", old["intake"]["constitution"])
        self.assertEqual("2026.1", old["rule_snapshot"]["version"])

    def test_supplement_consent_must_belong_to_same_participant(self):
        consent_id = self.grant(participant="p-001")
        con_id = self.open_consultation(consent_id)
        v1 = self.draft_version(con_id)
        self.sign_full_version(v1)
        self.advice.publish_version(request_id="pub1", actor_id="rv1", version_id=v1)
        other = self.grant(request_id="c-other", participant="p-002",
                           fields=("constitution", "symptoms", "allergy"))
        with self.assertRaises(ValidationError):
            self.advice.add_intake_version(request_id="iv-cross", actor_id="op1",
                                           consultation_id=con_id, trigger="supplement",
                                           supplement_summary="他人授权",
                                           intake={"constitution": "x"}, consent_id=other)

    def test_request_id_replays_without_duplicate_version(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        first = self.draft_version(con_id, request_id="dup")
        second = self.draft_version(con_id, request_id="dup")
        self.assertEqual(first, second)
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM consultation_versions").fetchone()["c"]
        self.assertEqual(1, count)


class SignatureTest(AdviceFixture):
    def test_expert_only_signs_own_section(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        version_id = self.draft_version(con_id)
        section = self.advice.add_section(
            actor_id="e1", version_id=version_id, kind="lifestyle",
            content={"text": "x"}, rule_refs=["dampness-diet"])
        with self.assertRaises(PermissionDenied):
            self.advice.sign_section(actor_id="e2", section_id=section["section_id"])
        with self.assertRaises(PermissionDenied):
            self.advice.sign_section(actor_id="op1", section_id=section["section_id"])
        self.advice.sign_section(actor_id="e1", section_id=section["section_id"])
        with self.assertRaises(ConflictError):
            self.advice.sign_section(actor_id="e1", section_id=section["section_id"])

    def test_unsigned_section_blocks_publish(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        version_id = self.draft_version(con_id)
        section = self.advice.add_section(
            actor_id="e1", version_id=version_id, kind="lifestyle",
            content={"text": "x"}, rule_refs=["dampness-diet"])
        with self.assertRaises(ConflictError) as caught:
            self.advice.publish_version(request_id="pub", actor_id="rv1", version_id=version_id)
        self.assertIn("尚未获得签署", str(caught.exception))

    def test_deactivated_signer_blocks_publish(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        version_id = self.draft_version(con_id)
        life, risk = self.sign_full_version(version_id)
        self.database.connection.execute("UPDATE actors SET active=0 WHERE actor_id='e2'")
        with self.assertRaises(ConflictError) as caught:
            self.advice.publish_version(request_id="pub", actor_id="rv1", version_id=version_id)
        self.assertIn("e2", str(caught.exception))

    def test_publish_requires_disclosure_scope(self):
        consent_id = self.grant(scopes=["intake_record", "advice_generation"])
        con_id = self.open_consultation(consent_id)
        version_id = self.draft_version(con_id)
        self.sign_full_version(version_id)
        with self.assertRaises(ConflictError) as caught:
            self.advice.publish_version(request_id="pub", actor_id="rv1", version_id=version_id)
        self.assertIn("advice_disclosure", str(caught.exception))

    def test_only_reviewer_publishes(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        version_id = self.draft_version(con_id)
        self.sign_full_version(version_id)
        with self.assertRaises(PermissionDenied):
            self.advice.publish_version(request_id="pub", actor_id="op1", version_id=version_id)
        with self.assertRaises(PermissionDenied):
            self.advice.publish_version(request_id="pub2", actor_id="e1", version_id=version_id)


class WithdrawalErasureTest(AdviceFixture):
    def test_withdrawal_erases_content_but_keeps_audit_and_structure(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        v1 = self.draft_version(con_id)
        life, risk = self.sign_full_version(v1)
        self.advice.publish_version(request_id="pub1", actor_id="rv1", version_id=v1)
        self.advice.get_version(actor_id="rv1", version_id=v1, purpose="follow_up")
        self.advice.withdraw_consent(request_id="w1", actor_id="op1",
                                     consent_id=consent_id, reason="参与者撤回")
        view = self.advice.get_version(actor_id="rv1", version_id=v1)
        self.assertEqual("erased", view["status"])
        self.assertIsNone(view["intake"])
        for section in view["sections"]:
            self.assertIsNone(section["content"])
            self.assertIn("擦除", section["hidden_reason"])
            self.assertTrue(section["signatures"])  # 签署事实保留
        # 访问审计事实仍然可查，且记录了访问时授权状态
        records = self.advice.list_access_records(actor_id="au1", consultation_id=con_id)
        statuses = {item["consent_status_at_access"] for item in records["items"]}
        self.assertIn("granted", statuses)
        self.assertIn("withdrawn", statuses)
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)

    def test_section_cannot_be_added_under_withdrawn_consent(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        v1 = self.draft_version(con_id)
        self.advice.withdraw_consent(request_id="w1", actor_id="op1", consent_id=consent_id)
        with self.assertRaises(PermissionDenied):
            self.advice.add_section(actor_id="e1", version_id=v1, kind="lifestyle",
                                    content={"text": "x"}, rule_refs=["dampness-diet"])

    def test_double_withdraw_conflicts(self):
        consent_id = self.grant()
        self.advice.withdraw_consent(request_id="w1", actor_id="op1", consent_id=consent_id)
        with self.assertRaises(ConflictError):
            self.advice.withdraw_consent(request_id="w2", actor_id="op1", consent_id=consent_id)


class VisibilityTest(AdviceFixture):
    def setUp(self):
        super().setUp()
        consent_id = self.grant()
        self.con_id = self.open_consultation(consent_id)
        self.version_id = self.draft_version(self.con_id)
        self.life, self.risk = self.sign_full_version(self.version_id)
        self.advice.publish_version(request_id="pub1", actor_id="rv1", version_id=self.version_id)

    def test_auditor_sees_hashes_but_no_sensitive_content(self):
        view = self.advice.get_version(actor_id="au1", version_id=self.version_id)
        self.assertIsNone(view["intake"])
        self.assertIn("审计员", view["intake_hidden_reason"])
        self.assertTrue(view["intake_hash"])
        for section in view["sections"]:
            self.assertIsNone(section["content"])
            self.assertIn("无权", section["hidden_reason"])
            self.assertTrue(section["content_hash"])
        summary = self.advice.get_consultation(actor_id="au1", consultation_id=self.con_id)
        self.assertEqual("***", summary["participant_id"])

    def test_authorized_roles_see_content_while_granted(self):
        for actor in ("rv1", "op1", "e1"):
            view = self.advice.get_version(actor_id=actor, version_id=self.version_id)
            self.assertIsNotNone(view["intake"], actor)
            self.assertTrue(any(s["content"] for s in view["sections"]), actor)

    def test_auditor_cannot_access_other_organizations_records(self):
        self.service.register_organization(request_id="org2", actor_id="a1",
                                           organization_id="o2", name="其他机构")
        self.service.register_actor(request_id="au2", actor_id="a1", new_actor_id="au2",
                                    display_name="外机构审计员", role="auditor", organization_id="o2")
        records = self.advice.list_access_records(actor_id="au2")
        self.assertEqual([], records["items"])
        with self.assertRaises(PermissionDenied):
            self.advice.get_version(actor_id="au2", version_id=self.version_id)

    def test_non_diagnosis_notice_always_present(self):
        view = self.advice.get_version(actor_id="au1", version_id=self.version_id)
        self.assertIn("文化体验", view["non_diagnosis_notice"])
        self.assertIn("不构成疾病诊断", view["non_diagnosis_notice"])


class ExplainTest(AdviceFixture):
    def test_explain_draft_is_ineffective_with_reason(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        version_id = self.draft_version(con_id)
        section = self.advice.add_section(
            actor_id="e1", version_id=version_id, kind="lifestyle",
            content={"text": "x"}, rule_refs=["dampness-diet"])
        explanation = self.advice.explain_advice(actor_id="rv1", section_id=section["section_id"])
        self.assertFalse(explanation["effective"])
        self.assertTrue(any("草稿" in r for r in explanation["ineffective_reasons"]))

    def test_explain_published_advice_is_effective(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        version_id = self.draft_version(con_id)
        life, risk = self.sign_full_version(version_id)
        self.advice.publish_version(request_id="pub1", actor_id="rv1", version_id=version_id)
        explanation = self.advice.explain_advice(actor_id="rv1", section_id=life)
        self.assertTrue(explanation["effective"])
        self.assertTrue(any("发布闸口" in r for r in explanation["effective_reasons"]))
        self.assertTrue(any("规则快照" in r for r in explanation["effective_reasons"]))
        self.assertTrue(any("签署" in r for r in explanation["effective_reasons"]))
        self.assertTrue(explanation["consent_status"]["scope_hash_matches_version"])

    def test_explain_superseded_advice_gives_replacement_reason(self):
        consent_id = self.grant()
        con_id = self.open_consultation(consent_id)
        v1 = self.draft_version(con_id, request_id="iv1")
        life, risk = self.sign_full_version(v1)
        self.advice.publish_version(request_id="pub1", actor_id="rv1", version_id=v1)
        v2 = self.advice.add_intake_version(
            request_id="iv2", actor_id="op1", consultation_id=con_id, trigger="supplement",
            supplement_summary="补充过敏史后调整芳香类建议",
            intake={"constitution": "痰湿质", "symptoms": "困倦", "allergy": "花粉"},
            consent_id=self.grant(request_id="c2", fields=("constitution", "symptoms", "allergy")),
        )["resource_id"]
        self.sign_full_version(v2)
        self.advice.publish_version(request_id="pub2", actor_id="rv1", version_id=v2)
        explanation = self.advice.explain_advice(actor_id="rv1", section_id=life)
        self.assertFalse(explanation["effective"])
        self.assertIn("过敏", explanation["version"]["superseded_reason"])
        self.assertEqual(v2, explanation["version"]["superseded_by_version"])


if __name__ == "__main__":
    unittest.main()
