from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ConflictError, ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (AUDIT_ROLES, CONSTRUCTION_STATE, CREATE_ROLES, DEPENDENCY_ROLES,
                    ENTITY, RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    construction_basis_blockers, dependency_basis_status,
                    escalation_required, find_cycle, has_started_construction,
                    invalid_basis_for_not_started, priority_score,
                    response_deadline_hours, role_for_transition, require_item_id,
                    validate_transition)


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
        if target == CONSTRUCTION_STATE:
            basis_blockers = construction_basis_blockers(
                self.repository.list_dependencies(downstream_item_id=item_id))
            if basis_blockers:
                raise ConflictError(
                    "共用构件未全部验收，下游不得开工", details={"blocked_basis": basis_blockers})
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        result = self.enrich(self.repository.get_item(item_id))
        upstream = [self._annotate_dependency(dep)
                    for dep in self.repository.list_dependencies(downstream_item_id=item_id)]
        result["upstream_basis"] = upstream
        result["invalid_basis"] = invalid_basis_for_not_started(
            upstream, result["status"])
        return result

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def register_dependency(self, payload: Dict[str, Any], actor: str,
                            role: str) -> Dict[str, Any]:
        """登记结构依赖：上游项目、共用部位、依据。同一关系只留一条；成环拒绝。"""
        ensure_role(role, DEPENDENCY_ROLES)
        actor = require_text(actor, "actor", 100)
        upstream_id = require_item_id(payload.get("upstream_item_id"))
        downstream_id = require_item_id(payload.get("downstream_item_id"))
        shared_part = require_text(payload.get("shared_part"), "shared_part", 200)
        basis = require_text(payload.get("basis"), "basis")
        dependency = self.repository.add_dependency(
            upstream_id, downstream_id, shared_part, basis, actor, find_cycle)
        self.repository.append_audit("dependency_register", ENTITY, downstream_id, actor, {
            "dependency_id": dependency["id"], "upstream_item_id": upstream_id,
            "downstream_item_id": downstream_id, "shared_part": shared_part,
        })
        return self._annotate_dependency(dependency)

    def list_dependencies(self, role: str, downstream_item_id: Optional[int] = None,
                          upstream_item_id: Optional[int] = None) -> list:
        self._view(role)
        return [self._annotate_dependency(dep)
                for dep in self.repository.list_dependencies(downstream_item_id, upstream_item_id)]

    @staticmethod
    def _annotate_dependency(dep: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(dep)
        result["basis_status"] = dependency_basis_status(dep["upstream_status"])
        return result

    def dependency_view(self, item_id: int, role: str) -> Dict[str, Any]:
        """单项目的结构依赖视图：上游依据是否满足/失效，供开工前核对。"""
        self._view(role)
        item = self.repository.get_item(item_id)
        upstream = [self._annotate_dependency(dep)
                    for dep in self.repository.list_dependencies(downstream_item_id=item_id)]
        downstream = [self._annotate_dependency(dep)
                      for dep in self.repository.list_dependencies(upstream_item_id=item_id)]
        return {
            "item_id": item_id,
            "construction_started": has_started_construction(item["status"]),
            "upstream": upstream,
            "downstream": downstream,
            "blocked_basis": ([] if item["status"] == CONSTRUCTION_STATE
                              else construction_basis_blockers(upstream)),
            "invalid_basis": invalid_basis_for_not_started(upstream, item["status"]),
        }

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
