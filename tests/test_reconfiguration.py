import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ReconfigurationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "reconfig.db"),
            RuleEngine(),
        )
        self.admin = Actor("boss", "admin")
        self.operator = Actor("op", "operator")
        self.viewer = Actor("look", "viewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _venue(self, name="Venue"):
        return self.service.create(self.admin, "venue", {"name": name, "address": "addr"})

    def _zone(self, venue, name, capacity):
        return self.service.create(
            self.admin, "zone", {"venue_id": venue["id"], "name": name, "capacity": capacity}
        )

    def _open_zone(self, zone):
        self.service.transition(self.operator, zone["id"], "open", {"checklist": "clear"})

    def _gate(self, venue, name, zone_ids):
        gate = self.service.create(
            self.admin, "gate",
            {"venue_id": venue["id"], "name": name, "zone_ids": zone_ids},
        )
        self.service.transition(self.operator, gate["id"], "open", {"operator_id": "op"})
        return gate

    def _admit(self, zone, gate, count):
        return self.service.transition(
            self.operator, zone["id"], "admit",
            {"gate_id": gate["id"], "count": count, "admitted_at": "t"},
        )

    def _resolved_incident(self, venue, zone, ref):
        incident = self.service.create(
            self.operator, "incident",
            {"venue_id": venue["id"], "zone_id": zone["id"], "source_ref": ref,
             "incident_type": "crowd", "severity": "low", "reported_at": "t"},
        )
        incident = self.service.transition(self.admin, incident["id"], "triage", {"priority": "low"})
        incident = self.service.transition(self.admin, incident["id"], "dispatch", {"commander_id": "c"})
        return self.service.transition(self.admin, incident["id"], "resolve", {"resolution": "ok"})

    def _task(self, venue, incident, zone, team):
        return self.service.create(
            self.admin, "task",
            {"incident_id": incident["id"], "venue_id": venue["id"],
             "zone_id": zone["id"], "team_id": team, "task_type": "crowd"},
        )

    # -- split --------------------------------------------------------------

    def test_split_conservation_and_reattachment(self):
        venue = self._venue()
        zone = self._zone(venue, "North", 1000)
        self._open_zone(zone)
        gate = self._gate(venue, "Gate A", [zone["id"]])
        self._admit(zone, gate, 400)

        post = self.service.create(
            self.admin, "post",
            {"venue_id": venue["id"], "zone_id": zone["id"], "staff_count": 4, "duty": "crowd"},
        )

        # A resolved (ended) incident keeps its original zone.
        ended_incident = self._resolved_incident(venue, zone, "radio-ended")
        # An unfinished task (on the ended incident) must be re-attached.
        active_task = self._task(venue, ended_incident, zone, "team-1")
        self.service.transition(self.admin, active_task["id"], "assign", {"assigned_at": "t"})
        # A finished task keeps its original zone.
        finished_task = self._task(venue, ended_incident, zone, "team-2")
        self.service.transition(self.admin, finished_task["id"], "assign", {"assigned_at": "t"})
        self.service.transition(self.admin, finished_task["id"], "cancel", {"reason": "done"})

        result = self.service.transition(
            self.admin, zone["id"], "split",
            {
                "name_a": "North East", "name_b": "North West",
                "capacity_a": 600, "capacity_b": 400,
                "occupancy_a": 250, "occupancy_b": 150,
                "task_assignments": {active_task["id"]: "a"},
            },
        )
        za, zb = result["zone_a"], result["zone_b"]

        # Conservation.
        self.assertEqual(za["data"]["capacity"] + zb["data"]["capacity"], 1000)
        self.assertEqual(za["data"]["current_occupancy"] + zb["data"]["current_occupancy"], 400)
        self.assertEqual(za["data"]["current_occupancy"], 250)
        self.assertEqual(zb["data"]["current_occupancy"], 150)

        # Source zone preserved as history, marked terminal.
        source = self.service.get(zone["id"])
        self.assertEqual(source["status"], "split")
        self.assertEqual(source["data"]["capacity"], 1000)
        self.assertEqual(source["data"]["current_occupancy"], 400)

        # Gate re-attached to both new zones, no longer references the source.
        gate_after = self.service.get(gate["id"])
        self.assertNotIn(zone["id"], gate_after["data"]["zone_ids"])
        self.assertIn(za["id"], gate_after["data"]["zone_ids"])
        self.assertIn(zb["id"], gate_after["data"]["zone_ids"])

        # Post re-attached to zone a.
        post_after = self.service.get(post["id"])
        self.assertEqual(post_after["data"]["zone_id"], za["id"])

        # Unfinished task re-attached per assignment; finished task keeps zone.
        self.assertEqual(self.service.get(active_task["id"])["data"]["zone_id"], za["id"])
        self.assertEqual(self.service.get(finished_task["id"])["data"]["zone_id"], zone["id"])

        # Ended incident keeps its original zone.
        self.assertEqual(self.service.get(ended_incident["id"])["data"]["zone_id"], zone["id"])

        # Structure version bumped.
        venue_after = self.service.get(venue["id"])
        self.assertEqual(venue_after["data"]["structure_version"], 2)

    def test_split_rejects_evacuating_zone(self):
        venue = self._venue()
        zone = self._zone(venue, "North", 1000)
        self._open_zone(zone)
        self.service.transition(self.admin, zone["id"], "evacuate", {"reason": "drill"})
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, zone["id"], "split",
                {"name_a": "A", "name_b": "B", "capacity_a": 500, "capacity_b": 500,
                 "occupancy_a": 0, "occupancy_b": 0},
            )

    def test_split_rejects_pending_incident(self):
        venue = self._venue()
        zone = self._zone(venue, "North", 1000)
        self._open_zone(zone)
        self.service.create(
            self.operator, "incident",
            {"venue_id": venue["id"], "zone_id": zone["id"], "source_ref": "r1",
             "incident_type": "crowd", "severity": "low", "reported_at": "t"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, zone["id"], "split",
                {"name_a": "A", "name_b": "B", "capacity_a": 500, "capacity_b": 500,
                 "occupancy_a": 0, "occupancy_b": 0},
            )

    def test_split_rejects_conservation_mismatch(self):
        venue = self._venue()
        zone = self._zone(venue, "North", 1000)
        self._open_zone(zone)
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, zone["id"], "split",
                {"name_a": "A", "name_b": "B", "capacity_a": 700, "capacity_b": 400,
                 "occupancy_a": 0, "occupancy_b": 0},
            )

    def test_split_requires_task_assignment(self):
        venue = self._venue()
        zone = self._zone(venue, "North", 1000)
        self._open_zone(zone)
        ended = self._resolved_incident(venue, zone, "r1")
        task = self._task(venue, ended, zone, "team-1")
        self.service.transition(self.admin, task["id"], "assign", {"assigned_at": "t"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, zone["id"], "split",
                {"name_a": "A", "name_b": "B", "capacity_a": 500, "capacity_b": 500,
                 "occupancy_a": 0, "occupancy_b": 0},
            )

    def test_split_requires_admin(self):
        venue = self._venue()
        zone = self._zone(venue, "North", 1000)
        self._open_zone(zone)
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.viewer, zone["id"], "split",
                {"name_a": "A", "name_b": "B", "capacity_a": 500, "capacity_b": 500,
                 "occupancy_a": 0, "occupancy_b": 0},
            )

    # -- merge --------------------------------------------------------------

    def test_merge_conservation_and_reattachment(self):
        venue = self._venue()
        east = self._zone(venue, "East", 400)
        west = self._zone(venue, "West", 600)
        self._open_zone(east)
        self._open_zone(west)
        gate_e = self._gate(venue, "Gate E", [east["id"]])
        gate_w = self._gate(venue, "Gate W", [west["id"]])
        shared = self._gate(venue, "Gate Shared", [east["id"], west["id"]])
        self._admit(east, gate_e, 100)
        self._admit(west, gate_w, 200)

        post_e = self.service.create(
            self.admin, "post",
            {"venue_id": venue["id"], "zone_id": east["id"], "staff_count": 3, "duty": "crowd"},
        )
        ended_e = self._resolved_incident(venue, east, "r-e")
        active_task = self._task(venue, ended_e, east, "team-1")
        self.service.transition(self.admin, active_task["id"], "assign", {"assigned_at": "t"})
        ended_w = self._resolved_incident(venue, west, "r-w")
        finished_task = self._task(venue, ended_w, west, "team-2")
        self.service.transition(self.admin, finished_task["id"], "assign", {"assigned_at": "t"})
        self.service.transition(self.admin, finished_task["id"], "cancel", {"reason": "done"})

        result = self.service.transition(
            self.admin, venue["id"], "merge",
            {"zone_ids": [east["id"], west["id"]], "name": "Grand Stand"},
        )
        merged = result["merged"]

        self.assertEqual(merged["data"]["capacity"], 1000)
        self.assertEqual(merged["data"]["current_occupancy"], 300)
        self.assertEqual(merged["status"], "open")

        # Sources preserved as history.
        self.assertEqual(self.service.get(east["id"])["status"], "merged")
        self.assertEqual(self.service.get(west["id"])["status"], "merged")

        # Gates re-attached to merged zone.
        for gate in (gate_e, gate_w, shared):
            after = self.service.get(gate["id"])
            self.assertNotIn(east["id"], after["data"]["zone_ids"])
            self.assertNotIn(west["id"], after["data"]["zone_ids"])
            self.assertIn(merged["id"], after["data"]["zone_ids"])
        # Shared gate deduped to a single merged reference.
        self.assertEqual(self.service.get(shared["id"])["data"]["zone_ids"].count(merged["id"]), 1)

        # Post and unfinished task re-attached; finished task and ended incidents keep.
        self.assertEqual(self.service.get(post_e["id"])["data"]["zone_id"], merged["id"])
        self.assertEqual(self.service.get(active_task["id"])["data"]["zone_id"], merged["id"])
        self.assertEqual(self.service.get(finished_task["id"])["data"]["zone_id"], west["id"])
        self.assertEqual(self.service.get(ended_e["id"])["data"]["zone_id"], east["id"])
        self.assertEqual(self.service.get(ended_w["id"])["data"]["zone_id"], west["id"])

        self.assertEqual(self.service.get(venue["id"])["data"]["structure_version"], 2)

    def test_merge_rejects_different_venues(self):
        venue1 = self._venue("V1")
        venue2 = self._venue("V2")
        z1 = self._zone(venue1, "Z1", 100)
        z2 = self._zone(venue2, "Z2", 100)
        self._open_zone(z1)
        self._open_zone(z2)
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, venue1["id"], "merge",
                {"zone_ids": [z1["id"], z2["id"]], "name": "X"},
            )

    def test_merge_rejects_pending_incident(self):
        venue = self._venue()
        z1 = self._zone(venue, "Z1", 100)
        z2 = self._zone(venue, "Z2", 100)
        self._open_zone(z1)
        self._open_zone(z2)
        self.service.create(
            self.operator, "incident",
            {"venue_id": venue["id"], "zone_id": z1["id"], "source_ref": "r1",
             "incident_type": "crowd", "severity": "low", "reported_at": "t"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, venue["id"], "merge",
                {"zone_ids": [z1["id"], z2["id"]], "name": "X"},
            )

    # -- concurrency --------------------------------------------------------

    def test_second_admin_sees_new_structure(self):
        venue = self._venue()
        z1 = self._zone(venue, "Z1", 100)
        z2 = self._zone(venue, "Z2", 100)
        z3 = self._zone(venue, "Z3", 100)
        self._open_zone(z1)
        self._open_zone(z2)
        self._open_zone(z3)

        # Both admins observed structure version 1.
        first = self.service.transition(
            self.admin, z1["id"], "split",
            {"name_a": "A", "name_b": "B", "capacity_a": 50, "capacity_b": 50,
             "occupancy_a": 0, "occupancy_b": 0},
            expected_version=1,
        )
        self.assertEqual(first["source"]["status"], "split")

        # Second admin's stale expectation must conflict, not overwrite.
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, venue["id"], "merge",
                {"zone_ids": [z2["id"], z3["id"]], "name": "M"},
                expected_version=1,
            )

        # Re-reading reveals the new structure version.
        self.assertEqual(self.service.get(venue["id"])["data"]["structure_version"], 2)
        # The merge can proceed against the new version.
        merged = self.service.transition(
            self.admin, venue["id"], "merge",
            {"zone_ids": [z2["id"], z3["id"]], "name": "M"},
            expected_version=2,
        )
        self.assertEqual(merged["merged"]["data"]["capacity"], 200)

    # -- rollback -----------------------------------------------------------

    def test_failure_rolls_back_whole_operation(self):
        venue = self._venue()
        zone = self._zone(venue, "North", 1000)
        self._open_zone(zone)
        gate = self._gate(venue, "Gate A", [zone["id"]])
        self._admit(zone, gate, 100)

        # Force a write failure mid-transaction.
        with mock.patch.object(
            self.service.repository, "_append_audit",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.transition(
                    self.admin, zone["id"], "split",
                    {"name_a": "A", "name_b": "B", "capacity_a": 500, "capacity_b": 500,
                     "occupancy_a": 50, "occupancy_b": 50},
                )

        # Nothing was written: source untouched, no new zones, version unchanged.
        source = self.service.get(zone["id"])
        self.assertEqual(source["status"], "open")
        self.assertEqual(source["data"]["capacity"], 1000)
        self.assertEqual(source["data"]["current_occupancy"], 100)
        self.assertEqual(self.service.get(venue["id"])["data"]["structure_version"], 1)
        zones = self.service.list("zone")
        self.assertEqual(len(zones), 1)
        self.assertEqual(zones[0]["id"], zone["id"])

    # -- history ------------------------------------------------------------

    def test_history_queryable_by_original_id(self):
        venue = self._venue()
        zone = self._zone(venue, "North", 1000)
        self._open_zone(zone)
        self.service.transition(
            self.admin, zone["id"], "split",
            {"name_a": "A", "name_b": "B", "capacity_a": 500, "capacity_b": 500,
             "occupancy_a": 0, "occupancy_b": 0},
        )
        # Original zone still fetchable by its original id.
        history = self.service.get(zone["id"])
        self.assertEqual(history["status"], "split")
        # Its audit trail is intact.
        trail = self.service.audit_log(zone["id"])
        actions = [entry["action"] for entry in trail]
        self.assertIn("create", actions)
        self.assertIn("split", actions)


if __name__ == "__main__":
    unittest.main()
