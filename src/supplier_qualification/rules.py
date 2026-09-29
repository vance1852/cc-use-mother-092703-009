"""交付批次适用性核对与资格变化影响分类的确定性规则。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence


def quantize_quantity(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DeliverySpec:
    """一次交付批次需要满足的供应来源。"""

    supplier_id: str
    factory_id: str
    product_code: str
    product_category: str
    required_voltage_kv: int


def suspension_covers(row: Mapping[str, object], product_code: str, factory_id: str) -> bool:
    product = row["product_code"]
    factory = row["factory_id"]
    return (product is None or product == product_code) and (factory is None or factory == factory_id)


def suspension_active_on(row: Mapping[str, object], as_of: str) -> bool:
    if row["effective_from"] > as_of:
        return False
    if row["effective_until"] is not None and row["effective_until"] < as_of:
        return False
    return row["lifted_on"] is None or row["lifted_on"] > as_of


def event_covers(row: Mapping[str, object], product_code: str, factory_id: str) -> bool:
    product = row["product_code"]
    factory = row["factory_id"]
    return (product is None or product == product_code) and (factory is None or factory == factory_id)


def event_open_on(row: Mapping[str, object], as_of: str) -> bool:
    if row["occurred_on"] > as_of:
        return False
    return row["closed_on"] is None or row["closed_on"] > as_of


def qualification_valid_on(row: Mapping[str, object], as_of: str) -> bool:
    if row["valid_from"] > as_of or row["valid_until"] < as_of:
        return False
    return row["revoked_at"] is None or row["revoked_at"] > as_of


def _finding(rule: str, result: str, message: str) -> dict[str, str]:
    return {"rule": rule, "result": result, "message": message}


def evaluate_applicability(
    *,
    spec: DeliverySpec,
    as_of: str,
    supplier: Mapping[str, object] | None,
    factory: Mapping[str, object] | None,
    contract: Mapping[str, object] | None,
    contract_scopes: Sequence[Mapping[str, object]],
    product_approvals: Sequence[Mapping[str, object]],
    suspensions: Sequence[Mapping[str, object]],
    quality_events: Sequence[Mapping[str, object]],
    emergency: Mapping[str, object] | None,
) -> dict[str, object]:
    """按核对日逐项核对交付批次适用性，返回结构化合规依据。

    emergency 为 None 且 contract 为 None 时（紧急替代申请阶段），合同范围规则
    标记为 emergency_required，不判定整体失败；执行替代时由 emergency 提供
    已批准的替代依据。
    """
    findings: list[dict[str, str]] = []

    if supplier is not None and supplier["active"]:
        findings.append(_finding("supplier_active", "pass", f"供应商 {spec.supplier_id} 在营"))
    else:
        findings.append(_finding("supplier_active", "fail", f"供应商 {spec.supplier_id} 不存在或已停用"))

    if (
        factory is not None
        and factory["supplier_id"] == spec.supplier_id
        and factory["active"]
    ):
        findings.append(_finding("factory_belongs", "pass", f"工厂 {spec.factory_id} 隶属供应商且在营"))
    else:
        findings.append(_finding("factory_belongs", "fail", f"工厂 {spec.factory_id} 不存在、已停用或不属于该供应商"))

    if contract is not None:
        if contract["valid_from"] <= as_of <= contract["valid_until"]:
            covered = any(
                scope["product_code"] == spec.product_code and scope["factory_id"] == spec.factory_id
                for scope in contract_scopes
            )
            if covered:
                findings.append(_finding(
                    "contract_scope", "pass",
                    f"合同 {contract['contract_id']} 在有效期内且约定范围覆盖工厂 {spec.factory_id} 的产品 {spec.product_code}",
                ))
            else:
                findings.append(_finding(
                    "contract_scope", "fail",
                    f"合同 {contract['contract_id']} 约定范围不覆盖工厂 {spec.factory_id} 的产品 {spec.product_code}（合同可能仅覆盖原厂）",
                ))
        else:
            findings.append(_finding(
                "contract_scope", "fail",
                f"合同 {contract['contract_id']} 在核对日 {as_of} 不在有效期（{contract['valid_from']} 至 {contract['valid_until']}）",
            ))
    elif emergency is not None:
        if emergency["approved"] and emergency["valid_until"] >= as_of:
            findings.append(_finding(
                "contract_scope", "pass",
                f"紧急替代批准 {emergency['substitution_id']} 覆盖合同外供应，批准有效期至 {emergency['valid_until']}",
            ))
        else:
            findings.append(_finding(
                "contract_scope", "fail",
                f"紧急替代批准 {emergency['substitution_id']} 未生效或有效期 {emergency['valid_until']} 早于核对日 {as_of}",
            ))
    else:
        findings.append(_finding(
            "contract_scope", "emergency_required",
            "替代供应不在原合同约定范围，需质量与采购分别紧急批准后方可执行",
        ))

    qualification_reasons: list[str] = []
    covering: Mapping[str, object] | None = None
    for approval in product_approvals:
        if approval["state"] != "active":
            qualification_reasons.append(f"获准产品记录 {approval['approval_id']} 已撤回")
            continue
        if approval["qual_category"] != spec.product_category:
            qualification_reasons.append(
                f"资质 {approval['qualification_id']} 产品类别 {approval['qual_category']} 与需求 {spec.product_category} 不符"
            )
            continue
        if approval["voltage_level_kv"] < spec.required_voltage_kv:
            qualification_reasons.append(
                f"资质 {approval['qualification_id']} 电压等级 {approval['voltage_level_kv']}kV 低于需求 {spec.required_voltage_kv}kV"
            )
            continue
        if not qualification_valid_on(approval, as_of):
            if approval["revoked_at"] is not None and approval["revoked_at"] <= as_of:
                qualification_reasons.append(f"资质 {approval['qualification_id']} 已于 {approval['revoked_at']} 撤销")
            else:
                qualification_reasons.append(
                    f"资质 {approval['qualification_id']} 在核对日 {as_of} 不在有效期"
                    f"（{approval['valid_from']} 至 {approval['valid_until']}）"
                )
            continue
        covering = approval
        break
    if covering is not None:
        findings.append(_finding(
            "qualification_coverage", "pass",
            f"获准产品由资质 {covering['qualification_id']}（{covering['standard_no']}，"
            f"{covering['voltage_level_kv']}kV）覆盖，有效期至 {covering['valid_until']}",
        ))
    elif not product_approvals:
        findings.append(_finding(
            "qualification_coverage", "fail",
            f"供应商在工厂 {spec.factory_id} 没有产品 {spec.product_code} 的获准记录",
        ))
    else:
        findings.append(_finding("qualification_coverage", "fail", "；".join(qualification_reasons)))

    active_suspensions = [
        row for row in suspensions
        if suspension_active_on(row, as_of) and suspension_covers(row, spec.product_code, spec.factory_id)
    ]
    if active_suspensions:
        ids = "、".join(str(row["suspension_id"]) for row in active_suspensions)
        findings.append(_finding("no_suspension", "fail", f"核对日存在有效暂停决定：{ids}"))
    else:
        findings.append(_finding("no_suspension", "pass", "核对日无覆盖该批次的有效暂停决定"))

    blocking_events = [
        row for row in quality_events
        if row["severity"] in ("major", "critical")
        and event_open_on(row, as_of)
        and event_covers(row, spec.product_code, spec.factory_id)
    ]
    if blocking_events:
        ids = "、".join(str(row["event_id"]) for row in blocking_events)
        findings.append(_finding("quality_event_clear", "fail", f"核对日存在未关闭的重大质量事件：{ids}"))
    else:
        findings.append(_finding("quality_event_clear", "pass", "核对日无未关闭的重大质量事件"))

    result = "fail" if any(item["result"] == "fail" for item in findings) else "pass"
    return {"as_of_date": as_of, "result": result, "findings": findings}


REVIEW_REQUIRED_STATES = ("cleared", "blocked", "review_required")


def classify_impact(state: str) -> str:
    """按订单当前状态分类资格变化影响。"""
    if state == "accepted":
        return "retained"
    if state == "shipped":
        return "acceptance_pending"
    if state in REVIEW_REQUIRED_STATES:
        return "review_required"
    raise ValueError(f"订单状态 {state} 不参与影响分类")


IMPACT_NOTES = {
    "review_required": "未发货订单，需重新核对交付批次适用性",
    "acceptance_pending": "已发货未验收，到货验收按届时规则核对",
    "retained": "已完成验收，按验收时规则保留",
}
