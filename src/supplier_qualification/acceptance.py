"""贯通资质目录、订单核对、暂停、紧急替代与整改关闭的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import QualificationService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
    service = QualificationService(connection, clock)
    for user_id, role in (
        ("admin", "qualification_admin"),
        ("quality", "quality"),
        ("buyer", "buyer"),
        ("lead", "procurement_lead"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    service.create_supplier("admin", {"supplier_id": "sup-dongfang", "name": "东方绝缘材料股份", "credit_code": "91510100MA6CET0001"})
    service.create_factory("admin", "sup-dongfang", {"factory_id": "fac-deyang", "name": "德阳绝缘材料厂", "address": "四川省德阳市"})
    service.create_qualification("admin", "sup-dongfang", {
        "qualification_id": "qual-df-1000",
        "qual_type": "type_test",
        "standard_no": "GB/T 1303",
        "product_category": "ins-board",
        "voltage_level_kv": 1000,
        "scope_text": "1000kV 及以下输变电设备用绝缘纸板",
        "valid_from": "2026-01-01",
        "valid_until": "2026-12-31",
    })
    service.create_approved_product("admin", "sup-dongfang", {
        "factory_id": "fac-deyang",
        "product_code": "IBD-T4",
        "product_category": "ins-board",
        "qualification_id": "qual-df-1000",
    })
    service.create_contract("admin", {
        "contract_id": "HT-2026-018",
        "supplier_id": "sup-dongfang",
        "title": "特高压工程绝缘纸板年度框架（仅覆盖德阳原厂）",
        "valid_from": "2026-01-01",
        "valid_until": "2026-12-31",
        "scopes": [{"product_code": "IBD-T4", "factory_id": "fac-deyang"}],
    })

    accepted = service.create_order("buyer", {
        "order_id": "PO-1001", "contract_id": "HT-2026-018", "supplier_id": "sup-dongfang",
        "factory_id": "fac-deyang", "product_code": "IBD-T4", "product_category": "ins-board",
        "required_voltage_kv": 500, "quantity": "12", "unit": "吨",
        "planned_delivery_on": "2026-09-30", "idempotency_key": "po-1001-key",
    })
    service.ship_order("buyer", "PO-1001", 1)
    clock.advance(days=2)
    service.accept_order("buyer", "PO-1001", 2)

    delayed = service.create_order("buyer", {
        "order_id": "PO-1002", "contract_id": "HT-2026-018", "supplier_id": "sup-dongfang",
        "factory_id": "fac-deyang", "product_code": "IBD-T4", "product_category": "ins-board",
        "required_voltage_kv": 500, "quantity": "8", "unit": "吨",
        "planned_delivery_on": "2026-10-20", "idempotency_key": "po-1002-key",
    })

    clock.advance(days=3)
    event = service.record_quality_event("quality", "sup-dongfang", {
        "event_id": "QE-0901", "product_code": "IBD-T4", "factory_id": "fac-deyang",
        "severity": "critical", "description": "出厂批次击穿电压复测不合格",
        "occurred_on": "2026-09-28",
    })
    suspension = service.decide_suspension("quality", "sup-dongfang", {
        "suspension_id": "SUS-0901", "product_code": "IBD-T4", "factory_id": "fac-deyang",
        "reason": "重大质量事件调查期间暂停发货", "effective_from": "2026-09-29",
    })

    service.create_supplier("admin", {"supplier_id": "sup-nanyang", "name": "南洋电工材料", "credit_code": "91440300MA5F000002"})
    service.create_factory("admin", "sup-nanyang", {"factory_id": "fac-zz", "name": "郑州绝缘材料厂", "address": "河南省郑州市"})
    service.create_qualification("admin", "sup-nanyang", {
        "qualification_id": "qual-ny-1000",
        "qual_type": "type_test",
        "standard_no": "GB/T 1303",
        "product_category": "ins-board",
        "voltage_level_kv": 1000,
        "scope_text": "1000kV 及以下输变电设备用绝缘纸板",
        "valid_from": "2026-03-01",
        "valid_until": "2027-02-28",
    })
    service.create_approved_product("admin", "sup-nanyang", {
        "factory_id": "fac-zz", "product_code": "IBD-T4N",
        "product_category": "ins-board", "qualification_id": "qual-ny-1000",
    })

    substitution = service.request_substitution("buyer", {
        "substitution_id": "SUB-0001", "order_id": "PO-1002",
        "substitute_supplier_id": "sup-nanyang", "substitute_factory_id": "fac-zz",
        "substitute_product_code": "IBD-T4N", "quantity": "8", "needed_by": "2026-10-25",
        "reason": "原厂重大质量事件导致交期延误", "idempotency_key": "sub-0001-key",
    })
    service.approve_substitution("quality", "SUB-0001", {
        "approval_type": "quality", "decision": "approved",
        "approved_quantity": "8", "valid_until": "2026-11-30",
        "note": "替代供方型式试验覆盖 500kV 需求，批次检测报告齐全",
    })
    service.approve_substitution("lead", "SUB-0001", {
        "approval_type": "procurement", "decision": "approved",
        "approved_quantity": "8", "valid_until": "2026-11-15",
        "note": "价格与交期确认，仅限本申请数量",
    })
    applied = service.apply_substitution("buyer", "SUB-0001", "PO-1002S")

    clock.advance(days=10)
    closure = service.close_quality_event("quality", "QE-0901", "整改完成，复测合格，工艺纠正措施已验证", 1)
    lifted = service.lift_suspension("quality", "SUS-0901", 1)
    renewal = service.renew_qualification("admin", "qual-df-1000", {
        "valid_until": "2027-12-31", "note": "型式试验复评通过，续期一年",
    })

    result = {
        "status": "ok",
        "accepted_order": accepted,
        "delayed_order": delayed,
        "quality_event_impacts": event["impacts"],
        "suspension_impacts": suspension["impacts"],
        "substitution": substitution,
        "applied": applied,
        "closure_impacts": closure["impacts"],
        "lifted_impacts": lifted["impacts"],
        "renewal_impacts": renewal["impacts"],
        "suspension_change": service.change_impacts("buyer", suspension["change_id"]),
        "substitution_basis": service.substitution_detail("buyer", "SUB-0001")["basis"],
        "supplier_changes": len(service.supplier_changes("buyer", "sup-dongfang")["changes"]),
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
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
