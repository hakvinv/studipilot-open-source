"""Normalize an existing finite Uni-calendar export for a cloud model. No UI or writes."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def normalize_ics(raw):
    tz=ZoneInfo('Europe/Berlin')
    lines=[]
    for line in raw.splitlines():
        if line.startswith((' ','\t')):
            if not lines:raise ValueError('Invalid folded calendar')
            lines[-1]+=line[1:]
        else:lines.append(line)
    def plain(value):
        return value.replace('\\n','\n').replace('\\N','\n').replace('\\,',',').replace('\\;',';').replace('\\\\','\\')
    def date(key,value):
        if 'VALUE=DATE' in key:return datetime.strptime(value,'%Y%m%d').date().isoformat()
        if value.endswith('Z'):
            dt=datetime.strptime(value,'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        else:
            zone=next((p.split('=',1)[1] for p in key.split(';')[1:] if p.startswith('TZID=')),'Europe/Berlin')
            dt=datetime.strptime(value,'%Y%m%dT%H%M%S').replace(tzinfo=ZoneInfo(zone))
        return dt.astimezone(tz).isoformat()
    events=[];current=None;depth=0
    for line in lines:
        if line=='BEGIN:VEVENT':
            if current is not None:raise ValueError('Nested event')
            current={};depth=1;continue
        if current is None:continue
        if line.startswith('BEGIN:'):depth+=1;continue
        if line.startswith('END:'):
            depth-=1
            if depth==0:
                if line!='END:VEVENT' or not {'uid','start_local','end_local','title'}<=current.keys():
                    raise ValueError('Incomplete event')
                current['date_local']=current['start_local'][:10]
                if not current['all_day']:
                    current['start_time_local']=current['start_local'][11:16]
                    current['end_time_local']=current['end_local'][11:16]
                events.append(current);current=None
            continue
        if depth!=1 or ':' not in line:continue # Alarm IDs/dates are not event IDs/dates.
        key,value=line.split(':',1);name=key.split(';')[0]
        if name in {'RRULE','RDATE','EXDATE'}:raise ValueError('Recurring event needs explicit expansion')
        if name=='DTSTART':current['start_local']=date(key,value);current['all_day']='VALUE=DATE' in key
        elif name=='DTEND':current['end_local']=date(key,value)
        elif name=='UID':current['uid']=value
        elif name=='SUMMARY':current['title']=plain(value)
        elif name=='LOCATION':current['location']=plain(value)
        elif name=='DESCRIPTION':current['notes']=plain(value)
    if current is not None:raise ValueError('Unterminated event')
    if len({e['uid'] for e in events})!=len(events):raise ValueError('Ambiguous duplicate event UID')
    return sorted(events,key=lambda e:(e['start_local'],e['title']))
