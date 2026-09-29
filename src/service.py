from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_id, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DEPENDENCY_ROLES, ENTITY,
                    RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)
from .dependencies import construction_blockers


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        if target == "construction":
            dep_blockers = construction_blockers(
                self.repository.construction_links(item_id))
            if dep_blockers:
                raise ConflictError("；".join(dep_blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        if target == "rejected":
            invalidated = self.repository.invalidate_downstream_edges(item_id)
            for edge in invalidated:
                self.repository.append_audit(
                    "dependency_invalidated", "结构依赖", edge["id"], actor, {
                        "upstream_id": edge["upstream_id"],
                        "downstream_id": edge["downstream_id"],
                        "shared_part": edge["shared_part"],
                        "basis": edge["basis"],
                    })
        return self.enrich(updated)

    def register_dependency(self, downstream_id: int, payload: Dict[str, Any],
                            actor: str, role: str) -> Dict[str, Any]:
        """为下游项目登记一个上游项目及共用部位、依据。"""
        ensure_role(role, DEPENDENCY_ROLES)
        actor = require_text(actor, "actor", 100)
        downstream_id = require_id(downstream_id, "downstream_id")
        upstream_id = require_id(
            payload.get("upstream_id", payload.get("upstream")), "upstream_id")
        shared_part = require_text(payload.get("shared_part"), "shared_part", 200)
        basis = require_text(payload.get("basis"), "basis")
        edge, reactivated = self.repository.add_dependency(
            upstream_id, downstream_id, shared_part, basis, actor)
        self.repository.append_audit(
            "dependency_reactivated" if reactivated else "dependency_registered",
            "结构依赖", edge["id"], actor, {
                "upstream_id": upstream_id, "downstream_id": downstream_id,
                "shared_part": shared_part, "basis": basis,
                "reactivated": reactivated,
            })
        return edge

    def list_dependencies(self, item_id: int, role: str,
                          direction: str = "both",
                          status: Optional[str] = None) -> list:
        self._view(role)
        if direction not in ("upstream", "downstream", "both"):
            from .domain import ValidationError
            raise ValidationError("direction只能是upstream、downstream或both")
        if status is not None and status not in ("active", "invalidated"):
            from .domain import ValidationError
            raise ValidationError("status只能是active或invalidated")
        self.repository.get_item(item_id)
        return self.repository.list_dependencies(item_id, direction, status)


    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
