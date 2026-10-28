import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from caldav.lib.error import NotFoundError
from icalendar import Calendar as ICalendar, Event as IEvent

from calendar_sync import CalendarSync


BERLIN = ZoneInfo("Europe/Berlin")


class FakeResource:
    def __init__(self, calendar, component):
        self.calendar = calendar
        self.component = component
        self.data = component.to_ical().decode()

    @property
    def icalendar_component(self):
        return self.component

    @property
    def icalendar_instance(self):
        result = ICalendar()
        result.add("version", "2.0")
        result.add("prodid", "-//test//uni//EN")
        result.add_component(self.component)
        return result

    def save(self, **kwargs):
        parsed = ICalendar.from_ical(self.data)
        self.component = parsed if parsed.name == "VEVENT" else parsed.walk("VEVENT")[0]
        self.calendar.events[str(self.component.get("UID"))] = self


class FakeCalendar:
    name = "Uni – WiSe 2026/27"

    def __init__(self):
        self.events = {}

    def get_event_by_uid(self, uid):
        if uid not in self.events:
            raise NotFoundError("not found")
        return self.events[uid]

    def save_event(self, data):
        parsed = ICalendar.from_ical(data)
        component = parsed if parsed.name == "VEVENT" else parsed.walk("VEVENT")[0]
        resource = FakeResource(self, component)
        self.events[str(component.get("UID"))] = resource
        return resource

    def search(self, **kwargs):
        return list(self.events.values())


class FakePrincipal:
    def __init__(self, calendar):
        self.calendar = calendar

    def calendars(self):
        return [self.calendar]


class FakeClient:
    def __init__(self, calendar):
        self.calendar = calendar

    def principal(self):
        return FakePrincipal(self.calendar)


def candidate(source_hash=None):
    return {
        "source_key": "innovation:plan:page-4:deadline-1",
        "source_hash": source_hash or hashlib.sha256(b"doc-v1").hexdigest(),
        "source_document": "01_StudIP/Innovation/plan.pdf",
        "source_location": "Seite 4",
        "course": "Innovationsoekonomik",
        "semester": "WiSe 2026/27",
        "evidence": "Abgabe am 2027-01-12",
        "verified": True,
        "title": "Prospect Paper abgeben",
        "start_local": "2027-01-12",
        "end_local": "2027-01-13",
        "all_day": True,
        "location": "",
        "notes": "Seminarabgabe",
        "alarm_minutes": 1440,
    }


class CalendarSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calendar = FakeCalendar()
        self.sync = CalendarSync(Path(self.tmp.name), lambda: FakeClient(self.calendar))

    def test_event_upsert_is_idempotent_and_keeps_source_citation(self):
        first = self.sync.apply_candidates([candidate()])
        again = self.sync.apply_candidates([candidate()])
        self.assertEqual(first["created"], 1)
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["skipped"], 1)
        event = next(iter(self.calendar.events.values())).icalendar_component
        description = str(event.get("DESCRIPTION"))
        self.assertIn("Dokument-SHA-256:", description)
        self.assertIn("WiSe 2026/27", description)

    def test_user_edit_conflicts_and_deleted_managed_event_is_not_recreated(self):
        row = candidate()
        self.sync.apply_candidates([row])
        resource = next(iter(self.calendar.events.values()))
        resource.component["SUMMARY"] = "Meine eigene Aenderung"
        conflict = self.sync.apply_candidates([{**row, "source_hash": hashlib.sha256(b"doc-v2").hexdigest()}])
        self.assertEqual(conflict["conflicts"], 1)
        self.calendar.events.clear()
        removed = self.sync.apply_candidates([{**row, "source_hash": hashlib.sha256(b"doc-v3").hexdigest()}])
        self.assertEqual(removed["created"], 0)
        self.assertEqual(removed["conflicts"], 1)

    def test_parser_change_updates_event_with_unchanged_source_hash(self):
        row = candidate()
        self.sync.apply_candidates([row])
        corrected = {**row, "title": "Prospect Paper final abgeben"}
        result = self.sync.apply_candidates([corrected])
        self.assertEqual(result["updated"], 1)
        event = next(iter(self.calendar.events.values())).icalendar_component
        self.assertEqual(str(event.get("SUMMARY")), "Prospect Paper final abgeben")

    def test_event_without_complete_source_metadata_is_skipped(self):
        incomplete = candidate()
        incomplete.pop("semester")
        result = self.sync.apply_candidates([incomplete])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.calendar.events, {})

    def test_recurring_event_keeps_local_time_across_berlin_dst_change(self):
        event = IEvent()
        event.add("uid", "weekly@uni.example")
        event.add("summary", "Tutorium")
        event.add("dtstart", datetime(2026, 10, 24, 10, 0, tzinfo=BERLIN))
        event.add("dtend", datetime(2026, 10, 24, 11, 30, tzinfo=BERLIN))
        event.add("rrule", {"freq": "daily", "count": 3})
        self.calendar.save_event(event.to_ical().decode())
        values = self.sync.events("2026-10-24T00:00:00+02:00", "2026-10-27T00:00:00+01:00")
        self.assertEqual(len(values), 3)
        self.assertEqual([value["start_local"][11:16] for value in values], ["10:00", "10:00", "10:00"])
        self.assertEqual(values[1]["start_local"], "2026-10-25T10:00:00+01:00")

    def test_weekly_candidate_roundtrip_keeps_dst_skips_break_and_silent_alert(self):
        row = candidate()
        row.update(title="Makroökonomie — Vorlesung", start_local="2026-10-14T12:00:00+02:00",
                   end_local="2026-10-14T14:00:00+02:00", all_day=False,
                   location="HS 2010", alarm_minutes=15,
                   rrule={"freq": "weekly", "byday": "WE", "until": "2027-01-29T23:59:00+01:00"},
                   exdates=["2026-12-23", "2026-12-30"])
        preview = self.sync.preview_candidates([row])
        self.assertEqual(preview["created"], 1)
        self.assertEqual(self.calendar.events, {})
        applied = self.sync.apply_candidates([row])
        self.assertEqual(applied["created"], 1)
        recurrence = self.sync.events("2026-10-14T00:00:00+02:00", "2027-01-05T00:00:00+01:00")
        self.assertTrue(recurrence)
        self.assertTrue(all(item["start_local"][11:16] == "12:00" for item in recurrence))
        dates = [item["date_local"] for item in recurrence]
        self.assertNotIn("2026-12-23", dates)
        self.assertIn("2026-10-28", dates)
        self.assertEqual(next(item["start_local"] for item in recurrence if item["date_local"] == "2026-10-28")[11:16], "12:00")
        self.assertTrue(next(item["start_local"] for item in recurrence if item["date_local"] == "2026-10-28").endswith("+01:00"))
        component = next(iter(self.calendar.events.values())).icalendar_component
        alarm = next((item for item in component.subcomponents if item.name == "VALARM"), None)
        self.assertIsNotNone(alarm)
        self.assertEqual(str(alarm.get("ACTION")), "DISPLAY")
        self.assertNotIn("AUDIO", component.to_ical().decode())
        next_preview = self.sync.preview_candidates([row])
        self.assertEqual(next_preview["unchanged"], 1)


if __name__ == "__main__":
    unittest.main()
