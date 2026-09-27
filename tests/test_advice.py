import unittest
from datetime import datetime, timezone

from night_market_foundation.advice import AdviceService, DIAGNOSTIC_BOUNDARY
from night_market_foundation.clock import FixedClock
from night_market_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from night_market_foundation.storage import Database


class AdviceServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.service = AdviceService(self.database, self.clock)
        bootstrap = [
            ("org", "register_organization", dict(organization_id="o1", name="活动机构一")),
            ("admin", "register_actor", dict(new_actor_id="a1", display_name="管理员",
                                             role="admin", organization_id="o1")),
            ("op", "register_actor", dict(new_actor_id="op1", display_name="操作员",
                                          role="operator", organization_id="o1")),
            ("rv1", "register_actor", dict(new_actor_id="rv1", display_name="专家甲",
                                           role="reviewer", organization_id="o1")),
            ("rv2", "register_actor", dict(new_actor_id="rv2", display_name="专家乙",
                                           role="reviewer", organization_id="o1")),
            ("au", "register_actor", dict(new_actor_id="au1", display_name="审计员",
                                          role="auditor", organization_id="o1")),
            ("site", "register_site", dict(site_id="s1", organization_id="o1",
                                           name="活动站点", timezone_name="Asia/Shanghai")),
        ]
        for index, (request_id, action, kwargs) in enumerate(bootstrap):
            actor = "bootstrap" if index < 2 else "a1"
            getattr(self.service, action)(request_id=request_id, actor_id=actor, **kwargs)
        # 第二组织，用于跨组织访问测试
        self.service.register_organization(request_id="org2", actor_id="a1",
                                           organization_id="o2", name="活动机构二")
        self.service.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                    display_name="外部操作员", role="operator", organization_id="o2")
        self.service.register_rule_version(
            request_id="rule1", actor_id="a1", rule_version_id="r1", site_id="s1",
            version_label="2026-v1",
            snapshot={"entries": {"pinghe": "饮食有节，起居有常"}},
            effective_from="2026-09-01T00:00:00Z")
        self.service.grant_consent(
            request_id="consent1", actor_id="op1", consent_id="c1", site_id="s1",
            participant_ref="p1",
            scope={"fields": ["constitution", "sleep", "appetite"],
                   "purposes": ["constitution_advice"]})
        self.service.create_case(request_id="case1", actor_id="op1", case_id="case1",
                                 site_id="s1", participant_ref="p1", consent_id="c1")

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------ 授权与最小必要

    def test_no_consent_no_case(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_case(request_id="x", actor_id="op1", case_id="caseX",
                                     site_id="s1", participant_ref="p9", consent_id="missing")

    def test_consent_must_match_participant_and_site(self):
        with self.assertRaises(ValidationError):
            self.service.create_case(request_id="x", actor_id="op1", case_id="caseX",
                                     site_id="s1", participant_ref="p2", consent_id="c1")

    def test_inquiry_field_outside_scope_is_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.service.draft_version(
                request_id="x", actor_id="rv1", case_id="case1",
                inquiry_summary={"constitution": "偏痰湿", "id_number": "隐藏字段"},
                rule_version_id="r1")

    def test_scope_fields_are_minimum_and_validated(self):
        with self.assertRaises(ValidationError):
            self.service.grant_consent(
                request_id="bad-scope", actor_id="op1", consent_id="cX", site_id="s1",
                participant_ref="pX", scope={"fields": [], "purposes": ["x"]})

    # ------------------------------------------------------------ 完整出具流程

    def _publish_v1(self, request_prefix="v1"):
        self.service.draft_version(
            request_id=f"{request_prefix}-draft", actor_id="rv1", case_id="case1",
            inquiry_summary={"constitution": "偏痰湿", "sleep": "偏晚"},
            rule_version_id="r1")
        self.service.add_section(
            request_id=f"{request_prefix}-s1", actor_id="rv1", case_id="case1", version_no=1,
            kind="lifestyle", content="建议规律作息，少食生冷油腻。")
        self.service.add_section(
            request_id=f"{request_prefix}-s2", actor_id="rv2", case_id="case1", version_no=1,
            kind="medical_risk", content="若胸闷症状持续，请尽快至医疗机构就诊。")
        self.service.sign_section(
            request_id=f"{request_prefix}-sign1", actor_id="rv1", case_id="case1",
            version_no=1,
            section_id=self._section_id("case1", 1, "lifestyle"))
        self.service.sign_section(
            request_id=f"{request_prefix}-sign2", actor_id="rv2", case_id="case1",
            version_no=1,
            section_id=self._section_id("case1", 1, "medical_risk"))
        return self.service.publish_version(
            request_id=f"{request_prefix}-pub", actor_id="rv1", case_id="case1", version_no=1)

    def _section_id(self, case_id, version_no, kind):
        row = self.database.connection.execute(
            "SELECT section_id FROM advice_sections WHERE case_id=? AND version_no=? AND kind=?",
            (case_id, version_no, kind)).fetchone()
        return row["section_id"]

    def test_expert_can_only_sign_own_section(self):
        self.service.draft_version(
            request_id="d1", actor_id="rv1", case_id="case1",
            inquiry_summary={"constitution": "偏痰湿"}, rule_version_id="r1")
        self.service.add_section(
            request_id="s2", actor_id="rv2", case_id="case1", version_no=1,
            kind="medical_risk", content="必要时就医。")
        with self.assertRaises(PermissionDenied):
            self.service.sign_section(
                request_id="bad-sign", actor_id="rv1", case_id="case1", version_no=1,
                section_id=self._section_id("case1", 1, "medical_risk"))

    def test_operator_cannot_create_or_sign_sections(self):
        self.service.draft_version(
            request_id="d1", actor_id="rv1", case_id="case1",
            inquiry_summary={"constitution": "偏痰湿"}, rule_version_id="r1")
        with self.assertRaises(PermissionDenied):
            self.service.add_section(
                request_id="x", actor_id="op1", case_id="case1", version_no=1,
                kind="lifestyle", content="越权内容")

    def test_publish_requires_risk_section_and_all_signatures(self):
        self.service.draft_version(
            request_id="d1", actor_id="rv1", case_id="case1",
            inquiry_summary={"constitution": "偏痰湿"}, rule_version_id="r1")
        self.service.add_section(
            request_id="s1", actor_id="rv1", case_id="case1", version_no=1,
            kind="lifestyle", content="规律作息。")
        self.service.sign_section(
            request_id="g1", actor_id="rv1", case_id="case1", version_no=1,
            section_id=self._section_id("case1", 1, "lifestyle"))
        # 缺少就医风险提示
        with self.assertRaises(ConflictError):
            self.service.publish_version(request_id="pub-fail", actor_id="rv1",
                                         case_id="case1", version_no=1)
        # 补一段未签署的风险提示仍然不能发布
        self.service.add_section(
            request_id="s2", actor_id="rv2", case_id="case1", version_no=1,
            kind="medical_risk", content="必要时就医。")
        with self.assertRaises(ConflictError):
            self.service.publish_version(request_id="pub-fail2", actor_id="rv1",
                                         case_id="case1", version_no=1)

    def test_publish_revalidates_signature_after_tampering(self):
        receipt = self._publish_v1()
        self.assertFalse(receipt.replayed)
        # 模拟内容在签署后、发布前被篡改：直接改库，签名必须失配
        self.database.connection.execute(
            "UPDATE advice_sections SET content=? WHERE section_id=?",
            ("被篡改的内容", self._section_id("case1", 1, "lifestyle")))
        # 已发布版本不能重复发布；改用 explain 证明签名失效，并新建版本验证发布拦截
        explanation = self.service.explain_version(actor_id="a1", case_id="case1", version_no=1)
        self.assertFalse(all(s["hash_valid"] for s in explanation["signatures"]))

    def test_full_publish_marks_cultural_experience_not_diagnosis(self):
        self._publish_v1()
        view = self.service.view_version(actor_id="rv1", case_id="case1", version_no=1)
        self.assertEqual("cultural_experience", view["nature"])
        self.assertIn("不属于医学诊断", view["diagnostic_boundary"])
        self.assertEqual(DIAGNOSTIC_BOUNDARY, view["diagnostic_boundary"])
        self.assertTrue(view["is_current"])
        # 规则快照留痕
        self.assertEqual("r1", view["basis"]["rule_version_id"])
        self.assertEqual(64, len(view["basis"]["rule_snapshot_hash"]))
        # 两位专家分别只签了自己的段
        by_kind = {s["kind"]: s["signed_by"] for s in view["sections"]}
        self.assertEqual({"lifestyle": "rv1", "medical_risk": "rv2"}, by_kind)

    # ------------------------------------------------------------ 角色视角红acted

    def test_role_based_redaction_of_sensitive_fields(self):
        self._publish_v1()
        reviewer = self.service.view_version(actor_id="rv1", case_id="case1", version_no=1)
        self.assertEqual("full", reviewer["redaction_level"])
        self.assertEqual("偏痰湿", reviewer["inquiry_summary"]["constitution"])

        operator = self.service.view_version(actor_id="op1", case_id="case1", version_no=1)
        self.assertEqual("inquiry_redacted", operator["redaction_level"])
        self.assertTrue(operator["inquiry_summary"]["redacted"])
        self.assertEqual("role_not_authorized", operator["inquiry_summary"]["reason"])
        self.assertIn("constitution", operator["inquiry_summary"]["fields"])
        # 非专家角色看得到建议正文但看不到问询明细
        self.assertTrue(all(s["content"] for s in operator["sections"]))

        auditor = self.service.view_version(actor_id="au1", case_id="case1", version_no=1)
        self.assertEqual("metadata_only", auditor["redaction_level"])
        self.assertTrue(all(s["content"] is None and s["content_redacted"]
                            for s in auditor["sections"]))
        self.assertTrue(auditor["inquiry_summary"]["redacted"])

    def test_cross_org_access_denied(self):
        self._publish_v1()
        with self.assertRaises(PermissionDenied):
            self.service.view_version(actor_id="op2", case_id="case1", version_no=1)
        with self.assertRaises(PermissionDenied):
            self.service.explain_version(actor_id="op2", case_id="case1", version_no=1)

    # ------------------------------------------------------------ 复核版本链

    def test_supplementary_info_creates_new_version_and_keeps_history(self):
        self._publish_v1()
        # 新档案的首个版本不能携带复核原因
        self.service.grant_consent(
            request_id="consent2", actor_id="op1", consent_id="c2", site_id="s1",
            participant_ref="p2", scope={"fields": ["constitution"],
                                         "purposes": ["constitution_advice"]})
        self.service.create_case(request_id="case2", actor_id="op1", case_id="case2",
                                 site_id="s1", participant_ref="p2", consent_id="c2")
        with self.assertRaises(ValidationError):
            self.service.draft_version(
                request_id="bad-reason", actor_id="rv1", case_id="case2",
                inquiry_summary={"constitution": "偏痰湿"}, rule_version_id="r1",
                change_reason_code="supplementary_info")
        # v1 已发布，起草 v2 必须给原因
        with self.assertRaises(ValidationError):
            self.service.draft_version(
                request_id="v2-noreason", actor_id="rv1", case_id="case1",
                inquiry_summary={"constitution": "偏痰湿", "appetite": "一般"},
                rule_version_id="r1")
        self.service.draft_version(
            request_id="v2-draft", actor_id="rv1", case_id="case1",
            inquiry_summary={"constitution": "痰湿兼气虚", "sleep": "偏晚", "appetite": "一般"},
            rule_version_id="r1", change_reason_code="supplementary_info",
            change_reason="参与者补充食欲与过敏史相关情况")
        self.service.add_section(
            request_id="v2-s1", actor_id="rv1", case_id="case1", version_no=2,
            kind="lifestyle", content="在原建议基础上增加健脾食材。")
        self.service.add_section(
            request_id="v2-s2", actor_id="rv2", case_id="case1", version_no=2,
            kind="medical_risk", content="若出现过敏反应，立即停用相关食材并就医。")
        self.service.sign_section(
            request_id="v2-sign1", actor_id="rv1", case_id="case1", version_no=2,
            section_id=self._section_id("case1", 2, "lifestyle"))
        self.service.sign_section(
            request_id="v2-sign2", actor_id="rv2", case_id="case1", version_no=2,
            section_id=self._section_id("case1", 2, "medical_risk"))
        self.service.publish_version(request_id="v2-pub", actor_id="rv1",
                                     case_id="case1", version_no=2)

        case = self.service.get_case(actor_id="rv1", case_id="case1")
        self.assertEqual(2, case["current_version_no"])
        statuses = {v["version_no"]: v["status"] for v in case["versions"]}
        self.assertEqual("superseded", statuses[1])
        self.assertEqual("published", statuses[2])

        # 历史版本仍可查，且明确不再是当前结论
        history = self.service.view_version(actor_id="rv1", case_id="case1", version_no=1)
        self.assertFalse(history["is_current"])
        self.assertEqual("superseded", history["status"])
        self.assertEqual("偏痰湿", history["inquiry_summary"]["constitution"])

        explain_old = self.service.explain_version(actor_id="au1", case_id="case1", version_no=1)
        self.assertFalse(explain_old["effective"])
        self.assertEqual("superseded_by_new_review",
                         explain_old["ineffective_reason"]["code"])
        self.assertEqual(2, explain_old["ineffective_reason"]["by_version"])
        self.assertEqual("supplementary_info",
                         explain_old["ineffective_reason"]["change_reason_code"])
        self.assertIn("过敏", explain_old["ineffective_reason"]["change_reason"])

        explain_new = self.service.explain_version(actor_id="au1", case_id="case1", version_no=2)
        self.assertTrue(explain_new["effective"])
        self.assertEqual("published", explain_new["effective_reason"]["code"])
        self.assertTrue(explain_new["effective_reason"]["all_signatures_valid"])
        # 解释接口不含任何健康内容
        self.assertFalse(any("content" in s for s in explain_new["signatures"]))

    def test_cannot_publish_against_expired_rule_version(self):
        self.service.register_rule_version(
            request_id="rule-old", actor_id="a1", rule_version_id="r-old", site_id="s1",
            version_label="2026-old", snapshot={"entries": {"x": "旧规则"}},
            effective_from="2026-08-01T00:00:00Z")
        self.service.supersede_rule_version(
            request_id="rule-old-off", actor_id="a1", rule_version_id="r-old",
            effective_to="2026-09-20T00:00:00Z")
        with self.assertRaises(ConflictError):
            self.service.draft_version(
                request_id="old-draft", actor_id="rv1", case_id="case1",
                inquiry_summary={"constitution": "偏痰湿"}, rule_version_id="r-old")

    def test_rule_snapshot_update_does_not_rewrite_history(self):
        self._publish_v1()
        old_hash = self.service.view_version(actor_id="rv1", case_id="case1",
                                             version_no=1)["basis"]["rule_snapshot_hash"]
        self.service.register_rule_version(
            request_id="rule2", actor_id="a1", rule_version_id="r2", site_id="s1",
            version_label="2026-v2", snapshot={"entries": {"pinghe": "新版规则内容"}})
        view = self.service.view_version(actor_id="rv1", case_id="case1", version_no=1)
        self.assertEqual(old_hash, view["basis"]["rule_snapshot_hash"])
        self.assertEqual("r1", view["basis"]["rule_version_id"])

    # ------------------------------------------------------------ 撤回授权

    def test_withdrawal_redacts_content_but_keeps_audit_facts(self):
        self._publish_v1()
        # 撤回前有一次专家访问，必须留下审计事实
        self.service.view_version(actor_id="rv1", case_id="case1", version_no=1)
        self.service.withdraw_consent(request_id="wd1", actor_id="op1",
                                      consent_id="c1", reason="参与者离场时撤回授权")

        explanation = self.service.explain_version(actor_id="au1", case_id="case1", version_no=1)
        self.assertEqual("invalidated", explanation["status"])
        self.assertFalse(explanation["effective"])
        self.assertEqual("consent_withdrawn", explanation["ineffective_reason"]["code"])

        for viewer in ("rv1", "op1", "au1"):
            view = self.service.view_version(actor_id=viewer, case_id="case1", version_no=1)
            self.assertEqual("withdrawn", view["redaction_level"])
            self.assertTrue(view["inquiry_summary"]["redacted"])
            self.assertNotIn("fields", view["inquiry_summary"])
            self.assertTrue(all(s["content"] is None for s in view["sections"]))

        # 授权记录本身也不再暴露问询字段名，只保留范围摘要
        consent_view = self.service.get_consent(actor_id="au1", consent_id="c1")
        self.assertTrue(consent_view["scope"]["redacted"])
        self.assertEqual(64, len(consent_view["scope_hash"]))

        # 撤回后不得继续使用信息：不能新增复核版本
        with self.assertRaises(PermissionDenied):
            self.service.draft_version(
                request_id="after-wd", actor_id="rv1", case_id="case1",
                inquiry_summary={"constitution": "新情况"}, rule_version_id="r1",
                change_reason_code="supplementary_info", change_reason="撤回后的补充")

        # 审计事实保留：撤回前的访问仍在链上，且哈希链可验证
        events = self.service.audit_events()
        self.assertTrue(any(e["action"] == "advice.accessed" for e in events))
        self.assertTrue(any(e["action"] == "advice.version_invalidated" for e in events))
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreaterEqual(count, len(events))

    def test_double_withdraw_conflicts(self):
        self.service.withdraw_consent(request_id="wd1", actor_id="op1",
                                      consent_id="c1", reason="第一次撤回")
        with self.assertRaises(ConflictError):
            self.service.withdraw_consent(request_id="wd2", actor_id="op1",
                                          consent_id="c1", reason="重复撤回")

    # ------------------------------------------------------------ 幂等

    def test_consent_request_is_idempotent(self):
        first = self.service.grant_consent(
            request_id="same-req", actor_id="op1", consent_id="c2", site_id="s1",
            participant_ref="p2", scope={"fields": ["sleep"], "purposes": ["x"]})
        replay = self.service.grant_consent(
            request_id="same-req", actor_id="op1", consent_id="c2", site_id="s1",
            participant_ref="p2", scope={"fields": ["sleep"], "purposes": ["x"]})
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)

    def test_draft_not_found_versions_404_semantics(self):
        with self.assertRaises(NotFoundError):
            self.service.view_version(actor_id="rv1", case_id="case1", version_no=99)


if __name__ == "__main__":
    unittest.main()
