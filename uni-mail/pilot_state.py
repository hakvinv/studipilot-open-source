"""Private, persistent Uni-Pilot obligation status for the single mailbox owner."""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal


class PilotStateError(ValueError):
    pass


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PilotState:
    """Store task state and completion history separately from mail contents."""

    def __init__(self, state):
        root = Path(state)
        self.directory = root / "uni-pilot"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "pilot.sqlite"
        with self.db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS obligations (
                    id TEXT PRIMARY KEY,
                    course TEXT NOT NULL,
                    title TEXT NOT NULL,
                    due TEXT,
                    state TEXT NOT NULL DEFAULT 'open'
                        CHECK (state IN ('open', 'completed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    completion_source TEXT,
                    completion_note TEXT
                );
                CREATE TABLE IF NOT EXISTS obligation_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    obligation_id TEXT NOT NULL,
                    event TEXT NOT NULL CHECK (event IN ('registered', 'updated', 'completed', 'reopened')),
                    occurred_at TEXT NOT NULL,
                    source TEXT,
                    note TEXT
                );
                CREATE INDEX IF NOT EXISTS obligation_events_by_id
                    ON obligation_events(obligation_id, event_id);
                """
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _record(value):
        if not isinstance(value, dict) or set(value) - {"id", "course", "title", "due"}:
            raise PilotStateError("Aufgabendatensatz enthaelt ungueltige Felder.")
        identifier = value.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", identifier):
            raise PilotStateError("Ungueltige Aufgaben-ID.")
        result = {"id": identifier}
        for key, maximum in (("course", 120), ("title", 500)):
            text = value.get(key)
            if not isinstance(text, str) or not text.strip() or len(text) > maximum:
                raise PilotStateError(f"Ungueltiger Aufgabenwert: {key}.")
            result[key] = text.strip()
        due = value.get("due")
        if due is not None:
            if not isinstance(due, str) or len(due) > 64:
                raise PilotStateError("Ungueltiges Faelligkeitsdatum.")
            try:
                date.fromisoformat(due) if "T" not in due else datetime.fromisoformat(due.replace("Z", "+00:00"))
            except ValueError as exc:
                raise PilotStateError("Faelligkeitsdatum muss ISO-8601 sein.") from exc
        result["due"] = due
        return result

    @staticmethod
    def _note(value):
        if not isinstance(value, str) or len(value) > 300:
            raise PilotStateError("Statusnotiz muss Text mit maximal 300 Zeichen sein.")
        return value.strip()

    def register(self, records):
        if not isinstance(records, list) or not 1 <= len(records) <= 50:
            raise PilotStateError("Es muessen 1 bis 50 Aufgaben uebergeben werden.")
        items = [self._record(item) for item in records]
        identifiers = [item["id"] for item in items]
        if len(set(identifiers)) != len(identifiers):
            raise PilotStateError("Aufgaben-IDs muessen eindeutig sein.")
        inserted = updated = unchanged = 0
        timestamp = now_utc()
        with self.db() as db:
            for item in items:
                existing = db.execute("SELECT * FROM obligations WHERE id=?", (item["id"],)).fetchone()
                if existing is None:
                    db.execute(
                        "INSERT INTO obligations(id,course,title,due,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (item["id"], item["course"], item["title"], item["due"], "open", timestamp, timestamp),
                    )
                    db.execute(
                        "INSERT INTO obligation_events(obligation_id,event,occurred_at) VALUES(?,?,?)",
                        (item["id"], "registered", timestamp),
                    )
                    inserted += 1
                elif any(existing[key] != item[key] for key in ("course", "title", "due")):
                    # Metadata may refresh from the private start context, but a
                    # refresh never reopens a completed obligation.
                    db.execute(
                        "UPDATE obligations SET course=?,title=?,due=?,updated_at=? WHERE id=?",
                        (item["course"], item["title"], item["due"], timestamp, item["id"]),
                    )
                    db.execute(
                        "INSERT INTO obligation_events(obligation_id,event,occurred_at,source) VALUES(?,?,?,?)",
                        (item["id"], "updated", timestamp, "private_context_sync"),
                    )
                    updated += 1
                else:
                    unchanged += 1
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged, "total": len(items)}

    def list(self, status: Literal["open", "completed", "all"] = "open", limit=100):
        if status not in {"open", "completed", "all"} or not 1 <= limit <= 100:
            raise PilotStateError("Ungueltiger Statusfilter oder Umfang.")
        sql = "SELECT * FROM obligations"
        params = ()
        if status != "all":
            sql += " WHERE state=?"
            params = (status,)
        sql += " ORDER BY CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, course, id LIMIT ?"
        with self.db() as db:
            rows = db.execute(sql, (*params, limit)).fetchall()
        return {
            "status_filter": status,
            "items": [dict(row) for row in rows],
            "count": len(rows),
            "note": "Abgeschlossene Aufgaben bleiben als Verlauf erhalten und erscheinen nicht in der offenen Liste.",
        }

    def complete(self, identifier, source: Literal["user_reported", "sent_folder_verified", "mail_send_confirmed"], note=""):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", identifier):
            raise PilotStateError("Ungueltige Aufgaben-ID.")
        if source not in {"user_reported", "sent_folder_verified", "mail_send_confirmed"}:
            raise PilotStateError("Unzulaessige Erledigungsquelle.")
        note = self._note(note)
        timestamp = now_utc()
        with self.db() as db:
            row = db.execute("SELECT state FROM obligations WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise PilotStateError("Aufgabe nicht gefunden; Status bleibt unveraendert.")
            if row["state"] == "completed":
                return {"id": identifier, "state": "completed", "already_completed": True}
            db.execute(
                "UPDATE obligations SET state='completed',updated_at=?,completed_at=?,completion_source=?,completion_note=? WHERE id=?",
                (timestamp, timestamp, source, note, identifier),
            )
            db.execute(
                "INSERT INTO obligation_events(obligation_id,event,occurred_at,source,note) VALUES(?,?,?,?,?)",
                (identifier, "completed", timestamp, source, note),
            )
        return {"id": identifier, "state": "completed", "completed_at": timestamp, "completion_source": source}

    def reopen(self, identifier, source: Literal["user_correction"] = "user_correction", note=""):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", identifier):
            raise PilotStateError("Ungueltige Aufgaben-ID.")
        note = self._note(note)
        timestamp = now_utc()
        with self.db() as db:
            row = db.execute("SELECT state FROM obligations WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise PilotStateError("Aufgabe nicht gefunden; Status bleibt unveraendert.")
            if row["state"] == "open":
                return {"id": identifier, "state": "open", "already_open": True}
            db.execute(
                "UPDATE obligations SET state='open',updated_at=?,completed_at=NULL,completion_source=NULL,completion_note=NULL WHERE id=?",
                (timestamp, identifier),
            )
            db.execute(
                "INSERT INTO obligation_events(obligation_id,event,occurred_at,source,note) VALUES(?,?,?,?,?)",
                (identifier, "reopened", timestamp, source, note),
            )
        return {"id": identifier, "state": "open", "reopened_at": timestamp}

    def history(self, identifier):
        with self.db() as db:
            exists = db.execute("SELECT 1 FROM obligations WHERE id=?", (identifier,)).fetchone()
            if not exists:
                raise PilotStateError("Aufgabe nicht gefunden.")
            events = db.execute(
                "SELECT event,occurred_at,source,note FROM obligation_events WHERE obligation_id=? ORDER BY event_id",
                (identifier,),
            ).fetchall()
        return {"id": identifier, "events": [dict(row) for row in events]}
