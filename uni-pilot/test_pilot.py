import importlib.util
import json
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from calendar_context import normalize_ics

spec=importlib.util.spec_from_file_location('collector',Path(__file__).with_name('collect.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class PilotTests(unittest.TestCase):
    def setUp(self):self.now=datetime(2026,10,20,8,tzinfo=ZoneInfo('Europe/Berlin'))

    def test_remote_origin_must_be_explicit_and_match_stored_configuration(self):
        with mock.patch.dict(m.os.environ, {"UNI_MAIL_MCP_ORIGIN": ""}):
            with self.assertRaisesRegex(ValueError, "Configure an HTTPS MCP origin"):
                m.remote("mail", "mail_status")
        with mock.patch.dict(m.os.environ, {"UNI_MAIL_MCP_ORIGIN": "https://allowed.example"}), mock.patch.object(m, "load", return_value={"url": "https://other.example"}):
            with self.assertRaisesRegex(ValueError, "Unexpected private MCP origin"):
                m.remote("mail", "mail_status")

    def test_occurrence_conflicts_use_half_open_intervals(self):
        def event(id,start,end,**kw):return dict(id=id,start=start,end=end,**kw)
        events=[event('a','2026-10-20T08:00:00+00:00','2026-10-20T10:00:00+00:00'),
            event('b','2026-10-20T10:00:00+02:00','2026-10-20T12:00:00+02:00'),
            event('c','2026-10-20T10:00:00+00:00','2026-10-20T11:00:00+00:00'),
            event('day','2026-10-20T00:00:00+00:00','2026-10-21T00:00:00+00:00',all_day=True),
            event('cancel','2026-10-20T08:00:00+00:00','2026-10-20T09:00:00+00:00',status=3)]
        self.assertEqual(m.overlaps(events),[{'first':'a','second':'b','date':'2026-10-20'}])

    def test_stale_source_and_missing_quote_invalidate_fact(self):
        index={'documents':[{'id':'x','sha':'new'}],'candidate_pages':[{'document_id':'x','page':1,'text':'Abgabe 25. Oktober','complete':True}]}
        item={'due':'2026-10-19','evidence':[{'document_id':'x','sha256':'old','page':1,'quote':'Abgabe 26. Oktober'}]}
        row=m.validate_obligations([item],index,self.now)[0]
        self.assertFalse(row['evidence_valid']);self.assertTrue(row['overdue_unconfirmed'])
        self.assertFalse(row['completion_verified']);self.assertEqual(len(row['review_reasons']),2)

    def test_verified_fact_keeps_conflict_and_planning_distinct(self):
        index={'documents':[{'id':'x','sha':'same'}],'candidate_pages':[{'document_id':'x','page':1,'text':'Bis zum 25. Oktober Vorschlag','complete':True}]}
        item={'due':'2026-10-25','lead_days':7,'conflict':'Andere Fassung nennt 26. Oktober',
            'evidence':[{'document_id':'x','sha256':'same','page':1,'quote':'25. Oktober\nVorschlag'}]}
        row=m.validate_obligations([item],index,self.now)[0]
        self.assertTrue(row['evidence_valid']);self.assertEqual(row['suggested_start'],'2026-10-18')
        self.assertTrue(row['start_is_planning_not_official']);self.assertEqual(row['conflict'],item['conflict'])

    def test_page_overlap_preserves_text(self):
        self.assertEqual(m.join_segments(['Abgabe am 25.', 'am 25. Oktober']), 'Abgabe am 25. Oktober')

    def test_private_file_atomic_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'latest.json';m.atomic_json(path,{'private':'fixture'})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertEqual(json.loads(path.read_text()),{'private':'fixture'})

    def test_unreviewed_document_survives_an_unchanged_poll(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'index.sqlite'
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE docs(id TEXT,source TEXT,course TEXT,sha TEXT); CREATE TABLE passages(doc_id TEXT,page INTEGER,text TEXT);')
                db.execute('INSERT INTO docs VALUES(?,?,?,?)',('x','original.pdf','Fixture [12345678]','v1'))
                db.execute('INSERT INTO passages VALUES(?,?,?)',('x',1,'Abgabe am 25. Oktober'))
            a=m.read_index(path,['12345678','missing'],{})
            b=m.read_index(path,['12345678','missing'],a)
            self.assertEqual(len(b['unreviewed_documents']),1);self.assertEqual(b['changed_documents'],[])
            self.assertEqual(b['missing_course_ids'],['missing'])
            self.assertFalse(b['candidate_pages'][0]['new_or_changed'])
            c=m.read_index(path,['12345678'],b,{'x':'v1'})
            self.assertEqual(c['unreviewed_documents'],[])

    def test_readable_stale_mail_is_not_reported_current(self):
        data={'mail':{'status':{'last_poll':{'at':'2026-10-19T08:00:00+02:00','status':'ok'}}},
            'rag_status':{'source_index_at':'2026-10-20T06:00:00+02:00','coverage_complete':False,'pending_documents':8},
            'local_index_status':{'updated_at':'2026-10-19T06:00:00+02:00'}}
        check=m.freshness(data,self.now)
        self.assertFalse(check['mail_poll_current']);self.assertFalse(check['local_matches_remote_source_index'])
        self.assertEqual(check['pending_documents'],8);self.assertFalse(check['index_coverage_complete'])

    def test_calendar_export_utc_is_berlin_with_dst(self):
        raw='BEGIN:VCALENDAR\n'
        for uid,date in [('summer','20261023'),('winter','20261106')]:
            raw+=f'BEGIN:VEVENT\nUID:{uid}\nDTSTART:{date}T110000Z\nDTEND:{date}T120000Z\nSUMMARY:Fixture\nEND:VEVENT\n'
        raw+='END:VCALENDAR'
        events=normalize_ics(raw)
        self.assertEqual(events[0]['start_time_local'],'13:00')
        self.assertEqual(events[0]['end_time_local'],'14:00')
        self.assertTrue(events[0]['start_local'].endswith('+02:00'))
        self.assertEqual(events[1]['start_time_local'],'12:00')
        self.assertTrue(events[1]['start_local'].endswith('+01:00'))

    def test_calendar_alarm_uid_never_overwrites_event(self):
        raw='BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:event-id\nDTSTART;TZID=Europe/Berlin:20261023T130000\nDTEND;TZID=Europe/Berlin:20261023T140000\nSUMMARY:Fixture\nBEGIN:VALARM\nUID:alarm-id\nTRIGGER:-PT30M\nEND:VALARM\nEND:VEVENT\nEND:VCALENDAR'
        event=normalize_ics(raw)[0]
        self.assertEqual(event['uid'],'event-id');self.assertEqual(event['start_time_local'],'13:00')

    def test_calendar_all_day_and_recurrence_are_explicit(self):
        raw='BEGIN:VEVENT\nUID:deadline\nDTSTART;VALUE=DATE:20261025\nDTEND;VALUE=DATE:20261026\nSUMMARY:Fixture\nEND:VEVENT'
        event=normalize_ics(raw)[0]
        self.assertTrue(event['all_day']);self.assertNotIn('start_time_local',event)
        with self.assertRaises(ValueError):normalize_ics(raw.replace('SUMMARY:', 'RRULE:FREQ=DAILY\nSUMMARY:'))


if __name__=='__main__':unittest.main()
