"""关键供应商资格事务用例。

职责主线：
1. 资质工程师维护企业资质（含有效范围/有效期）、获准产品与工厂；
2. 质量角色登记质量事件、作出/解除暂停决定；
3. 采购订单在发货时按"当时有效的资格 + 批次检测合格"双重核对，并固化合规快照；
4. 紧急替代须质量与采购分别批准，且只对明确数量和期限有效；
5. 资质续期、暂停、整改关闭等变化发生时，未发货订单置复核标记，
   已发货/已验收订单按当时规则保留（快照不被回溯改写）；
6. 采购可查询替代供应商的合规依据，以及每次资格变化影响的订单。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    ApprovedProductInput,
    CertificateInput,
    EmergencyRequestInput,
    PurchaseOrderInput,
    QualityEventInput,
    canonical_json,
    certificate_covers,
    classify_impact,
    date_text,
    decimal_text,
    digest,
    identifier,
    optional_identifier,
    required_text,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS: Mapping[str, set[str]] = {
    "qual_engineer": {
        "cert.write", "product.write", "plant.write", "renewal.write", "report.read",
    },
    "quality": {
        "event.write", "event.close", "suspension.write", "emergency.quality", "report.read",
    },
    "buyer": {
        "order.write", "ship.write", "emergency.procurement", "review.write", "report.read",
    },
    "auditor": {"report.read", "audit.read"},
}


class QualificationService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ----- 基础 -----
    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

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
        event_hash = digest(body)
        self.connection.execute(
            "INSERT INTO qual_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

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

    # ----- 企业资质 -----
    def register_certificate(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "cert.write")
        cert = CertificateInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO certificates(certificate_id,supplier_id,cert_type,cert_name,scope_text,"
                    "valid_from,valid_until,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (cert.certificate_id, cert.supplier_id, cert.cert_type, cert.cert_name,
                     cert.scope_text, cert.valid_from, cert.valid_until, actor_id, self._now()),
                )
                self._audit("certificate", cert.certificate_id, "certificate.registered", actor_id, {
                    "supplier_id": cert.supplier_id, "cert_type": cert.cert_type,
                    "valid_from": cert.valid_from, "valid_until": cert.valid_until,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("资质编号已经存在") from exc
        return self.certificate(cert.certificate_id)

    def certificate(self, certificate_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM certificates WHERE certificate_id=?", (certificate_id,)
        ).fetchone()
        if row is None:
            raise NotFound("企业资质不存在")
        return dict(row)

    def list_certificates(self, supplier_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.connection.execute(
            "SELECT * FROM certificates WHERE supplier_id=? ORDER BY valid_until DESC, certificate_id",
            (supplier_id,),
        ).fetchall()]

    def renew_certificate(self, actor_id: str, old_certificate_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """资质续期：旧证置 renewed，新证生效；范围内未发货订单需复核。"""
        self._require(actor_id, "renewal.write")
        old = self.connection.execute(
            "SELECT * FROM certificates WHERE certificate_id=?", (old_certificate_id,)
        ).fetchone()
        if old is None:
            raise NotFound("企业资质不存在")
        if old["status"] not in ("active", "expired"):
            raise InvalidState("只有有效或已到期资质可以续期")
        new_id = raw.get("certificate_id")
        if not isinstance(new_id, str) or not new_id.strip():
            raise ValidationFailed("新资质编号 certificate_id 不能为空")
        new_from = raw.get("valid_from")
        new_until = raw.get("valid_until")
        new_from, new_until = _date_range_values(new_from, new_until)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE certificates SET status='renewed', revision=revision+1 WHERE certificate_id=?",
                    (old_certificate_id,),
                )
                self.connection.execute(
                    "INSERT INTO certificates(certificate_id,supplier_id,cert_type,cert_name,scope_text,"
                    "valid_from,valid_until,supersedes_certificate_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (new_id, old["supplier_id"], old["cert_type"], old["cert_name"],
                     old["scope_text"], new_from, new_until, old_certificate_id, actor_id, self._now()),
                )
                self._audit("certificate", new_id, "certificate.renewed", actor_id, {
                    "supplier_id": old["supplier_id"], "supersedes": old_certificate_id,
                    "valid_from": new_from, "valid_until": new_until,
                })
                self._propagate_change(
                    "certificate.renewed", "certificate", new_id.strip(),
                    old["supplier_id"], None, None,
                    f"资质 {old_certificate_id} 已续期为 {new_id.strip()}",
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("新资质编号冲突") from exc
        return self.certificate(new_id.strip())

    def expire_certificate(self, actor_id: str, certificate_id: str) -> dict[str, Any]:
        """日界驱动：把已过有效期的资质置为 expired，并影响未发货订单。"""
        self._require(actor_id, "renewal.write")
        row = self.connection.execute(
            "SELECT * FROM certificates WHERE certificate_id=?", (certificate_id,)
        ).fetchone()
        if row is None:
            raise NotFound("企业资质不存在")
        if row["status"] != "active":
            raise InvalidState("只有有效资质可以标记到期")
        if row["valid_until"] >= self._today():
            raise InvalidState("资质仍在有效期内")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE certificates SET status='expired', revision=revision+1 WHERE certificate_id=?",
                (certificate_id,),
            )
            self._audit("certificate", certificate_id, "certificate.expired", actor_id, {
                "supplier_id": row["supplier_id"], "valid_until": row["valid_until"],
            })
            self._propagate_change(
                "certificate.expired", "certificate", certificate_id,
                row["supplier_id"], None, None,
                f"资质 {certificate_id} 已过有效期",
            )
        return self.certificate(certificate_id)

    def revoke_certificate(self, actor_id: str, certificate_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "cert.write")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("撤销原因不能为空")
        row = self.connection.execute(
            "SELECT * FROM certificates WHERE certificate_id=?", (certificate_id,)
        ).fetchone()
        if row is None:
            raise NotFound("企业资质不存在")
        if row["status"] != "active":
            raise InvalidState("只有有效资质可以撤销")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE certificates SET status='revoked', revision=revision+1 WHERE certificate_id=?",
                (certificate_id,),
            )
            self._audit("certificate", certificate_id, "certificate.revoked", actor_id, {
                "supplier_id": row["supplier_id"], "reason": reason.strip(),
            })
            self._propagate_change(
                "certificate.revoked", "certificate", certificate_id,
                row["supplier_id"], None, None,
                f"资质 {certificate_id} 被撤销：{reason.strip()}",
            )
        return self.certificate(certificate_id)

    # ----- 获准产品 -----
    def register_approved_product(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "product.write")
        product = ApprovedProductInput.from_dict(raw)
        if product.certificate_id is not None:
            cert = self.connection.execute(
                "SELECT * FROM certificates WHERE certificate_id=?", (product.certificate_id,)
            ).fetchone()
            if cert is None:
                raise NotFound("关联企业资质不存在")
            if cert["supplier_id"] != product.supplier_id:
                raise ValidationFailed("获准产品必须关联本企业的资质")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO approved_products(approval_id,supplier_id,material_code,material_name,"
                    "plant_id,spec_revision,valid_from,valid_until,certificate_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (product.approval_id, product.supplier_id, product.material_code, product.material_name,
                     product.plant_id, product.spec_revision, product.valid_from, product.valid_until,
                     product.certificate_id, actor_id, self._now()),
                )
                self._audit("approved_product", product.approval_id, "product.approved", actor_id, {
                    "supplier_id": product.supplier_id, "material_code": product.material_code,
                    "plant_id": product.plant_id,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("获准产品授权冲突（编号或供应商/物料/工厂范围重复）") from exc
        return self.approved_product(product.approval_id)

    def approved_product(self, approval_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM approved_products WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise NotFound("获准产品授权不存在")
        return dict(row)

    def withdraw_approved_product(self, actor_id: str, approval_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "product.write")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        row = self.connection.execute(
            "SELECT * FROM approved_products WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise NotFound("获准产品授权不存在")
        if row["status"] != "active":
            raise InvalidState("授权已被撤回")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE approved_products SET status='withdrawn' WHERE approval_id=?", (approval_id,)
            )
            self._audit("approved_product", approval_id, "product.withdrawn", actor_id, {
                "supplier_id": row["supplier_id"], "material_code": row["material_code"],
                "reason": reason.strip(),
            })
            self._propagate_change(
                "approval.withdrawn", "approved_product", approval_id,
                row["supplier_id"], row["material_code"], row["plant_id"],
                f"获准产品授权 {approval_id} 撤回：{reason.strip()}",
            )
        return self.approved_product(approval_id)

    # ----- 工厂 -----
    def register_plant(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plant.write")
        plant_id = _identifier_field(raw, "plant_id")
        supplier_id = _identifier_field(raw, "supplier_id")
        name = _text_field(raw, "name")
        location = _text_field(raw, "location", 256)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO plants(plant_id,supplier_id,name,location,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (plant_id, supplier_id, name, location, actor_id, self._now()),
                )
                self._audit("plant", plant_id, "plant.registered", actor_id,
                            {"supplier_id": supplier_id, "name": name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("工厂编号已经存在") from exc
        return self.plant(plant_id)

    def plant(self, plant_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM plants WHERE plant_id=?", (plant_id,)).fetchone()
        if row is None:
            raise NotFound("工厂不存在")
        return dict(row)

    def list_plants(self, supplier_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.connection.execute(
            "SELECT * FROM plants WHERE supplier_id=? ORDER BY plant_id", (supplier_id,)
        ).fetchall()]

    # ----- 质量事件 -----
    def open_quality_event(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        event = QualityEventInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO quality_events(event_id,supplier_id,material_code,plant_id,severity,"
                    "title,detail,occurred_on,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (event.event_id, event.supplier_id, event.material_code, event.plant_id,
                     event.severity, event.title, event.detail, event.occurred_on, actor_id, self._now()),
                )
                self._audit("quality_event", event.event_id, "event.opened", actor_id, {
                    "supplier_id": event.supplier_id, "severity": event.severity,
                    "material_code": event.material_code, "title": event.title,
                })
                self._propagate_change(
                    "event.opened", "quality_event", event.event_id,
                    event.supplier_id, event.material_code, event.plant_id,
                    f"质量事件 {event.event_id}（{event.severity}）：{event.title}",
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("质量事件编号已经存在") from exc
        return self.quality_event(event.event_id)

    def quality_event(self, event_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM quality_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound("质量事件不存在")
        return dict(row)

    def update_corrective_action(self, actor_id: str, event_id: str, action: str) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        if not isinstance(action, str) or not action.strip():
            raise ValidationFailed("整改措施不能为空")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE quality_events SET state='correcting', corrective_action=? "
                "WHERE event_id=? AND state IN ('open','correcting')",
                (action.strip(), event_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("质量事件不存在或已关闭")
            self._audit("quality_event", event_id, "event.correcting", actor_id, {"action": action.strip()})
        return self.quality_event(event_id)

    def close_quality_event(self, actor_id: str, event_id: str, corrective_action: str | None = None) -> dict[str, Any]:
        """整改关闭：未发货订单仍需复核确认，已发货/已验收按当时规则保留。"""
        self._require(actor_id, "event.close")
        row = self.connection.execute(
            "SELECT * FROM quality_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound("质量事件不存在")
        if row["state"] == "closed":
            raise InvalidState("质量事件已经关闭")
        action = (corrective_action or row["corrective_action"] or "").strip()
        if not action:
            raise ValidationFailed("关闭质量事件必须填写整改措施")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE quality_events SET state='closed', corrective_action=?, closed_by=?, closed_at=? "
                "WHERE event_id=?",
                (action, actor_id, self._now(), event_id),
            )
            self._audit("quality_event", event_id, "event.closed", actor_id, {
                "corrective_action": action,
            })
            self._propagate_change(
                "event.closed", "quality_event", event_id,
                row["supplier_id"], row["material_code"], row["plant_id"],
                f"质量事件 {event_id} 整改关闭，未发货订单需复核",
            )
        return self.quality_event(event_id)

    # ----- 暂停决定 -----
    def suspend_supplier(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "suspension.write")
        suspension_id = _identifier_field(raw, "suspension_id")
        supplier_id = _identifier_field(raw, "supplier_id")
        material_code = _optional_identifier_field(raw, "material_code")
        plant_id = _optional_identifier_field(raw, "plant_id")
        reason_text = _text_field(raw, "reason_text", 1024)
        starts_at = raw.get("starts_at")
        starts_at = self._today() if starts_at is None else date_text(starts_at, "starts_at")
        reason_event_id = _optional_identifier_field(raw, "reason_event_id")
        if reason_event_id is not None:
            event_row = self.connection.execute(
                "SELECT supplier_id FROM quality_events WHERE event_id=?", (reason_event_id,)
            ).fetchone()
            if event_row is None:
                raise NotFound("关联质量事件不存在")
            if event_row["supplier_id"] != supplier_id:
                raise ValidationFailed("暂停决定关联了其他企业的质量事件")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO suspensions(suspension_id,supplier_id,material_code,plant_id,reason_event_id,"
                "reason_text,starts_at,decided_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (suspension_id, supplier_id, material_code, plant_id, reason_event_id,
                 reason_text, starts_at, actor_id, self._now()),
            )
            self._audit("suspension", suspension_id, "suspension.started", actor_id, {
                "supplier_id": supplier_id, "material_code": material_code,
                "plant_id": plant_id, "reason_event_id": reason_event_id,
            })
            self._propagate_change(
                "suspension.started", "suspension", suspension_id,
                supplier_id, material_code, plant_id,
                f"暂停决定 {suspension_id}：{reason_text}",
            )
        return self.suspension(suspension_id)

    def suspension(self, suspension_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM suspensions WHERE suspension_id=?", (suspension_id,)
        ).fetchone()
        if row is None:
            raise NotFound("暂停决定不存在")
        return dict(row)

    def lift_suspension(self, actor_id: str, suspension_id: str) -> dict[str, Any]:
        """解除暂停：恢复供货资格，但未发货订单仍需复核后才能发运。"""
        self._require(actor_id, "suspension.write")
        row = self.connection.execute(
            "SELECT * FROM suspensions WHERE suspension_id=?", (suspension_id,)
        ).fetchone()
        if row is None:
            raise NotFound("暂停决定不存在")
        if row["state"] != "active":
            raise InvalidState("暂停决定已解除")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE suspensions SET state='lifted', lifted_at=? WHERE suspension_id=?",
                (self._now(), suspension_id),
            )
            self._audit("suspension", suspension_id, "suspension.lifted", actor_id, {
                "supplier_id": row["supplier_id"],
            })
            self._propagate_change(
                "suspension.lifted", "suspension", suspension_id,
                row["supplier_id"], row["material_code"], row["plant_id"],
                f"暂停决定 {suspension_id} 已解除，未发货订单需复核",
            )
        return self.suspension(suspension_id)

    # ----- 资格适用性核对（合规依据） -----
    def _resolve_certificate(self, certificate_id: str, on: str) -> sqlite3.Row | None:
        """按"当日有效"解析资质：先查本证，再沿续期接替链找到当日生效的新证。

        已 revoked 的资质不参与接替；历史已固化快照不经过此解析（时点资格）。
        """
        direct = self.connection.execute(
            "SELECT * FROM certificates WHERE certificate_id=? AND status IN ('active','renewed') "
            "AND valid_from<=? AND valid_until>=?",
            (certificate_id, on, on),
        ).fetchone()
        if direct is not None:
            return direct
        return self.connection.execute(
            "WITH RECURSIVE chain(ancestor_id, descendant_id, depth) AS ("
            "  SELECT certificate_id, certificate_id, 0 FROM certificates WHERE certificate_id=? "
            "  UNION ALL "
            "  SELECT c.ancestor_id, x.certificate_id, c.depth+1 "
            "  FROM chain c JOIN certificates x ON x.supersedes_certificate_id=c.descendant_id"
            ") "
            "SELECT cert.* FROM chain ch JOIN certificates cert ON cert.certificate_id=ch.descendant_id "
            "WHERE cert.status='active' AND cert.valid_from<=? AND cert.valid_until>=? "
            "ORDER BY ch.depth LIMIT 1",
            (certificate_id, on, on),
        ).fetchone()

    def evaluate_supply(
        self,
        supplier_id: str,
        material_code: str,
        plant_id: str | None = None,
        on_date: str | None = None,
    ) -> dict[str, Any]:
        """核对某企业在指定日期向指定物料/工厂供货的资格，给出结构化合规依据。"""
        on = on_date or self._today()
        reasons: list[str] = []
        product_row = self.connection.execute(
            "SELECT * FROM approved_products WHERE supplier_id=? AND material_code=? AND status='active' "
            "AND valid_from<=? AND valid_until>=? "
            "AND (plant_id IS NULL OR plant_id=?) ORDER BY valid_until DESC LIMIT 1",
            (supplier_id, material_code, on, on, plant_id),
        ).fetchone()
        product_view: dict[str, Any] | None = None
        if product_row is None:
            reasons.append("no_approved_product")
        else:
            if plant_id is not None and product_row["plant_id"] is not None \
                    and product_row["plant_id"] != plant_id:
                reasons.append("product_scope_plant")
            product_view = {
                "approval_id": product_row["approval_id"],
                "material_code": product_row["material_code"],
                "material_name": product_row["material_name"],
                "plant_id": product_row["plant_id"],
                "spec_revision": product_row["spec_revision"],
                "valid_from": product_row["valid_from"],
                "valid_until": product_row["valid_until"],
            }

        cert_row: sqlite3.Row | None = None
        if product_row is not None and product_row["certificate_id"] is not None:
            cert_row = self._resolve_certificate(product_row["certificate_id"], on)
            if cert_row is None:
                reasons.append("certificate_expired")
        elif product_row is not None:
            cert_row = self.connection.execute(
                "SELECT * FROM certificates WHERE supplier_id=? AND status IN ('active','renewed') "
                "AND valid_from<=? AND valid_until>=? ORDER BY valid_until DESC",
                (supplier_id, on, on),
            ).fetchall()
            cert_row = next(
                (c for c in cert_row if certificate_covers(
                    c["scope_text"], product_row["material_name"], material_code)),
                None,
            )
            if cert_row is None:
                reasons.append("no_certificate_covering_scope")
        cert_view = None if cert_row is None else {
            "certificate_id": cert_row["certificate_id"],
            "cert_type": cert_row["cert_type"],
            "cert_name": cert_row["cert_name"],
            "scope_text": cert_row["scope_text"],
            "valid_from": cert_row["valid_from"],
            "valid_until": cert_row["valid_until"],
        }

        plant_status = None
        if plant_id is not None:
            plant_row = self.connection.execute(
                "SELECT supplier_id,status FROM plants WHERE plant_id=?", (plant_id,)
            ).fetchone()
            if plant_row is not None:
                plant_status = plant_row["status"]
                if plant_row["supplier_id"] != supplier_id:
                    reasons.append("plant_belongs_other_supplier")
                if plant_status != "active":
                    reasons.append("plant_inactive")

        suspensions = self.connection.execute(
            "SELECT suspension_id,material_code,plant_id,reason_text FROM suspensions "
            "WHERE supplier_id=? AND state='active' AND starts_at<=? "
            "AND (material_code IS NULL OR material_code=?) "
            "AND (plant_id IS NULL OR plant_id=?)",
            (supplier_id, on, material_code, plant_id),
        ).fetchall()
        if suspensions:
            reasons.append("suspended")

        events = self.connection.execute(
            "SELECT event_id,severity,state,title FROM quality_events "
            "WHERE supplier_id=? AND state IN ('open','correcting') "
            "AND (material_code IS NULL OR material_code=?) "
            "AND (plant_id IS NULL OR plant_id=?)",
            (supplier_id, material_code, plant_id),
        ).fetchall()
        blocking_events = [dict(e) for e in events if e["severity"] == "critical"]
        warning_events = [dict(e) for e in events if e["severity"] != "critical"]
        if blocking_events:
            reasons.append("critical_event_open")

        return {
            "supplier_id": supplier_id,
            "material_code": material_code,
            "plant_id": plant_id,
            "on_date": on,
            "eligible": not reasons,
            "reasons": reasons,
            "approved_product": product_view,
            "certificate": cert_view,
            "plant_status": plant_status,
            "blocking_suspensions": [dict(s) for s in suspensions],
            "blocking_events": blocking_events,
            "warning_events": warning_events,
        }

    def find_alternatives(
        self,
        actor_id: str,
        material_code: str,
        plant_id: str | None = None,
        on_date: str | None = None,
    ) -> dict[str, Any]:
        """采购查询：列出该物料当前可合规供货的替代选择及其合规依据。"""
        self._require(actor_id, "report.read")
        on = on_date or self._today()
        rows = self.connection.execute(
            "SELECT DISTINCT supplier_id FROM approved_products WHERE material_code=? AND status='active' "
            "AND valid_from<=? AND valid_until>=? AND (plant_id IS NULL OR plant_id=?)",
            (material_code, on, on, plant_id),
        ).fetchall()
        choices: list[dict[str, Any]] = []
        for row in rows:
            basis = self.evaluate_supply(row["supplier_id"], material_code, plant_id, on)
            if basis["eligible"]:
                choices.append({
                    "supplier_id": row["supplier_id"],
                    "material_code": material_code,
                    "compliance_basis": {
                        "approved_product": basis["approved_product"],
                        "certificate": basis["certificate"],
                        "plant_status": basis["plant_status"],
                        "checked_on": on,
                    },
                })
        return {"material_code": material_code, "plant_id": plant_id, "on_date": on,
                "alternatives": choices, "count": len(choices)}

    # ----- 采购订单与交付批次 -----
    def create_order(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "order.write")
        order = PurchaseOrderInput.from_dict(raw)
        # 采购方案立项时即核对一次资格（提示性，不阻断建档；真正闸门在发货）
        basis = self.evaluate_supply(order.supplier_id, order.material_code, order.plant_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO purchase_orders(order_id,supplier_id,material_code,plant_id,quantity,"
                    "unit,expect_delivery_on,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (order.order_id, order.supplier_id, order.material_code, order.plant_id,
                     decimal_text(order.quantity), order.unit, order.expect_delivery_on,
                     actor_id, self._now()),
                )
                self._audit("purchase_order", order.order_id, "order.created", actor_id, {
                    "supplier_id": order.supplier_id, "material_code": order.material_code,
                    "quantity": decimal_text(order.quantity),
                    "planning_eligible": basis["eligible"],
                    "planning_reasons": basis["reasons"],
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("采购订单编号已经存在") from exc
        result = self.order(order.order_id)
        result["planning_eligibility"] = {"eligible": basis["eligible"], "reasons": basis["reasons"]}
        return result

    def order(self, order_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM purchase_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if row is None:
            raise NotFound("采购订单不存在")
        result = dict(row)
        if result.get("qualification_snapshot_json"):
            result["qualification_snapshot"] = json.loads(result.pop("qualification_snapshot_json"))
        else:
            result.pop("qualification_snapshot_json")
            result["qualification_snapshot"] = None
        result["review_required"] = bool(result["review_required"])
        return result

    def list_orders(self, supplier_id: str | None = None, material_code: str | None = None,
                    review_required: bool | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM purchase_orders WHERE 1=1"
        params: list[Any] = []
        if supplier_id:
            sql += " AND supplier_id=?"
            params.append(supplier_id)
        if material_code:
            sql += " AND material_code=?"
            params.append(material_code)
        if review_required is not None:
            sql += " AND review_required=?"
            params.append(1 if review_required else 0)
        sql += " ORDER BY order_id"
        orders = []
        for row in self.connection.execute(sql, params).fetchall():
            item = dict(row)
            item["review_required"] = bool(item["review_required"])
            item.pop("qualification_snapshot_json", None)
            orders.append(item)
        return orders

    def register_order_lot(self, actor_id: str, order_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记交付批次及其执行的检测标准；批次检测只覆盖登记过的批次。"""
        self._require(actor_id, "ship.write")
        order = self._order_row(order_id)
        batch_no = _text_field(raw, "batch_no", 64)
        plant_id = _optional_identifier_field(raw, "plant_id") or order["plant_id"]
        standard = _text_field(raw, "inspection_standard", 256)
        order_lot_id = raw.get("order_lot_id") or f"{order_id}:{batch_no}"
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO order_lots(order_lot_id,order_id,batch_no,plant_id,inspection_standard,"
                    "inspection_result,created_at) VALUES(?,?,?,?,?,'pending',?)",
                    (str(order_lot_id).strip(), order_id, batch_no, plant_id, standard, self._now()),
                )
                self._audit("order_lot", str(order_lot_id).strip(), "lot.registered", actor_id, {
                    "order_id": order_id, "batch_no": batch_no, "plant_id": plant_id,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("交付批次已经登记") from exc
        return self.order_lot(str(order_lot_id).strip())

    def order_lot(self, order_lot_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM order_lots WHERE order_lot_id=?", (order_lot_id,)
        ).fetchone()
        if row is None:
            raise NotFound("交付批次不存在")
        return dict(row)

    def record_lot_inspection(self, actor_id: str, order_id: str, batch_no: str,
                              inspection_result: str, inspection_report: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "ship.write")
        if inspection_result not in ("passed", "failed"):
            raise ValidationFailed("inspection_result 必须是 passed 或 failed")
        row = self.connection.execute(
            "SELECT * FROM order_lots WHERE order_id=? AND batch_no=?", (order_id, batch_no)
        ).fetchone()
        if row is None:
            raise NotFound("交付批次未登记，不能按批次检测放行")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE order_lots SET inspection_result=?, inspection_report=?, inspected_by=?, "
                "inspected_at=? WHERE order_lot_id=?",
                (inspection_result, inspection_report, actor_id, self._now(), row["order_lot_id"]),
            )
            self._audit("order_lot", row["order_lot_id"], "lot.inspected", actor_id, {
                "order_id": order_id, "batch_no": batch_no, "result": inspection_result,
            })
        return self.order_lot(row["order_lot_id"])

    def ship_order(self, actor_id: str, order_id: str, batch_no: str,
                   substitute_approval_id: str | None = None) -> dict[str, Any]:
        """发货闸门：资格当时有效 + 批次检测合格；紧急替代走双批准豁免。"""
        self._require(actor_id, "ship.write")
        order = self._order_row(order_id)
        if order["state"] != "open":
            raise InvalidState("只有未发货订单可以发运")
        lot = self.connection.execute(
            "SELECT * FROM order_lots WHERE order_id=? AND batch_no=?", (order_id, batch_no)
        ).fetchone()
        if lot is None:
            raise InvalidState("交付批次未登记，不能发运")
        if lot["inspection_result"] != "passed":
            raise InvalidState("交付批次检测未通过（或尚无结论），不能发运")

        emergency_view = None
        if substitute_approval_id is None:
            if order["review_required"]:
                raise InvalidState("订单存在待复核的资格变化，须先完成复核或走紧急替代批准")
            shipping_supplier = order["supplier_id"]
            shipping_plant = lot["plant_id"] or order["plant_id"]
            if order["plant_id"] is not None and lot["plant_id"] is not None \
                    and lot["plant_id"] != order["plant_id"]:
                raise InvalidState("交付批次工厂与采购方案约定工厂不一致")
        else:
            approval = self.connection.execute(
                "SELECT * FROM emergency_approvals WHERE approval_id=?", (substitute_approval_id,)
            ).fetchone()
            if approval is None:
                raise NotFound("紧急替代批准不存在")
            emergency_view = self._validated_emergency(approval, order, lot)
            shipping_supplier = approval["substitute_supplier_id"]
            shipping_plant = lot["plant_id"]

        basis = self.evaluate_supply(shipping_supplier, order["material_code"], shipping_plant)
        if not basis["eligible"]:
            raise InvalidState(f"供货资格核对不通过：{','.join(basis['reasons'])}")

        snapshot = {
            "shipped_at": self._now(),
            "shipped_on": self._today(),
            "shipping_supplier_id": shipping_supplier,
            "original_supplier_id": order["supplier_id"],
            "batch": {
                "batch_no": batch_no,
                "plant_id": shipping_plant,
                "inspection_standard": lot["inspection_standard"],
                "inspection_report": lot["inspection_report"],
            },
            "eligibility_basis": basis,
            "emergency_approval": emergency_view,
        }
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE purchase_orders SET state='intransit', shipped_at=?, review_required=0, "
                "review_reason=NULL, qualification_snapshot_json=? WHERE order_id=? AND state='open'",
                (self._now(), json.dumps(snapshot, ensure_ascii=False, sort_keys=True), order_id),
            )
            self._audit("purchase_order", order_id, "order.shipped", actor_id, {
                "batch_no": batch_no, "shipping_supplier_id": shipping_supplier,
                "substituted": substitute_approval_id is not None,
                "basis_reasons": basis["reasons"],
            })
        return self.order(order_id)

    def _validated_emergency(self, approval: sqlite3.Row, order: sqlite3.Row,
                             lot: sqlite3.Row) -> dict[str, Any]:
        if approval["order_id"] != order["order_id"]:
            raise ValidationFailed("紧急替代批准不属于该订单")
        if approval["state"] != "approved":
            raise InvalidState("紧急替代尚未取得质量与采购的分别批准")
        today = self._today()
        if not (approval["valid_from"] <= today <= approval["valid_until"]):
            raise InvalidState("紧急替代批准不在有效期限内")
        if Decimal(approval["quantity"]) < Decimal(order["quantity"]):
            raise InvalidState("紧急替代批准数量小于订单数量，批准不覆盖本批次")
        if approval["unit"] != order["unit"]:
            raise InvalidState("紧急替代批准计量单位与订单不一致")
        if approval["material_code"] != order["material_code"]:
            raise InvalidState("紧急替代批准物料与订单不一致")
        if lot["plant_id"] is None:
            raise InvalidState("替代交付批次必须登记实际工厂")
        return {
            "approval_id": approval["approval_id"],
            "substitute_supplier_id": approval["substitute_supplier_id"],
            "quantity": approval["quantity"],
            "unit": approval["unit"],
            "valid_from": approval["valid_from"],
            "valid_until": approval["valid_until"],
            "quality_approved_by": approval["quality_approved_by"],
            "procurement_approved_by": approval["procurement_approved_by"],
        }

    def accept_order(self, actor_id: str, order_id: str) -> dict[str, Any]:
        """完成验收：合规快照自此冻结，后续资格变化一律不回溯。"""
        self._require(actor_id, "ship.write")
        order = self._order_row(order_id)
        if order["state"] != "intransit":
            raise InvalidState("只有已发货订单可以验收")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE purchase_orders SET state='received', accepted_at=? WHERE order_id=?",
                (self._now(), order_id),
            )
            self._audit("purchase_order", order_id, "order.accepted", actor_id, {
                "supplier_id": order["supplier_id"], "batch_snapshot_retained": True,
            })
        return self.order(order_id)

    def cancel_order(self, actor_id: str, order_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "order.write")
        order = self._order_row(order_id)
        if order["state"] not in ("open",):
            raise InvalidState("只有未发货订单可以取消")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE purchase_orders SET state='cancelled' WHERE order_id=?", (order_id,)
            )
            self._audit("purchase_order", order_id, "order.cancelled", actor_id, {"reason": reason})
        return self.order(order_id)

    def resolve_order_review(self, actor_id: str, order_id: str, note: str) -> dict[str, Any]:
        """采购在资质续期/暂停解除/整改关闭后复核未发货订单并清除标记。"""
        self._require(actor_id, "review.write")
        if not isinstance(note, str) or not note.strip():
            raise ValidationFailed("复核结论不能为空")
        order = self._order_row(order_id)
        if not order["review_required"]:
            raise InvalidState("订单没有待处理的复核标记")
        if order["state"] != "open":
            raise InvalidState("只有未发货订单需要复核")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE purchase_orders SET review_required=0, review_reason=NULL, reviewed_by=?, "
                "reviewed_at=? WHERE order_id=?",
                (actor_id, self._now(), order_id),
            )
            self._audit("purchase_order", order_id, "order.review_resolved", actor_id, {"note": note.strip()})
        return self.order(order_id)

    # ----- 紧急替代双批准 -----
    def create_emergency_request(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "order.write")
        request = EmergencyRequestInput.from_dict(raw)
        order = self._order_row(request.order_id)
        if order["state"] != "open":
            raise InvalidState("只能对未发货订单申请紧急替代")
        if request.substitute_supplier_id == order["supplier_id"]:
            raise ValidationFailed("替代供应商不能与原供应商相同")
        substitute_basis = self.evaluate_supply(
            request.substitute_supplier_id, order["material_code"], order["plant_id"],
            request.valid_from,
        )
        if substitute_basis["approved_product"] is None:
            raise ValidationFailed("替代供应商没有该物料的有效获准产品授权")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO emergency_approvals(approval_id,order_id,original_supplier_id,"
                    "substitute_supplier_id,material_code,quantity,unit,valid_from,valid_until,"
                    "reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (request.approval_id, request.order_id, order["supplier_id"],
                     request.substitute_supplier_id, order["material_code"],
                     decimal_text(request.quantity), request.unit,
                     request.valid_from, request.valid_until, request.reason, actor_id, self._now()),
                )
                self._audit("emergency_approval", request.approval_id, "emergency.requested", actor_id, {
                    "order_id": request.order_id,
                    "substitute_supplier_id": request.substitute_supplier_id,
                    "quantity": decimal_text(request.quantity),
                    "valid_from": request.valid_from, "valid_until": request.valid_until,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("紧急替代批准编号已经存在") from exc
        return self.emergency_approval(request.approval_id)

    def emergency_approval(self, approval_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM emergency_approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise NotFound("紧急替代批准不存在")
        result = dict(row)
        result["dual_approved"] = bool(result["quality_approved_at"] and result["procurement_approved_at"])
        return result

    def approve_emergency(self, actor_id: str, approval_id: str, side: str) -> dict[str, Any]:
        """质量与采购分别批准；两边都完成后才生效，批准仅对载明数量和期限有效。"""
        if side == "quality":
            self._require(actor_id, "emergency.quality")
            column, stamp = "quality_approved_by", "quality_approved_at"
        elif side == "procurement":
            self._require(actor_id, "emergency.procurement")
            column, stamp = "procurement_approved_by", "procurement_approved_at"
        else:
            raise ValidationFailed("side 必须是 quality 或 procurement")
        row = self.connection.execute(
            "SELECT * FROM emergency_approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise NotFound("紧急替代批准不存在")
        if row["state"] == "void":
            raise InvalidState("紧急替代批准已作废")
        if row[column] is not None:
            raise Conflict(f"{side} 已经完成批准")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                f"UPDATE emergency_approvals SET {column}=?, {stamp}=? WHERE approval_id=?",
                (actor_id, self._now(), approval_id),
            )
            updated = self.connection.execute(
                "SELECT * FROM emergency_approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
            if updated["quality_approved_at"] and updated["procurement_approved_at"]:
                self.connection.execute(
                    "UPDATE emergency_approvals SET state='approved' WHERE approval_id=?", (approval_id,)
                )
            self._audit("emergency_approval", approval_id, f"emergency.{side}_approved", actor_id, {
                "order_id": row["order_id"], "state_after": (
                    "approved" if updated["quality_approved_at"] and updated["procurement_approved_at"]
                    else "pending"
                ),
            })
        return self.emergency_approval(approval_id)

    def void_emergency(self, actor_id: str, approval_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "order.write")
        row = self.connection.execute(
            "SELECT * FROM emergency_approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise NotFound("紧急替代批准不存在")
        if row["state"] == "approved":
            raise InvalidState("已生效批准请按期限到期处理，不能直接作废")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE emergency_approvals SET state='void' WHERE approval_id=?", (approval_id,)
            )
            self._audit("emergency_approval", approval_id, "emergency.voided", actor_id, {"reason": reason})
        return self.emergency_approval(approval_id)

    # ----- 变化影响追溯 -----
    def _propagate_change(
        self,
        change_kind: str,
        entity_type: str,
        entity_id: str,
        supplier_id: str,
        material_code: str | None,
        plant_id: str | None,
        detail: str,
    ) -> None:
        """把一次资格变化落到该企业每张订单：未发货→复核，已发货/已验收→按当时规则保留。"""
        orders = self.connection.execute(
            "SELECT order_id,state,material_code,plant_id FROM purchase_orders "
            "WHERE supplier_id=? ORDER BY order_id",
            (supplier_id,),
        ).fetchall()
        now = self._now()
        for order in orders:
            classification = classify_impact(
                order["state"], order["material_code"], order["plant_id"],
                material_code, plant_id,
            )
            if classification == "none":
                continue
            self.connection.execute(
                "INSERT INTO qualification_change_impacts(change_kind,entity_type,entity_id,supplier_id,"
                "material_code,plant_id,order_id,classification,detail,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (change_kind, entity_type, entity_id, supplier_id, material_code, plant_id,
                 order["order_id"], classification, detail, now),
            )
            if classification == "review":
                self.connection.execute(
                    "UPDATE purchase_orders SET review_required=1, review_reason=COALESCE(review_reason,?) "
                    "WHERE order_id=? AND state='open' AND review_required=0",
                    (detail, order["order_id"]),
                )

    def change_impacts(self, actor_id: str, entity_type: str, entity_id: str) -> dict[str, Any]:
        """每次资格变化影响了哪些订单，以及每张是"需复核"还是"按当时规则保留"。"""
        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT * FROM qualification_change_impacts WHERE entity_type=? AND entity_id=? ORDER BY impact_id",
            (entity_type, entity_id),
        ).fetchall()
        impacts = [dict(r) for r in rows]
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "review_order_ids": [r["order_id"] for r in impacts if r["classification"] == "review"],
            "retained_order_ids": [r["order_id"] for r in impacts if r["classification"] == "retained"],
            "impacts": impacts,
        }

    def order_impact_history(self, actor_id: str, order_id: str) -> dict[str, Any]:
        """采购查询：某张订单经历过的每次资格变化及其处置分类。"""
        self._require(actor_id, "report.read")
        self._order_row(order_id)
        rows = self.connection.execute(
            "SELECT * FROM qualification_change_impacts WHERE order_id=? ORDER BY impact_id",
            (order_id,),
        ).fetchall()
        return {"order_id": order_id, "events": [dict(r) for r in rows], "count": len(rows)}

    def order_compliance(self, actor_id: str, order_id: str) -> dict[str, Any]:
        """订单合规视图：立项/发货快照依据 + 当前资格状态 + 待复核原因。"""
        self._require(actor_id, "report.read")
        order = self.order(order_id)
        current = self.evaluate_supply(order["supplier_id"], order["material_code"], order["plant_id"])
        emergency = self.connection.execute(
            "SELECT * FROM emergency_approvals WHERE order_id=? ORDER BY created_at", (order_id,)
        ).fetchall()
        return {
            "order_id": order_id,
            "state": order["state"],
            "review_required": order["review_required"],
            "review_reason": order["review_reason"],
            "shipped_snapshot": order["qualification_snapshot"],
            "current_eligibility": current,
            "emergency_approvals": [
                {k: row[k] for k in (
                    "approval_id", "substitute_supplier_id", "quantity", "unit",
                    "valid_from", "valid_until", "state",
                    "quality_approved_by", "procurement_approved_by")}
                for row in emergency
            ],
        }

    # ----- 审计链 -----
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
            if row["previous_hash"] != previous_hash or row["event_hash"] != digest(body):
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    def _order_row(self, order_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM purchase_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if row is None:
            raise NotFound("采购订单不存在")
        return row


# ----- 小工具 -----
def _identifier_field(raw: Mapping[str, Any], field: str) -> str:
    return identifier(raw.get(field), field)


def _optional_identifier_field(raw: Mapping[str, Any], field: str) -> str | None:
    return optional_identifier(raw.get(field), field)


def _text_field(raw: Mapping[str, Any], field: str, maximum: int = 256) -> str:
    return required_text(raw.get(field), field, maximum)


def _date_range_values(start: object, end: object) -> tuple[str, str]:
    from datetime import date

    if not isinstance(start, str) or not isinstance(end, str):
        raise ValidationFailed("valid_from 和 valid_until 必须是 YYYY-MM-DD 日期")
    try:
        start_iso = date.fromisoformat(start).isoformat()
        end_iso = date.fromisoformat(end).isoformat()
    except ValueError as exc:
        raise ValidationFailed("日期必须是 YYYY-MM-DD 格式") from exc
    if end_iso <= start_iso:
        raise ValidationFailed("valid_until 必须晚于 valid_from")
    return start_iso, end_iso
