from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from supplier_qualification.api import JsonApplication
from supplier_qualification.clock import FrozenClock
from supplier_qualification.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from supplier_qualification.rules import (
    DeliverySpec,
    classify_impact,
    evaluate_applicability,
    suspension_active_on,
)
from supplier_qualification.service import QualificationService


class RuleTests(unittest.TestCase):
    def spec(self) -> DeliverySpec:
        return DeliverySpec("sup-a", "fac-a", "IBD-T4", "ins-board", 500)

    def rows(self) -> dict[str, object]:
        return {
            "supplier": {"supplier_id": "sup-a", "active": 1},
            "factory": {"factory_id": "fac-a", "supplier_id": "sup-a", "active": 1},
            "contract": {"contract_id": "HT-1", "valid_from": "2026-01-01", "valid_until": "2026-12-31"},
            "contract_scopes": [{"product_code": "IBD-T4", "factory_id": "fac-a"}],
            "product_approvals": [{
                "approval_id": 1, "state": "active", "qualification_id": "qual-a",
                "standard_no": "GB/T 1303", "qual_category": "ins-board", "voltage_level_kv": 1000,
                "valid_from": "2026-01-01", "valid_until": "2026-12-31", "revoked_at": None,
            }],
            "suspensions": [],
            "quality_events": [],
            "emergency": None,
        }

    def evaluate(self, **overrides: object) -> dict[str, object]:
        rows = self.rows()
        rows.update(overrides)
        return evaluate_applicability(spec=self.spec(), as_of="2026-06-01", **rows)

    def test_all_rules_pass(self) -> None:
        result = self.evaluate()
        self.assertEqual(result["result"], "pass")
        self.assertEqual([f["rule"] for f in result["findings"]], [
            "supplier_active", "factory_belongs", "contract_scope",
            "qualification_coverage", "no_suspension", "quality_event_clear",
        ])

    def test_contract_scope_limits_to_original_factory(self) -> None:
        result = self.evaluate(contract_scopes=[{"product_code": "IBD-T4", "factory_id": "fac-other"}])
        self.assertEqual(result["result"], "fail")
        finding = next(f for f in result["findings"] if f["rule"] == "contract_scope")
        self.assertIn("原厂", finding["message"])

    def test_expired_qualification_fails(self) -> None:
        approvals = self.rows()["product_approvals"]
        approvals[0]["valid_until"] = "2026-05-31"
        result = self.evaluate(product_approvals=approvals)
        self.assertEqual(result["result"], "fail")
        finding = next(f for f in result["findings"] if f["rule"] == "qualification_coverage")
        self.assertIn("不在有效期", finding["message"])

    def test_lower_voltage_qualification_fails(self) -> None:
        approvals = self.rows()["product_approvals"]
        approvals[0]["voltage_level_kv"] = 220
        result = self.evaluate(product_approvals=approvals)
        self.assertEqual(result["result"], "fail")
        finding = next(f for f in result["findings"] if f["rule"] == "qualification_coverage")
        self.assertIn("低于需求", finding["message"])

    def test_suspension_point_in_time(self) -> None:
        row = {"suspension_id": "SUS-1", "product_code": None, "factory_id": None,
               "effective_from": "2026-05-01", "effective_until": None, "lifted_on": "2026-06-10"}
        self.assertTrue(suspension_active_on(row, "2026-06-01"))
        self.assertFalse(suspension_active_on(row, "2026-06-10"))
        self.assertFalse(suspension_active_on(row, "2026-04-30"))

    def test_open_major_event_blocks_until_closed(self) -> None:
        event = {"event_id": "QE-1", "severity": "major", "product_code": "IBD-T4",
                 "factory_id": None, "occurred_on": "2026-05-20", "closed_on": None}
        self.assertEqual(self.evaluate(quality_events=[event])["result"], "fail")
        event["closed_on"] = "2026-05-31"
        self.assertEqual(self.evaluate(quality_events=[event])["result"], "pass")

    def test_minor_event_does_not_block(self) -> None:
        event = {"event_id": "QE-2", "severity": "minor", "product_code": None,
                 "factory_id": None, "occurred_on": "2026-05-20", "closed_on": None}
        self.assertEqual(self.evaluate(quality_events=[event])["result"], "pass")

    def test_substitute_basis_marks_contract_as_emergency(self) -> None:
        result = self.evaluate(contract=None, contract_scopes=[])
        self.assertEqual(result["result"], "pass")
        finding = next(f for f in result["findings"] if f["rule"] == "contract_scope")
        self.assertEqual(finding["result"], "emergency_required")

    def test_emergency_approval_covers_contract_rule_within_validity(self) -> None:
        emergency = {"substitution_id": "SUB-1", "approved": True, "valid_until": "2026-06-30"}
        self.assertEqual(self.evaluate(contract=None, emergency=emergency)["result"], "pass")
        emergency["valid_until"] = "2026-05-31"
        self.assertEqual(self.evaluate(contract=None, emergency=emergency)["result"], "fail")

    def test_classify_impact(self) -> None:
        self.assertEqual(classify_impact("cleared"), "review_required")
        self.assertEqual(classify_impact("review_required"), "review_required")
        self.assertEqual(classify_impact("shipped"), "acceptance_pending")
        self.assertEqual(classify_impact("accepted"), "retained")
        with self.assertRaises(ValueError):
            classify_impact("substituted")


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = QualificationService(self.connection, self.clock)
        for user_id, role in (
            ("admin", "qualification_admin"),
            ("quality", "quality"),
            ("buyer", "buyer"),
            ("lead", "procurement_lead"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_supplier("admin", {"supplier_id": "sup-df", "name": "东方绝缘", "credit_code": "91" + "0" * 16})
        self.service.create_factory("admin", "sup-df", {"factory_id": "fac-dy", "name": "德阳厂", "address": "德阳"})
        self.service.create_factory("admin", "sup-df", {"factory_id": "fac-xy", "name": "咸阳分厂", "address": "咸阳"})
        self.service.create_qualification("admin", "sup-df", {
            "qualification_id": "qual-df", "qual_type": "type_test", "standard_no": "GB/T 1303",
            "product_category": "ins-board", "voltage_level_kv": 1000,
            "scope_text": "1000kV 及以下绝缘纸板", "valid_from": "2026-01-01", "valid_until": "2026-12-31",
        })
        self.service.create_approved_product("admin", "sup-df", {
            "factory_id": "fac-dy", "product_code": "IBD-T4",
            "product_category": "ins-board", "qualification_id": "qual-df",
        })
        self.service.create_contract("admin", {
            "contract_id": "HT-01", "supplier_id": "sup-df", "title": "年度框架（仅德阳原厂）",
            "valid_from": "2026-01-01", "valid_until": "2026-12-31",
            "scopes": [{"product_code": "IBD-T4", "factory_id": "fac-dy"}],
        })

    def tearDown(self) -> None:
        self.connection.close()

    def order_payload(self, order_id: str = "PO-1", **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "order_id": order_id, "contract_id": "HT-01", "supplier_id": "sup-df",
            "factory_id": "fac-dy", "product_code": "IBD-T4", "product_category": "ins-board",
            "required_voltage_kv": 500, "quantity": "10", "unit": "吨",
            "planned_delivery_on": "2026-10-15", "idempotency_key": f"{order_id}-key",
        }
        payload.update(overrides)
        return payload

    def create_substitute_supplier(self) -> None:
        self.service.create_supplier("admin", {"supplier_id": "sup-ny", "name": "南洋电工", "credit_code": "91" + "1" * 16})
        self.service.create_factory("admin", "sup-ny", {"factory_id": "fac-zz", "name": "郑州厂", "address": "郑州"})
        self.service.create_qualification("admin", "sup-ny", {
            "qualification_id": "qual-ny", "qual_type": "type_test", "standard_no": "GB/T 1303",
            "product_category": "ins-board", "voltage_level_kv": 1000,
            "scope_text": "1000kV 及以下绝缘纸板", "valid_from": "2026-03-01", "valid_until": "2027-02-28",
        })
        self.service.create_approved_product("admin", "sup-ny", {
            "factory_id": "fac-zz", "product_code": "IBD-T4N",
            "product_category": "ins-board", "qualification_id": "qual-ny",
        })

    def substitution_payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "substitution_id": "SUB-1", "order_id": "PO-1",
            "substitute_supplier_id": "sup-ny", "substitute_factory_id": "fac-zz",
            "substitute_product_code": "IBD-T4N", "quantity": "10", "needed_by": "2026-10-25",
            "reason": "原厂交期延误", "idempotency_key": "sub-1-key",
        }
        payload.update(overrides)
        return payload

    def approve(self, actor: str, approval_type: str, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "approval_type": approval_type, "decision": "approved",
            "approved_quantity": "10", "valid_until": "2026-11-30", "note": "同意",
        }
        payload.update(overrides)
        return self.service.approve_substitution(actor, "SUB-1", payload)

    def test_order_cleared_when_fully_compliant(self) -> None:
        result = self.service.create_order("buyer", self.order_payload())
        self.assertEqual(result["state"], "cleared")
        compliance = self.service.order_compliance("buyer", "PO-1")
        self.assertEqual(len(compliance["checks"]), 1)
        self.assertEqual(compliance["checks"][0]["phase"], "creation")
        self.assertEqual(compliance["checks"][0]["detail"]["result"], "pass")

    def test_order_blocked_when_contract_only_covers_original_factory(self) -> None:
        result = self.service.create_order("buyer", self.order_payload(factory_id="fac-xy"))
        self.assertEqual(result["state"], "blocked")
        detail = self.service.order_compliance("buyer", "PO-1")["checks"][0]["detail"]
        finding = next(f for f in detail["findings"] if f["rule"] == "contract_scope")
        self.assertEqual(finding["result"], "fail")
        self.assertIn("原厂", finding["message"])

    def test_order_blocked_when_qualification_expires_before_delivery(self) -> None:
        result = self.service.create_order("buyer", self.order_payload(planned_delivery_on="2027-01-15"))
        self.assertEqual(result["state"], "blocked")

    def test_order_replay_and_payload_conflict(self) -> None:
        first = self.service.create_order("buyer", self.order_payload())
        second = self.service.create_order("buyer", self.order_payload())
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.create_order("buyer", self.order_payload(quantity="11"))

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_order("quality", self.order_payload())
        with self.assertRaises(Forbidden):
            self.service.create_supplier("buyer", {"supplier_id": "sup-x", "name": "x", "credit_code": "x"})
        with self.assertRaises(Forbidden):
            self.service.decide_suspension("buyer", "sup-df", {
                "suspension_id": "SUS-1", "reason": "r", "effective_from": "2026-09-24",
            })

    def test_suspension_distinguishes_unshipped_from_accepted(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.service.ship_order("buyer", "PO-1", 1)
        self.service.accept_order("buyer", "PO-1", 2)
        self.service.create_order("buyer", self.order_payload("PO-2"))
        self.service.create_order("buyer", self.order_payload("PO-3"))
        self.service.ship_order("buyer", "PO-3", 1)
        result = self.service.decide_suspension("quality", "sup-df", {
            "suspension_id": "SUS-1", "product_code": "IBD-T4",
            "reason": "重大质量事件", "effective_from": "2026-09-24",
        })
        self.assertEqual(result["impacts"], {"review_required": 1, "acceptance_pending": 1, "retained": 1})
        impacts = self.service.change_impacts("buyer", result["change_id"])["impacts"]
        self.assertEqual([i["order_id"] for i in impacts["review_required"]], ["PO-2"])
        self.assertEqual([i["order_id"] for i in impacts["acceptance_pending"]], ["PO-3"])
        self.assertEqual([i["order_id"] for i in impacts["retained"]], ["PO-1"])
        self.assertEqual(self.service.get_order("buyer", "PO-2")["state"], "review_required")
        self.assertEqual(self.service.get_order("buyer", "PO-1")["state"], "accepted")
        with self.assertRaises(InvalidState):
            self.service.ship_order("buyer", "PO-2", 2)

    def test_recheck_after_suspension_lifted(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.service.decide_suspension("quality", "sup-df", {
            "suspension_id": "SUS-1", "reason": "调查", "effective_from": "2026-09-24",
        })
        self.assertEqual(self.service.get_order("buyer", "PO-1")["state"], "review_required")
        recheck = self.service.recheck_order("buyer", "PO-1")
        self.assertEqual(recheck["state"], "blocked")
        self.service.lift_suspension("quality", "SUS-1", 1)
        recheck = self.service.recheck_order("buyer", "PO-1")
        self.assertEqual(recheck["state"], "cleared")
        shipped = self.service.ship_order("buyer", "PO-1", 5)
        self.assertEqual(shipped["state"], "shipped")

    def test_shipment_blocked_when_qualification_expires_before_ship(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.clock.advance(days=100)
        with self.assertRaises(InvalidState):
            self.service.ship_order("buyer", "PO-1", 1)
        self.assertEqual(self.service.get_order("buyer", "PO-1")["state"], "review_required")
        checks = self.service.order_compliance("buyer", "PO-1")["checks"]
        self.assertEqual(checks[-1]["phase"], "shipment")
        self.assertEqual(checks[-1]["result"], "fail")

    def test_acceptance_snapshot_kept_under_then_current_rules(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.service.ship_order("buyer", "PO-1", 1)
        self.service.accept_order("buyer", "PO-1", 2)
        self.service.decide_suspension("quality", "sup-df", {
            "suspension_id": "SUS-1", "reason": "事后调查", "effective_from": "2026-09-24",
        })
        order = self.service.get_order("buyer", "PO-1")
        self.assertEqual(order["state"], "accepted")
        checks = self.service.order_compliance("buyer", "PO-1")["checks"]
        acceptance = next(c for c in checks if c["phase"] == "acceptance")
        self.assertEqual(acceptance["result"], "pass")
        self.assertEqual(acceptance["as_of_date"], "2026-09-24")

    def test_renewal_unblocks_expired_order_after_recheck(self) -> None:
        self.service.create_contract("admin", {
            "contract_id": "HT-02", "supplier_id": "sup-df", "title": "跨年框架",
            "valid_from": "2026-01-01", "valid_until": "2027-12-31",
            "scopes": [{"product_code": "IBD-T4", "factory_id": "fac-dy"}],
        })
        self.service.create_order(
            "buyer", self.order_payload("PO-1", contract_id="HT-02", planned_delivery_on="2027-01-15")
        )
        self.assertEqual(self.service.get_order("buyer", "PO-1")["state"], "blocked")
        renewal = self.service.renew_qualification("admin", "qual-df", {
            "valid_until": "2027-12-31", "note": "复评通过续期",
        })
        self.assertEqual(renewal["impacts"]["review_required"], 1)
        recheck = self.service.recheck_order("buyer", "PO-1")
        self.assertEqual(recheck["state"], "cleared")
        impacts = self.service.change_impacts("buyer", renewal["change_id"])["impacts"]
        self.assertEqual([i["order_id"] for i in impacts["review_required"]], ["PO-1"])

    def test_renewal_must_extend_validity(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.renew_qualification("admin", "qual-df", {
                "valid_until": "2026-12-31", "note": "未延长",
            })

    def test_quality_event_close_triggers_review_classification(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        event = self.service.record_quality_event("quality", "sup-df", {
            "event_id": "QE-1", "product_code": "IBD-T4", "severity": "critical",
            "description": "批次复测不合格", "occurred_on": "2026-09-24",
        })
        self.assertEqual(event["impacts"]["review_required"], 1)
        closure = self.service.close_quality_event("quality", "QE-1", "整改完成复测合格", 1)
        self.assertEqual(closure["impacts"]["review_required"], 1)
        recheck = self.service.recheck_order("buyer", "PO-1")
        self.assertEqual(recheck["state"], "cleared")

    def test_minor_quality_event_does_not_trigger_impacts(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        event = self.service.record_quality_event("quality", "sup-df", {
            "event_id": "QE-1", "severity": "minor",
            "description": "外包装破损", "occurred_on": "2026-09-24",
        })
        self.assertEqual(event["impacts"], {"review_required": 0, "acceptance_pending": 0, "retained": 0})
        self.assertEqual(self.service.get_order("buyer", "PO-1")["state"], "cleared")

    def test_revocation_blocks_recheck(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.service.revoke_qualification("quality", "qual-df", "型式试验复核不通过")
        recheck = self.service.recheck_order("buyer", "PO-1")
        self.assertEqual(recheck["state"], "blocked")
        with self.assertRaises(InvalidState):
            self.service.renew_qualification("admin", "qual-df", {
                "valid_until": "2027-12-31", "note": "已撤销资质",
            })

    def test_substitution_requires_dual_approval_and_records_basis(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.create_substitute_supplier()
        requested = self.service.request_substitution("buyer", self.substitution_payload())
        self.assertEqual(requested["state"], "pending")
        detail = self.service.substitution_detail("buyer", "SUB-1")
        contract_rule = next(f for f in detail["basis"]["findings"] if f["rule"] == "contract_scope")
        self.assertEqual(contract_rule["result"], "emergency_required")
        self.assertEqual(detail["basis"]["result"], "pass")
        with self.assertRaises(Forbidden):
            self.service.approve_substitution("buyer", "SUB-1", {
                "approval_type": "quality", "decision": "approved",
                "approved_quantity": "10", "valid_until": "2026-11-30", "note": "x",
            })
        first = self.approve("quality", "quality")
        self.assertEqual(first["state"], "pending")
        second = self.approve("lead", "procurement")
        self.assertEqual(second["state"], "approved")
        detail = self.service.substitution_detail("buyer", "SUB-1")
        self.assertEqual(detail["effective"], {"approved_quantity": "10.000", "valid_until": "2026-11-30"})
        applied = self.service.apply_substitution("buyer", "SUB-1", "PO-1S")
        self.assertEqual(applied["state"], "cleared")
        self.assertEqual(self.service.get_order("buyer", "PO-1")["state"], "substituted")
        substitute_order = self.service.get_order("buyer", "PO-1S")
        self.assertEqual(substitute_order["supplier_id"], "sup-ny")
        self.assertEqual(substitute_order["substitution_id"], "SUB-1")
        with self.assertRaises(InvalidState):
            self.service.apply_substitution("buyer", "SUB-1", "PO-1S2")

    def test_substitution_approval_limited_to_explicit_quantity(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.create_substitute_supplier()
        self.service.request_substitution("buyer", self.substitution_payload())
        self.approve("quality", "quality", approved_quantity="6")
        self.approve("lead", "procurement")
        with self.assertRaises(InvalidState):
            self.service.apply_substitution("buyer", "SUB-1", "PO-1S")

    def test_substitution_approval_limited_to_explicit_period(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.create_substitute_supplier()
        self.service.request_substitution("buyer", self.substitution_payload())
        self.approve("quality", "quality", valid_until="2026-10-01")
        self.approve("lead", "procurement", valid_until="2026-10-10")
        self.clock.advance(days=20)
        with self.assertRaises(InvalidState):
            self.service.apply_substitution("buyer", "SUB-1", "PO-1S")
        self.assertEqual(self.service.substitution_detail("buyer", "SUB-1")["substitution"]["state"], "expired")

    def test_substitution_rejected_by_either_function(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.create_substitute_supplier()
        self.service.request_substitution("buyer", self.substitution_payload())
        self.approve("quality", "quality", decision="rejected", note="替代供方报告不齐全")
        self.assertEqual(
            self.service.substitution_detail("buyer", "SUB-1")["substitution"]["state"], "rejected"
        )
        with self.assertRaises(InvalidState):
            self.approve("lead", "procurement")

    def test_substitution_replay_and_unqualified_substitute_basis(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.create_substitute_supplier()
        first = self.service.request_substitution("buyer", self.substitution_payload())
        second = self.service.request_substitution("buyer", self.substitution_payload())
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.request_substitution("buyer", self.substitution_payload(quantity="11"))
        basis = self.service.substitution_detail("buyer", "SUB-1")["basis"]
        self.assertEqual(basis["result"], "pass")
        unqualified = self.service.request_substitution("buyer", self.substitution_payload(
            substitution_id="SUB-2", substitute_product_code="IBD-UNKNOWN", idempotency_key="sub-2-key",
        ))
        self.assertEqual(unqualified["basis_result"], "fail")

    def test_substitution_quantity_must_match_order(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.create_substitute_supplier()
        with self.assertRaises(ValidationFailed):
            self.service.request_substitution("buyer", self.substitution_payload(quantity="5"))

    def test_supplier_changes_and_profile_queries(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.service.decide_suspension("quality", "sup-df", {
            "suspension_id": "SUS-1", "reason": "调查", "effective_from": "2026-09-24",
        })
        self.service.lift_suspension("quality", "SUS-1", 1)
        changes = self.service.supplier_changes("buyer", "sup-df")["changes"]
        self.assertEqual([c["change_type"] for c in changes], ["suspension", "suspension_lifted"])
        profile = self.service.supplier_profile("buyer", "sup-df")
        self.assertEqual(profile["qualifications"][0]["status_on_today"], "active")
        self.assertEqual(len(profile["approved_products"]), 1)
        with self.assertRaises(NotFound):
            self.service.supplier_profile("buyer", "sup-unknown")

    def test_audit_chain_detects_tampering(self) -> None:
        self.service.create_order("buyer", self.order_payload("PO-1"))
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE qual_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.app = JsonApplication(QualificationService(self.connection, self.clock))

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict[str, object], actor: str = "admin"):
        return self.app.handle(
            "POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8")
        )

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_missing_actor_header(self) -> None:
        response = self.app.handle("POST", "/suppliers", body=b"{}")
        self.assertEqual(response.status, 422)
        self.assertIn("X-Actor-Id", response.body["error"]["message"])

    def test_supplier_and_order_routes(self) -> None:
        self.post("/users", {"user_id": "admin", "display_name": "资质管理员", "role": "qualification_admin"})
        self.post("/users", {"user_id": "buyer", "display_name": "采购工程师", "role": "buyer"})
        response = self.post("/suppliers", {"supplier_id": "sup-df", "name": "东方绝缘", "credit_code": "91" + "0" * 16})
        self.assertEqual(response.status, 201)
        self.post("/suppliers/sup-df/factories", {"factory_id": "fac-dy", "name": "德阳厂", "address": "德阳"})
        self.post("/suppliers/sup-df/qualifications", {
            "qualification_id": "qual-df", "qual_type": "type_test", "standard_no": "GB/T 1303",
            "product_category": "ins-board", "voltage_level_kv": 1000,
            "scope_text": "1000kV 及以下", "valid_from": "2026-01-01", "valid_until": "2026-12-31",
        })
        self.post("/suppliers/sup-df/products", {
            "factory_id": "fac-dy", "product_code": "IBD-T4",
            "product_category": "ins-board", "qualification_id": "qual-df",
        })
        self.post("/contracts", {
            "contract_id": "HT-01", "supplier_id": "sup-df", "title": "年度框架",
            "valid_from": "2026-01-01", "valid_until": "2026-12-31",
            "scopes": [{"product_code": "IBD-T4", "factory_id": "fac-dy"}],
        })
        response = self.post("/orders", {
            "order_id": "PO-1", "contract_id": "HT-01", "supplier_id": "sup-df",
            "factory_id": "fac-dy", "product_code": "IBD-T4", "product_category": "ins-board",
            "required_voltage_kv": 500, "quantity": "10", "unit": "吨",
            "planned_delivery_on": "2026-10-15", "idempotency_key": "po-1-key",
        }, actor="buyer")
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["state"], "cleared")
        response = self.app.handle("GET", "/orders/PO-1/compliance", {"X-Actor-Id": "buyer"})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body["checks"]), 1)
        response = self.app.handle("GET", "/suppliers/sup-df", {"X-Actor-Id": "buyer"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["supplier"]["supplier_id"], "sup-df")
        response = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "buyer"})
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
