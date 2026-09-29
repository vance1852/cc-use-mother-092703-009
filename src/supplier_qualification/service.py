"""企业资质、获准产品、质量事件、暂停决定、采购订单核对与紧急替代的应用服务。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    ApprovalSpec,
    ApprovedProductSpec,
    ContractSpec,
    FactorySpec,
    OrderSpec,
    QualificationSpec,
    QualityEventSpec,
    SubstitutionSpec,
    SupplierSpec,
    SuspensionSpec,
    identifier,
    date_text,
    required_text,
)
from .rules import (
    IMPACT_NOTES,
    classify_impact,
    canonical_json,
    decimal_text,
    digest,
    evaluate_applicability,
    quantize_quantity,
    DeliverySpec,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "qualification_admin": {"supplier.write", "catalog.write"},
    "quality": {"quality.event", "quality.suspension", "substitution.approve_quality"},
    "buyer": {"order.write", "substitution.write", "compliance.read", "impact.read"},
    "procurement_lead": {"substitution.approve_procurement", "compliance.read", "impact.read"},
    "auditor": {"audit.read", "compliance.read", "impact.read"},
}

SUBSTITUTABLE_STATES = ("cleared", "blocked", "review_required")
BLOCKING_SEVERITIES = ("major", "critical")


class QualificationService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> str:
        return self._now()[:10]

    def _evaluation_date(self, planned_delivery_on: str) -> str:
        return max(planned_delivery_on, self._today())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM qual_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM qual_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO qual_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _fetch_one(self, sql: str, args: Sequence[object], message: str) -> sqlite3.Row:
        row = self.connection.execute(sql, args).fetchone()
        if row is None:
            raise NotFound(message)
        return row

    def _supplier(self, supplier_id: str) -> sqlite3.Row:
        return self._fetch_one(
            "SELECT * FROM suppliers WHERE supplier_id=?", (supplier_id,), "供应商不存在"
        )

    def _qualification(self, qualification_id: str) -> sqlite3.Row:
        return self._fetch_one(
            "SELECT * FROM qualifications WHERE qualification_id=?",
            (qualification_id,),
            "资质不存在",
        )

    def _order(self, order_id: str) -> sqlite3.Row:
        return self._fetch_one(
            "SELECT * FROM purchase_orders WHERE order_id=?", (order_id,), "采购订单不存在"
        )

    def _substitution(self, substitution_id: str) -> sqlite3.Row:
        return self._fetch_one(
            "SELECT * FROM substitutions WHERE substitution_id=?",
            (substitution_id,),
            "紧急替代申请不存在",
        )

    # ------------------------------------------------------------------
    # 用户与目录
    # ------------------------------------------------------------------

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO qual_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def create_supplier(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "supplier.write")
        spec = SupplierSpec.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO suppliers(supplier_id,name,credit_code,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (spec.supplier_id, spec.name, spec.credit_code, actor_id, self._now()),
                )
                self._audit("supplier", spec.supplier_id, "supplier.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("供应商编号或统一社会信用代码已经存在") from exc
        return {"supplier_id": spec.supplier_id, "name": spec.name}

    def create_factory(self, actor_id: str, supplier_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        self._supplier(supplier_id)
        spec = FactorySpec.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO factories(factory_id,supplier_id,name,address,created_at) VALUES(?,?,?,?,?)",
                    (spec.factory_id, supplier_id, spec.name, spec.address, self._now()),
                )
                self._audit("factory", spec.factory_id, "factory.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("工厂编号冲突或供应商下工厂名称重复") from exc
        return {"factory_id": spec.factory_id, "supplier_id": supplier_id}

    def create_qualification(self, actor_id: str, supplier_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        self._supplier(supplier_id)
        spec = QualificationSpec.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO qualifications(qualification_id,supplier_id,qual_type,standard_no,"
                    "product_category,voltage_level_kv,scope_text,valid_from,valid_until,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        spec.qualification_id,
                        supplier_id,
                        spec.qual_type,
                        spec.standard_no,
                        spec.product_category,
                        spec.voltage_level_kv,
                        spec.scope_text,
                        spec.valid_from,
                        spec.valid_until,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("qualification", spec.qualification_id, "qualification.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("资质编号已经存在") from exc
        return {"qualification_id": spec.qualification_id, "valid_until": spec.valid_until}

    def create_approved_product(
        self, actor_id: str, supplier_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        self._supplier(supplier_id)
        spec = ApprovedProductSpec.from_dict(raw)
        factory = self._fetch_one(
            "SELECT * FROM factories WHERE factory_id=?", (spec.factory_id,), "工厂不存在"
        )
        if factory["supplier_id"] != supplier_id:
            raise ValidationFailed("工厂不属于该供应商")
        qualification = self._qualification(spec.qualification_id)
        if qualification["supplier_id"] != supplier_id:
            raise ValidationFailed("资质不属于该供应商")
        if qualification["product_category"] != spec.product_category:
            raise ValidationFailed("获准产品类别必须落在资质有效范围内")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO approved_products(supplier_id,factory_id,product_code,product_category,"
                    "qualification_id,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        supplier_id,
                        spec.factory_id,
                        spec.product_code,
                        spec.product_category,
                        spec.qualification_id,
                        actor_id,
                        self._now(),
                    ),
                )
                approval_id = int(cursor.lastrowid)
                self._audit(
                    "approved_product", str(approval_id), "approved_product.created", actor_id, raw
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该工厂下产品已有获准记录") from exc
        return {"approval_id": approval_id, "product_code": spec.product_code}

    def create_contract(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        spec = ContractSpec.from_dict(raw)
        self._supplier(spec.supplier_id)
        for scope in spec.scopes:
            factory = self._fetch_one(
                "SELECT * FROM factories WHERE factory_id=?", (scope.factory_id,), "工厂不存在"
            )
            if factory["supplier_id"] != spec.supplier_id:
                raise ValidationFailed("合同约定范围的工厂必须属于合同供应商")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO contracts(contract_id,supplier_id,title,valid_from,valid_until,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        spec.contract_id,
                        spec.supplier_id,
                        spec.title,
                        spec.valid_from,
                        spec.valid_until,
                        actor_id,
                        self._now(),
                    ),
                )
                for scope in spec.scopes:
                    self.connection.execute(
                        "INSERT INTO contract_scopes(contract_id,product_code,factory_id) VALUES(?,?,?)",
                        (spec.contract_id, scope.product_code, scope.factory_id),
                    )
                self._audit(
                    "contract",
                    spec.contract_id,
                    "contract.created",
                    actor_id,
                    {
                        "title": spec.title,
                        "scopes": [
                            {"product_code": scope.product_code, "factory_id": scope.factory_id}
                            for scope in spec.scopes
                        ],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("合同编号已经存在") from exc
        return {"contract_id": spec.contract_id, "scopes": len(spec.scopes)}

    # ------------------------------------------------------------------
    # 交付批次适用性核对
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        *,
        spec: DeliverySpec,
        as_of: str,
        contract_id: str | None,
        emergency: Mapping[str, object] | None,
    ) -> dict[str, object]:
        supplier = self.connection.execute(
            "SELECT * FROM suppliers WHERE supplier_id=?", (spec.supplier_id,)
        ).fetchone()
        factory = self.connection.execute(
            "SELECT * FROM factories WHERE factory_id=?", (spec.factory_id,)
        ).fetchone()
        contract = None
        scopes: list[sqlite3.Row] = []
        if contract_id is not None:
            contract = self.connection.execute(
                "SELECT * FROM contracts WHERE contract_id=?", (contract_id,)
            ).fetchone()
            if contract is None:
                raise NotFound("合同不存在")
            scopes = self.connection.execute(
                "SELECT * FROM contract_scopes WHERE contract_id=? ORDER BY scope_id", (contract_id,)
            ).fetchall()
        approvals = self.connection.execute(
            "SELECT ap.approval_id,ap.state,ap.product_code,q.qualification_id,q.qual_type,"
            "q.standard_no,q.product_category AS qual_category,q.voltage_level_kv,"
            "q.valid_from,q.valid_until,q.revoked_at "
            "FROM approved_products ap "
            "JOIN qualifications q ON q.qualification_id=ap.qualification_id "
            "WHERE ap.supplier_id=? AND ap.factory_id=? AND ap.product_code=? "
            "ORDER BY ap.approval_id",
            (spec.supplier_id, spec.factory_id, spec.product_code),
        ).fetchall()
        suspensions = self.connection.execute(
            "SELECT * FROM suspensions WHERE supplier_id=? ORDER BY suspension_id", (spec.supplier_id,)
        ).fetchall()
        events = self.connection.execute(
            "SELECT * FROM quality_events WHERE supplier_id=? ORDER BY event_id", (spec.supplier_id,)
        ).fetchall()
        return evaluate_applicability(
            spec=spec,
            as_of=as_of,
            supplier=supplier,
            factory=factory,
            contract=contract,
            contract_scopes=scopes,
            product_approvals=approvals,
            suspensions=suspensions,
            quality_events=events,
            emergency=emergency,
        )

    def _record_check(
        self,
        order_id: str,
        phase: str,
        evaluation: Mapping[str, object],
        actor_id: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO compliance_checks(order_id,phase,as_of_date,result,detail_json,checked_by,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                order_id,
                phase,
                str(evaluation["as_of_date"]),
                str(evaluation["result"]),
                canonical_json(evaluation),
                actor_id,
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def _order_spec(self, order: sqlite3.Row) -> DeliverySpec:
        return DeliverySpec(
            supplier_id=order["supplier_id"],
            factory_id=order["factory_id"],
            product_code=order["product_code"],
            product_category=order["product_category"],
            required_voltage_kv=int(order["required_voltage_kv"]),
        )

    def _emergency_context(self, order: sqlite3.Row) -> Mapping[str, object] | None:
        substitution_id = order["substitution_id"]
        if substitution_id is None:
            return None
        approvals = self.connection.execute(
            "SELECT * FROM substitution_approvals WHERE substitution_id=? AND decision='approved'",
            (substitution_id,),
        ).fetchall()
        substitution = self._substitution(substitution_id)
        approved = substitution["state"] in ("approved", "applied") and len(approvals) == 2
        valid_until = min((row["valid_until"] for row in approvals), default="0000-00-00")
        return {
            "substitution_id": substitution_id,
            "approved": approved,
            "valid_until": valid_until,
        }

    def _evaluate_order(self, order: sqlite3.Row, as_of: str) -> dict[str, object]:
        emergency = self._emergency_context(order)
        return self._evaluate(
            spec=self._order_spec(order),
            as_of=as_of,
            contract_id=None if emergency is not None else order["contract_id"],
            emergency=emergency,
        )

    # ------------------------------------------------------------------
    # 采购订单
    # ------------------------------------------------------------------

    def create_order(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "order.write")
        spec = OrderSpec.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM qual_idempotency "
            "WHERE scope='order' AND idempotency_key=?",
            (spec.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同订单内容")
            return json.loads(stored["response_json"])
        contract = self._fetch_one(
            "SELECT * FROM contracts WHERE contract_id=?", (spec.contract_id,), "合同不存在"
        )
        if contract["supplier_id"] != spec.supplier_id:
            raise ValidationFailed("合同供应商与订单供应商不一致")
        evaluation = self._evaluate(
            spec=DeliverySpec(
                supplier_id=spec.supplier_id,
                factory_id=spec.factory_id,
                product_code=spec.product_code,
                product_category=spec.product_category,
                required_voltage_kv=spec.required_voltage_kv,
            ),
            as_of=self._evaluation_date(spec.planned_delivery_on),
            contract_id=spec.contract_id,
            emergency=None,
        )
        state = "cleared" if evaluation["result"] == "pass" else "blocked"
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO purchase_orders(order_id,contract_id,supplier_id,factory_id,product_code,"
                    "product_category,required_voltage_kv,quantity,unit,planned_delivery_on,state,"
                    "idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        spec.order_id,
                        spec.contract_id,
                        spec.supplier_id,
                        spec.factory_id,
                        spec.product_code,
                        spec.product_category,
                        spec.required_voltage_kv,
                        decimal_text(quantize_quantity(spec.quantity)),
                        spec.unit,
                        spec.planned_delivery_on,
                        state,
                        spec.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                check_id = self._record_check(spec.order_id, "creation", evaluation, actor_id)
                response = {
                    "order_id": spec.order_id,
                    "state": state,
                    "check_id": check_id,
                    "result": evaluation["result"],
                }
                self.connection.execute(
                    "INSERT INTO qual_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('order',?,?,?,?)",
                    (spec.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "order",
                    spec.order_id,
                    "order.created",
                    actor_id,
                    {"state": state, "check_id": check_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("订单编号或幂等键冲突") from exc
        return response

    def recheck_order(self, actor_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "order.write")
        order = self._order(order_id)
        if order["state"] not in ("cleared", "blocked", "review_required"):
            raise InvalidState("当前状态的订单不能复核")
        evaluation = self._evaluate_order(order, self._evaluation_date(order["planned_delivery_on"]))
        state = "cleared" if evaluation["result"] == "pass" else "blocked"
        with transaction(self.connection, immediate=True):
            check_id = self._record_check(order_id, "recheck", evaluation, actor_id)
            self.connection.execute(
                "UPDATE purchase_orders SET state=?,revision=revision+1 WHERE order_id=? AND state=?",
                (state, order_id, order["state"]),
            )
            self._audit(
                "order", order_id, "order.rechecked", actor_id,
                {"state": state, "check_id": check_id},
            )
        return {"order_id": order_id, "state": state, "check_id": check_id, "result": evaluation["result"]}

    def ship_order(self, actor_id: str, order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "order.write")
        order = self._order(order_id)
        if order["state"] != "cleared" or int(order["revision"]) != expected_revision:
            raise InvalidState("订单不是当前可发货版本")
        evaluation = self._evaluate_order(order, self._today())
        if evaluation["result"] != "pass":
            with transaction(self.connection, immediate=True):
                check_id = self._record_check(order_id, "shipment", evaluation, actor_id)
                self.connection.execute(
                    "UPDATE purchase_orders SET state='review_required',revision=revision+1 "
                    "WHERE order_id=? AND state='cleared'",
                    (order_id,),
                )
                self._audit(
                    "order", order_id, "order.shipment_blocked", actor_id, {"check_id": check_id}
                )
            raise InvalidState("发货前核对未通过，订单已转入复核")
        with transaction(self.connection, immediate=True):
            check_id = self._record_check(order_id, "shipment", evaluation, actor_id)
            self.connection.execute(
                "UPDATE purchase_orders SET state='shipped',shipped_at=?,revision=revision+1 "
                "WHERE order_id=? AND revision=?",
                (self._now(), order_id, expected_revision),
            )
            self._audit("order", order_id, "order.shipped", actor_id, {"check_id": check_id})
        return {"order_id": order_id, "state": "shipped", "check_id": check_id}

    def accept_order(self, actor_id: str, order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "order.write")
        order = self._order(order_id)
        if order["state"] != "shipped" or int(order["revision"]) != expected_revision:
            raise InvalidState("订单不是当前可验收版本")
        evaluation = self._evaluate_order(order, self._today())
        if evaluation["result"] != "pass":
            with transaction(self.connection, immediate=True):
                check_id = self._record_check(order_id, "acceptance", evaluation, actor_id)
                self._audit(
                    "order", order_id, "order.acceptance_blocked", actor_id, {"check_id": check_id}
                )
            raise InvalidState("到货验收核对未通过，订单保持已发货状态")
        with transaction(self.connection, immediate=True):
            check_id = self._record_check(order_id, "acceptance", evaluation, actor_id)
            self.connection.execute(
                "UPDATE purchase_orders SET state='accepted',accepted_at=?,revision=revision+1 "
                "WHERE order_id=? AND revision=?",
                (self._now(), order_id, expected_revision),
            )
            self._audit("order", order_id, "order.accepted", actor_id, {"check_id": check_id})
        return {"order_id": order_id, "state": "accepted", "check_id": check_id}

    # ------------------------------------------------------------------
    # 质量事件与暂停决定
    # ------------------------------------------------------------------

    def _record_change(
        self,
        actor_id: str,
        change_type: str,
        supplier_id: str,
        qualification_id: str | None,
        reference_id: str | None,
        detail: Mapping[str, Any],
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO qualification_changes(change_type,supplier_id,qualification_id,reference_id,"
            "detail_json,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                change_type,
                supplier_id,
                qualification_id,
                reference_id,
                canonical_json(detail),
                actor_id,
                self._now(),
            ),
        )
        change_id = int(cursor.lastrowid)
        self._audit(
            "qualification_change",
            str(change_id),
            f"change.{change_type}",
            actor_id,
            {"supplier_id": supplier_id, **detail},
        )
        return change_id

    def _apply_impacts(self, change_id: int, orders: Sequence[sqlite3.Row]) -> dict[str, int]:
        counts = {"review_required": 0, "acceptance_pending": 0, "retained": 0}
        for order in orders:
            classification = classify_impact(order["state"])
            counts[classification] += 1
            self.connection.execute(
                "INSERT INTO order_impacts(change_id,order_id,classification,note,created_at) "
                "VALUES(?,?,?,?,?)",
                (change_id, order["order_id"], classification, IMPACT_NOTES[classification], self._now()),
            )
            if classification == "review_required" and order["state"] != "review_required":
                self.connection.execute(
                    "UPDATE purchase_orders SET state='review_required',revision=revision+1 "
                    "WHERE order_id=? AND state=?",
                    (order["order_id"], order["state"]),
                )
        return counts

    def _orders_for_qualification(self, supplier_id: str, qualification_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT DISTINCT o.* FROM purchase_orders o "
            "JOIN approved_products ap ON ap.supplier_id=o.supplier_id "
            "AND ap.factory_id=o.factory_id AND ap.product_code=o.product_code "
            "WHERE o.supplier_id=? AND ap.qualification_id=? AND o.state IN "
            "('cleared','blocked','review_required','shipped','accepted') "
            "ORDER BY o.order_id",
            (supplier_id, qualification_id),
        ).fetchall()

    def _orders_for_scope(
        self, supplier_id: str, product_code: str | None, factory_id: str | None
    ) -> list[sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM purchase_orders WHERE supplier_id=? AND state IN "
            "('cleared','blocked','review_required','shipped','accepted') ORDER BY order_id",
            (supplier_id,),
        ).fetchall()
        return [
            row
            for row in rows
            if (product_code is None or row["product_code"] == product_code)
            and (factory_id is None or row["factory_id"] == factory_id)
        ]

    def record_quality_event(
        self, actor_id: str, supplier_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "quality.event")
        self._supplier(supplier_id)
        spec = QualityEventSpec.from_dict(raw)
        if spec.factory_id is not None:
            factory = self._fetch_one(
                "SELECT * FROM factories WHERE factory_id=?", (spec.factory_id,), "工厂不存在"
            )
            if factory["supplier_id"] != supplier_id:
                raise ValidationFailed("工厂不属于该供应商")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO quality_events(event_id,supplier_id,factory_id,product_code,severity,"
                    "description,occurred_on,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        spec.event_id,
                        supplier_id,
                        spec.factory_id,
                        spec.product_code,
                        spec.severity,
                        spec.description,
                        spec.occurred_on,
                        actor_id,
                        self._now(),
                    ),
                )
                change_id = self._record_change(
                    actor_id,
                    "quality_event_opened",
                    supplier_id,
                    None,
                    spec.event_id,
                    {"severity": spec.severity, "occurred_on": spec.occurred_on},
                )
                impacts = {"review_required": 0, "acceptance_pending": 0, "retained": 0}
                if spec.severity in BLOCKING_SEVERITIES:
                    impacts = self._apply_impacts(
                        change_id,
                        self._orders_for_scope(supplier_id, spec.product_code, spec.factory_id),
                    )
        except sqlite3.IntegrityError as exc:
            raise Conflict("质量事件编号已经存在") from exc
        return {"event_id": spec.event_id, "change_id": change_id, "impacts": impacts}

    def close_quality_event(
        self, actor_id: str, event_id: str, closure_note: str, expected_revision: int
    ) -> dict[str, Any]:
        self._require(actor_id, "quality.event")
        note = required_text(closure_note, "closure_note", 1024)
        event = self._fetch_one(
            "SELECT * FROM quality_events WHERE event_id=?", (event_id,), "质量事件不存在"
        )
        if event["state"] != "open" or int(event["revision"]) != expected_revision:
            raise InvalidState("质量事件不是当前待整改版本")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE quality_events SET state='closed',closed_on=?,closure_note=?,revision=revision+1 "
                "WHERE event_id=? AND revision=?",
                (self._today(), note, event_id, expected_revision),
            )
            change_id = self._record_change(
                actor_id,
                "quality_event_closed",
                event["supplier_id"],
                None,
                event_id,
                {"closure_note": note},
            )
            impacts = self._apply_impacts(
                change_id,
                self._orders_for_scope(
                    event["supplier_id"], event["product_code"], event["factory_id"]
                ),
            )
        return {"event_id": event_id, "state": "closed", "change_id": change_id, "impacts": impacts}

    def decide_suspension(
        self, actor_id: str, supplier_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "quality.suspension")
        self._supplier(supplier_id)
        spec = SuspensionSpec.from_dict(raw)
        if spec.factory_id is not None:
            factory = self._fetch_one(
                "SELECT * FROM factories WHERE factory_id=?", (spec.factory_id,), "工厂不存在"
            )
            if factory["supplier_id"] != supplier_id:
                raise ValidationFailed("工厂不属于该供应商")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO suspensions(suspension_id,supplier_id,product_code,factory_id,reason,"
                    "effective_from,effective_until,decided_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        spec.suspension_id,
                        supplier_id,
                        spec.product_code,
                        spec.factory_id,
                        spec.reason,
                        spec.effective_from,
                        spec.effective_until,
                        actor_id,
                        self._now(),
                    ),
                )
                change_id = self._record_change(
                    actor_id,
                    "suspension",
                    supplier_id,
                    None,
                    spec.suspension_id,
                    {
                        "reason": spec.reason,
                        "effective_from": spec.effective_from,
                        "effective_until": spec.effective_until,
                    },
                )
                impacts = self._apply_impacts(
                    change_id,
                    self._orders_for_scope(supplier_id, spec.product_code, spec.factory_id),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("暂停决定编号已经存在") from exc
        return {"suspension_id": spec.suspension_id, "change_id": change_id, "impacts": impacts}

    def lift_suspension(
        self, actor_id: str, suspension_id: str, expected_revision: int
    ) -> dict[str, Any]:
        self._require(actor_id, "quality.suspension")
        suspension = self._fetch_one(
            "SELECT * FROM suspensions WHERE suspension_id=?", (suspension_id,), "暂停决定不存在"
        )
        if suspension["state"] != "active" or int(suspension["revision"]) != expected_revision:
            raise InvalidState("暂停决定不是当前有效版本")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE suspensions SET state='lifted',lifted_on=?,revision=revision+1 "
                "WHERE suspension_id=? AND revision=?",
                (self._today(), suspension_id, expected_revision),
            )
            change_id = self._record_change(
                actor_id,
                "suspension_lifted",
                suspension["supplier_id"],
                None,
                suspension_id,
                {"lifted_on": self._today()},
            )
            impacts = self._apply_impacts(
                change_id,
                self._orders_for_scope(
                    suspension["supplier_id"], suspension["product_code"], suspension["factory_id"]
                ),
            )
        return {"suspension_id": suspension_id, "state": "lifted", "change_id": change_id, "impacts": impacts}

    def renew_qualification(
        self, actor_id: str, qualification_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        qualification = self._qualification(qualification_id)
        new_valid_until = date_text(raw.get("valid_until"), "valid_until")
        note = required_text(raw.get("note"), "note", 1024)
        if qualification["revoked_at"] is not None:
            raise InvalidState("资质已撤销，不能续期")
        if new_valid_until <= qualification["valid_until"]:
            raise ValidationFailed("续期后的有效期必须晚于当前有效期")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE qualifications SET valid_until=?,revision=revision+1 WHERE qualification_id=?",
                (new_valid_until, qualification_id),
            )
            change_id = self._record_change(
                actor_id,
                "renewal",
                qualification["supplier_id"],
                qualification_id,
                None,
                {"valid_until": new_valid_until, "note": note},
            )
            impacts = self._apply_impacts(
                change_id,
                self._orders_for_qualification(qualification["supplier_id"], qualification_id),
            )
        return {
            "qualification_id": qualification_id,
            "valid_until": new_valid_until,
            "change_id": change_id,
            "impacts": impacts,
        }

    def revoke_qualification(
        self, actor_id: str, qualification_id: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "quality.suspension")
        qualification = self._qualification(qualification_id)
        reason_text = required_text(reason, "reason", 1024)
        if qualification["revoked_at"] is not None:
            raise InvalidState("资质已经撤销")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE qualifications SET revoked_at=?,revision=revision+1 WHERE qualification_id=?",
                (self._today(), qualification_id),
            )
            change_id = self._record_change(
                actor_id,
                "revocation",
                qualification["supplier_id"],
                qualification_id,
                None,
                {"reason": reason_text, "revoked_at": self._today()},
            )
            impacts = self._apply_impacts(
                change_id,
                self._orders_for_qualification(qualification["supplier_id"], qualification_id),
            )
        return {"qualification_id": qualification_id, "change_id": change_id, "impacts": impacts}

    # ------------------------------------------------------------------
    # 紧急替代
    # ------------------------------------------------------------------

    def request_substitution(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "substitution.write")
        spec = SubstitutionSpec.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM qual_idempotency "
            "WHERE scope='substitution' AND idempotency_key=?",
            (spec.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同替代申请内容")
            return json.loads(stored["response_json"])
        order = self._order(spec.order_id)
        if order["state"] not in SUBSTITUTABLE_STATES:
            raise InvalidState("当前状态的订单不能申请紧急替代")
        if quantize_quantity(spec.quantity) != Decimal(order["quantity"]):
            raise ValidationFailed("替代数量必须等于订单数量")
        if (
            spec.substitute_supplier_id,
            spec.substitute_factory_id,
            spec.substitute_product_code,
        ) == (order["supplier_id"], order["factory_id"], order["product_code"]):
            raise ValidationFailed("替代方案不能与原订单供应来源相同")
        basis = self._evaluate(
            spec=DeliverySpec(
                supplier_id=spec.substitute_supplier_id,
                factory_id=spec.substitute_factory_id,
                product_code=spec.substitute_product_code,
                product_category=order["product_category"],
                required_voltage_kv=int(order["required_voltage_kv"]),
            ),
            as_of=spec.needed_by,
            contract_id=None,
            emergency=None,
        )
        basis["original"] = {
            "order_id": order["order_id"],
            "supplier_id": order["supplier_id"],
            "factory_id": order["factory_id"],
            "product_code": order["product_code"],
            "contract_id": order["contract_id"],
        }
        basis["substitute"] = {
            "supplier_id": spec.substitute_supplier_id,
            "factory_id": spec.substitute_factory_id,
            "product_code": spec.substitute_product_code,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO substitutions(substitution_id,order_id,substitute_supplier_id,"
                    "substitute_factory_id,substitute_product_code,quantity,needed_by,reason,basis_json,"
                    "idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        spec.substitution_id,
                        spec.order_id,
                        spec.substitute_supplier_id,
                        spec.substitute_factory_id,
                        spec.substitute_product_code,
                        decimal_text(quantize_quantity(spec.quantity)),
                        spec.needed_by,
                        spec.reason,
                        canonical_json(basis),
                        spec.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                response = {
                    "substitution_id": spec.substitution_id,
                    "state": "pending",
                    "basis_result": basis["result"],
                }
                self.connection.execute(
                    "INSERT INTO qual_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('substitution',?,?,?,?)",
                    (spec.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "substitution",
                    spec.substitution_id,
                    "substitution.requested",
                    actor_id,
                    {"order_id": spec.order_id, "basis_result": basis["result"]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("替代申请编号或幂等键冲突") from exc
        return response

    def approve_substitution(
        self, actor_id: str, substitution_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        spec = ApprovalSpec.from_dict(raw)
        permission = (
            "substitution.approve_quality"
            if spec.approval_type == "quality"
            else "substitution.approve_procurement"
        )
        self._require(actor_id, permission)
        substitution = self._substitution(substitution_id)
        if substitution["state"] != "pending":
            raise InvalidState("替代申请不在待批准状态")
        if spec.decision == "approved" and spec.valid_until < self._today():
            raise ValidationFailed("批准有效期不能早于今天")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO substitution_approvals(substitution_id,approval_type,decision,"
                    "approved_quantity,valid_until,note,approver,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        substitution_id,
                        spec.approval_type,
                        spec.decision,
                        decimal_text(quantize_quantity(spec.approved_quantity)),
                        spec.valid_until,
                        spec.note,
                        actor_id,
                        self._now(),
                    ),
                )
                approvals = self.connection.execute(
                    "SELECT * FROM substitution_approvals WHERE substitution_id=?",
                    (substitution_id,),
                ).fetchall()
                if any(row["decision"] == "rejected" for row in approvals):
                    state = "rejected"
                elif {row["approval_type"] for row in approvals} == {"quality", "procurement"}:
                    state = "approved"
                else:
                    state = "pending"
                self.connection.execute(
                    "UPDATE substitutions SET state=?,revision=revision+1 WHERE substitution_id=?",
                    (state, substitution_id),
                )
                self._audit(
                    "substitution",
                    substitution_id,
                    f"substitution.{spec.decision}",
                    actor_id,
                    {"approval_type": spec.approval_type, "state": state},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该职能已对本申请作出批准决定") from exc
        return {"substitution_id": substitution_id, "state": state}

    def apply_substitution(
        self, actor_id: str, substitution_id: str, new_order_id: str
    ) -> dict[str, Any]:
        self._require(actor_id, "substitution.write")
        new_order_id = identifier(new_order_id, "new_order_id")
        substitution = self._substitution(substitution_id)
        if substitution["state"] == "applied":
            raise InvalidState("替代申请已执行")
        if substitution["state"] != "approved":
            raise InvalidState("替代申请未获质量与采购双方批准")
        approvals = self.connection.execute(
            "SELECT * FROM substitution_approvals WHERE substitution_id=? AND decision='approved'",
            (substitution_id,),
        ).fetchall()
        min_quantity = min(Decimal(row["approved_quantity"]) for row in approvals)
        min_valid_until = min(row["valid_until"] for row in approvals)
        today = self._today()
        if today > min_valid_until:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE substitutions SET state='expired',revision=revision+1 "
                    "WHERE substitution_id=? AND state='approved'",
                    (substitution_id,),
                )
                self._audit(
                    "substitution", substitution_id, "substitution.expired", actor_id,
                    {"valid_until": min_valid_until},
                )
            raise InvalidState("紧急替代批准已过有效期")
        if Decimal(substitution["quantity"]) > min_quantity:
            raise InvalidState("批准数量小于申请数量，不能执行替代")
        order = self._order(substitution["order_id"])
        emergency = {"substitution_id": substitution_id, "approved": True, "valid_until": min_valid_until}
        evaluation = self._evaluate(
            spec=DeliverySpec(
                supplier_id=substitution["substitute_supplier_id"],
                factory_id=substitution["substitute_factory_id"],
                product_code=substitution["substitute_product_code"],
                product_category=order["product_category"],
                required_voltage_kv=int(order["required_voltage_kv"]),
            ),
            as_of=self._evaluation_date(substitution["needed_by"]),
            contract_id=None,
            emergency=emergency,
        )
        state = "cleared" if evaluation["result"] == "pass" else "blocked"
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO purchase_orders(order_id,contract_id,supplier_id,factory_id,product_code,"
                    "product_category,required_voltage_kv,quantity,unit,planned_delivery_on,state,"
                    "substitution_id,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_order_id,
                        order["contract_id"],
                        substitution["substitute_supplier_id"],
                        substitution["substitute_factory_id"],
                        substitution["substitute_product_code"],
                        order["product_category"],
                        order["required_voltage_kv"],
                        substitution["quantity"],
                        order["unit"],
                        substitution["needed_by"],
                        state,
                        substitution_id,
                        f"sub:{substitution_id}",
                        actor_id,
                        self._now(),
                    ),
                )
                check_id = self._record_check(new_order_id, "creation", evaluation, actor_id)
                self.connection.execute(
                    "UPDATE purchase_orders SET state='substituted',revision=revision+1 WHERE order_id=?",
                    (order["order_id"],),
                )
                self.connection.execute(
                    "UPDATE substitutions SET state='applied',revision=revision+1 WHERE substitution_id=?",
                    (substitution_id,),
                )
                self._audit(
                    "order", order["order_id"], "order.substituted", actor_id,
                    {"substitution_id": substitution_id, "new_order_id": new_order_id},
                )
                self._audit(
                    "substitution", substitution_id, "substitution.applied", actor_id,
                    {"new_order_id": new_order_id, "state": state},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("新订单编号冲突") from exc
        return {
            "order_id": new_order_id,
            "state": state,
            "check_id": check_id,
            "substitution_id": substitution_id,
            "original_order_id": order["order_id"],
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def supplier_profile(self, actor_id: str, supplier_id: str) -> dict[str, Any]:
        self._require(actor_id, "compliance.read")
        supplier = self._supplier(supplier_id)
        today = self._today()
        factories = self.connection.execute(
            "SELECT * FROM factories WHERE supplier_id=? ORDER BY factory_id", (supplier_id,)
        ).fetchall()
        qualifications = []
        for row in self.connection.execute(
            "SELECT * FROM qualifications WHERE supplier_id=? ORDER BY qualification_id",
            (supplier_id,),
        ).fetchall():
            item = dict(row)
            if row["revoked_at"] is not None and row["revoked_at"] <= today:
                item["status_on_today"] = "revoked"
            elif row["valid_until"] < today:
                item["status_on_today"] = "expired"
            elif row["valid_from"] > today:
                item["status_on_today"] = "pending"
            else:
                item["status_on_today"] = "active"
            qualifications.append(item)
        products = self.connection.execute(
            "SELECT * FROM approved_products WHERE supplier_id=? ORDER BY approval_id", (supplier_id,)
        ).fetchall()
        suspensions = self.connection.execute(
            "SELECT * FROM suspensions WHERE supplier_id=? AND state='active' ORDER BY suspension_id",
            (supplier_id,),
        ).fetchall()
        events = self.connection.execute(
            "SELECT * FROM quality_events WHERE supplier_id=? AND state='open' ORDER BY event_id",
            (supplier_id,),
        ).fetchall()
        return {
            "supplier": dict(supplier),
            "factories": [dict(row) for row in factories],
            "qualifications": qualifications,
            "approved_products": [dict(row) for row in products],
            "active_suspensions": [dict(row) for row in suspensions],
            "open_quality_events": [dict(row) for row in events],
        }

    def get_order(self, actor_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "compliance.read")
        return dict(self._order(order_id))

    def order_compliance(self, actor_id: str, order_id: str) -> dict[str, Any]:
        self._require(actor_id, "compliance.read")
        self._order(order_id)
        checks = []
        for row in self.connection.execute(
            "SELECT * FROM compliance_checks WHERE order_id=? ORDER BY check_id", (order_id,)
        ).fetchall():
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            checks.append(item)
        return {"order_id": order_id, "checks": checks}

    def substitution_detail(self, actor_id: str, substitution_id: str) -> dict[str, Any]:
        self._require(actor_id, "compliance.read")
        substitution = dict(self._substitution(substitution_id))
        basis = json.loads(substitution.pop("basis_json"))
        approvals = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM substitution_approvals WHERE substitution_id=? ORDER BY approval_id",
                (substitution_id,),
            ).fetchall()
        ]
        approved = [row for row in approvals if row["decision"] == "approved"]
        effective = None
        if len(approved) == 2:
            effective = {
                "approved_quantity": min(row["approved_quantity"] for row in approved),
                "valid_until": min(row["valid_until"] for row in approved),
            }
        return {
            "substitution": substitution,
            "basis": basis,
            "approvals": approvals,
            "effective": effective,
        }

    def change_impacts(self, actor_id: str, change_id: int) -> dict[str, Any]:
        self._require(actor_id, "impact.read")
        change = self._fetch_one(
            "SELECT * FROM qualification_changes WHERE change_id=?", (change_id,), "资格变化记录不存在"
        )
        item = dict(change)
        item["detail"] = json.loads(item.pop("detail_json"))
        impacts: dict[str, list[dict[str, Any]]] = {
            "review_required": [],
            "acceptance_pending": [],
            "retained": [],
        }
        for row in self.connection.execute(
            "SELECT * FROM order_impacts WHERE change_id=? ORDER BY impact_id", (change_id,)
        ).fetchall():
            impacts[row["classification"]].append(
                {"order_id": row["order_id"], "note": row["note"]}
            )
        return {"change": item, "impacts": impacts}

    def supplier_changes(self, actor_id: str, supplier_id: str) -> dict[str, Any]:
        self._require(actor_id, "impact.read")
        self._supplier(supplier_id)
        changes = []
        for row in self.connection.execute(
            "SELECT * FROM qualification_changes WHERE supplier_id=? ORDER BY change_id",
            (supplier_id,),
        ).fetchall():
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            changes.append(item)
        return {"supplier_id": supplier_id, "changes": changes}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM qual_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
