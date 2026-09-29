from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from supplier_qualification.api import JsonApplication
from supplier_qualification.clock import FrozenClock
from supplier_qualification.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from supplier_qualification.models import classify_impact
from supplier_qualification.service import QualificationService


MATERIAL = "MAT-INSUL-500KV"


class QualificationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
        self.service = QualificationService(self.connection, self.clock)
        for user_id, role in (
            ("qe", "qual_engineer"), ("qa", "quality"), ("buyer", "buyer"), ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self._catalog()

    def tearDown(self) -> None:
        self.connection.close()

    def _catalog(self) -> None:
        service = self.service
        service.register_plant("qe", {"plant_id": "P1", "supplier_id": "S1", "name": "一厂", "location": "山东"})
        service.register_plant("qe", {"plant_id": "P2", "supplier_id": "S2", "name": "二厂", "location": "江苏"})
        service.register_certificate("qe", {
            "certificate_id": "C1", "supplier_id": "S1", "cert_type": "PRODUCTION_LICENSE",
            "cert_name": "生产许可", "scope_text": f"绝缘材料 {MATERIAL}",
            "valid_from": "2025-01-01", "valid_until": "2026-12-31"})
        service.register_certificate("qe", {
            "certificate_id": "C2", "supplier_id": "S2", "cert_type": "ISO9001",
            "cert_name": "体系认证", "scope_text": "*",
            "valid_from": "2025-01-01", "valid_until": "2028-12-31"})
        service.register_approved_product("qe", {
            "approval_id": "A1", "supplier_id": "S1", "material_code": MATERIAL,
            "material_name": "匝间绝缘纸", "plant_id": "P1", "spec_revision": "V3",
            "valid_from": "2025-01-01", "valid_until": "2029-12-31", "certificate_id": "C1"})
        service.register_approved_product("qe", {
            "approval_id": "A2", "supplier_id": "S2", "material_code": MATERIAL,
            "material_name": "匝间绝缘纸", "plant_id": None, "spec_revision": "V2",
            "valid_from": "2025-01-01", "valid_until": "2028-12-31", "certificate_id": "C2"})

    def _order(self, order_id: str = "PO1", supplier: str = "S1", plant: str | None = "P1") -> dict:
        return self.service.create_order("buyer", {
            "order_id": order_id, "supplier_id": supplier, "material_code": MATERIAL,
            "plant_id": plant, "quantity": "500", "unit": "kg",
            "expect_delivery_on": "2026-10-01"})

    def _ready_lot(self, order_id: str, batch: str, plant: str = "P1", standard: str = "GB/T X") -> None:
        self.service.register_order_lot("buyer", order_id,
                                        {"batch_no": batch, "plant_id": plant, "inspection_standard": standard})
        self.service.record_lot_inspection("buyer", order_id, batch, "passed", f"RPT-{batch}")

    def test_role_permissions_are_segregated(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.suspend_supplier("buyer", {
                "suspension_id": "X", "supplier_id": "S1", "reason_text": "无权暂停"})
        with self.assertRaises(Forbidden):
            self.service.approve_emergency("qe", "X", "quality")
        with self.assertRaises(Forbidden):
            self.service.register_certificate("qa", {
                "certificate_id": "CX", "supplier_id": "S1", "cert_type": "ISO9001",
                "cert_name": "x", "scope_text": "*",
                "valid_from": "2026-01-01", "valid_until": "2027-01-01"})

    def test_shipment_requires_registered_and_passed_batch_inspection(self) -> None:
        self._order()
        with self.assertRaises(InvalidState):  # 批次未登记
            self.service.ship_order("buyer", "PO1", "B1")
        self.service.register_order_lot("buyer", "PO1", {"batch_no": "B1", "inspection_standard": "GB/T X"})
        with self.assertRaises(InvalidState):  # 检测无结论
            self.service.ship_order("buyer", "PO1", "B1")
        self.service.record_lot_inspection("buyer", "PO1", "B1", "failed", "不合格")
        with self.assertRaises(InvalidState):  # 检测不合格
            self.service.ship_order("buyer", "PO1", "B1")

    def test_normal_flow_freezes_snapshot_on_acceptance(self) -> None:
        self._order()
        self._ready_lot("PO1", "B1")
        shipped = self.service.ship_order("buyer", "PO1", "B1")
        self.assertEqual(shipped["state"], "intransit")
        self.assertEqual(shipped["qualification_snapshot"]["shipping_supplier_id"], "S1")
        self.service.accept_order("buyer", "PO1")
        # 暂停后已验收订单仍保留原厂快照
        self.service.suspend_supplier("qa", {
            "suspension_id": "S1", "supplier_id": "S1", "material_code": MATERIAL,
            "plant_id": "P1", "reason_text": "调查"})
        retained = self.service.order("PO1")
        self.assertEqual(retained["state"], "received")
        self.assertEqual(retained["qualification_snapshot"]["shipping_supplier_id"], "S1")
        self.assertFalse(retained["review_required"])

    def test_suspension_marks_only_open_orders_for_review(self) -> None:
        self._order("PO1")
        self._ready_lot("PO1", "B1")
        self.service.ship_order("buyer", "PO1", "B1")
        self.service.accept_order("buyer", "PO1")
        self._order("PO2")
        self._ready_lot("PO2", "B2")
        self._order("PO3")
        self.service.suspend_supplier("qa", {
            "suspension_id": "S2", "supplier_id": "S1", "material_code": MATERIAL,
            "plant_id": "P1", "reason_text": "批次异常"})
        impacts = self.service.change_impacts("auditor", "suspension", "S2")
        self.assertEqual(impacts["review_order_ids"], ["PO2", "PO3"])
        self.assertEqual(impacts["retained_order_ids"], ["PO1"])
        # 暂停期 + 待复核：即使批次合格也不能发货
        with self.assertRaises(InvalidState):
            self.service.ship_order("buyer", "PO2", "B2")

    def test_scope_filters_orders_outside_material_or_plant(self) -> None:
        self._order("PO1")
        # 暂停另一个工厂不影响 P1 的订单
        self.service.suspend_supplier("qa", {
            "suspension_id": "S3", "supplier_id": "S1", "material_code": MATERIAL,
            "plant_id": "P9", "reason_text": "其他工厂"})
        impacts = self.service.change_impacts("auditor", "suspension", "S3")
        self.assertEqual(impacts["review_order_ids"], [])
        self.assertFalse(self.service.order("PO1")["review_required"])

    def test_review_resolution_then_lift_allows_shipment(self) -> None:
        self._order()
        self._ready_lot("PO1", "B1")
        self.service.suspend_supplier("qa", {
            "suspension_id": "S4", "supplier_id": "S1", "material_code": MATERIAL,
            "plant_id": "P1", "reason_text": "调查"})
        self.service.lift_suspension("qa", "S4")
        with self.assertRaises(InvalidState):  # 暂停解除但未复核
            self.service.ship_order("buyer", "PO1", "B1")
        self.service.resolve_order_review("buyer", "PO1", "已核对解除决定")
        shipped = self.service.ship_order("buyer", "PO1", "B1")
        self.assertEqual(shipped["state"], "intransit")

    def test_emergency_requires_both_approvals_quantity_and_window(self) -> None:
        self._order()
        self._ready_lot("PO1", "B-ALT", plant="P2")
        self.service.create_emergency_request("buyer", {
            "approval_id": "E1", "order_id": "PO1", "substitute_supplier_id": "S2",
            "quantity": "500", "unit": "kg",
            "valid_from": "2026-09-01", "valid_until": "2026-09-10", "reason": "交期延误"})
        self.service.approve_emergency("qa", "E1", "quality")
        with self.assertRaises(InvalidState):  # 采购尚未批准
            self.service.ship_order("buyer", "PO1", "B-ALT", "E1")
        self.service.approve_emergency("buyer", "E1", "procurement")
        shipped = self.service.ship_order("buyer", "PO1", "B-ALT", "E1")
        self.assertEqual(shipped["qualification_snapshot"]["shipping_supplier_id"], "S2")
        self.assertEqual(shipped["qualification_snapshot"]["emergency_approval"]["quantity"], "500.000")
        # 同一侧不能重复批准
        with self.assertRaises(Conflict):
            self.service.approve_emergency("qa", "E1", "quality")

    def test_emergency_quantity_must_cover_order(self) -> None:
        self._order()
        self._ready_lot("PO1", "B-ALT", plant="P2")
        self.service.create_emergency_request("buyer", {
            "approval_id": "E2", "order_id": "PO1", "substitute_supplier_id": "S2",
            "quantity": "100", "unit": "kg",
            "valid_from": "2026-09-01", "valid_until": "2026-09-10", "reason": "少量试用"})
        self.service.approve_emergency("qa", "E2", "quality")
        self.service.approve_emergency("buyer", "E2", "procurement")
        with self.assertRaises(InvalidState):  # 批准数量不足
            self.service.ship_order("buyer", "PO1", "B-ALT", "E2")

    def test_emergency_window_is_enforced(self) -> None:
        self._order()
        self._ready_lot("PO1", "B-ALT", plant="P2")
        self.service.create_emergency_request("buyer", {
            "approval_id": "E3", "order_id": "PO1", "substitute_supplier_id": "S2",
            "quantity": "500", "unit": "kg",
            "valid_from": "2026-09-02", "valid_until": "2026-09-05", "reason": "窗口外"})
        self.service.approve_emergency("qa", "E3", "quality")
        self.service.approve_emergency("buyer", "E3", "procurement")
        with self.assertRaises(InvalidState):  # 今天 9/1 不在期限内
            self.service.ship_order("buyer", "PO1", "B-ALT", "E3")

    def test_emergency_approval_is_order_bound(self) -> None:
        self._order("PO1")
        self._order("PO2")
        self._ready_lot("PO2", "B-ALT", plant="P2")
        self.service.create_emergency_request("buyer", {
            "approval_id": "E4", "order_id": "PO1", "substitute_supplier_id": "S2",
            "quantity": "500", "unit": "kg",
            "valid_from": "2026-09-01", "valid_until": "2026-09-10", "reason": "仅 PO1"})
        self.service.approve_emergency("qa", "E4", "quality")
        self.service.approve_emergency("buyer", "E4", "procurement")
        with self.assertRaises(ValidationFailed):  # 批准不属于 PO2
            self.service.ship_order("buyer", "PO2", "B-ALT", "E4")

    def test_alternatives_only_list_eligible_suppliers_with_basis(self) -> None:
        result = self.service.find_alternatives("buyer", MATERIAL, "P2")
        suppliers = {a["supplier_id"] for a in result["alternatives"]}
        self.assertIn("S2", suppliers)
        # 暂停 S2 后不再出现
        self.service.suspend_supplier("qa", {
            "suspension_id": "S5", "supplier_id": "S2", "reason_text": "全面暂停"})
        result_after = self.service.find_alternatives("buyer", MATERIAL, "P2")
        self.assertEqual(result_after["alternatives"], [])
        # 合规依据给出具体原因
        basis = self.service.evaluate_supply("S2", MATERIAL, "P2")
        self.assertFalse(basis["eligible"])
        self.assertIn("suspended", basis["reasons"])

    def test_expired_certificate_blocks_eligibility(self) -> None:
        self.clock.current = datetime(2027, 1, 2, tzinfo=timezone.utc)
        basis = self.service.evaluate_supply("S1", MATERIAL, "P1")
        self.assertFalse(basis["eligible"])
        self.assertIn("certificate_expired", basis["reasons"])
        # 显式到期处理后产生影响记录
        self.service.expire_certificate("qe", "C1")
        self.assertEqual(
            self.service.certificate("C1")["status"], "expired")

    def test_renewal_keeps_old_certificate_coverage_until_its_expiry(self) -> None:
        self._order()
        self._ready_lot("PO1", "B1")
        renewed = self.service.renew_certificate("qe", "C1", {
            "certificate_id": "C1-NEW", "valid_from": "2027-01-01", "valid_until": "2029-12-31"})
        self.assertEqual(renewed["supersedes_certificate_id"], "C1")
        # 续期使未发货订单进入复核
        self.assertTrue(self.service.order("PO1")["review_required"])
        # 续期当下（2026-09）旧证仍在自身有效期内覆盖，采购复核后可正常发货
        self.service.resolve_order_review("buyer", "PO1", "核对续期链接")
        self.service.ship_order("buyer", "PO1", "B1")
        # 未来新证生效日，旧证已过有效期，授权沿续期接替链解析到当日有效的新证
        self.clock.current = datetime(2027, 1, 5, tzinfo=timezone.utc)
        basis = self.service.evaluate_supply("S1", MATERIAL, "P1")
        self.assertTrue(basis["eligible"])
        self.assertEqual(basis["certificate"]["certificate_id"], "C1-NEW")

    def test_closed_critical_event_and_review_flow(self) -> None:
        self._order()
        self.service.open_quality_event("qa", {
            "event_id": "EV1", "supplier_id": "S1", "material_code": MATERIAL, "plant_id": "P1",
            "severity": "critical", "title": "击穿", "detail": "批次异常",
            "occurred_on": "2026-08-30"})
        basis = self.service.evaluate_supply("S1", MATERIAL, "P1")
        self.assertIn("critical_event_open", basis["reasons"])
        with self.assertRaises(ValidationFailed):  # 关闭必须有整改措施
            self.service.close_quality_event("qa", "EV1")
        self.service.update_corrective_action("qa", "EV1", "工艺整改")
        self.service.close_quality_event("qa", "EV1")
        # 整改关闭后资格恢复，但未发货订单仍需复核
        self.assertTrue(self.service.evaluate_supply("S1", MATERIAL, "P1")["eligible"])
        self.assertTrue(self.service.order("PO1")["review_required"])

    def test_order_impact_history_records_each_change(self) -> None:
        self._order()
        self.service.open_quality_event("qa", {
            "event_id": "EV2", "supplier_id": "S1", "material_code": MATERIAL, "plant_id": "P1",
            "severity": "major", "title": "外观", "detail": "划伤", "occurred_on": "2026-09-01"})
        self.service.suspend_supplier("qa", {
            "suspension_id": "S6", "supplier_id": "S1", "material_code": MATERIAL,
            "plant_id": "P1", "reason_event_id": "EV2", "reason_text": "暂停"})
        self.service.close_quality_event("qa", "EV2", "返工")
        history = self.service.order_impact_history("buyer", "PO1")
        kinds = [e["change_kind"] for e in history["events"]]
        self.assertEqual(kinds, ["event.opened", "suspension.started", "event.closed"])
        self.assertTrue(all(e["classification"] == "review" for e in history["events"]))

    def test_audit_chain_detects_tampering(self) -> None:
        self._order()
        self.assertTrue(self.service.audit_chain("auditor")["valid"])
        self.connection.execute("UPDATE qual_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("auditor")["valid"])

    def test_validation_rejects_bad_dates_and_quantities(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.register_certificate("qe", {
                "certificate_id": "CBAD", "supplier_id": "S1", "cert_type": "ISO9001",
                "cert_name": "x", "scope_text": "*",
                "valid_from": "2027-01-01", "valid_until": "2026-01-01"})
        with self.assertRaises(ValidationFailed):
            self.service.create_order("buyer", {
                "order_id": "BAD", "supplier_id": "S1", "material_code": MATERIAL,
                "quantity": "0", "unit": "kg", "expect_delivery_on": "2026-10-01"})


class ImpactClassificationTests(unittest.TestCase):
    def test_open_in_scope_is_review(self) -> None:
        self.assertEqual(classify_impact("open", "M", "P", None, None), "review")
        self.assertEqual(classify_impact("open", "M", "P", "M", "P"), "review")

    def test_shipped_and_accepted_are_retained(self) -> None:
        for state in ("intransit", "received"):
            self.assertEqual(classify_impact(state, "M", "P", "M", "P"), "retained")

    def test_out_of_scope_or_cancelled_is_none(self) -> None:
        self.assertEqual(classify_impact("open", "M1", "P", "M2", "P"), "none")
        self.assertEqual(classify_impact("open", "M", "P1", "M", "P2"), "none")
        self.assertEqual(classify_impact("cancelled", "M", "P", "M", "P"), "none")


class ApiBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = QualificationService(
            self.connection, FrozenClock(datetime(2026, 9, 1, tzinfo=timezone.utc)))
        self.app = JsonApplication(self.service)
        self.service.create_user("qe", "qe", "qual_engineer")

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_missing_actor(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("GET", "/orders")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_unknown_route_and_forbidden(self) -> None:
        response = self.app.handle("POST", "/certificates", {"X-Actor-Id": "qe"},
                                   b'{"certificate_id":"C","supplier_id":"S","cert_type":"BAD",'
                                   b'"cert_name":"x","scope_text":"*",'
                                   b'"valid_from":"2026-01-01","valid_until":"2027-01-01"}')
        self.assertEqual(response.status, 422)
        missing = self.app.handle("GET", f"/certificates/{'NOPE'}", {"X-Actor-Id": "qe"})
        self.assertEqual(missing.status, 404)


if __name__ == "__main__":
    unittest.main()
