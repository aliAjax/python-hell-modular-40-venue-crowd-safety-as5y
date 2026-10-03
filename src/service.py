from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow
from .rules import RuleEngine

PENDING_INCIDENT_STATUSES = {"reported", "triaged", "dispatched", "reopened"}
UNFINISHED_TASK_STATUSES = {"draft", "assigned", "enroute", "on_scene"}


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
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
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

    def split_zone(self, actor, data):
        self._ensure_admin(actor)
        payload = data or {}
        with self.repository.transaction() as tx:
            source = self._get_zone(tx, payload.get("zone_id"))
            self._check_expected_version(source, payload.get("expected_version"))
            self._ensure_zone_reorganizable(tx, source)

            specs = payload.get("new_zones") or payload.get("zones")
            new_zones, new_ids = self._build_split_zones(tx, actor, source, specs)
            gates = self._rezone_split_gates(tx, source, new_ids, payload)
            tasks = self._rezone_split_tasks(tx, source, new_ids, payload)

            now = utcnow()
            source_data = dict(source["data"])
            source_data.update(
                {
                    "superseded_at": now,
                    "superseded_by_zone_ids": list(new_ids),
                    "reorganization": {"type": "split", "replacement_zone_ids": list(new_ids)},
                }
            )
            old_status = source["status"]
            source = tx.update_entity(source["id"], source["version"], "superseded", source_data)
            self._append_audit(
                tx,
                source["id"],
                actor,
                "split",
                old_status,
                "superseded",
                {"replacement_zone_ids": list(new_ids)},
            )

            created_zones = []
            for spec in new_zones:
                created = tx.create_entity(
                    spec["id"],
                    "zone",
                    "closed",
                    spec["data"],
                    actor.user_id,
                )
                created_zones.append(created)
                self._append_audit(
                    tx,
                    created["id"],
                    actor,
                    "split_created",
                    None,
                    "closed",
                    {"source_zone_id": source["id"]},
                )

            updated_gates = [
                self._move_gate(tx, actor, gate, gates[gate["id"]], "split", source["id"])
                for gate in self._connected_gates(tx, [source["id"]])
            ]
            updated_tasks = [
                self._move_task(tx, actor, task, tasks[task["id"]], "split", source["id"])
                for task in self._unfinished_tasks_for_zone(tx, source["id"])
            ]

        return {
            "source_zone": source,
            "zones": created_zones,
            "gates": updated_gates,
            "tasks": updated_tasks,
        }

    def merge_zones(self, actor, data):
        self._ensure_admin(actor)
        payload = data or {}
        with self.repository.transaction() as tx:
            source_ids = self._merge_source_ids(payload)
            sources = [self._get_zone(tx, zone_id) for zone_id in source_ids]
            expected = payload.get("expected_versions", payload.get("expected_version"))
            for index, source in enumerate(sources):
                self._check_expected_version(source, self._expected_for(source, index, expected))
                self._ensure_zone_reorganizable(tx, source)
            venue_id = sources[0]["data"].get("venue_id")
            if any(source["data"].get("venue_id") != venue_id for source in sources):
                raise ValidationError("merged zones must belong to the same venue")

            target_id = str(payload.get("target_id") or payload.get("id") or uuid4())
            name = str(payload.get("name", "")).strip()
            if not name:
                raise ValidationError("merged zone name is required")
            if target_id in source_ids or tx.get_entity(target_id):
                raise ConflictError("entity already exists: " + target_id)

            capacity = sum(int(source["data"].get("capacity", 0)) for source in sources)
            occupancy = sum(int(source["data"].get("current_occupancy", 0)) for source in sources)
            target_data = {
                "venue_id": venue_id,
                "name": name,
                "capacity": capacity,
                "current_occupancy": occupancy,
                "merged_from_zone_ids": list(source_ids),
                "reorganization_type": "merge",
            }
            gates = self._connected_gates(tx, source_ids)
            tasks = self._unfinished_tasks_for_any_zone(tx, source_ids)

            target = tx.create_entity(target_id, "zone", "closed", target_data, actor.user_id)
            self._append_audit(
                tx,
                target["id"],
                actor,
                "merge_created",
                None,
                "closed",
                {"source_zone_ids": list(source_ids)},
            )

            now = utcnow()
            replaced_sources = []
            for source in sources:
                source_data = dict(source["data"])
                source_data.update(
                    {
                        "superseded_at": now,
                        "superseded_by_zone_ids": [target_id],
                        "reorganization": {
                            "type": "merge",
                            "replacement_zone_id": target_id,
                        },
                    }
                )
                replaced = tx.update_entity(
                    source["id"], source["version"], "superseded", source_data
                )
                replaced_sources.append(replaced)
                self._append_audit(
                    tx,
                    source["id"],
                    actor,
                    "merge",
                    source["status"],
                    "superseded",
                    {"replacement_zone_id": target_id},
                )

            updated_gates = [
                self._move_gate(
                    tx,
                    actor,
                    gate,
                    self._merged_gate_zone_ids(gate["data"].get("zone_ids") or [], source_ids, target_id),
                    "merge",
                    target_id,
                )
                for gate in gates
            ]
            updated_tasks = [
                self._move_task(tx, actor, task, target_id, "merge", target_id)
                for task in tasks
            ]

        return {
            "source_zones": replaced_sources,
            "zone": target,
            "gates": updated_gates,
            "tasks": updated_tasks,
        }

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

    @staticmethod
    def _ensure_admin(actor):
        if actor.role != "admin":
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _append_audit(tx, entity_id, actor, action, from_status, to_status, detail):
        tx.append_audit(
            entity_id,
            actor.user_id,
            actor.role,
            action,
            from_status,
            to_status,
            detail,
        )

    @staticmethod
    def _get_zone(tx, zone_id):
        if not zone_id:
            raise ValidationError("zone_id is required")
        zone = tx.get_entity(str(zone_id))
        if not zone or zone["kind"] != "zone":
            raise NotFoundError("zone not found: " + str(zone_id))
        return zone

    @staticmethod
    def _check_expected_version(entity, expected_version):
        if expected_version is not None and int(expected_version) != entity["version"]:
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, entity["version"])
            )

    def _ensure_zone_reorganizable(self, tx, zone):
        if zone["status"] == "superseded" or zone["data"].get("superseded_at"):
            replacements = zone["data"].get("superseded_by_zone_ids") or []
            raise ConflictError(
                "zone %s has been superseded by %s" % (zone["id"], ",".join(replacements))
            )
        if zone["status"] == "evacuating":
            raise ConflictError("cannot reorganize an evacuating zone")
        for incident in tx.list_entities(kind="incident"):
            if incident["data"].get("zone_id") == zone["id"] and incident["status"] in PENDING_INCIDENT_STATUSES:
                raise ConflictError("zone has a pending incident: " + incident["id"])

    @staticmethod
    def _as_int(value, field):
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValidationError(field + " must be an integer")

    def _build_split_zones(self, tx, actor, source, specs):
        if not isinstance(specs, list) or len(specs) != 2:
            raise ValidationError("exactly two new zones are required")
        source_capacity = self._as_int(source["data"].get("capacity"), "capacity")
        source_occupancy = self._as_int(source["data"].get("current_occupancy", 0), "current_occupancy")
        total_capacity = 0
        total_occupancy = 0
        seen_ids = set()
        normalized = []
        for index, spec in enumerate(specs):
            if not isinstance(spec, dict):
                raise ValidationError("new zone must be an object")
            name = str(spec.get("name", "")).strip()
            if not name:
                raise ValidationError("new zone name is required")
            capacity = self._as_int(spec.get("capacity"), "capacity")
            occupancy = self._as_int(spec.get("current_occupancy", spec.get("occupancy", 0)), "current_occupancy")
            if capacity <= 0:
                raise ValidationError("zone capacity must be positive")
            if occupancy < 0:
                raise ValidationError("current_occupancy cannot be negative")
            if occupancy > capacity:
                raise ValidationError("zone current_occupancy cannot exceed capacity")
            zone_id = str(spec.get("id") or uuid4())
            if zone_id == source["id"] or zone_id in seen_ids or tx.get_entity(zone_id):
                raise ConflictError("entity already exists: " + zone_id)
            seen_ids.add(zone_id)
            total_capacity += capacity
            total_occupancy += occupancy
            normalized.append(
                {
                    "id": zone_id,
                    "data": {
                        "venue_id": source["data"].get("venue_id"),
                        "name": name,
                        "capacity": capacity,
                        "current_occupancy": occupancy,
                        "split_from_zone_id": source["id"],
                        "reorganization_type": "split",
                    },
                }
            )
        if total_capacity != source_capacity:
            raise ValidationError(
                "split capacities must total %s, got %s" % (source_capacity, total_capacity)
            )
        if total_occupancy != source_occupancy:
            raise ValidationError(
                "split occupancies must total %s, got %s" % (source_occupancy, total_occupancy)
            )
        return normalized, [item["id"] for item in normalized]

    @staticmethod
    def _connected_gates(tx, zone_ids):
        zone_ids = set(zone_ids)
        return [
            gate
            for gate in tx.list_entities(kind="gate")
            if zone_ids.intersection(gate["data"].get("zone_ids") or [])
        ]

    def _rezone_split_gates(self, tx, source, new_ids, payload):
        explicit = payload.get("gate_targets") or payload.get("gate_zone_ids") or {}
        if not isinstance(explicit, dict):
            raise ValidationError("gate_targets must be an object")
        connected = self._connected_gates(tx, [source["id"]])
        result = {}
        for gate in connected:
            target_ids = explicit.get(gate["id"], list(new_ids))
            if isinstance(target_ids, str):
                target_ids = [target_ids]
            if not isinstance(target_ids, list) or not target_ids:
                raise ValidationError("gate target zones are required for gate " + gate["id"])
            target_set = set(target_ids)
            if not target_set.issubset(set(new_ids)):
                raise ValidationError("gate %s targets an invalid split zone" % gate["id"])
            if len(target_set) != len(target_ids):
                raise ValidationError("duplicate target zone for gate " + gate["id"])
            existing = [
                zone_id for zone_id in gate["data"].get("zone_ids") or [] if zone_id != source["id"]
            ]
            result[gate["id"]] = existing + list(target_ids)
        return result

    @staticmethod
    def _unfinished_tasks_for_zone(tx, zone_id):
        return [
            task
            for task in tx.list_entities(kind="task")
            if task["data"].get("zone_id") == zone_id and task["status"] in UNFINISHED_TASK_STATUSES
        ]

    @staticmethod
    def _unfinished_tasks_for_any_zone(tx, zone_ids):
        zone_ids = set(zone_ids)
        return [
            task
            for task in tx.list_entities(kind="task")
            if task["data"].get("zone_id") in zone_ids and task["status"] in UNFINISHED_TASK_STATUSES
        ]

    def _rezone_split_tasks(self, tx, source, new_ids, payload):
        explicit = payload.get("task_targets") or {}
        if not isinstance(explicit, dict):
            raise ValidationError("task_targets must be an object")
        default_target = payload.get("task_zone_id")
        tasks = self._unfinished_tasks_for_zone(tx, source["id"])
        result = {}
        for task in tasks:
            target = explicit.get(task["id"], default_target)
            if not target:
                raise ValidationError("target zone is required for task " + task["id"])
            if target not in new_ids:
                raise ValidationError("task %s targets an invalid split zone" % task["id"])
            result[task["id"]] = target
        for task_id in explicit:
            if task_id not in result:
                raise ValidationError("task is not an unfinished task in this zone: " + str(task_id))
        return result

    def _move_gate(self, tx, actor, gate, zone_ids, operation, related_zone_id):
        data = dict(gate["data"])
        data["zone_ids"] = list(zone_ids)
        updated = tx.update_entity(gate["id"], gate["version"], gate["status"], data)
        self._append_audit(
            tx,
            gate["id"],
            actor,
            "rezone",
            gate["status"],
            gate["status"],
            {"operation": operation, "zone_ids": list(zone_ids), "related_zone_id": related_zone_id},
        )
        return updated

    def _move_task(self, tx, actor, task, zone_id, operation, related_zone_id):
        data = dict(task["data"])
        data["zone_id"] = zone_id
        updated = tx.update_entity(task["id"], task["version"], task["status"], data)
        self._append_audit(
            tx,
            task["id"],
            actor,
            "rezone",
            task["status"],
            task["status"],
            {
                "operation": operation,
                "from_zone_id": task["data"].get("zone_id"),
                "to_zone_id": zone_id,
                "related_zone_id": related_zone_id,
            },
        )
        return updated

    @staticmethod
    def _merge_source_ids(payload):
        zone_ids = payload.get("zone_ids") or payload.get("source_zone_ids")
        if not isinstance(zone_ids, list) or len(zone_ids) != 2:
            raise ValidationError("exactly two source zones are required")
        if not all(zone_ids) or zone_ids[0] == zone_ids[1]:
            raise ValidationError("two distinct source zones are required")
        return [str(zone_id) for zone_id in zone_ids]

    @staticmethod
    def _expected_for(zone, index, expected):
        if expected is None:
            return None
        if isinstance(expected, dict):
            return expected.get(zone["id"])
        if isinstance(expected, list):
            return expected[index] if index < len(expected) else None
        return expected

    @staticmethod
    def _merged_gate_zone_ids(zone_ids, source_ids, target_id):
        source_ids = set(source_ids)
        result = []
        replaced = False
        for zone_id in zone_ids:
            if zone_id in source_ids:
                if not replaced:
                    result.append(target_id)
                    replaced = True
            elif zone_id not in result:
                result.append(zone_id)
        if not replaced:
            result.append(target_id)
        return result
