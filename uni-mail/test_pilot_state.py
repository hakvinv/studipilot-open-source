import stat
import tempfile
import unittest
from pathlib import Path

from pilot_state import PilotState, PilotStateError


TASKS = [
    {"id": "seminar-email", "course": "Digitalisierung", "title": "Finale Seminaranmeldung senden", "due": "2026-10-25"},
    {"id": "prospect-paper", "course": "Innovationsoekonomik", "title": "Prospect Paper abgeben", "due": "2027-01-07T23:59:00+01:00"},
]


class PilotStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pilot = PilotState(Path(self.tmp.name))

    def test_register_is_idempotent_and_open_by_default(self):
        self.assertEqual(self.pilot.register(TASKS), {"inserted": 2, "updated": 0, "unchanged": 0, "total": 2})
        self.assertEqual(self.pilot.register(TASKS), {"inserted": 0, "updated": 0, "unchanged": 2, "total": 2})
        result = self.pilot.list()
        self.assertEqual(result["count"], 2)
        self.assertTrue(all(item["state"] == "open" for item in result["items"]))

    def test_completion_leaves_open_briefing_and_is_retained_in_history(self):
        self.pilot.register(TASKS)
        result = self.pilot.complete("seminar-email", "sent_folder_verified", "Treffer im Gesendet-Ordner")
        self.assertEqual(result["state"], "completed")
        self.assertEqual([x["id"] for x in self.pilot.list("open")["items"]], ["prospect-paper"])
        completed = self.pilot.list("completed")["items"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["completion_source"], "sent_folder_verified")
        self.assertEqual(self.pilot.history("seminar-email")["events"][-1]["event"], "completed")
        self.assertTrue(self.pilot.complete("seminar-email", "user_reported")["already_completed"])

    def test_context_refresh_updates_metadata_without_reopening(self):
        self.pilot.register(TASKS)
        self.pilot.complete("seminar-email", "user_reported", "Nutzer meldet erledigt")
        changed = [{**TASKS[0], "title": "Finale Seminaranmeldung und Themenwunsch senden"}]
        self.assertEqual(self.pilot.register(changed), {"inserted": 0, "updated": 1, "unchanged": 0, "total": 1})
        task = next(x for x in self.pilot.list("all")["items"] if x["id"] == "seminar-email")
        self.assertEqual(task["state"], "completed")
        self.assertEqual(task["title"], changed[0]["title"])

    def test_reopen_is_explicit_and_preserves_event_log(self):
        self.pilot.register(TASKS)
        self.pilot.complete("seminar-email", "user_reported")
        self.assertEqual(self.pilot.reopen("seminar-email")["state"], "open")
        self.assertEqual([x["event"] for x in self.pilot.history("seminar-email")["events"]], ["registered", "completed", "reopened"])

    def test_invalid_records_sources_and_unknown_ids_are_rejected(self):
        with self.assertRaises(PilotStateError):
            self.pilot.register([{**TASKS[0], "due": "next Tuesday"}])
        with self.assertRaises(PilotStateError):
            self.pilot.register([TASKS[0], TASKS[0]])
        self.pilot.register(TASKS)
        with self.assertRaises(PilotStateError):
            self.pilot.complete("missing-task", "user_reported")
        with self.assertRaises(PilotStateError):
            self.pilot.complete("seminar-email", "deadline_passed")
        self.assertEqual(self.pilot.list("open")["count"], 2)

    def test_private_storage_permissions(self):
        self.assertEqual(stat.S_IMODE(self.pilot.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.pilot.path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
