import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ShiftTakeoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.alice = Actor("alice", "dispatcher")
        self.bob = Actor("bob", "dispatcher")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, location="Tower A"):
        return self.service.create(
            self.admin, "equipment",
            {"asset_no": "E-1", "equipment_type": "elevator", "location": location, "inspection_interval_days": 365},
        )

    def alarm(self, equipment, code="DOOR-JAM"):
        return self.service.create(
            self.admin, "alarm",
            {"equipment_id": equipment["id"], "code": code, "occurred_at": "2026-10-02T10:00:00Z"},
        )

    def test_dispatcher_starts_shift_and_takes_over_alarm(self):
        equipment = self.equipment()
        alarm = self.alarm(equipment)
        shift = self.service.start_shift(self.alice, {"area": "Tower A"})
        self.assertEqual(shift["status"], "on_duty")
        takeover = self.service.take_over_alarm(self.alice, alarm["id"])
        self.assertEqual(takeover["status"], "active")
        self.assertEqual(takeover["data"]["shift_id"], shift["id"])
        # taking over the same alarm again is idempotent
        again = self.service.take_over_alarm(self.alice, alarm["id"])
        self.assertEqual(again["id"], takeover["id"])

    def test_alarm_can_only_have_one_active_takeover(self):
        equipment = self.equipment()
        alarm = self.alarm(equipment)
        self.service.start_shift(self.alice, {"area": "Tower A"})
        self.service.start_shift(self.bob, {"area": "Tower A"})
        self.service.take_over_alarm(self.alice, alarm["id"])
        with self.assertRaises(ConflictError):
            self.service.take_over_alarm(self.bob, alarm["id"])

    def test_concurrent_takeover_first_wins_second_conflicts(self):
        equipment = self.equipment()
        alarm = self.alarm(equipment)
        self.service.start_shift(self.alice, {"area": "Tower A"})
        self.service.start_shift(self.bob, {"area": "Tower A"})
        # Simulate a race: both dispatchers submit takeover at the same time.
        # The partial unique index on (alarm_id where active) must let exactly one win.
        results = []
        errors = []
        for actor in (self.alice, self.bob):
            try:
                results.append(self.service.take_over_alarm(actor, alarm["id"]))
            except ConflictError as exc:
                errors.append(str(exc))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("already taken over", errors[0])

    def test_takeover_requires_on_duty_shift(self):
        equipment = self.equipment()
        alarm = self.alarm(equipment)
        with self.assertRaises(ValidationError):
            self.service.take_over_alarm(self.alice, alarm["id"])

    def test_takeover_rejects_alarm_outside_area(self):
        equipment = self.equipment(location="Tower B")
        alarm = self.alarm(equipment)
        self.service.start_shift(self.alice, {"area": "Tower A"})
        with self.assertRaises(ValidationError):
            self.service.take_over_alarm(self.alice, alarm["id"])

    def test_handover_transfers_takeover_and_ends_previous_shift(self):
        equipment = self.equipment()
        alarm = self.alarm(equipment)
        alice_shift = self.service.start_shift(self.alice, {"area": "Tower A"})
        bob_shift = self.service.start_shift(self.bob, {"area": "Tower A"})
        self.service.take_over_alarm(self.alice, alarm["id"])
        handover = self.service.initiate_handover(self.alice, alice_shift["id"], bob_shift["id"])
        self.assertEqual(handover["status"], "pending")
        self.assertEqual(handover["data"]["unclosed_count"], 1)
        # Bob takes over the alarm -> Alice's takeover invalid, Bob's active
        takeover = self.service.take_over_alarm(self.bob, alarm["id"])
        self.assertEqual(takeover["data"]["shift_id"], bob_shift["id"])
        # handover auto-completes once all unclosed alarms are taken over
        updated_handover = self.service.get(handover["id"])
        self.assertEqual(updated_handover["status"], "completed")
        updated_alice_shift = self.service.get(alice_shift["id"])
        self.assertEqual(updated_alice_shift["status"], "ended")

    def test_handover_cannot_complete_before_all_taken_over(self):
        equipment = self.equipment()
        alarm = self.alarm(equipment)
        alice_shift = self.service.start_shift(self.alice, {"area": "Tower A"})
        bob_shift = self.service.start_shift(self.bob, {"area": "Tower A"})
        self.service.take_over_alarm(self.alice, alarm["id"])
        handover = self.service.initiate_handover(self.alice, alice_shift["id"], bob_shift["id"])
        with self.assertRaises(ValidationError):
            self.service.complete_handover(self.bob, handover["id"])

    def test_take_over_all_covers_unclosed_alarms(self):
        equipment = self.equipment()
        alarm1 = self.alarm(equipment, code="A1")
        alarm2 = self.alarm(equipment, code="A2")
        self.service.start_shift(self.alice, {"area": "Tower A"})
        taken = self.service.take_over_all(self.alice)
        self.assertEqual(len(taken), 2)
        ids = {t["data"]["alarm_id"] for t in taken}
        self.assertEqual(ids, {alarm1["id"], alarm2["id"]})

    def test_resolve_alarm_invalidates_takeover(self):
        equipment = self.equipment()
        alarm = self.alarm(equipment)
        self.service.start_shift(self.alice, {"area": "Tower A"})
        takeover = self.service.take_over_alarm(self.alice, alarm["id"])
        # dispatch then resolve the alarm
        self.service.transition(self.alice, alarm["id"], "dispatch", {"team": "Alpha"})
        self.service.transition(self.alice, alarm["id"], "resolve", {"resolution": "passenger safe"})
        updated_takeover = self.service.get(takeover["id"])
        self.assertEqual(updated_takeover["status"], "invalid")

    def test_false_alarm_invalidates_takeover(self):
        equipment = self.equipment()
        alarm = self.alarm(equipment)
        self.service.start_shift(self.alice, {"area": "Tower A"})
        takeover = self.service.take_over_alarm(self.alice, alarm["id"])
        self.service.transition(self.alice, alarm["id"], "mark_false", {})
        updated_takeover = self.service.get(takeover["id"])
        self.assertEqual(updated_takeover["status"], "invalid")

    def test_handover_unclosed_count_recalculated_on_resolve(self):
        equipment = self.equipment()
        alarm1 = self.alarm(equipment, code="A1")
        alarm2 = self.alarm(equipment, code="A2")
        alice_shift = self.service.start_shift(self.alice, {"area": "Tower A"})
        bob_shift = self.service.start_shift(self.bob, {"area": "Tower A"})
        self.service.take_over_all(self.alice)
        handover = self.service.initiate_handover(self.alice, alice_shift["id"], bob_shift["id"])
        self.assertEqual(handover["data"]["unclosed_count"], 2)
        # resolve one alarm -> unclosed count drops to 1
        self.service.transition(self.alice, alarm1["id"], "dispatch", {"team": "Alpha"})
        self.service.transition(self.alice, alarm1["id"], "resolve", {"resolution": "ok"})
        updated_handover = self.service.get(handover["id"])
        self.assertEqual(updated_handover["data"]["unclosed_count"], 1)

    def test_end_shift_escalates_untaken_alarms(self):
        equipment = self.equipment()
        alarm = self.alarm(equipment)
        shift = self.service.start_shift(self.alice, {"area": "Tower A"})
        # end shift without taking over the alarm
        self.service.end_shift(self.alice, shift["id"])
        escalations = self.service.list(kind="escalation")
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0]["data"]["alarm_id"], alarm["id"])
        self.assertEqual(escalations[0]["data"]["escalated_to"], "supervisor")
        self.assertEqual(escalations[0]["status"], "pending")

    def test_end_shift_with_taken_alarms_no_escalation(self):
        equipment = self.equipment()
        alarm = self.alarm(equipment)
        shift = self.service.start_shift(self.alice, {"area": "Tower A"})
        self.service.take_over_alarm(self.alice, alarm["id"])
        self.service.end_shift(self.alice, shift["id"])
        self.assertEqual(len(self.service.list(kind="escalation")), 0)

    def test_cannot_end_shift_twice(self):
        equipment = self.equipment()
        self.service.start_shift(self.alice, {"area": "Tower A"})
        shift = self.service.list(kind="shift")[0]
        self.service.end_shift(self.alice, shift["id"])
        with self.assertRaises(ValidationError):
            self.service.end_shift(self.alice, shift["id"])


if __name__ == "__main__":
    unittest.main()
