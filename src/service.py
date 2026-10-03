from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (
    RuleEngine,
    check_merge_conservation,
    check_split_conservation,
    is_pending_incident,
    is_unfinished_task,
    require_reconfig_role,
)


def _as_int(data, field):
    try:
        value = int(data.get(field))
    except (TypeError, ValueError):
        raise ValidationError("%s must be an integer" % field)
    return value


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
        if action == "split" and entity["kind"] == "zone":
            return self.split_zone(actor, entity, data, expected_version)
        if action in ("merge", "merge_zones") and entity["kind"] == "venue":
            return self.merge_zones(actor, entity, data, expected_version)
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

    # -- zone split / merge -------------------------------------------------

    def _structure_version(self, venue):
        return int(venue["data"].get("structure_version", 1))

    def _check_structure_version(self, venue, expected_version):
        current = self._structure_version(venue)
        expected = int(expected_version) if expected_version is not None else current
        if current != expected:
            raise ConflictError(
                "structure version conflict: expected %s, found %s" % (expected, current)
            )
        return current

    def split_zone(self, actor, zone, data, expected_version=None):
        require_reconfig_role(actor)
        data = dict(data or {})

        def work(connection):
            venue = self.repository._get_entity(connection, zone["data"]["venue_id"])
            if not venue:
                raise NotFoundError("venue not found")
            self._check_structure_version(venue, expected_version)

            source = self.repository._get_entity(connection, zone["id"])
            if not source or source["kind"] != "zone":
                raise NotFoundError("zone not found")
            if source["status"] == "evacuating":
                raise ConflictError("cannot split a zone that is evacuating")
            if source["status"] in ("split", "merged"):
                raise ConflictError("zone has already been %s" % source["status"])

            incidents = self.repository._find_entities(
                connection, "incident", "zone_id", source["id"]
            )
            for incident in incidents:
                if is_pending_incident(incident):
                    raise ConflictError("cannot split zone with pending incidents")

            name_a = str(data.get("name_a") or "").strip()
            name_b = str(data.get("name_b") or "").strip()
            if not name_a or not name_b:
                raise ValidationError("name_a and name_b are required")
            capacity_a = _as_int(data, "capacity_a")
            capacity_b = _as_int(data, "capacity_b")
            occupancy_a = _as_int(data, "occupancy_a")
            occupancy_b = _as_int(data, "occupancy_b")
            if capacity_a <= 0 or capacity_b <= 0:
                raise ValidationError("new zone capacities must be positive")
            if occupancy_a < 0 or occupancy_b < 0:
                raise ValidationError("occupancy cannot be negative")
            if occupancy_a > capacity_a or occupancy_b > capacity_b:
                raise ValidationError("occupancy cannot exceed capacity")
            check_split_conservation(
                source, capacity_a, occupancy_a, capacity_b, occupancy_b
            )

            tasks = self.repository._find_entities(
                connection, "task", "zone_id", source["id"]
            )
            unfinished = [task for task in tasks if is_unfinished_task(task)]
            assignments = data.get("task_assignments") or {}
            if not isinstance(assignments, dict):
                raise ValidationError("task_assignments must be an object")
            unfinished_ids = {task["id"] for task in unfinished}
            for task in unfinished:
                if assignments.get(task["id"]) not in ("a", "b"):
                    raise ValidationError(
                        "unfinished task %s must be assigned to zone a or b" % task["id"]
                    )
            for task_id in assignments:
                if task_id not in unfinished_ids:
                    raise ValidationError(
                        "task_assignments references task %s not in this zone" % task_id
                    )
            gate_assignments = data.get("gate_assignments") or {}
            if not isinstance(gate_assignments, dict):
                raise ValidationError("gate_assignments must be an object")

            new_status = source["status"]
            if new_status not in ("open", "limited", "closed"):
                new_status = "closed"
            zone_a_id = str(uuid4())
            zone_b_id = str(uuid4())
            base = {"venue_id": source["data"]["venue_id"]}
            zone_a_data = dict(base, name=name_a, capacity=capacity_a,
                               current_occupancy=occupancy_a)
            zone_b_data = dict(base, name=name_b, capacity=capacity_b,
                               current_occupancy=occupancy_b)
            if new_status == "limited":
                zone_a_data["admit_limit"] = capacity_a
                zone_b_data["admit_limit"] = capacity_b
            self.repository._create_entity(
                connection, zone_a_id, "zone", new_status, zone_a_data, actor.user_id
            )
            self.repository._create_entity(
                connection, zone_b_id, "zone", new_status, zone_b_data, actor.user_id
            )

            # Source zone is preserved as history (original data kept) but marked
            # terminal so it is clearly no longer part of the active structure.
            self.repository._save_entity(
                connection, source["id"], "split", dict(source["data"])
            )

            def dest_zones(gate_id):
                dests = gate_assignments.get(gate_id, ["a", "b"])
                if not isinstance(dests, list) or not dests:
                    dests = ["a", "b"]
                mapped = []
                for dest in dests:
                    if dest == "a":
                        mapped.append(zone_a_id)
                    elif dest == "b":
                        mapped.append(zone_b_id)
                return mapped or [zone_a_id, zone_b_id]

            gates = self.repository._find_gates_for_zone(connection, source["id"])
            for gate in gates:
                old_ids = list(gate["data"].get("zone_ids") or [])
                new_ids = dest_zones(gate["id"])
                updated_ids = [zid for zid in old_ids if zid != source["id"]]
                for zid in new_ids:
                    if zid not in updated_ids:
                        updated_ids.append(zid)
                gate_data = dict(gate["data"], zone_ids=updated_ids)
                self.repository._save_entity(connection, gate["id"], gate["status"], gate_data)
                self.repository._append_audit(
                    connection, gate["id"], actor.user_id, actor.role, "reassign",
                    gate["status"], gate["status"],
                    {"from_zone": source["id"], "to_zones": new_ids},
                )

            for kind in ("post", "medical_point"):
                for entity in self.repository._find_entities(
                    connection, kind, "zone_id", source["id"]
                ):
                    entity_data = dict(entity["data"], zone_id=zone_a_id)
                    self.repository._save_entity(
                        connection, entity["id"], entity["status"], entity_data
                    )
                    self.repository._append_audit(
                        connection, entity["id"], actor.user_id, actor.role, "reassign",
                        entity["status"], entity["status"],
                        {"from_zone": source["id"], "to_zone": zone_a_id},
                    )

            for incident in incidents:
                if is_pending_incident(incident):
                    incident_data = dict(incident["data"], zone_id=zone_a_id)
                    self.repository._save_entity(
                        connection, incident["id"], incident["status"], incident_data
                    )
                    self.repository._append_audit(
                        connection, incident["id"], actor.user_id, actor.role, "reassign",
                        incident["status"], incident["status"],
                        {"from_zone": source["id"], "to_zone": zone_a_id},
                    )
                # resolved incidents keep their original zone for history

            for task in tasks:
                if is_unfinished_task(task):
                    dest = assignments[task["id"]]
                    new_zone = zone_a_id if dest == "a" else zone_b_id
                    task_data = dict(task["data"], zone_id=new_zone)
                    self.repository._save_entity(
                        connection, task["id"], task["status"], task_data
                    )
                    self.repository._append_audit(
                        connection, task["id"], actor.user_id, actor.role, "reassign",
                        task["status"], task["status"],
                        {"from_zone": source["id"], "to_zone": new_zone},
                    )
                # finished tasks keep their original zone for history

            venue_data = dict(venue["data"])
            venue_data["structure_version"] = self._structure_version(venue) + 1
            self.repository._save_entity(connection, venue["id"], venue["status"], venue_data)

            for new_id, label in ((zone_a_id, "a"), (zone_b_id, "b")):
                self.repository._append_audit(
                    connection, new_id, actor.user_id, actor.role, "create",
                    None, new_status, {"kind": "zone", "from_split": source["id"], "side": label},
                )
            self.repository._append_audit(
                connection, source["id"], actor.user_id, actor.role, "split",
                source["status"], "split",
                {"new_zone_a": zone_a_id, "new_zone_b": zone_b_id,
                 "capacity_a": capacity_a, "capacity_b": capacity_b,
                 "occupancy_a": occupancy_a, "occupancy_b": occupancy_b},
            )
            self.repository._append_audit(
                connection, venue["id"], actor.user_id, actor.role, "split",
                venue["status"], venue["status"],
                {"source_zone": source["id"], "new_zone_a": zone_a_id,
                 "new_zone_b": zone_b_id, "structure_version": venue_data["structure_version"]},
            )
            return {
                "source": self.repository._get_entity(connection, source["id"]),
                "zone_a": self.repository._get_entity(connection, zone_a_id),
                "zone_b": self.repository._get_entity(connection, zone_b_id),
            }

        return self.repository.run_in_transaction(work)

    def merge_zones(self, actor, venue, data, expected_version=None):
        require_reconfig_role(actor)
        data = dict(data or {})

        def work(connection):
            venue_row = self.repository._get_entity(connection, venue["id"])
            if not venue_row:
                raise NotFoundError("venue not found")
            self._check_structure_version(venue_row, expected_version)

            zone_ids = data.get("zone_ids")
            if not isinstance(zone_ids, list) or len(zone_ids) != 2:
                raise ValidationError("zone_ids must contain exactly two zone ids")
            if len(set(zone_ids)) != 2:
                raise ValidationError("cannot merge a zone with itself")
            name = str(data.get("name") or "").strip()
            if not name:
                raise ValidationError("merged zone name is required")

            zones = []
            for zone_id in zone_ids:
                zone = self.repository._get_entity(connection, zone_id)
                if not zone or zone["kind"] != "zone":
                    raise NotFoundError("zone not found: " + str(zone_id))
                if zone["data"].get("venue_id") != venue_row["id"]:
                    raise ValidationError("zones must belong to the venue")
                if zone["status"] == "evacuating":
                    raise ConflictError("cannot merge a zone that is evacuating")
                if zone["status"] in ("split", "merged"):
                    raise ConflictError("zone %s has already been %s" % (zone_id, zone["status"]))
                incidents = self.repository._find_entities(
                    connection, "incident", "zone_id", zone_id
                )
                for incident in incidents:
                    if is_pending_incident(incident):
                        raise ConflictError("cannot merge zone %s with pending incidents" % zone_id)
                zones.append(zone)

            zone_a, zone_b = zones
            merged_capacity = int(zone_a["data"].get("capacity", 0)) + int(
                zone_b["data"].get("capacity", 0)
            )
            merged_occupancy = int(zone_a["data"].get("current_occupancy", 0)) + int(
                zone_b["data"].get("current_occupancy", 0)
            )
            check_merge_conservation(zone_a, zone_b, merged_capacity, merged_occupancy)

            statuses = {zone_a["status"], zone_b["status"]}
            if "open" in statuses:
                merged_status = "open"
            elif "limited" in statuses:
                merged_status = "limited"
            else:
                merged_status = "closed"

            merged_id = str(uuid4())
            merged_data = {
                "venue_id": venue_row["id"],
                "name": name,
                "capacity": merged_capacity,
                "current_occupancy": merged_occupancy,
            }
            if merged_status == "limited":
                merged_data["admit_limit"] = merged_capacity
            self.repository._create_entity(
                connection, merged_id, "zone", merged_status, merged_data, actor.user_id
            )

            for zone in zones:
                self.repository._save_entity(
                    connection, zone["id"], "merged", dict(zone["data"])
                )

            source_ids = {zone_a["id"], zone_b["id"]}

            # Gates: replace either source zone with the merged zone (dedup).
            touched_gates = {}
            for zone in zones:
                for gate in self.repository._find_gates_for_zone(connection, zone["id"]):
                    touched_gates.setdefault(gate["id"], gate)
            for gate in touched_gates.values():
                old_ids = list(gate["data"].get("zone_ids") or [])
                updated_ids = [zid for zid in old_ids if zid not in source_ids]
                if merged_id not in updated_ids:
                    updated_ids.append(merged_id)
                gate_data = dict(gate["data"], zone_ids=updated_ids)
                self.repository._save_entity(connection, gate["id"], gate["status"], gate_data)
                self.repository._append_audit(
                    connection, gate["id"], actor.user_id, actor.role, "reassign",
                    gate["status"], gate["status"],
                    {"from_zones": sorted(source_ids), "to_zone": merged_id},
                )

            for kind in ("post", "medical_point"):
                seen = set()
                for zone in zones:
                    for entity in self.repository._find_entities(
                        connection, kind, "zone_id", zone["id"]
                    ):
                        if entity["id"] in seen:
                            continue
                        seen.add(entity["id"])
                        entity_data = dict(entity["data"], zone_id=merged_id)
                        self.repository._save_entity(
                            connection, entity["id"], entity["status"], entity_data
                        )
                        self.repository._append_audit(
                            connection, entity["id"], actor.user_id, actor.role, "reassign",
                            entity["status"], entity["status"],
                            {"from_zones": sorted(source_ids), "to_zone": merged_id},
                        )

            seen_incidents = set()
            for zone in zones:
                for incident in self.repository._find_entities(
                    connection, "incident", "zone_id", zone["id"]
                ):
                    if incident["id"] in seen_incidents:
                        continue
                    seen_incidents.add(incident["id"])
                    if is_pending_incident(incident):
                        incident_data = dict(incident["data"], zone_id=merged_id)
                        self.repository._save_entity(
                            connection, incident["id"], incident["status"], incident_data
                        )
                        self.repository._append_audit(
                            connection, incident["id"], actor.user_id, actor.role, "reassign",
                            incident["status"], incident["status"],
                            {"from_zones": sorted(source_ids), "to_zone": merged_id},
                        )
                    # resolved incidents keep their original zone for history

            seen_tasks = set()
            for zone in zones:
                for task in self.repository._find_entities(
                    connection, "task", "zone_id", zone["id"]
                ):
                    if task["id"] in seen_tasks:
                        continue
                    seen_tasks.add(task["id"])
                    if is_unfinished_task(task):
                        task_data = dict(task["data"], zone_id=merged_id)
                        self.repository._save_entity(
                            connection, task["id"], task["status"], task_data
                        )
                        self.repository._append_audit(
                            connection, task["id"], actor.user_id, actor.role, "reassign",
                            task["status"], task["status"],
                            {"from_zones": sorted(source_ids), "to_zone": merged_id},
                        )
                    # finished tasks keep their original zone for history

            venue_data = dict(venue_row["data"])
            venue_data["structure_version"] = self._structure_version(venue_row) + 1
            self.repository._save_entity(connection, venue["id"], venue["status"], venue_data)

            self.repository._append_audit(
                connection, merged_id, actor.user_id, actor.role, "create",
                None, merged_status, {"kind": "zone", "from_merge": sorted(source_ids)},
            )
            for zone in zones:
                self.repository._append_audit(
                    connection, zone["id"], actor.user_id, actor.role, "merge",
                    zone["status"], "merged",
                    {"merged_zone": merged_id, "partner": (
                        zone_b["id"] if zone["id"] == zone_a["id"] else zone_a["id"]
                    )},
                )
            self.repository._append_audit(
                connection, venue["id"], actor.user_id, actor.role, "merge",
                venue["status"], venue["status"],
                {"source_zones": sorted(source_ids), "merged_zone": merged_id,
                 "structure_version": venue_data["structure_version"]},
            )
            return {
                "zone_a": self.repository._get_entity(connection, zone_a["id"]),
                "zone_b": self.repository._get_entity(connection, zone_b["id"]),
                "merged": self.repository._get_entity(connection, merged_id),
            }

        return self.repository.run_in_transaction(work)
