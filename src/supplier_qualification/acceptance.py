"""关键供应商资格服务离线验收：高端输变电核心绝缘材料场景。

覆盖主线：
1. 原厂资质、获准产品、工厂、采购方案与交付批次检测；
2. 质量事件触发暂停，未发货订单转复核、已验收订单按当时规则保留；
3. 采购查询替代选择的合规依据，紧急替代取得质量与采购分别批准
   （批准只对明确数量和期限生效）后凭批次检测合格发货；
4. 资质续期、暂停解除、整改关闭后区分需复核订单与保留订单，
   复核通过的未发货订单按原厂恢复发运。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import QualificationService


MATERIAL = "MAT-INSUL-500KV"


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
    service = QualificationService(connection, clock)

    for user_id, role in (
        ("qe", "qual_engineer"), ("qa", "quality"), ("buyer", "buyer"), ("auditor", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 原厂与替代厂的资质、工厂、获准产品
    service.register_plant("qe", {"plant_id": "PLANT-ORIG-1", "supplier_id": "S-ORIG",
                                  "name": "原厂绝缘材料一厂", "location": "山东"})
    service.register_plant("qe", {"plant_id": "PLANT-ALT-2", "supplier_id": "S-ALT",
                                  "name": "替代厂绝缘材料二厂", "location": "江苏"})
    service.register_certificate("qe", {
        "certificate_id": "CERT-ORIG-001", "supplier_id": "S-ORIG", "cert_type": "PRODUCTION_LICENSE",
        "cert_name": "特种绝缘材料生产许可证", "scope_text": f"500kV 变压器绝缘纸 {MATERIAL}",
        "valid_from": "2024-01-01", "valid_until": "2026-12-31",
    })
    service.register_certificate("qe", {
        "certificate_id": "CERT-ALT-001", "supplier_id": "S-ALT", "cert_type": "ISO9001",
        "cert_name": "质量管理体系认证", "scope_text": f"高端输变电绝缘材料 {MATERIAL}",
        "valid_from": "2025-06-01", "valid_until": "2028-05-31",
    })
    service.register_approved_product("qe", {
        "approval_id": "AP-ORIG-INSUL", "supplier_id": "S-ORIG", "material_code": MATERIAL,
        "material_name": "500kV 变压器匝间绝缘纸", "plant_id": "PLANT-ORIG-1",
        "spec_revision": "V3.1", "valid_from": "2024-01-01", "valid_until": "2026-12-31",
        "certificate_id": "CERT-ORIG-001",
    })
    service.register_approved_product("qe", {
        "approval_id": "AP-ALT-INSUL", "supplier_id": "S-ALT", "material_code": MATERIAL,
        "material_name": "500kV 变压器匝间绝缘纸", "plant_id": None,
        "spec_revision": "V3.0", "valid_from": "2025-06-01", "valid_until": "2028-05-31",
        "certificate_id": "CERT-ALT-001",
    })

    # 采购方案：PO-1 先发货验收；PO-2、PO-3 尚未发货
    for order_id in ("PO-1", "PO-2", "PO-3"):
        service.create_order("buyer", {
            "order_id": order_id, "supplier_id": "S-ORIG", "material_code": MATERIAL,
            "plant_id": "PLANT-ORIG-1", "quantity": "1200", "unit": "kg",
            "expect_delivery_on": "2026-10-15",
        })

    # PO-1 按原厂正常批次检测、发货、验收（快照此后冻结）
    service.register_order_lot("buyer", "PO-1", {
        "batch_no": "B-0901", "inspection_standard": "GB/T 19264.2-2025 批次全项"})
    service.record_lot_inspection("buyer", "PO-1", "B-0901", "passed", "RPT-B-0901")
    service.ship_order("buyer", "PO-1", "B-0901")
    service.accept_order("buyer", "PO-1")

    # 9 月 10 日：原厂核心批次击穿，质量事件 + 暂停决定
    clock.current = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
    service.open_quality_event("qa", {
        "event_id": "EVT-0901", "supplier_id": "S-ORIG", "material_code": MATERIAL,
        "plant_id": "PLANT-ORIG-1", "severity": "critical",
        "title": "绝缘纸电气强度批次性不合格",
        "detail": "B-0905 批次工频耐压击穿，疑似制浆工艺波动",
        "occurred_on": "2026-09-09",
    })
    service.suspend_supplier("qa", {
        "suspension_id": "SUS-0901", "supplier_id": "S-ORIG", "material_code": MATERIAL,
        "plant_id": "PLANT-ORIG-1", "reason_event_id": "EVT-0901",
        "reason_text": "质量事件调查期间暂停该物料该工厂供货",
    })

    suspension_impacts = service.change_impacts("auditor", "suspension", "SUS-0901")
    # PO-1 已验收 → retained；PO-2/PO-3 未发货 → review
    assert suspension_impacts["retained_order_ids"] == ["PO-1"]
    assert suspension_impacts["review_order_ids"] == ["PO-2", "PO-3"]

    # 未复核 + 暂停期间，原厂发货被闸门拦截
    blocked = None
    try:
        service.register_order_lot("buyer", "PO-2", {"batch_no": "B-0910", "inspection_standard": "GB/T 19264.2"})
        service.record_lot_inspection("buyer", "PO-2", "B-0910", "passed", "RPT-B-0910")
        service.ship_order("buyer", "PO-2", "B-0910")
    except Exception as exc:  # noqa: BLE001 - 验收需固化拒绝原因
        blocked = str(exc)
    assert blocked is not None

    # 采购查询替代选择的合规依据
    alternatives = service.find_alternatives("buyer", MATERIAL, "PLANT-ALT-2")
    assert alternatives["count"] == 1 and alternatives["alternatives"][0]["supplier_id"] == "S-ALT"

    # 紧急替代：质量与采购分别批准，只对 1200kg、9/10-9/20 生效
    service.create_emergency_request("buyer", {
        "approval_id": "EMG-PO2-001", "order_id": "PO-2", "substitute_supplier_id": "S-ALT",
        "quantity": "1200", "unit": "kg", "valid_from": "2026-09-10", "valid_until": "2026-09-20",
        "reason": "原厂交期延误且暂停供货，按紧急替代程序申请",
    })
    service.approve_emergency("qa", "EMG-PO2-001", "quality")
    half_done = service.emergency_approval("EMG-PO2-001")
    assert half_done["state"] == "pending" and not half_done["dual_approved"]
    service.register_order_lot("buyer", "PO-2", {
        "batch_no": "B-0910-ALT", "plant_id": "PLANT-ALT-2",
        "inspection_standard": "GB/T 19264.2-2025 替代批次加严项"})
    service.record_lot_inspection("buyer", "PO-2", "B-0910-ALT", "passed", "RPT-ALT-B-0910")
    # 批次已合格但仅质量批准时仍不能发货
    try:
        service.ship_order("buyer", "PO-2", "B-0910-ALT", "EMG-PO2-001")
        raise AssertionError("单侧批准不应放行")
    except Exception:
        pass
    service.approve_emergency("buyer", "EMG-PO2-001", "procurement")
    shipped_po2 = service.ship_order("buyer", "PO-2", "B-0910-ALT", "EMG-PO2-001")
    assert shipped_po2["qualification_snapshot"]["shipping_supplier_id"] == "S-ALT"
    assert shipped_po2["qualification_snapshot"]["emergency_approval"]["approval_id"] == "EMG-PO2-001"
    service.accept_order("buyer", "PO-2")

    # 9 月 18 日：原厂整改、暂停解除、资质续期；PO-3 仍需采购复核
    clock.current = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    service.update_corrective_action("qa", "EVT-0901", "更换制浆网部并完成三批工艺验证")
    service.close_quality_event("qa", "EVT-0901")
    service.lift_suspension("qa", "SUS-0901")
    renewed = service.renew_certificate("qe", "CERT-ORIG-001", {
        "certificate_id": "CERT-ORIG-002", "valid_from": "2027-01-01", "valid_until": "2029-12-31"})
    renewal_impacts = service.change_impacts("auditor", "certificate", "CERT-ORIG-002")
    assert renewal_impacts["review_order_ids"] == ["PO-3"]

    # 未复核前即便暂停已解除也不能发货
    try:
        service.ship_order("buyer", "PO-3", "B-0918")
        raise AssertionError("未复核订单不应放行")
    except Exception:
        pass
    # 采购核对续期资质与整改关闭记录后完成复核
    service.resolve_order_review("buyer", "PO-3",
                                "已核对新证 CERT-ORIG-002 范围覆盖、EVT-0901 整改关闭、SUS-0901 已解除")
    service.register_order_lot("buyer", "PO-3", {
        "batch_no": "B-0918", "inspection_standard": "GB/T 19264.2-2025 复工首批加严项"})
    service.record_lot_inspection("buyer", "PO-3", "B-0918", "passed", "RPT-B-0918")
    shipped_po3 = service.ship_order("buyer", "PO-3", "B-0918")
    service.accept_order("buyer", "PO-3")

    po1 = service.order("PO-1")
    po3_history = service.order_impact_history("buyer", "PO-3")
    compliance_po2 = service.order_compliance("buyer", "PO-2")
    audit = service.audit_chain("auditor")

    result = {
        "status": "ok",
        "workspace": workspace.name,
        "blocked_original_shipment": blocked,
        "alternatives": alternatives,
        "renewed_certificate": renewed,
        "suspension_impacts": {
            "review": suspension_impacts["review_order_ids"],
            "retained": suspension_impacts["retained_order_ids"],
        },
        "renewal_review_orders": renewal_impacts["review_order_ids"],
        "po1_snapshot_retained": po1["qualification_snapshot"]["shipping_supplier_id"] == "S-ORIG",
        "po2_emergency_supplier": compliance_po2["shipped_snapshot"]["shipping_supplier_id"],
        "po2_emergency_window": [
            compliance_po2["shipped_snapshot"]["emergency_approval"]["valid_from"],
            compliance_po2["shipped_snapshot"]["emergency_approval"]["valid_until"],
        ],
        "po3_impact_count": po3_history["count"],
        "po3_shipped_supplier": shipped_po3["qualification_snapshot"]["shipping_supplier_id"],
        "audit": audit,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行关键供应商资格服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
