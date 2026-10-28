// Read expanded event occurrences using Apple's supported EventKit API. No writes.
ObjC.import('EventKit');
ObjC.import('Foundation');
function run(argv) {
    const store = $.EKEventStore.alloc.init;
    const status = Number($.EKEventStore.authorizationStatusForEntityType(0));
    if (status !== 3) return JSON.stringify({ok:false, reason:'calendar_permission_missing', authorization:status});
    const from = new Date(argv[0]);
    const until = new Date(argv[1]);
    const all = store.calendarsForEntityType(0);
    const selected = $.NSMutableArray.alloc.init;
    const calendars = [];
    for (let i=0;i<all.count;i++) {
        const c=all.objectAtIndex(i), name=ObjC.unwrap(c.title);
        if (name.indexOf('Uni – ')!==0) continue;
        selected.addObject(c);
        calendars.push({name:name,source:ObjC.unwrap(c.source.title),source_type:Number(c.source.sourceType)});
    }
    if (!selected.count) return JSON.stringify({ok:false,reason:'no_uni_calendars',authorization:status});
    const pred=store.predicateForEventsWithStartDateEndDateCalendars(
        $.NSDate.dateWithTimeIntervalSince1970(from.getTime()/1000),
        $.NSDate.dateWithTimeIntervalSince1970(until.getTime()/1000),selected);
    const found=store.eventsMatchingPredicate(pred), events=[];
    function text(x) {return x && !x.isNil() ? ObjC.unwrap(x) : '';}
    function date(x) {return new Date(Number(x.timeIntervalSince1970)*1000).toISOString();}
    for(let i=0;i<found.count;i++) {
        const e=found.objectAtIndex(i);
        events.push({id:text(e.eventIdentifier),calendar:text(e.calendar.title),title:text(e.title),
            start:date(e.startDate),end:date(e.endDate),all_day:Boolean(e.allDay),
            location:text(e.location),notes:text(e.notes),status:Number(e.status),
            last_modified:e.lastModifiedDate && !e.lastModifiedDate.isNil() ? date(e.lastModifiedDate) : null});
    }
    events.sort((a,b)=>a.start.localeCompare(b.start));
    return JSON.stringify({ok:true,read_at:new Date().toISOString(),from:from.toISOString(),until:until.toISOString(),calendars:calendars,events:events,expanded_occurrences:true});
}
