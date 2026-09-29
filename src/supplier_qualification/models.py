"""供应商资格领域输入契约与纯规则计算。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
CERT_TYPES = {"ISO9001", "ISO14001", "PRODUCTION_LICENSE", "NETWORK_ACCESS", "SPECIAL_EQUIPMENT", "CUSTOM"}
SEVERITIES = {"minor", "major", "critical"}
# 未发货口径：只有 open 订单会被资格变化置为待复核；
# intransit 已固化发货时合规快照，与 received 一样按当时规则保留。
REVIEW_ORDER_STATES = ("open",)
RETAINED_ORDER_STATES = ("intransit", "received")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def optional_identifier(value: object, field: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return identifier(value, field)


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def quantity_value(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite() or result <= 0:
        raise ValidationFailed(f"{field} 必须是正数")
    return result.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def date_range(raw: Mapping[str, Any], start_field: str = "valid_from", end_field: str = "valid_until") -> tuple[str, str]:
    start = date_text(raw.get(start_field), start_field)
    end = date_text(raw.get(end_field), end_field)
    if end <= start:
        raise ValidationFailed(f"{end_field} 必须晚于 {start_field}")
    return start, end


@dataclass(frozen=True, slots=True)
class CertificateInput:
    certificate_id: str
    supplier_id: str
    cert_type: str
    cert_name: str
    scope_text: str
    valid_from: str
    valid_until: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CertificateInput":
        cert_type = required_text(raw.get("cert_type"), "cert_type", 32).upper()
        if cert_type not in CERT_TYPES:
            raise ValidationFailed("cert_type 不受支持")
        valid_from, valid_until = date_range(raw)
        return cls(
            certificate_id=identifier(raw.get("certificate_id"), "certificate_id"),
            supplier_id=identifier(raw.get("supplier_id"), "supplier_id"),
            cert_type=cert_type,
            cert_name=required_text(raw.get("cert_name"), "cert_name"),
            scope_text=required_text(raw.get("scope_text"), "scope_text", 1024),
            valid_from=valid_from,
            valid_until=valid_until,
        )


@dataclass(frozen=True, slots=True)
class ApprovedProductInput:
    approval_id: str
    supplier_id: str
    material_code: str
    material_name: str
    plant_id: str | None
    spec_revision: str | None
    valid_from: str
    valid_until: str
    certificate_id: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApprovedProductInput":
        valid_from, valid_until = date_range(raw)
        return cls(
            approval_id=identifier(raw.get("approval_id"), "approval_id"),
            supplier_id=identifier(raw.get("supplier_id"), "supplier_id"),
            material_code=identifier(raw.get("material_code"), "material_code"),
            material_name=required_text(raw.get("material_name"), "material_name"),
            plant_id=optional_identifier(raw.get("plant_id"), "plant_id"),
            spec_revision=optional_identifier(raw.get("spec_revision"), "spec_revision"),
            valid_from=valid_from,
            valid_until=valid_until,
            certificate_id=optional_identifier(raw.get("certificate_id"), "certificate_id"),
        )


@dataclass(frozen=True, slots=True)
class QualityEventInput:
    event_id: str
    supplier_id: str
    severity: str
    title: str
    detail: str
    occurred_on: str
    material_code: str | None
    plant_id: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QualityEventInput":
        severity = required_text(raw.get("severity"), "severity", 16).lower()
        if severity not in SEVERITIES:
            raise ValidationFailed("severity 必须是 minor、major 或 critical")
        return cls(
            event_id=identifier(raw.get("event_id"), "event_id"),
            supplier_id=identifier(raw.get("supplier_id"), "supplier_id"),
            severity=severity,
            title=required_text(raw.get("title"), "title"),
            detail=required_text(raw.get("detail"), "detail", 2048),
            occurred_on=date_text(raw.get("occurred_on"), "occurred_on"),
            material_code=optional_identifier(raw.get("material_code"), "material_code"),
            plant_id=optional_identifier(raw.get("plant_id"), "plant_id"),
        )


@dataclass(frozen=True, slots=True)
class PurchaseOrderInput:
    order_id: str
    supplier_id: str
    material_code: str
    plant_id: str | None
    quantity: Decimal
    unit: str
    expect_delivery_on: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PurchaseOrderInput":
        return cls(
            order_id=identifier(raw.get("order_id"), "order_id"),
            supplier_id=identifier(raw.get("supplier_id"), "supplier_id"),
            material_code=identifier(raw.get("material_code"), "material_code"),
            plant_id=optional_identifier(raw.get("plant_id"), "plant_id"),
            quantity=quantity_value(raw.get("quantity"), "quantity"),
            unit=required_text(raw.get("unit"), "unit", 16),
            expect_delivery_on=date_text(raw.get("expect_delivery_on"), "expect_delivery_on"),
        )


@dataclass(frozen=True, slots=True)
class EmergencyRequestInput:
    approval_id: str
    order_id: str
    substitute_supplier_id: str
    quantity: Decimal
    unit: str
    valid_from: str
    valid_until: str
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EmergencyRequestInput":
        valid_from, valid_until = date_range(raw)
        return cls(
            approval_id=identifier(raw.get("approval_id"), "approval_id"),
            order_id=identifier(raw.get("order_id"), "order_id"),
            substitute_supplier_id=identifier(raw.get("substitute_supplier_id"), "substitute_supplier_id"),
            quantity=quantity_value(raw.get("quantity"), "quantity"),
            unit=required_text(raw.get("unit"), "unit", 16),
            valid_from=valid_from,
            valid_until=valid_until,
            reason=required_text(raw.get("reason"), "reason", 1024),
        )


def _scope_overlaps(order_material: str | None, order_plant: str | None,
                    change_material: str | None, change_plant: str | None) -> bool:
    """变化范围（NULL 表示全部）是否覆盖订单的物料/工厂维度。"""
    if change_material is not None and change_material != order_material:
        return False
    if change_plant is not None and change_plant != order_plant:
        return False
    return True


def certificate_covers(scope_text: str, material_name: str, material_code: str) -> bool:
    """资质有效范围文本是否覆盖目标物料（同时记录物料编码与名称时更严格）。"""
    scope = scope_text.upper()
    return "*" in scope or material_code.upper() in scope or material_name.upper() in scope


def classify_impact(order_state: str, order_material: str | None, order_plant: str | None,
                    change_material: str | None, change_plant: str | None) -> str:
    """资格变化对单个订单的影响分类。

    review  — 未发货（open）且在变化范围内，需要复核；
    retained— 已发货（intransit，含发货快照）或已完成验收（received），
              按发运/验收当时规则保留，不回溯；
    none    — 已取消或不在变化范围内。
    """
    if not _scope_overlaps(order_material, order_plant, change_material, change_plant):
        return "none"
    if order_state in REVIEW_ORDER_STATES:
        return "review"
    if order_state in RETAINED_ORDER_STATES:
        return "retained"
    return "none"
