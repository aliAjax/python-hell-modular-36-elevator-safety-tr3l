import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ShiftTakeoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatch_a = Actor("dispatcher-a", "dispatcher")
        self.dispatch_b = Actor("dispatcher-b", "dispatcher")

    def tearDown(self):
        self.tmp.cleanup()

    def _equipment_and_alarm(self, asset_no="E-100", code="TRAPPED"):
        equipment = self.service.create(self.admin, "equipment", {
            "asset_no": asset_no,
            "equipment_type": "elevator",
            "location": "Tower A",
            "area": "Tower A",
            "inspection_interval_days": 365,
        })
        alarm = self.service.create(self.dispatch_a, "alarm", {
            "equipment_id": equipment["id"],
            "code": code,
            "occurred_at": "2026-10-02T08:00:00Z",
        })
        return equipment, alarm

    def _shift(self, actor, dispatcher_id, shift_id):
        return self.service.create(actor, "shift", {
            "id": shift_id,
            "dispatcher_id": dispatcher_id,
            "area": "Tower A",
            "starts_at": "2026-10-02T08:00:00Z",
            "ends_at": "2026-10-02T20:00:00Z",
        })

    def test_handover_requires_next_shift_to_own_every_open_alarm(self):
        _, alarm = self._equipment_and_alarm()
        first = self._shift(self.dispatch_a, "dispatcher-a", "shift-a")
        second = self._shift(self.dispatch_b, "dispatcher-b", "shift-b")
        first = self.service.transition(self.dispatch_a, first["id"], "start_duty", {})
        second = self.service.transition(self.dispatch_b, second["id"], "start_duty", {})
        self.service.takeover_alarm(self.dispatch_a, alarm["id"], first["id"])

        handover = self.service.begin_handover(self.dispatch_a, first["id"])
        self.assertEqual(handover["status"], "in_handover")
        self.assertEqual(handover["data"]["unclosed_alarm_count"], 1)
        self.assertEqual(handover["data"]["pending_takeover_alarm_count"], 1)

        with self.assertRaises(ConflictError):
            self.service.end_shift(self.dispatch_a, first["id"])
        escalation = self.service.list("escalation", "open")[0]
        self.assertEqual(escalation["data"]["alarm_id"], alarm["id"])

        result = self.service.takeover_all_handover_alarms(
            self.dispatch_b, handover["id"], second["id"]
        )
        self.assertEqual(result["handover"]["status"], "completed")
        self.assertEqual(result["handover"]["data"]["taken_over_alarm_count"], 1)
        self.assertEqual(self.service.get(first["id"])["status"], "ended")
        self.assertEqual(self.service.list_takeovers(alarm_id=alarm["id"], status="active")[0]["shift_id"], "shift-b")

    def test_concurrent_takeover_first_wins(self):
        _, alarm = self._equipment_and_alarm(code="DOOR")
        first = self.service.transition(
            self.dispatch_a, self._shift(self.dispatch_a, "dispatcher-a", "shift-a")["id"], "start_duty", {}
        )
        second = self.service.transition(
            self.dispatch_b, self._shift(self.dispatch_b, "dispatcher-b", "shift-b")["id"], "start_duty", {}
        )
        barrier = threading.Barrier(2)
        results = []

        def take(actor, shift_id):
            try:
                barrier.wait(timeout=5)
                results.append(("ok", self.service.takeover_alarm(actor, alarm["id"], shift_id)))
            except Exception as exc:
                results.append(("error", type(exc).__name__, str(exc)))

        thread_a = threading.Thread(target=take, args=(self.dispatch_a, first["id"]))
        thread_b = threading.Thread(target=take, args=(self.dispatch_b, second["id"]))
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

        successes = [item for item in results if item[0] == "ok"]
        failures = [item for item in results if item[0] == "error"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][1], "ConflictError")
        self.assertIn("already taken over", failures[0][2])

    def test_later_takeover_without_handover_sees_existing_owner(self):
        _, alarm = self._equipment_and_alarm(code="CALL")
        first = self.service.transition(
            self.dispatch_a, self._shift(self.dispatch_a, "dispatcher-a", "shift-a")["id"], "start_duty", {}
        )
        second = self.service.transition(
            self.dispatch_b, self._shift(self.dispatch_b, "dispatcher-b", "shift-b")["id"], "start_duty", {}
        )
        self.service.takeover_alarm(self.dispatch_a, alarm["id"], first["id"])
        with self.assertRaises(ConflictError) as caught:
            self.service.takeover_alarm(self.dispatch_b, alarm["id"], second["id"])
        self.assertIn("already taken over by shift shift-a", str(caught.exception))

    def test_false_alarm_invalidates_takeover_and_recalculates_handover(self):
        _, alarm = self._equipment_and_alarm(code="FALSE")
        first = self.service.transition(
            self.dispatch_a, self._shift(self.dispatch_a, "dispatcher-a", "shift-a")["id"], "start_duty", {}
        )
        self.service.takeover_alarm(self.dispatch_a, alarm["id"], first["id"])
        handover = self.service.begin_handover(self.dispatch_a, first["id"])
        self.assertEqual(handover["data"]["unclosed_alarm_count"], 1)

        updated = self.service.transition(self.dispatch_a, alarm["id"], "mark_false", {})
        self.assertEqual(updated["status"], "false_alarm")
        self.assertEqual(
            self.service.list_takeovers(alarm_id=alarm["id"])[0]["status"],
            "invalidated",
        )
        refreshed = self.service.get(handover["id"])
        self.assertEqual(refreshed["status"], "completed")
        self.assertEqual(refreshed["data"]["unclosed_alarm_count"], 0)
        self.assertEqual(self.service.get(first["id"])["status"], "ended")


if __name__ == "__main__":
    unittest.main()
