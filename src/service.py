from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    RecomputeError,
)
from .repository import utcnow
from .rules import (
    RuleEngine,
    downstream_facilities,
    infer_consignment_parent,
    infer_facility_links,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        # 测试/演练用：对下一次重算注入断点（(run, facility_id) -> 是否中断）
        self.recompute_fault = None

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _propagation_edges(self):
        return {(e["from_id"], e["to_id"]) for e in self.repository.list_propagation_edges()}

    def _positive_results(self, consignment_id):
        return [
            lr
            for lr in self.repository.list_entities(kind="lab_result", status="effective")
            if lr["data"].get("consignment_id") == consignment_id
            and lr["data"].get("pest_found")
        ]

    def _runs_for(self, consignment_id):
        return [
            r
            for r in self.repository.list_entities(kind="trace_run")
            if r["data"].get("consignment_id") == consignment_id
        ]

    def _find_notification(self, status=None, **data_match):
        for notification in self.repository.list_entities(kind="notification"):
            if status is not None and notification["status"] != status:
                continue
            data = notification["data"]
            if all(data.get(key) == value for key, value in data_match.items()):
                return notification
        return None

    def _next_notification_version(self, consignment_id, facility_id):
        versions = [
            int(notification["data"].get("version", 1))
            for notification in self.repository.list_entities(kind="notification")
            if notification["data"].get("consignment_id") == consignment_id
            and notification["data"].get("facility_id") == facility_id
        ]
        return (max(versions) + 1) if versions else 1

    def _checkpoint(self, run, processed, notification_id):
        data = dict(run["data"])
        data["processed"] = processed
        ids = list(data.get("notification_ids", []))
        if notification_id not in ids:
            ids.append(notification_id)
        data["notification_ids"] = ids
        return self.repository.update_entity(run["id"], run["version"], "in_progress", data)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "lab_result":
            return self.submit_lab_result(actor, data, idempotency_key)["result"]
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def submit_lab_result(self, actor, data, idempotency_key=None):
        """提交实验室结果。

        同一 sample_id 已有生效结果时，先到的继续生效，后到的保留为冲突现场记录；
        生效的阳性结果会立即触发追溯重算。
        """
        kind = "lab_result"
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return {
                        "result": entity,
                        "conflict": entity["status"] == "conflicted",
                        "conflict_with": entity["data"].get("conflict_with"),
                    }
        self.rules.validate_create(actor, kind, payload, self._lookup)
        consignment_id = payload["consignment_id"]
        if not self.repository.get_entity(consignment_id):
            raise NotFoundError("consignment not found: " + consignment_id)
        result_data = {
            "sample_id": payload["sample_id"],
            "consignment_id": consignment_id,
            "pest_found": bool(payload["pest_found"]),
            "effective_at": payload.get("effective_at") or utcnow(),
            "inspector": actor.user_id,
            "note": payload.get("note"),
        }
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        entity, conflict = self.repository.create_lab_result(entity_id, result_data, actor.user_id)
        self.audit.record(
            entity_id,
            actor,
            "submit_lab_result",
            None,
            entity["status"],
            {"sample_id": result_data["sample_id"], "conflict": conflict},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if not conflict and result_data["pest_found"]:
            self.recompute(actor, consignment_id)
        return {
            "result": entity,
            "conflict": conflict,
            "conflict_with": entity["data"].get("conflict_with") if conflict else None,
        }

    def recompute(self, actor, consignment_id, _fault=None):
        """按当前生效的阳性结果重算追溯账。

        - 晚到结果改变阳性集合时，未完成（in_progress）的追溯立即作废重算；
        - 已有结论（concluded）保留可查；
        - 重算中断后再次调用会从断点续算，已处理的种植点不重复通知；
        - 未确认的通知作废重发，已确认的通知保留原版本。
        """
        fault = _fault if _fault is not None else self.recompute_fault
        consignment = self.repository.get_entity(consignment_id)
        if not consignment:
            raise NotFoundError("consignment not found: " + consignment_id)
        positive = self._positive_results(consignment_id)
        positive_ids = {item["id"] for item in positive}
        runs = sorted(self._runs_for(consignment_id), key=lambda item: item["created_at"])
        in_progress = next((item for item in runs if item["status"] == "in_progress"), None)
        concluded = [item for item in runs if item["status"] == "concluded"]
        active_concluded = concluded[-1] if concluded else None

        if not positive:
            if in_progress:
                self.repository.update_entity(
                    in_progress["id"],
                    in_progress["version"],
                    "invalidated",
                    {**in_progress["data"], "invalidated_by": actor.user_id, "reason": "no_positive_result"},
                )
                self.audit.record(
                    in_progress["id"], actor, "recompute_invalidate", "in_progress", "invalidated",
                    {"reason": "no_positive_result"},
                )
            return {"run": None, "notifications": [], "invalidated": bool(in_progress)}

        consignments = self.repository.list_entities(kind="consignment")
        facilities = self.repository.list_entities(kind="facility")
        edges = self._propagation_edges()
        facility_ids = downstream_facilities(consignments, facilities, consignment_id, edges)

        def _matches(run):
            return set(run["data"].get("lab_result_ids", [])) == positive_ids and \
                run["data"].get("facility_ids", []) == facility_ids

        if in_progress and _matches(in_progress):
            run = in_progress  # 断点续算：阳性集合未变，从断点继续
        else:
            if in_progress:
                self.repository.update_entity(
                    in_progress["id"],
                    in_progress["version"],
                    "invalidated",
                    {**in_progress["data"], "invalidated_by": actor.user_id, "reason": "late_result"},
                )
                self.audit.record(
                    in_progress["id"], actor, "recompute_invalidate", "in_progress", "invalidated",
                    {"reason": "late_result"},
                )
            if active_concluded and _matches(active_concluded):
                existing = [
                    item
                    for item in self.repository.list_entities(kind="notification")
                    if item["data"].get("run_id") == active_concluded["id"]
                ]
                return {"run": active_concluded, "notifications": existing, "noop": True}
            run_data = {
                "consignment_id": consignment_id,
                "lab_result_ids": [item["id"] for item in positive],
                "sample_ids": sorted({item["data"].get("sample_id") for item in positive}),
                "facility_ids": facility_ids,
                "processed": [],
                "notification_ids": [],
            }
            run = self.repository.create_entity(
                str(uuid4()), "trace_run", "in_progress", run_data, actor.user_id
            )
            self.audit.record(
                run["id"], actor, "recompute_start", None, "in_progress",
                {"consignment_id": consignment_id},
            )

        notifications = []
        processed = list(run["data"].get("processed", []))
        for facility_id in facility_ids:
            if facility_id in processed:
                existing = self._find_notification(run_id=run["id"], facility_id=facility_id)
                if existing:
                    notifications.append(existing)
                continue
            if fault is not None and fault(run, facility_id):
                raise RecomputeError(
                    "recompute interrupted at checkpoint " + facility_id,
                    run_id=run["id"],
                    checkpoint=facility_id,
                )
            # 已确认的通知保留原版本，不再重发
            confirmed = self._find_notification(
                status="confirmed", consignment_id=consignment_id, facility_id=facility_id
            )
            if confirmed:
                notifications.append(confirmed)
                processed.append(facility_id)
                run = self._checkpoint(run, processed, confirmed["id"])
                continue
            # 断点续算：本批次已处理过，不重复通知
            already = self._find_notification(run_id=run["id"], facility_id=facility_id)
            if already:
                notifications.append(already)
                processed.append(facility_id)
                run = self._checkpoint(run, processed, already["id"])
                continue
            # 未确认的通知作废重发
            pending = self._find_notification(
                status="pending", consignment_id=consignment_id, facility_id=facility_id
            )
            if pending:
                self.repository.update_entity(
                    pending["id"],
                    pending["version"],
                    "voided",
                    {**pending["data"], "voided_by": run["id"]},
                )
            notif_data = {
                "run_id": run["id"],
                "consignment_id": consignment_id,
                "facility_id": facility_id,
                "pest_found": True,
                "version": self._next_notification_version(consignment_id, facility_id),
                "issued_at": utcnow(),
            }
            notification = self.repository.create_entity(
                str(uuid4()), "notification", "pending", notif_data, actor.user_id
            )
            self.audit.record(
                notification["id"], actor, "notify", None, "pending",
                {"facility_id": facility_id, "version": notif_data["version"]},
            )
            notifications.append(notification)
            processed.append(facility_id)
            run = self._checkpoint(run, processed, notification["id"])

        final_data = dict(run["data"])
        final_data["processed"] = processed
        final_data["notification_ids"] = [item["id"] for item in notifications]
        run = self.repository.update_entity(run["id"], run["version"], "concluded", final_data)
        self.audit.record(
            run["id"], actor, "recompute_done", "in_progress", "concluded",
            {"notified": len(notifications)},
        )
        return {"run": run, "notifications": notifications}

    def confirm_notification(self, actor, notification_id):
        notification = self.repository.get_entity(notification_id)
        if not notification:
            raise NotFoundError("notification not found: " + notification_id)
        if notification["status"] != "pending":
            raise InvalidTransition(
                "notification %s is %s, cannot confirm" % (notification_id, notification["status"])
            )
        allowed = self.rules.ROLE_ACTIONS.get("confirm", ("admin", "quarantine"))
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s cannot confirm notifications" % actor.role)
        data = {
            **notification["data"],
            "confirmed_by": actor.user_id,
            "confirmed_at": utcnow(),
        }
        updated = self.repository.update_entity(
            notification_id, notification["version"], "confirmed", data
        )
        self.audit.record(
            notification_id, actor, "confirm", "pending", "confirmed",
            {"facility_id": data["facility_id"]},
        )
        return updated

    def upgrade_propagation(self, actor):
        """按原发地/目的地补齐旧数据缺失的传播关系。

        - 缺 parent_id 的批次，以“目的地等于本批原发地”的批次为父批；
        - 缺关联的种植点，以目的地同名补齐传播边；
        - 幂等：已有的 parent_id / 传播边不重复添加，历史通知不动。
        """
        consignments = self.repository.list_entities(kind="consignment")
        facilities = self.repository.list_entities(kind="facility")
        edges = self._propagation_edges()
        added = {"parent_ids": [], "edges": []}
        for consignment in consignments:
            if consignment["data"].get("parent_id"):
                continue
            parent_id = infer_consignment_parent(consignment, consignments)
            if parent_id:
                data = dict(consignment["data"])
                data["parent_id"] = parent_id
                self.repository.update_entity(consignment["id"], consignment["version"], consignment["status"], data)
                added["parent_ids"].append({"consignment_id": consignment["id"], "parent_id": parent_id})
        for consignment in consignments:
            for facility_id in infer_facility_links(consignment, facilities):
                if (consignment["id"], facility_id) not in edges:
                    self.repository.add_propagation_edge(consignment["id"], facility_id, "facility", "inferred")
                    edges.add((consignment["id"], facility_id))
                    added["edges"].append({"from": consignment["id"], "to": facility_id, "source": "inferred"})
        for facility in facilities:
            for consignment_id in facility["data"].get("consignment_ids", []) or []:
                if (consignment_id, facility["id"]) not in edges:
                    self.repository.add_propagation_edge(consignment_id, facility["id"], "facility", "declared")
                    edges.add((consignment_id, facility["id"]))
                    added["edges"].append({"from": consignment_id, "to": facility["id"], "source": "declared"})
        self.audit.record("system", actor, "upgrade_propagation", None, "done", added)
        return added

    def list_runs(self, consignment_id=None, status=None):
        runs = self.repository.list_entities(kind="trace_run")
        if consignment_id:
            runs = [item for item in runs if item["data"].get("consignment_id") == consignment_id]
        if status:
            runs = [item for item in runs if item["status"] == status]
        return runs

    def list_notifications(self, consignment_id=None, facility_id=None, status=None):
        items = self.repository.list_entities(kind="notification")
        if consignment_id:
            items = [item for item in items if item["data"].get("consignment_id") == consignment_id]
        if facility_id:
            items = [item for item in items if item["data"].get("facility_id") == facility_id]
        if status:
            items = [item for item in items if item["status"] == status]
        return items

    def list_conflicts(self):
        return self.repository.list_entities(kind="lab_result", status="conflicted")
