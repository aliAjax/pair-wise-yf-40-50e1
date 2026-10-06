from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
    normalize_effective_at,
)


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _validate_release(actor, entity, data, lookup):
    if data.get("pest_found"):
        raise ValidationError("pest-positive consignment cannot be released")
    if data.get("treatment") not in ("none", "completed", "certified"):
        raise ValidationError("release requires a valid treatment state")
    return {"released_by": actor.user_id}


def trace_downstream(consignments, start_id):
    pending = [start_id]
    visited = set()
    result = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for item in consignments:
            if item.get("parent_id") == current:
                pending.append(item.get("id"))
    return result


def active_propagation_edges(edges, effective_at):
    """Edges that are already in effect at ``effective_at``.

    When several links share the same upstream/downstream pair, only the
    earliest effective one applies at that instant.
    """
    candidates = {}
    for edge in edges:
        if edge.get("effective_from") and edge["effective_from"] > effective_at:
            continue
        if edge.get("effective_to") and edge["effective_to"] <= effective_at:
            continue
        key = (edge["upstream"], edge["downstream"])
        current = candidates.get(key)
        if current is None or (edge.get("effective_from") or "") < (current.get("effective_from") or ""):
            candidates[key] = edge
    return list(candidates.values())


def trace_downstream_facilities(edges, start_location, effective_at):
    """BFS along propagation links effective at ``effective_at``.

    Returns a list of ``{"location", "via_edge_id", "consignment_ids"}``
    for each downstream planting point, in discovery order.
    """
    active = active_propagation_edges(edges, effective_at)
    pending = [start_location]
    visited = {start_location}
    findings = {}
    order = []
    while pending:
        current = pending.pop(0)
        for edge in active:
            if edge["upstream"] != current:
                continue
            downstream = edge["downstream"]
            if downstream not in findings:
                order.append(downstream)
                findings[downstream] = {
                    "location": downstream,
                    "via_edge_id": edge["id"],
                    "consignment_ids": [],
                }
            if edge.get("consignment_id"):
                findings[downstream]["consignment_ids"].append(edge["consignment_id"])
            if downstream not in visited:
                visited.add(downstream)
                pending.append(downstream)
    return [findings[name] for name in order]


def validate_lab_result(data):
    sample_id = data.get("sample_id")
    if not sample_id:
        raise ValidationError("missing required field: sample_id")
    if not isinstance(data.get("pest_found"), bool):
        raise ValidationError("pest_found must be true or false")
    payload = {
        "sample_id": str(sample_id),
        "pest_found": bool(data["pest_found"]),
        "pest_name": data.get("pest_name") or "",
        "finding": data.get("finding") or data.get("lab_note") or "",
    }
    effective_at = normalize_effective_at(data.get("effective_at"))
    if effective_at:
        payload["effective_at"] = effective_at
    return payload


def validate_propagation_link(data):
    upstream = data.get("upstream")
    downstream = data.get("downstream")
    if not upstream:
        raise ValidationError("missing required field: upstream")
    if not downstream:
        raise ValidationError("missing required field: downstream")
    if upstream == downstream:
        raise ValidationError("upstream and downstream must differ")
    payload = {
        "upstream": str(upstream),
        "downstream": str(downstream),
    }
    if data.get("consignment_id"):
        payload["consignment_id"] = str(data["consignment_id"])
    effective_from = normalize_effective_at(data.get("effective_from"))
    if effective_from:
        payload["effective_from"] = effective_from
    effective_to = normalize_effective_at(data.get("effective_to"))
    if effective_to:
        payload["effective_to"] = effective_to
    if effective_from and effective_to and effective_to <= effective_from:
        raise ValidationError("effective_to must be after effective_from")
    return payload


def ack_target(payload, facilities, location):
    """Resolve a location name to a registered facility id when possible."""
    for facility in facilities:
        if facility["data"].get("name") == location:
            payload["facility_id"] = facility["id"]
    return payload


CUSTOM_CREATE = {'consignment': _validate_consignment}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected',), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected')}, 'facility': {'trace': (('registered',), 'traced')}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address')}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('pest_found', 'treatment'), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('facility', 'trace'): ('consignment_ids',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine')}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine')}
    LEDGER_ROLES = ('admin', 'inspector', 'lab', 'quarantine')
    ACK_ROLES = ('admin', 'inspector', 'quarantine')

    def ensure_ledger_role(self, actor):
        self._ensure_role(actor, self.LEDGER_ROLES)

    def ensure_ack_role(self, actor):
        self._ensure_role(actor, self.ACK_ROLES)

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
