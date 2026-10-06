from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
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


def _validate_lab_result(actor, data, lookup):
    if data.get("pest_found") is None:
        raise ValidationError("pest_found is required for lab results")
    if not data.get("sample_id"):
        raise ValidationError("sample_id is required")
    if not data.get("consignment_id"):
        raise ValidationError("consignment_id is required")
    return {}


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


def _consignment_children(consignments, parent_id):
    return [
        item
        for item in consignments
        if (item.get("data", {}) or {}).get("parent_id") == parent_id
    ]


def downstream_facilities(consignments, facilities, start_id, edges=None):
    """沿 parent_id 链和传播边从 start_id 向下游 BFS，返回受影响种植点 id（去重、保序）。

    种植点与批次的关联有三种来源：目的地名称匹配、facility 上显式登记的
    consignment_ids、以及按原发地/目的地补齐的传播边。
    """
    by_id = {item["id"]: item for item in consignments}
    edge_set = set(edges or [])
    visited_consignments = set()
    visited_facilities = []
    queue = [start_id]
    while queue:
        current = queue.pop(0)
        if current in visited_consignments:
            continue
        visited_consignments.add(current)
        consignment = by_id.get(current)
        if consignment is None:
            continue
        cdata = consignment.get("data", {}) or {}
        for facility in facilities:
            fid = facility["id"]
            if fid in visited_facilities:
                continue
            data = facility.get("data", {}) or {}
            linked = data.get("consignment_ids") or []
            if (
                cdata.get("destination")
                and data.get("name") == cdata["destination"]
            ) or current in linked or (current, fid) in edge_set:
                visited_facilities.append(fid)
        for child in _consignment_children(consignments, current):
            if child["id"] not in visited_consignments:
                queue.append(child["id"])
        for src, dst in edge_set:
            if src == current and dst in by_id and dst not in visited_consignments:
                queue.append(dst)
    return visited_facilities


def infer_consignment_parent(consignment, others):
    """按原发地/目的地推断上游批次：目的地等于本批次原发地的批次即为父批。"""
    cdata = consignment.get("data", {}) or {}
    origin = cdata.get("origin")
    if not origin:
        return None
    for other in others:
        if other["id"] == consignment["id"]:
            continue
        odata = other.get("data", {}) or {}
        if odata.get("destination") == origin:
            return other["id"]
    return None


def infer_facility_links(consignment, facilities):
    """目的地与批次目的地同名的种植点即下游关联点。"""
    cdata = consignment.get("data", {}) or {}
    destination = cdata.get("destination")
    links = []
    for facility in facilities:
        data = facility.get("data", {}) or {}
        if destination and data.get("name") == destination:
            links.append(facility["id"])
    return links


CUSTOM_CREATE = {
    'consignment': _validate_consignment,
    'lab_result': _validate_lab_result,
}
CUSTOM_TRANSITIONS = {
    ('consignment', 'quarantine'): _validate_quarantine,
    ('consignment', 'release'): _validate_release,
}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility', 'lab-results': 'lab_result', 'lab_results': 'lab_result', 'trace-runs': 'trace_run', 'trace_runs': 'trace_run', 'notifications': 'notification'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered', 'lab_result': 'effective', 'trace_run': 'in_progress', 'notification': 'pending'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected',), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected')}, 'facility': {'trace': (('registered',), 'traced')}, 'notification': {'confirm': (('pending',), 'confirmed')}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address'), 'lab_result': ('sample_id', 'consignment_id', 'pest_found')}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('pest_found', 'treatment'), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('facility', 'trace'): ('consignment_ids',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine'), 'lab_result': ('admin', 'inspector', 'lab')}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine'), 'confirm': ('admin', 'quarantine')}

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
