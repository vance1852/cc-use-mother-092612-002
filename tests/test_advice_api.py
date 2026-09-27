import unittest

from night_market_foundation.advice import AdviceService
from night_market_foundation.api import route
from night_market_foundation.clock import FixedClock
from datetime import datetime, timezone
from night_market_foundation.storage import Database


def headers(actor):
    return {"X-Actor-Id": actor}


class AdviceApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = AdviceService(
            self.database, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="活动机构")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rv1", actor_id="a1", new_actor_id="rv1",
                                    display_name="专家甲", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="rv2", actor_id="a1", new_actor_id="rv2",
                                    display_name="专家乙", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="站点", timezone_name="Asia/Shanghai")
        self.service.register_rule_version(
            request_id="rule1", actor_id="a1", rule_version_id="r1", site_id="s1",
            version_label="2026-v1", snapshot={"entries": {"tanshi": "少食生冷"}},
            effective_from="2026-09-01T00:00:00Z")
        self.service.grant_consent(
            request_id="consent1", actor_id="op1", consent_id="c1", site_id="s1",
            participant_ref="p1",
            scope={"fields": ["constitution", "sleep"], "purposes": ["constitution_advice"]})
        self.service.create_case(request_id="case1", actor_id="op1", case_id="case1",
                                 site_id="s1", participant_ref="p1", consent_id="c1")
        self.service.draft_version(
            request_id="v1d", actor_id="rv1", case_id="case1",
            inquiry_summary={"constitution": "偏痰湿", "sleep": "偏晚"}, rule_version_id="r1")
        self._section("v1ls", "rv1", 1, "lifestyle", "规律作息，少食生冷。")
        self._section("v1mr", "rv2", 1, "medical_risk", "症状持续请及时就医。")
        self._sign("v1sg1", "rv1", 1, "lifestyle")
        self._sign("v1sg2", "rv2", 1, "medical_risk")
        self.service.publish_version(request_id="v1pub", actor_id="rv1",
                                     case_id="case1", version_no=1)

    def tearDown(self):
        self.database.close()

    def _section(self, request_id, actor, version_no, kind, content):
        status, payload = route(self.service, "POST", "/advice/sections",
                                {"request_id": request_id, "case_id": "case1",
                                 "version_no": version_no, "kind": kind, "content": content},
                                headers(actor))
        self.assertEqual(201, status, payload)
        return payload["resource_id"]

    def _sign(self, request_id, actor, version_no, kind):
        section_id = self.database.connection.execute(
            "SELECT section_id FROM advice_sections WHERE case_id='case1' AND version_no=? AND kind=?",
            (version_no, kind)).fetchone()["section_id"]
        status, payload = route(self.service, "POST", "/advice/sections/sign",
                                {"request_id": request_id, "case_id": "case1",
                                 "version_no": version_no, "section_id": section_id},
                                headers(actor))
        self.assertEqual(201, status, payload)

    def test_reviewer_sees_full_inquiry_over_http(self):
        status, payload = route(self.service, "GET",
                                "/advice/versions?case_id=case1&version_no=1",
                                None, headers("rv1"))
        self.assertEqual(200, status)
        self.assertEqual("full", payload["redaction_level"])
        self.assertEqual("偏痰湿", payload["inquiry_summary"]["constitution"])
        self.assertEqual("cultural_experience", payload["nature"])

    def test_operator_and_auditor_see_redacted_sensitive_fields(self):
        status, operator = route(self.service, "GET",
                                 "/advice/versions?case_id=case1&version_no=1",
                                 None, headers("op1"))
        self.assertEqual(200, status)
        self.assertEqual("inquiry_redacted", operator["redaction_level"])
        self.assertNotIn("偏痰湿", str(operator["inquiry_summary"]))
        # 建议正文对现场操作员可见，否则无法口头转达
        self.assertTrue(all(s["content"] for s in operator["sections"]))

        status, auditor = route(self.service, "GET",
                                "/advice/versions?case_id=case1&version_no=1",
                                None, headers("au1"))
        self.assertEqual("metadata_only", auditor["redaction_level"])
        self.assertTrue(all(s["content"] is None for s in auditor["sections"]))

    def test_explain_endpoint_states_effective_reason(self):
        status, payload = route(self.service, "GET",
                                "/advice/versions/explain?case_id=case1&version_no=1",
                                None, headers("au1"))
        self.assertEqual(200, status)
        self.assertTrue(payload["effective"])
        self.assertEqual("published", payload["effective_reason"]["code"])
        self.assertTrue(payload["effective_reason"]["all_signatures_valid"])
        self.assertEqual("r1", payload["rule_basis"]["rule_version_id"])

    def test_superseded_version_explains_ineffective_reason(self):
        self.service.draft_version(
            request_id="v2d", actor_id="rv1", case_id="case1",
            inquiry_summary={"constitution": "痰湿兼气虚", "sleep": "偏晚"},
            rule_version_id="r1", change_reason_code="supplementary_info",
            change_reason="补充食欲与过敏史")
        self._section("v2ls", "rv1", 2, "lifestyle", "增加健脾食材。")
        self._section("v2mr", "rv2", 2, "medical_risk", "出现过敏反应立即就医。")
        self._sign("v2sg1", "rv1", 2, "lifestyle")
        self._sign("v2sg2", "rv2", 2, "medical_risk")
        status, payload = route(self.service, "POST", "/advice/versions/publish",
                                {"request_id": "v2pub", "case_id": "case1", "version_no": 2},
                                headers("rv1"))
        self.assertEqual(201, status)

        status, old = route(self.service, "GET",
                            "/advice/versions/explain?case_id=case1&version_no=1",
                            None, headers("au1"))
        self.assertEqual(200, status)
        self.assertFalse(old["effective"])
        self.assertEqual("superseded_by_new_review", old["ineffective_reason"]["code"])
        self.assertEqual(2, old["ineffective_reason"]["by_version"])
        self.assertEqual("supplementary_info",
                         old["ineffective_reason"]["change_reason_code"])

    def test_withdrawal_redacts_for_all_roles_over_http(self):
        status, _ = route(self.service, "POST", "/advice/consents/withdraw",
                          {"request_id": "wd", "consent_id": "c1", "reason": "参与者撤回"},
                          headers("op1"))
        self.assertEqual(201, status)
        for actor in ("rv1", "op1", "au1", "a1"):
            status, payload = route(self.service, "GET",
                                    "/advice/versions?case_id=case1&version_no=1",
                                    None, headers(actor))
            self.assertEqual(200, status)
            self.assertEqual("withdrawn", payload["redaction_level"])
            self.assertTrue(all(s["content"] is None for s in payload["sections"]))
        # 审计员仍可核验访问事实
        status, audit = route(self.service, "GET", "/audit-events?after_sequence=0",
                              None, headers("au1"))
        self.assertEqual(200, status)
        self.assertTrue(any(e["action"] == "advice.accessed" for e in audit["items"]))

    def test_recording_outside_scope_is_403(self):
        status, payload = route(self.service, "POST", "/advice/versions",
                                {"request_id": "overreach", "case_id": "case1",
                                 "inquiry_summary": {"constitution": "偏痰湿",
                                                     "id_number": "110101..."},
                                 "rule_version_id": "r1",
                                 "change_reason_code": "supplementary_info",
                                 "change_reason": "越权字段"},
                                headers("rv1"))
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])


if __name__ == "__main__":
    unittest.main()
