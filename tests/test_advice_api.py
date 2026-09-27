"""建议留痕与复核模块的 HTTP/JSON 边界测试。"""

import unittest

from night_market_foundation.api import route
from night_market_foundation.advice_service import AdviceService
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database


def call(service, advice, method, path, body=None, actor=""):
    return route(service, method, path, body or {}, {"X-Actor-Id": actor}, advice=advice)


class AdviceApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.advice = AdviceService(self.database)
        call(self.service, self.advice, "POST", "/organizations",
             {"request_id": "org", "organization_id": "o1", "name": "夜市机构"}, "bootstrap")
        for key, aid, name, role in [
            ("admin", "a1", "管理员", "admin"),
            ("op", "op1", "操作员", "operator"),
            ("rv", "rv1", "复核员", "reviewer"),
            ("ex1", "e1", "养生专家", "expert"),
            ("au", "au1", "审计员", "auditor"),
        ]:
            actor = "bootstrap" if key == "admin" else "a1"
            call(self.service, self.advice, "POST", "/actors",
                 {"request_id": key, "new_actor_id": aid, "display_name": name,
                  "role": role, "organization_id": "o1"}, actor)
        call(self.service, self.advice, "POST", "/sites",
             {"request_id": "site", "site_id": "s1", "organization_id": "o1",
              "name": "站点", "timezone_name": "Asia/Shanghai"}, "op1")
        status, rule = call(self.service, self.advice, "POST", "/rule-versions", {
            "request_id": "rule1", "rule_set_id": "tcm-life", "version": "2026.1",
            "content": {"items": [
                {"id": "dampness-diet", "text": "痰湿体质宜清淡饮食"},
                {"id": "risk-chest-tight", "text": "胸闷持续应及时就医"},
            ]}}, "rv1")
        self.assertEqual(201, status)
        status, consent = call(self.service, self.advice, "POST", "/consents", {
            "request_id": "c1", "participant_id": "p-001",
            "scopes": ["intake_record", "advice_generation", "advice_disclosure"],
            "fields": ["constitution", "symptoms"]}, "op1")
        self.consent_id = consent["resource_id"]
        _, consultation = call(self.service, self.advice, "POST", "/consultations",
                               {"request_id": "con1", "site_id": "s1",
                                "consent_id": self.consent_id}, "op1")
        self.con_id = consultation["resource_id"]
        _, version = call(self.service, self.advice, "POST", "/advice-versions", {
            "request_id": "iv1", "consultation_id": self.con_id,
            "intake": {"constitution": "痰湿质", "symptoms": "困倦"}}, "op1")
        self.version_id = version["resource_id"]
        _, section = call(self.service, self.advice, "POST", "/advice-sections", {
            "version_id": self.version_id, "kind": "lifestyle", "title": "饮食起居",
            "content": {"text": "清淡饮食、规律作息"},
            "rule_refs": ["dampness-diet"]}, "e1")
        self.section_id = section["section_id"]
        call(self.service, self.advice, "POST", "/advice-sections/sign",
             {"section_id": self.section_id}, "e1")
        _, risk = call(self.service, self.advice, "POST", "/advice-sections", {
            "version_id": self.version_id, "kind": "risk_alert",
            "content": {"text": "持续胸闷请就医"}, "rule_refs": ["risk-chest-tight"]}, "e1")
        call(self.service, self.advice, "POST", "/advice-sections/sign",
             {"section_id": risk["section_id"]}, "e1")

    def tearDown(self):
        self.database.close()

    def test_advice_routes_absent_advice_service_fall_through_to_404(self):
        status, payload = route(self.service, "GET", "/advice-explain?section_id=x", None)
        self.assertEqual(404, status)

    def test_publish_and_effectiveness_explained_over_api(self):
        status, payload = call(self.service, self.advice, "POST", "/advice-versions/publish",
                               {"request_id": "pub1", "version_id": self.version_id}, "rv1")
        self.assertEqual(201, status)
        status, explanation = call(self.service, self.advice, "GET",
                                   f"/advice-explain?section_id={self.section_id}", actor="rv1")
        self.assertEqual(200, status)
        self.assertTrue(explanation["effective"])
        self.assertEqual("effective", explanation["rule_snapshot"]["rule_status_now"])
        self.assertTrue(explanation["consent_status"]["scope_hash_matches_version"])

    def test_reviewer_sees_sensitive_fields_auditor_does_not(self):
        status, reviewer = call(self.service, self.advice, "GET",
                                f"/advice-versions?version_id={self.version_id}", actor="rv1")
        self.assertEqual(200, status)
        self.assertEqual("痰湿质", reviewer["intake"]["constitution"])
        self.assertTrue(any(s["content"] for s in reviewer["sections"]))

        status, auditor = call(self.service, self.advice, "GET",
                               f"/advice-versions?version_id={self.version_id}", actor="au1")
        self.assertEqual(200, status)
        self.assertIsNone(auditor["intake"])
        self.assertIn("审计员", auditor["intake_hidden_reason"])
        for section in auditor["sections"]:
            self.assertIsNone(section["content"])
            self.assertIn("无权", section["hidden_reason"])
        # 非诊断声明对所有角色都展示
        self.assertIn("不构成疾病诊断", auditor["non_diagnosis_notice"])

    def test_withdrawal_makes_advice_ineffective_but_access_facts_remain(self):
        call(self.service, self.advice, "POST", "/advice-versions/publish",
             {"request_id": "pub1", "version_id": self.version_id}, "rv1")
        call(self.service, self.advice, "GET",
             f"/advice-versions?version_id={self.version_id}&purpose=follow_up", actor="rv1")
        status, withdrawn = call(self.service, self.advice, "POST", "/consents/withdraw",
                                 {"request_id": "w1", "consent_id": self.consent_id,
                                  "reason": "参与者撤回"}, "op1")
        self.assertEqual(200, status)
        status, view = call(self.service, self.advice, "GET",
                            f"/advice-versions?version_id={self.version_id}", actor="rv1")
        self.assertEqual("erased", view["status"])
        self.assertFalse(view["effective"])
        self.assertIsNone(view["intake"])
        status, records = call(self.service, self.advice, "GET",
                               f"/access-records?consultation_id={self.con_id}", actor="au1")
        self.assertEqual(200, status)
        self.assertTrue(records["items"])
        statuses = {item["consent_status_at_access"] for item in records["items"]}
        self.assertEqual({"granted", "withdrawn"}, statuses)

    def test_publish_gate_rejects_missing_signature_with_409(self):
        call(self.service, self.advice, "POST", "/advice-versions/publish",
             {"request_id": "pub1", "version_id": self.version_id}, "rv1")
        _, extra = call(self.service, self.advice, "POST", "/advice-versions", {
            "request_id": "iv2", "consultation_id": self.con_id, "trigger": "supplement",
            "supplement_summary": "补充信息", "consent_id": self.consent_id,
            "intake": {"constitution": "痰湿质", "symptoms": "困倦、补充"}}, "op1")
        call(self.service, self.advice, "POST", "/advice-sections", {
            "version_id": extra["resource_id"], "kind": "lifestyle",
            "content": {"text": "新建议"}, "rule_refs": ["dampness-diet"]}, "e1")
        status, payload = call(self.service, self.advice, "POST", "/advice-versions/publish",
                               {"request_id": "pub2", "version_id": extra["resource_id"]}, "rv1")
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_auditor_cannot_register_consent(self):
        status, payload = call(self.service, self.advice, "POST", "/consents", {
            "request_id": "c-bad", "participant_id": "pz",
            "scopes": ["intake_record"], "fields": ["constitution"]}, "au1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])


if __name__ == "__main__":
    unittest.main()
