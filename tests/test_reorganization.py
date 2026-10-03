import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository, SQLiteSession
from src.rules import RuleEngine
from src.service import DomainService


class ReorganizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "reorg.db"),
            RuleEngine(),
        )
        self.admin = Actor("admin", "admin")
        self.coordinator = Actor("coordinator", "coordinator")
        self.supervisor = Actor("supervisor", "supervisor")
        self.operator = Actor("operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _venue(self, name="Grand Hall"):
        return self.service.create(
            self.coordinator,
            "venue",
            {"name": name, "address": "1 Stadium Road"},
        )

    def _zone(self, venue, name, capacity, occupancy=0):
        zone = self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": name, "capacity": capacity},
        )
        if occupancy:
            gate = self.service.create(
                self.coordinator,
                "gate",
                {"venue_id": venue["id"], "name": name + " Gate", "zone_ids": [zone["id"]]},
            )
            self.service.transition(self.operator, gate["id"], "open", {"operator_id": "operator"})
            self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})
            zone = self.service.transition(
                self.operator,
                zone["id"],
                "admit",
                {"gate_id": gate["id"], "count": occupancy, "admitted_at": "t0"},
            )
        return zone

    def _gate(self, venue, name, zone_ids):
        return self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": name, "zone_ids": zone_ids},
        )

    def _resolved_incident(self, venue, zone, source_ref):
        incident = self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "source_ref": source_ref,
                "incident_type": "medical",
                "severity": "low",
                "reported_at": "t0",
            },
        )
        incident = self.service.transition(
            self.supervisor, incident["id"], "triage", {"priority": "medical"}
        )
        incident = self.service.transition(
            self.coordinator, incident["id"], "dispatch", {"commander_id": "commander"}
        )
        return self.service.transition(
            self.coordinator, incident["id"], "resolve", {"resolution": "done"}
        )

    def _task(self, venue, zone, incident, team, complete=False):
        task = self.service.create(
            self.supervisor,
            "task",
            {
                "incident_id": incident["id"],
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "team_id": team,
                "task_type": "medical",
            },
        )
        if not complete:
            return task
        task = self.service.transition(self.coordinator, task["id"], "assign", {"assigned_at": "t1"})
        task = self.service.transition(self.operator, task["id"], "acknowledge", {"acknowledged_at": "t2"})
        task = self.service.transition(self.operator, task["id"], "arrive", {"arrived_at": "t3"})
        return self.service.transition(
            self.operator,
            task["id"],
            "complete",
            {"completed_at": "t4", "outcome": "done"},
        )

    def test_split_preserves_totals_and_rezones_live_links(self):
        venue = self._venue()
        zone = self._zone(venue, "North Stand", 100, 60)
        gate = self._gate(venue, "North Gate", [zone["id"]])
        incident = self._resolved_incident(venue, zone, "radio-closed")
        active_task = self._task(venue, zone, incident, "team-active")
        completed_task = self._task(venue, zone, incident, "team-done", complete=True)

        result = self.service.split_zone(
            self.admin,
            {
                "zone_id": zone["id"],
                "expected_version": zone["version"],
                "new_zones": [
                    {"id": "north-a", "name": "North A", "capacity": 60, "current_occupancy": 35},
                    {"id": "north-b", "name": "North B", "capacity": 40, "current_occupancy": 25},
                ],
                "task_targets": {active_task["id"]: "north-b"},
            },
        )

        self.assertEqual(result["source_zone"]["status"], "superseded")
        self.assertEqual(result["source_zone"]["data"]["superseded_by_zone_ids"], ["north-a", "north-b"])
        self.assertEqual([item["data"]["capacity"] for item in result["zones"]], [60, 40])
        self.assertEqual(
            [item["data"]["current_occupancy"] for item in result["zones"]], [35, 25]
        )
        self.assertEqual(len(result["gates"]), 2)
        self.assertTrue(all(item["data"]["zone_ids"] == ["north-a", "north-b"] for item in result["gates"]))
        self.assertIn(gate["id"], {item["id"] for item in result["gates"]})
        self.assertEqual(result["tasks"][0]["data"]["zone_id"], "north-b")

        stored_gate = self.service.get(gate["id"])
        stored_active_task = self.service.get(active_task["id"])
        stored_completed_task = self.service.get(completed_task["id"])
        stored_incident = self.service.get(incident["id"])
        self.assertEqual(stored_gate["data"]["zone_ids"], ["north-a", "north-b"])
        self.assertEqual(stored_active_task["data"]["zone_id"], "north-b")
        self.assertEqual(stored_completed_task["data"]["zone_id"], zone["id"])
        self.assertEqual(stored_incident["data"]["zone_id"], zone["id"])

        historical = self.service.get(zone["id"])
        self.assertEqual(historical["id"], zone["id"])
        self.assertEqual(historical["data"]["current_occupancy"], 60)
        audit_actions = {item["action"] for item in self.service.audit_log()}
        self.assertIn("split", audit_actions)
        self.assertIn("split_created", audit_actions)
        self.assertIn("rezone", audit_actions)

    def test_merge_preserves_totals_and_rezones_live_links(self):
        venue = self._venue()
        left = self._zone(venue, "Left", 60, 35)
        right = self._zone(venue, "Right", 40, 25)
        left_gate = self._gate(venue, "Left Gate", [left["id"]])
        shared_gate = self._gate(venue, "Shared Gate", [left["id"], right["id"]])
        left_incident = self._resolved_incident(venue, left, "left-closed")
        right_incident = self._resolved_incident(venue, right, "right-closed")
        left_task = self._task(venue, left, left_incident, "team-left")
        right_task = self._task(venue, right, right_incident, "team-right")

        result = self.service.merge_zones(
            self.admin,
            {
                "zone_ids": [left["id"], right["id"]],
                "expected_versions": {left["id"]: left["version"], right["id"]: right["version"]},
                "target_id": "combined",
                "name": "Combined",
            },
        )

        self.assertEqual(result["zone"]["data"]["capacity"], 100)
        self.assertEqual(result["zone"]["data"]["current_occupancy"], 60)
        self.assertEqual(result["zone"]["data"]["merged_from_zone_ids"], [left["id"], right["id"]])
        self.assertEqual([zone["status"] for zone in result["source_zones"]], ["superseded", "superseded"])
        gates = {gate["id"]: gate["data"]["zone_ids"] for gate in result["gates"]}
        self.assertEqual(gates[left_gate["id"]], ["combined"])
        self.assertEqual(gates[shared_gate["id"]], ["combined"])
        moved_tasks = {task["id"]: task["data"]["zone_id"] for task in result["tasks"]}
        self.assertEqual(moved_tasks, {left_task["id"]: "combined", right_task["id"]: "combined"})
        self.assertEqual(self.service.get(left_incident["id"])["data"]["zone_id"], left["id"])
        self.assertEqual(self.service.get(right_incident["id"])["data"]["zone_id"], right["id"])
        self.assertEqual(self.service.get(left["id"])["data"]["superseded_by_zone_ids"], ["combined"])

    def test_split_rejects_evacuating_zone(self):
        venue = self._venue()
        zone = self._zone(venue, "Evacuating", 100, 10)
        self.service.transition(
            self.supervisor, zone["id"], "evacuate", {"reason": "fire drill"}
        )
        with self.assertRaises(ConflictError):
            self.service.split_zone(
                self.admin,
                {
                    "zone_id": zone["id"],
                    "new_zones": [
                        {"name": "A", "capacity": 50},
                        {"name": "B", "capacity": 50},
                    ],
                },
            )

    def test_merge_rejects_pending_incident(self):
        venue = self._venue()
        left = self._zone(venue, "Left", 50)
        right = self._zone(venue, "Right", 50)
        self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": venue["id"],
                "zone_id": left["id"],
                "source_ref": "radio-open",
                "incident_type": "security",
                "severity": "medium",
                "reported_at": "t0",
            },
        )
        with self.assertRaises(ConflictError):
            self.service.merge_zones(
                self.admin,
                {"zone_ids": [left["id"], right["id"]], "name": "Combined"},
            )

    def test_split_capacity_must_balance(self):
        venue = self._venue()
        zone = self._zone(venue, "Balanced", 100, 40)
        with self.assertRaises(ValidationError):
            self.service.split_zone(
                self.admin,
                {
                    "zone_id": zone["id"],
                    "new_zones": [
                        {"name": "A", "capacity": 50, "current_occupancy": 20},
                        {"name": "B", "capacity": 40, "current_occupancy": 20},
                    ],
                },
            )

    def test_merge_requires_same_venue_and_admin_role(self):
        first_venue = self._venue("First")
        second_venue = self._venue("Second")
        left = self._zone(first_venue, "Left", 50)
        right = self._zone(second_venue, "Right", 50)
        with self.assertRaises(ValidationError):
            self.service.merge_zones(
                self.admin,
                {"zone_ids": [left["id"], right["id"]], "name": "Combined"},
            )
        with self.assertRaises(PermissionDenied):
            self.service.split_zone(
                self.coordinator,
                {
                    "zone_id": left["id"],
                    "new_zones": [
                        {"name": "A", "capacity": 25},
                        {"name": "B", "capacity": 25},
                    ],
                },
            )

    def test_concurrent_changes_first_wins_and_later_sees_new_structure(self):
        venue = self._venue()
        zone = self._zone(venue, "Concurrent", 100, 60)
        gate = self._gate(venue, "Concurrent Gate", [zone["id"]])
        errors = []
        first_result = {}
        barrier = threading.Barrier(2)

        def split(suffix):
            barrier.wait()
            try:
                result = self.service.split_zone(
                    self.admin,
                    {
                        "zone_id": zone["id"],
                        "new_zones": [
                            {
                                "id": "winner-a",
                                "name": "Winner A",
                                "capacity": 60,
                                "current_occupancy": 30,
                            },
                            {
                                "id": "winner-b",
                                "name": "Winner B",
                                "capacity": 40,
                                "current_occupancy": 30,
                            },
                        ],
                    }
                    if suffix == "first"
                    else {
                        "zone_id": zone["id"],
                        "new_zones": [
                            {"id": "winner-a", "name": "Winner A", "capacity": 60, "current_occupancy": 30},
                            {"id": "winner-b", "name": "Winner B", "capacity": 40, "current_occupancy": 30},
                        ],
                    },
                )
                first_result.update(result)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=split, args=("first",)), threading.Thread(target=split, args=("second",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        self.assertEqual(len(first_result["zones"]), 2)
        self.assertEqual(self.service.get(gate["id"])["data"]["zone_ids"], ["winner-a", "winner-b"])

        # The losing request can read the committed replacement structure and continue on it.
        merged = self.service.merge_zones(
            self.admin,
            {
                "zone_ids": ["winner-a", "winner-b"],
                "target_id": "winner-merged",
                "name": "Winner Merged",
            },
        )
        self.assertEqual(merged["zone"]["data"]["capacity"], 100)
        self.assertEqual(merged["zone"]["data"]["current_occupancy"], 60)

    def test_failed_write_rolls_back_entire_split(self):
        venue = self._venue()
        zone = self._zone(venue, "Atomic", 100, 20)
        gate = self._gate(venue, "Atomic Gate", [zone["id"]])
        original_create = SQLiteSession.create_entity

        def failing_create(session, entity_id, kind, status, data, actor_id):
            if entity_id == "atomic-b":
                raise RuntimeError("simulated write failure")
            return original_create(session, entity_id, kind, status, data, actor_id)

        with patch.object(SQLiteSession, "create_entity", autospec=True, side_effect=failing_create):
            with self.assertRaises(RuntimeError):
                self.service.split_zone(
                    self.admin,
                    {
                        "zone_id": zone["id"],
                        "new_zones": [
                            {"id": "atomic-a", "name": "Atomic A", "capacity": 50, "current_occupancy": 10},
                            {"id": "atomic-b", "name": "Atomic B", "capacity": 50, "current_occupancy": 10},
                        ],
                    },
                )

        self.assertEqual(self.service.get(zone["id"])["status"], "open")
        self.assertIsNone(self.service.repository.get_entity("atomic-a"))
        self.assertIsNone(self.service.repository.get_entity("atomic-b"))
        self.assertEqual(self.service.get(gate["id"])["data"]["zone_ids"], [zone["id"]])
        self.assertNotIn("split", {item["action"] for item in self.service.audit_log(zone["id"])})


if __name__ == "__main__":
    unittest.main()
