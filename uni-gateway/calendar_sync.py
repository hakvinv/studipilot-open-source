"""Private iCloud CalDAV access and stable, conflict-aware event writes."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from caldav import DAVClient
from caldav.lib.error import NotFoundError
from icalendar import Alarm, Calendar as ICalendar, Event as IEvent
import recurring_ical_events

BERLIN = ZoneInfo("Europe/Berlin")
PREFIX = "Uni – "


class CalendarNotConfigured(RuntimeError):
    pass


def stable_event_id(source_key: str) -> str:
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    return f"uni-{digest}@uni-management"


def normalize_occurrence(component: IEvent) -> dict:
    start = component.decoded("DTSTART")
    end = component.decoded("DTEND") if component.get("DTEND") else None
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    if all_day:
        start_day = start
        end_day = end if isinstance(end, date) and not isinstance(end, datetime) else start + timedelta(days=1)
        start_local, end_local = start_day.isoformat(), end_day.isoformat()
    else:
        if not isinstance(start, datetime):
            raise ValueError("Kalendertermin hat keine Uhrzeit")
        start = start.replace(tzinfo=BERLIN) if start.tzinfo is None else start
        end = end or (start + timedelta(hours=1))
        end = end.replace(tzinfo=BERLIN) if end.tzinfo is None else end
        start_local = start.astimezone(BERLIN).isoformat()
        end_local = end.astimezone(BERLIN).isoformat()
    return {
        "uid": str(component.get("UID", "")),
        "title": str(component.get("SUMMARY", "")),
        "start_local": start_local,
        "end_local": end_local,
        "date_local": start_local[:10],
        "all_day": all_day,
        "location": str(component.get("LOCATION", "")),
        "notes": str(component.get("DESCRIPTION", "")),
    }


class CalendarSync:
    def __init__(self, state: Path | str, client_factory=None):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state / "calendar.sqlite"
        self.client_factory = client_factory or self._client
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS event_map(uid TEXT PRIMARY KEY, source_key TEXT NOT NULL, source_hash TEXT NOT NULL, written_hash TEXT NOT NULL, calendar TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS sync_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db_path.chmod(0o600)

    def db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _client():
        username = os.environ.get("ICLOUD_USER", "").strip()
        password = os.environ.get("ICLOUD_APP_PASSWORD", "").strip()
        if not username or not password:
            raise CalendarNotConfigured("iCloud-CalDAV-Zugang fehlt; ICLOUD_USER und ICLOUD_APP_PASSWORD in Railway einrichten.")
        return DAVClient("https://caldav.icloud.com", username=username, password=password,
                         timeout=30, require_tls=True)

    def _calendars(self):
        client = self.client_factory()
        principal = client.principal()
        calendars = principal.calendars()
        selected = [c for c in calendars if str(getattr(c, "name", "") or "").startswith(PREFIX)]
        return client, selected

    def record_attempt(self, result):
        summary = {key: result.get(key) for key in (
            "state", "at", "candidate_count", "created", "updated", "unchanged", "skipped", "conflicts", "error_type")}
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO sync_state(key,value) VALUES('worker_last_attempt',?)",
                       (json.dumps(summary, ensure_ascii=False),))

    def status(self):
        configured = bool(os.environ.get("ICLOUD_USER") and os.environ.get("ICLOUD_APP_PASSWORD"))
        try:
            _, calendars = self._calendars()
            with self.db() as db:
                count = db.execute("SELECT count(*) FROM event_map").fetchone()[0]
                last = db.execute("SELECT value FROM sync_state WHERE key='last_sync'").fetchone()
                worker = db.execute("SELECT value FROM sync_state WHERE key='worker_last_attempt'").fetchone()
            return {"state": "ok", "configured": True, "uni_calendars": [str(c.name) for c in calendars],
                    "managed_events": count, "last_sync": json.loads(last[0]) if last else None,
                    "worker_last_attempt": json.loads(worker[0]) if worker else None}
        except CalendarNotConfigured as exc:
            return {"state": "needs_setup", "configured": False, "message": str(exc), "uni_calendars": []}
        except Exception as exc:
            return {"state": "error" if configured else "needs_setup", "configured": configured,
                    "message": "iCloud-Kalender konnte nicht gelesen werden.", "error_type": type(exc).__name__,
                    "uni_calendars": []}

    def events(self, starts: str, ends: str):
        start = datetime.fromisoformat(starts)
        end = datetime.fromisoformat(ends)
        if start.tzinfo is None or end.tzinfo is None or end <= start or end - start > timedelta(days=400):
            raise ValueError("Zeitfenster muss ISO-8601 mit Zeitzone sein und darf höchstens 400 Tage umfassen.")
        _, calendars = self._calendars()
        found = []
        for calendar in calendars:
            for resource in calendar.search(start=start, end=end, event=True, expand=False):
                schedule = recurring_ical_events.of(resource.icalendar_instance, skip_bad_series=True)
                for component in schedule.between(start, end):
                    item = normalize_occurrence(component)
                    recurrence = component.get("RECURRENCE-ID")
                    item["occurrence_id"] = item["uid"] + ("#" + recurrence.dt.isoformat() if recurrence else "")
                    item["calendar"] = str(calendar.name)
                    found.append(item)
        return sorted(found, key=lambda item: (item["start_local"], item["title"]))

    @staticmethod
    def _event_digest(event: dict) -> str:
        fields = {key: event.get(key, "") for key in (
            "title", "start_local", "end_local", "all_day", "location", "notes",
            "rrule", "exdates", "alarm_minutes")}
        fields["rrule"] = CalendarSync._normalize_rrule(fields["rrule"])
        return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _normalize_rrule(value):
        if not value:
            return None
        value = dict(value)

        def first(item):
            if isinstance(item, (list, tuple)):
                return item[0] if item else None
            return item

        freq = first(value.get("freq", ""))
        byday = first(value.get("byday", ""))
        until = first(value.get("until"))
        if hasattr(until, "dt"):
            until = until.dt
        if isinstance(until, str):
            try:
                until = datetime.fromisoformat(until.replace("Z", "+00:00"))
            except ValueError:
                pass
        if isinstance(until, datetime):
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            until = until.astimezone(timezone.utc).isoformat()
        elif hasattr(until, "isoformat"):
            until = until.isoformat()
        return {"freq": str(freq or "").lower(), "byday": str(byday or "").upper(),
                "until": str(until or "")}

    @staticmethod
    def _candidate_component(candidate, event):
        component = IEvent()
        component.add("uid", stable_event_id(candidate["source_key"]))
        component.add("summary", event["title"])
        component.add("dtstamp", datetime.now(tz=BERLIN))
        if event["all_day"]:
            component.add("dtstart", date.fromisoformat(event["start_local"]))
            component.add("dtend", date.fromisoformat(event["end_local"]))
        else:
            component.add("dtstart", datetime.fromisoformat(event["start_local"]).astimezone(BERLIN))
            component.add("dtend", datetime.fromisoformat(event["end_local"]).astimezone(BERLIN))
        if event["location"]:
            component.add("location", event["location"])
        if event["notes"]:
            component.add("description", event["notes"])
        component.add("X-UNI-SOURCE-HASH", candidate["source_hash"])
        recurrence = candidate.get("rrule") or {}
        if recurrence:
            until = datetime.fromisoformat(recurrence["until"])
            component.add("rrule", {"freq": recurrence["freq"], "byday": recurrence["byday"],
                                     "until": until.astimezone(timezone.utc)})
        for value in candidate.get("exdates", []):
            if event["all_day"]:
                excluded = date.fromisoformat(value)
            else:
                local_start = datetime.fromisoformat(event["start_local"]).astimezone(BERLIN)
                event_time = local_start.timetz().replace(tzinfo=None)
                excluded = datetime.combine(date.fromisoformat(value), event_time, tzinfo=BERLIN)
            component.add("exdate", excluded)
        alarm_minutes = candidate.get("alarm_minutes")
        if alarm_minutes:
            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", event["title"])
            alarm.add("trigger", timedelta(minutes=-int(alarm_minutes)))
            component.add_component(alarm)
        return component

    @staticmethod
    def _component_snapshot(component):
        start = component.decoded("DTSTART")
        end = component.decoded("DTEND") if component.get("DTEND") else None
        all_day = isinstance(start, date) and not isinstance(start, datetime)
        if all_day:
            start_local = start.isoformat()
            end_local = end.isoformat() if isinstance(end, date) else (start + timedelta(days=1)).isoformat()
        else:
            start = start.replace(tzinfo=BERLIN) if start.tzinfo is None else start
            end = end or (start + timedelta(hours=1))
            end = end.replace(tzinfo=BERLIN) if end.tzinfo is None else end
            start_local, end_local = start.isoformat(), end.isoformat()
        rrule = component.get("RRULE")
        recurrence = None
        if rrule:
            freq = rrule.get("FREQ", "")
            if isinstance(freq, (list, tuple)):
                freq = freq[0] if freq else ""
            byday = rrule.get("BYDAY", "")
            if isinstance(byday, list):
                byday = ",".join(str(x) for x in byday)
            until = rrule.get("UNTIL")
            if isinstance(until, (list, tuple)):
                until = until[0] if until else None
            if hasattr(until, "dt"):
                until = until.dt
            recurrence = {"freq": freq, "byday": str(byday),
                          "until": until.isoformat() if hasattr(until, "isoformat") else str(until)}
            recurrence = CalendarSync._normalize_rrule(recurrence)
        exdates = []
        raw_exdates = component.get("EXDATE", [])
        for prop in raw_exdates if isinstance(raw_exdates, list) else [raw_exdates]:
            for item in getattr(prop, "dts", []):
                value = item.dt
                exdates.append(value.date().isoformat() if isinstance(value, datetime) else value.isoformat())
        alarms = []
        for alarm in component.subcomponents:
            if alarm.name != "VALARM":
                continue
            trigger = alarm.get("TRIGGER")
            trigger = getattr(trigger, "dt", trigger)
            alarms.append({"action": str(alarm.get("ACTION", "")),
                           "minutes": int(abs(trigger.total_seconds()) / 60) if isinstance(trigger, timedelta) else None})
        return {"title": str(component.get("SUMMARY", "")), "start_local": start_local,
                "end_local": end_local, "all_day": all_day,
                "location": str(component.get("LOCATION", "")),
                "notes": str(component.get("DESCRIPTION", "")),
                "rrule": recurrence, "exdates": sorted(exdates),
                "alarm_minutes": sorted((x["minutes"] for x in alarms if x["action"] == "DISPLAY" and x["minutes"] is not None))}

    def preview_candidates(self, candidates: list[dict]):
        """Read-only reconciliation against the selected iCloud calendar."""
        if not candidates:
            return {"state": "ok", "created": 0, "updated": 0, "unchanged": 0, "conflicts": [], "skipped": 0}
        _, calendars = self._calendars()
        preferred = os.environ.get("ICLOUD_WRITE_CALENDAR", "").strip()
        selected = [c for c in calendars if str(c.name) == preferred] if preferred else calendars
        if len(selected) != 1:
            raise CalendarNotConfigured("Fuer den Abgleich muss genau ein iCloud-Kalender 'Uni – ' entsprechen oder ICLOUD_WRITE_CALENDAR gesetzt sein.")
        calendar = selected[0]
        counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
        conflicts = []
        for candidate in candidates:
            required = ("source_key", "source_hash", "source_document", "source_location", "course", "semester", "evidence")
            if (not candidate.get("verified") or any(not candidate.get(field) for field in required)
                    or len(str(candidate.get("source_hash", ""))) != 64):
                counts["skipped"] += 1
                continue
            fields = {key: candidate.get(key, "") for key in ("title", "start_local", "end_local", "all_day", "location", "notes")}
            fields["all_day"] = bool(fields["all_day"])
            citation = (f"Quelle: {candidate['source_document']}, {candidate['source_location']}\n"
                        f"Kurs: {candidate['course']} | Semester: {candidate['semester']}\n"
                        f"Dokument-SHA-256: {candidate['source_hash']}\nBeleg: {candidate['evidence']}")
            fields["notes"] = (str(fields["notes"]).strip() + "\n\n" + citation).strip()
            fields["rrule"] = candidate.get("rrule")
            fields["exdates"] = sorted(candidate.get("exdates", []))
            fields["alarm_minutes"] = [int(candidate["alarm_minutes"])] if candidate.get("alarm_minutes") else []
            try:
                desired_hash = self._event_digest(fields)
                uid = stable_event_id(candidate["source_key"])
                existing = calendar.get_event_by_uid(uid)
                snapshot = self._component_snapshot(existing.icalendar_component)
                current_hash = self._event_digest(snapshot)
                with self.db() as db:
                    previous = db.execute("SELECT * FROM event_map WHERE uid=?", (uid,)).fetchone()
                if not previous:
                    conflicts.append({"source_key": candidate["source_key"], "reason": "UID bereits durch ein anderes Ereignis belegt"})
                elif current_hash != previous["written_hash"]:
                    conflicts.append({"source_key": candidate["source_key"], "reason": "Ereignis im iCloud-Kalender zwischenzeitlich bearbeitet"})
                elif current_hash == desired_hash:
                    counts["unchanged"] += 1
                else:
                    counts["updated"] += 1
            except NotFoundError:
                counts["created"] += 1
            except Exception as exc:
                conflicts.append({"source_key": candidate.get("source_key", ""), "reason": type(exc).__name__})
        return {"state": "ok", **counts, "conflicts": conflicts}

    def apply_candidates(self, candidates: list[dict]):
        """Apply source-backed events. Never overwrite an event edited since our last write."""
        if not candidates:
            return {"created": 0, "updated": 0, "conflicts": [], "skipped": 0}
        _, calendars = self._calendars()
        preferred = os.environ.get("ICLOUD_WRITE_CALENDAR", "").strip()
        writable = [c for c in calendars if str(c.name) == preferred] if preferred else calendars
        if len(writable) != 1:
            raise CalendarNotConfigured("Fuer Kalender-Schreibzugriffe muss genau ein iCloud-Kalender 'Uni – ' entsprechen oder ICLOUD_WRITE_CALENDAR gesetzt sein.")
        calendar = writable[0]
        created = updated = skipped = 0
        conflicts = []
        for candidate in candidates:
            required = ("source_key", "source_hash", "source_document", "source_location", "course", "semester", "evidence")
            if (not candidate.get("verified") or any(not candidate.get(field) for field in required)
                    or len(str(candidate.get("source_hash", ""))) != 64):
                skipped += 1
                continue
            event = {key: candidate.get(key, "") for key in ("title", "start_local", "end_local", "all_day", "location", "notes")}
            event["all_day"] = bool(event["all_day"])
            try:
                start = date.fromisoformat(event["start_local"]) if event["all_day"] else datetime.fromisoformat(event["start_local"])
                end = date.fromisoformat(event["end_local"]) if event["all_day"] else datetime.fromisoformat(event["end_local"])
            except (TypeError, ValueError):
                skipped += 1
                continue
            if end <= start or not event["title"]:
                skipped += 1
                continue
            citation = (f"Quelle: {candidate['source_document']}, {candidate['source_location']}\n"
                        f"Kurs: {candidate['course']} | Semester: {candidate['semester']}\n"
                        f"Dokument-SHA-256: {candidate['source_hash']}\nBeleg: {candidate['evidence']}")
            event["notes"] = (str(event["notes"]).strip() + "\n\n" + citation).strip()
            uid = stable_event_id(candidate["source_key"])
            desired = self._candidate_component(candidate, event)
            desired_snapshot = self._component_snapshot(desired)
            payload_hash = self._event_digest(desired_snapshot)
            previous = None
            with self.db() as db:
                previous = db.execute("SELECT * FROM event_map WHERE uid=?", (uid,)).fetchone()
            try:
                resource = calendar.get_event_by_uid(uid)
                component = resource.icalendar_component
                current = self._component_snapshot(component)
                current_hash = self._event_digest(current)
                if not previous:
                    conflicts.append({"source_key": candidate["source_key"], "reason": "UID bereits durch ein anderes Ereignis belegt"})
                    continue
                if current_hash != previous["written_hash"]:
                    conflicts.append({"source_key": candidate["source_key"], "reason": "Ereignis im iCloud-Kalender zwischenzeitlich bearbeitet"})
                    continue
                # The rendered event can change after a parser or policy fix even
                # when the source document itself is byte-identical.
                if payload_hash == previous["written_hash"]:
                    skipped += 1
                    continue
                component = IEvent.from_ical(component.to_ical())
                component["SUMMARY"] = event["title"]
                if event["all_day"]:
                    component["DTSTART"] = date.fromisoformat(event["start_local"])
                    component["DTEND"] = date.fromisoformat(event["end_local"])
                else:
                    component["DTSTART"] = datetime.fromisoformat(event["start_local"]).astimezone(BERLIN)
                    component["DTEND"] = datetime.fromisoformat(event["end_local"]).astimezone(BERLIN)
                if event["location"]:
                    component["LOCATION"] = event["location"]
                elif "LOCATION" in component:
                    del component["LOCATION"]
                if event["notes"]:
                    component["DESCRIPTION"] = event["notes"]
                elif "DESCRIPTION" in component:
                    del component["DESCRIPTION"]
                component["X-UNI-SOURCE-HASH"] = candidate["source_hash"]
                for prop in ("RRULE", "EXDATE"):
                    if prop in component:
                        del component[prop]
                component.subcomponents = [item for item in component.subcomponents if item.name != "VALARM"]
                if "RRULE" in desired:
                    component.add("RRULE", desired["RRULE"])
                raw_exdates = desired.get("EXDATE", [])
                for exdate in raw_exdates if isinstance(raw_exdates, list) else [raw_exdates]:
                    component.add("EXDATE", exdate)
                for alarm in desired.subcomponents:
                    component.add_component(alarm)
                resource.data = component.to_ical().decode("utf-8")
                resource.save(precondition="etag")
                updated += 1
            except NotFoundError:
                if previous:
                    conflicts.append({"source_key": candidate["source_key"], "reason": "Zuvor verwalteter Kalendereintrag wurde entfernt"})
                    continue
                calendar.save_event(desired.to_ical().decode("utf-8"))
                created += 1
            except Exception as exc:
                conflicts.append({"source_key": candidate["source_key"], "reason": type(exc).__name__})
                continue
            with self.db() as db:
                db.execute("INSERT OR REPLACE INTO event_map VALUES(?,?,?,?,?,?)",
                           (uid, candidate["source_key"], candidate["source_hash"], payload_hash, str(calendar.name), datetime.now(tz=BERLIN).isoformat()))
        summary = {"at": datetime.now(tz=BERLIN).isoformat(), "created": created, "updated": updated,
                   "conflicts": len(conflicts), "skipped": skipped}
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO sync_state(key,value) VALUES('last_sync',?)", (json.dumps(summary),))
        return {**summary, "conflict_details": conflicts}
