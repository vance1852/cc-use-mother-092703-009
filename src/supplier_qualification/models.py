"""关键供应商资格服务的输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
PRODUCT_CATEGORIES = {"ins-paper", "ins-board", "ins-oil", "epoxy-resin", "ins-film"}
QUAL_TYPES = {"type_test", "iso9001", "grid_admission", "industry_cert"}
SEVERITIES = {"minor", "major", "critical"}
APPROVAL_TYPES = {"quality", "procurement"}
APPROVAL_DECISIONS = {"approved", "rejected"}


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
    if value is None:
        return None
    return identifier(value, field)


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def voltage_level(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1500:
        raise ValidationFailed(f"{field} 必须是 1 到 1500 的整数千伏")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def optional_date(value: object, field: str) -> str | None:
    if value is None:
        return None
    return date_text(value, field)


def product_category(value: object, field: str = "product_category") -> str:
    result = required_text(value, field, 32)
    if result not in PRODUCT_CATEGORIES:
        raise ValidationFailed(f"{field} 必须是 {sorted(PRODUCT_CATEGORIES)} 之一")
    return result


@dataclass(frozen=True, slots=True)
class SupplierSpec:
    supplier_id: str
    name: str
    credit_code: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SupplierSpec":
        return cls(
            supplier_id=identifier(raw.get("supplier_id"), "supplier_id"),
            name=required_text(raw.get("name"), "name"),
            credit_code=required_text(raw.get("credit_code"), "credit_code", 32),
        )


@dataclass(frozen=True, slots=True)
class FactorySpec:
    factory_id: str
    name: str
    address: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FactorySpec":
        return cls(
            factory_id=identifier(raw.get("factory_id"), "factory_id"),
            name=required_text(raw.get("name"), "name"),
            address=required_text(raw.get("address"), "address", 512),
        )


@dataclass(frozen=True, slots=True)
class QualificationSpec:
    qualification_id: str
    qual_type: str
    standard_no: str
    product_category: str
    voltage_level_kv: int
    scope_text: str
    valid_from: str
    valid_until: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QualificationSpec":
        qual_type = required_text(raw.get("qual_type"), "qual_type", 32)
        if qual_type not in QUAL_TYPES:
            raise ValidationFailed(f"qual_type 必须是 {sorted(QUAL_TYPES)} 之一")
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_until = date_text(raw.get("valid_until"), "valid_until")
        if valid_from > valid_until:
            raise ValidationFailed("valid_from 不能晚于 valid_until")
        return cls(
            qualification_id=identifier(raw.get("qualification_id"), "qualification_id"),
            qual_type=qual_type,
            standard_no=required_text(raw.get("standard_no"), "standard_no", 64),
            product_category=product_category(raw.get("product_category")),
            voltage_level_kv=voltage_level(raw.get("voltage_level_kv"), "voltage_level_kv"),
            scope_text=required_text(raw.get("scope_text"), "scope_text", 512),
            valid_from=valid_from,
            valid_until=valid_until,
        )


@dataclass(frozen=True, slots=True)
class ApprovedProductSpec:
    factory_id: str
    product_code: str
    product_category: str
    qualification_id: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApprovedProductSpec":
        return cls(
            factory_id=identifier(raw.get("factory_id"), "factory_id"),
            product_code=identifier(raw.get("product_code"), "product_code"),
            product_category=product_category(raw.get("product_category")),
            qualification_id=identifier(raw.get("qualification_id"), "qualification_id"),
        )


@dataclass(frozen=True, slots=True)
class ContractScope:
    product_code: str
    factory_id: str


@dataclass(frozen=True, slots=True)
class ContractSpec:
    contract_id: str
    supplier_id: str
    title: str
    valid_from: str
    valid_until: str
    scopes: tuple[ContractScope, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ContractSpec":
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_until = date_text(raw.get("valid_until"), "valid_until")
        if valid_from > valid_until:
            raise ValidationFailed("valid_from 不能晚于 valid_until")
        raw_scopes = raw.get("scopes")
        if not isinstance(raw_scopes, list) or not 1 <= len(raw_scopes) <= 50:
            raise ValidationFailed("scopes 必须是 1 到 50 条的数组")
        scopes = []
        seen = set()
        for index, item in enumerate(raw_scopes):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"scopes[{index}] 必须是对象")
            scope = ContractScope(
                product_code=identifier(item.get("product_code"), f"scopes[{index}].product_code"),
                factory_id=identifier(item.get("factory_id"), f"scopes[{index}].factory_id"),
            )
            if scope in seen:
                raise ValidationFailed("scopes 中存在重复的产品工厂组合")
            seen.add(scope)
            scopes.append(scope)
        return cls(
            contract_id=identifier(raw.get("contract_id"), "contract_id"),
            supplier_id=identifier(raw.get("supplier_id"), "supplier_id"),
            title=required_text(raw.get("title"), "title"),
            valid_from=valid_from,
            valid_until=valid_until,
            scopes=tuple(scopes),
        )


@dataclass(frozen=True, slots=True)
class QualityEventSpec:
    event_id: str
    factory_id: str | None
    product_code: str | None
    severity: str
    description: str
    occurred_on: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QualityEventSpec":
        severity = required_text(raw.get("severity"), "severity", 16)
        if severity not in SEVERITIES:
            raise ValidationFailed(f"severity 必须是 {sorted(SEVERITIES)} 之一")
        return cls(
            event_id=identifier(raw.get("event_id"), "event_id"),
            factory_id=optional_identifier(raw.get("factory_id"), "factory_id"),
            product_code=optional_identifier(raw.get("product_code"), "product_code"),
            severity=severity,
            description=required_text(raw.get("description"), "description", 1024),
            occurred_on=date_text(raw.get("occurred_on"), "occurred_on"),
        )


@dataclass(frozen=True, slots=True)
class SuspensionSpec:
    suspension_id: str
    product_code: str | None
    factory_id: str | None
    reason: str
    effective_from: str
    effective_until: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SuspensionSpec":
        effective_from = date_text(raw.get("effective_from"), "effective_from")
        effective_until = optional_date(raw.get("effective_until"), "effective_until")
        if effective_until is not None and effective_until < effective_from:
            raise ValidationFailed("effective_until 不能早于 effective_from")
        return cls(
            suspension_id=identifier(raw.get("suspension_id"), "suspension_id"),
            product_code=optional_identifier(raw.get("product_code"), "product_code"),
            factory_id=optional_identifier(raw.get("factory_id"), "factory_id"),
            reason=required_text(raw.get("reason"), "reason", 1024),
            effective_from=effective_from,
            effective_until=effective_until,
        )


@dataclass(frozen=True, slots=True)
class OrderSpec:
    order_id: str
    contract_id: str
    supplier_id: str
    factory_id: str
    product_code: str
    product_category: str
    required_voltage_kv: int
    quantity: Decimal
    unit: str
    planned_delivery_on: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OrderSpec":
        return cls(
            order_id=identifier(raw.get("order_id"), "order_id"),
            contract_id=identifier(raw.get("contract_id"), "contract_id"),
            supplier_id=identifier(raw.get("supplier_id"), "supplier_id"),
            factory_id=identifier(raw.get("factory_id"), "factory_id"),
            product_code=identifier(raw.get("product_code"), "product_code"),
            product_category=product_category(raw.get("product_category")),
            required_voltage_kv=voltage_level(raw.get("required_voltage_kv"), "required_voltage_kv"),
            quantity=decimal_value(raw.get("quantity"), "quantity", minimum=Decimal("0.001")),
            unit=required_text(raw.get("unit"), "unit", 16),
            planned_delivery_on=date_text(raw.get("planned_delivery_on"), "planned_delivery_on"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SubstitutionSpec:
    substitution_id: str
    order_id: str
    substitute_supplier_id: str
    substitute_factory_id: str
    substitute_product_code: str
    quantity: Decimal
    needed_by: str
    reason: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SubstitutionSpec":
        return cls(
            substitution_id=identifier(raw.get("substitution_id"), "substitution_id"),
            order_id=identifier(raw.get("order_id"), "order_id"),
            substitute_supplier_id=identifier(raw.get("substitute_supplier_id"), "substitute_supplier_id"),
            substitute_factory_id=identifier(raw.get("substitute_factory_id"), "substitute_factory_id"),
            substitute_product_code=identifier(raw.get("substitute_product_code"), "substitute_product_code"),
            quantity=decimal_value(raw.get("quantity"), "quantity", minimum=Decimal("0.001")),
            needed_by=date_text(raw.get("needed_by"), "needed_by"),
            reason=required_text(raw.get("reason"), "reason", 1024),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ApprovalSpec:
    approval_type: str
    decision: str
    approved_quantity: Decimal
    valid_until: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApprovalSpec":
        approval_type = required_text(raw.get("approval_type"), "approval_type", 16)
        if approval_type not in APPROVAL_TYPES:
            raise ValidationFailed("approval_type 必须是 quality 或 procurement")
        decision = required_text(raw.get("decision"), "decision", 16)
        if decision not in APPROVAL_DECISIONS:
            raise ValidationFailed("decision 必须是 approved 或 rejected")
        return cls(
            approval_type=approval_type,
            decision=decision,
            approved_quantity=decimal_value(
                raw.get("approved_quantity"), "approved_quantity", minimum=Decimal("0.001")
            ),
            valid_until=date_text(raw.get("valid_until"), "valid_until"),
            note=required_text(raw.get("note"), "note", 1024),
        )
