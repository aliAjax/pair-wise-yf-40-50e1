from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .repository import utcnow
from .rules import (
    RuleEngine,
    ack_target,
    trace_downstream_facilities,
    validate_lab_result,
    validate_propagation_link,
)


class LedgerProcessingError(DomainError):
    """Raised when a notification dispatch itself fails (recorded on the run)."""


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
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
        if kind == "consignment" and payload.get("origin") and payload.get("destination"):
            # Movement between port and planting point registers a time-scoped
            # propagation link used by the downstream ledger.
            self.repository.upsert_link(
                upstream=payload["origin"],
                downstream=payload["destination"],
                consignment_id=entity["id"],
                effective_from=entity["created_at"],
                source="consignment",
            )
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

    # ------------------------------------------------------------------
    # Time-aware traceability ledger
    # ------------------------------------------------------------------
    SYSTEM_ACTOR = "trace-ledger"

    def _require_consignment(self, consignment_id):
        entity = self.repository.get_entity(consignment_id)
        if not entity or entity["kind"] != "consignment":
            raise NotFoundError("consignment not found: " + str(consignment_id))
        return entity

    def register_link(self, actor, data):
        if actor.role not in ("admin", "quarantine"):
            raise PermissionDenied("role %s cannot register propagation links" % actor.role)
        payload = validate_propagation_link(data)
        if payload.get("consignment_id") and not self.repository.get_entity(payload["consignment_id"]):
            raise NotFoundError("consignment not found: " + payload["consignment_id"])
        link = self.repository.upsert_link(source="manual", **payload)
        self.audit.record(link["id"], actor, "register_link", None, "active", {
            "upstream": link["upstream"],
            "downstream": link["downstream"],
            "consignment_id": link["consignment_id"],
        })
        return link

    def list_links(self, upstream=None):
        return self.repository.list_links(upstream=upstream)

    def backfill_links(self, actor):
        if actor.role != "admin":
            raise PermissionDenied("only admin may run link backfill")
        added = self.repository.backfill_links()
        self.audit.record("system", actor, "backfill_links", None, "done", {"added": added})
        return {"added": added}

    def submit_lab_result(self, actor, consignment_id, data):
        self.rules.ensure_ledger_role(actor)
        consignment = self._require_consignment(consignment_id)
        payload = validate_lab_result(data)
        submitted_at = utcnow()
        effective_at = payload.get("effective_at") or submitted_at
        record, outcome, winner = self.repository.submit_lab(
            sample_id=payload["sample_id"],
            consignment_id=consignment_id,
            pest_found=payload["pest_found"],
            pest_name=payload["pest_name"],
            finding=payload["finding"],
            submitted_by=actor.user_id,
            submitted_at=submitted_at,
            effective_at=effective_at,
        )
        if outcome == "effective":
            self.audit.record(consignment_id, actor, "lab_effective",
                             consignment["status"], consignment["status"], {
                                 "submission_id": record["id"],
                                 "sample_id": record["sample_id"],
                                 "pest_found": record["pest_found"],
                                 "pest_name": record["pest_name"],
                             })
            run = self.recompute_for_consignment(consignment_id, record, actor)
            return {"submission": record, "outcome": "effective", "run": run}

        self.audit.record(consignment_id, actor, "lab_conflict",
                         consignment["status"], consignment["status"], {
                             "submission_id": record["id"],
                             "sample_id": record["sample_id"],
                             "winner_id": winner["id"] if winner else record["winner_id"],
                             "winner_by": winner["submitted_by"] if winner else None,
                             "field_record": {
                                 "pest_found": record["pest_found"],
                                 "pest_name": record["pest_name"],
                                 "finding": record["finding"],
                             },
                         })
        return {
            "submission": record,
            "outcome": "conflict",
            "conflict": {
                "reason": "sample %s already has an effective result" % record["sample_id"],
                "effective": winner,
                "retained_record": {
                    "id": record["id"],
                    "submitted_by": record["submitted_by"],
                    "submitted_at": record["submitted_at"],
                    "pest_found": record["pest_found"],
                    "pest_name": record["pest_name"],
                    "finding": record["finding"],
                },
            },
        }

    def list_lab_results(self, consignment_id=None, sample_id=None, status=None):
        return self.repository.list_submissions(
            consignment_id=consignment_id, sample_id=sample_id, status=status
        )

    def preview_trace(self, consignment_id, effective_at=None):
        consignment = self._require_consignment(consignment_id)
        origin = consignment["data"].get("origin")
        effective_at = effective_at or utcnow()
        edges = self.repository.list_links()
        findings = trace_downstream_facilities(edges, origin, effective_at)
        return {
            "root_consignment_id": consignment_id,
            "origin": origin,
            "effective_at": effective_at,
            "targets": findings,
        }

    def recompute_for_consignment(self, consignment_id, submission=None, actor=None):
        """Invalidate unfinished ledger runs for this batch and create a fresh
        run computed from links effective at the submission's effective time."""
        consignment = self._require_consignment(consignment_id)
        if submission is None:
            submissions = self.repository.list_submissions(
                consignment_id=consignment_id, status="effective"
            )
            submission = submissions[-1] if submissions else None
        if not submission:
            raise ValidationError("no effective lab result for this consignment")

        reason = ("new effective lab result %s" % submission["id"])
        previous = self.repository.list_submissions(
            consignment_id=consignment_id, status="effective"
        )
        earlier_positive = any(s["id"] != submission["id"] and s["pest_found"]
                               for s in previous)
        self.repository.invalidate_open_runs(
            consignment_id, reason,
            at=submission["submitted_at"],
            # A retest that overturns the conclusion voids even the notices
            # of already-completed runs; re-confirmation of a positive batch
            # leaves those conclusions (and their notices) in place.
            void_unconfirmed=(not submission["pest_found"] and earlier_positive),
        )

        run_id = str(uuid4())
        items = []
        if submission["pest_found"]:
            edges = self.repository.list_links()
            # The batch's own destination is notified directly; downstream
            # propagation continues from that planting point.
            findings = trace_downstream_facilities(
                edges, consignment["data"].get("destination"), submission["effective_at"]
            )
            items.append({
                "target_location": consignment["data"].get("destination"),
                "detail": {
                    "kind": "destination",
                    "consignment_ids": [consignment_id],
                    "pest_name": submission.get("pest_name", ""),
                },
            })
            for finding in findings:
                items.append({
                    "target_location": finding["location"],
                    "detail": {
                        "kind": "downstream",
                        "via_edge_id": finding["via_edge_id"],
                        "consignment_ids": finding["consignment_ids"],
                        "pest_name": submission.get("pest_name", ""),
                    },
                })
        run = self.repository.create_run(
            run_id=run_id,
            root_consignment_id=consignment_id,
            effective_at=submission["effective_at"],
            pest_found=submission["pest_found"],
            trigger_submission_id=submission["id"],
            items=items,
        )
        self.audit.record(consignment_id, actor or _SystemActor(), "trace_recompute",
                         None, run["status"], {
                             "run_id": run_id,
                             "generation": run["generation"],
                             "targets": len(items),
                             "pest_found": submission["pest_found"],
                         })
        return self.process_run(run_id, actor=actor, submitted_at=submission["submitted_at"])

    def _facility_for_location(self, location):
        facilities = self.repository.list_entities(kind="facility")
        for facility in facilities:
            if facility["data"].get("name") == location:
                return facility["id"]
        return None

    def process_run(self, run_id, actor=None, submitted_at=None):
        """Process run items one batch target at a time. Each handled target
        is checkpointed, so a later retry never notifies twice."""
        run = self.repository.get_run(run_id)
        if not run:
            raise NotFoundError("trace run not found: " + run_id)
        if run["status"] == "completed":
            return run
        if run["status"] == "invalidated":
            raise ConflictError("run %s was invalidated by a newer recompute" % run_id)
        issued_by = actor.user_id if actor else self.SYSTEM_ACTOR
        self.repository.mark_run(run_id, "running")
        submission = (
            self.repository.get_submission(run["trigger_submission_id"])
            if run["trigger_submission_id"] else None
        )
        facilities = self.repository.list_entities(kind="facility")
        while True:
            item = self.repository.next_pending_item(run_id)
            if item is None:
                break
            try:
                notification, outcome = self._notify_item(
                    run, item, submission, facilities, issued_by,
                    submitted_at or (submission["submitted_at"] if submission else None),
                )
                self.repository.mark_item(run_id, item["seq"], "notified",
                                          notification_id=notification["id"])
                self.audit.record(notification["id"], actor or _SystemActor(),
                                  "notification_" + outcome, None, notification["status"], {
                                      "run_id": run_id,
                                      "target": item["target_location"],
                                      "version_no": notification["version_no"],
                                  })
            except Exception as exc:
                # Breakpoint: the item stays pending and the run is failed,
                # ready to resume without re-notifying earlier targets.
                self.repository.mark_item(run_id, item["seq"], "pending",
                                          error=str(exc))
                self.repository.mark_run(run_id, "failed", error=str(exc))
                raise LedgerProcessingError(
                    "run %s failed at target %s: %s"
                    % (run_id, item["target_location"], exc)
                )
        completed = self.repository.mark_run(run_id, "completed", completed=True)
        return self.repository.get_run(run_id)

    def _notify_item(self, run, item, submission, facilities, issued_by, at):
        payload = {
            "root_consignment_id": run["root_consignment_id"],
            "target_location": item["target_location"],
            "pest_found": True,
            "pest_name": (item["detail"].get("pest_name")
                          or (submission["pest_name"] if submission else "")),
            "sample_id": submission["sample_id"] if submission else None,
            "submission_id": submission["id"] if submission else None,
            "effective_at": run["effective_at"],
            "consignment_ids": item["detail"].get("consignment_ids", []),
            "via_edge_id": item["detail"].get("via_edge_id"),
        }
        payload = ack_target(payload, facilities, item["target_location"])
        return self.repository.issue_notification(
            run_id=run["id"],
            root_consignment_id=run["root_consignment_id"],
            target_location=item["target_location"],
            facility_id=payload.get("facility_id"),
            payload=payload,
            issued_by=issued_by,
            at=at,
        )

    def resume_run(self, run_id, actor=None):
        run = self.repository.get_run(run_id)
        if not run:
            raise NotFoundError("trace run not found: " + run_id)
        return self.process_run(run_id, actor=actor)

    def list_runs(self, consignment_id=None, status=None):
        return self.repository.list_runs(root_consignment_id=consignment_id, status=status)

    def get_run_detail(self, run_id):
        run = self.repository.get_run(run_id)
        if not run:
            raise NotFoundError("trace run not found: " + run_id)
        run = dict(run)
        run["items"] = self.repository.list_run_items(run_id)
        return run

    def list_notifications(self, consignment_id=None, target_location=None,
                           status=None, include_void=True):
        return self.repository.list_notifications(
            root_consignment_id=consignment_id,
            target_location=target_location,
            status=status,
            include_void=include_void,
        )

    def acknowledge_notification(self, actor, notification_id, note=None):
        self.rules.ensure_ack_role(actor)
        notification = self.repository.acknowledge_notification(
            notification_id, actor.user_id, note
        )
        self.audit.record(notification_id, actor, "acknowledge",
                         "issued", "acknowledged", {"note": note})
        return notification


class _SystemActor:
    user_id = DomainService.SYSTEM_ACTOR
    role = "quarantine"
