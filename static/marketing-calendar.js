/* Calendar dates come from the server in the selected timezone. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let data = JSON.parse($('mc-data').textContent), agenda = false, sequence = 0;
  const names = {article:'Blog post',help:'Help article',social:'Social post',video:'Video',email:'Email',setup:'Channel setup'};
  const posted = ['published','sent'];
  function node(tag, text, cls) { const n = document.createElement(tag); if(text !== undefined) n.textContent=text; if(cls) n.className=cls; return n; }
  function matches(e) {
    const kind=$('mc-kind').value, status=$('mc-status').value;
    return (!kind || e.kind===kind) && (!status || (status==='posted' ? posted.includes(e.status) : status==='planned' ? ['planned','scheduled'].includes(e.status) : status==='attention' ? ['failed','overdue','cancelled'].includes(e.status) : e.status===status));
  }
  function time(e) {
    if (!e.at || e.time_kind==='planned' && /T00:00:00/.test(e.at) && e.source==='content_calendar') return 'Date only';
    return new Intl.DateTimeFormat('en-IN',{timeZone:data.timezone,hour:'2-digit',minute:'2-digit',second:posted.includes(e.status)?'2-digit':undefined,hour12:false,timeZoneName:'short'}).format(new Date(e.at));
  }
  function link(label, href) {
    if (!href || !(href.startsWith('/') && !href.startsWith('//') || /^https:\/\//.test(href))) return null;
    const a=node('a',label,'btn');a.href=href;return a;
  }
  function details(e) {
    const box=$('mc-details'); box.replaceChildren(node('span',(names[e.kind]||e.kind)+' · '+e.channel,'eyebrow'),node('h3',e.title),node('p',e.brand_name),node('strong',e.status.toUpperCase(),'mc-status '+e.status),node('p',(e.date||'Unscheduled')+' · '+time(e)),node('p',e.detail||''));
    if(e.time_kind==='confirmed_at') box.append(node('p','This is the confirmation time. The original publication or send time was not available.','mc-explanation'));
    if(e.status==='projected') box.append(node('p','This is a forecast window. Topic, verification and quality checks can change the date.','mc-explanation'));
    box.append(node('p','Source: '+e.source,'sub'));
    for(const a of [link('View published page',e.public_url),link('Review work',e.url)]) if(a) box.append(a);
  }
  function eventButton(e, full=false) {
    const b=node('button',undefined,'mc-event '+e.status);b.type='button';b.title=e.title+' · '+e.status;
    b.append(node('span',(names[e.kind]||e.kind)+' · '+e.status,'mc-event-meta'),node('span',e.title,'mc-event-title'));
    if(full) b.append(node('span',(e.date||'Undated')+' · '+time(e),'mc-event-meta'));
    else if(e.at) b.append(node('span',(e.status==='projected'?'From ':'')+time(e),'mc-event-meta'));
    b.addEventListener('click',()=>details(e));return b;
  }
  function render() {
    const [year,month]=data.month.split('-').map(Number), first=new Date(Date.UTC(year,month-1,1)), start=new Date(first);
    start.setUTCDate(1-(first.getUTCDay()+6)%7);
    $('mc-heading').textContent=new Intl.DateTimeFormat('en',{month:'long',year:'numeric',timeZone:'UTC'}).format(first);
    $('mc-month').value=data.month;
    const grid=$('mc-grid');grid.replaceChildren();
    for(const d of ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']) grid.append(node('div',d,'mc-weekday'));
    const events=data.events.filter(matches).sort((a,b)=>(a.at||a.date).localeCompare(b.at||b.date));
    for(let i=0;i<42;i++) {
      const day=new Date(start);day.setUTCDate(start.getUTCDate()+i);const iso=day.toISOString().slice(0,10);
      const cell=node('div',undefined,'mc-day'+(day.getUTCMonth()!==month-1?' mc-muted':'')+(iso===data.today?' mc-today':''));
      const number=node('span',String(day.getUTCDate()),'mc-day-number');number.setAttribute('aria-label',iso);cell.append(number);
      for(const e of events.filter(e=>e.date===iso)) cell.append(eventButton(e));grid.append(cell);
    }
    const list=$('mc-agenda');list.replaceChildren();
    for(const e of events) list.append(eventButton(e,true));
    if(!events.length) list.append(node('p','No matching events in this month.'));
    grid.hidden=agenda;list.hidden=!agenda;$('mc-view').textContent=agenda?'Month view':'Agenda view';
    const backlog=$('mc-backlog');backlog.replaceChildren();const pending=data.unscheduled.filter(matches);
    for(const e of pending) backlog.append(eventButton(e,true));
    if(!pending.length) backlog.append(node('p','No matching undated or overdue work.'));
    $('mc-message').textContent=(data.coverage.complete?'':'Some source histories reached the display limit. ')+events.filter(e=>posted.includes(e.status)).length+' published / sent · '+events.filter(e=>e.status==='projected').length+' projected windows · '+pending.length+' backlog items';
  }
  async function load(month=data.month) {
    const request=++sequence, p=new URLSearchParams({month,timezone:$('mc-zone').value});if($('mc-brand').value) p.set('brand_id',$('mc-brand').value);
    $('mc-message').textContent='Loading calendar…';
    try {const r=await fetch('/api/marketing-calendar?'+p);if(!r.ok) throw Error('Unable to load calendar');const next=await r.json();if(request!==sequence)return;data=next;history.replaceState(null,'','/calendar?'+p);render();}
    catch(e) {if(request===sequence)$('mc-message').textContent='Calendar could not load. Your previous view is retained. Please try again.';}
  }
  function move(offset) {const [y,m]=data.month.split('-').map(Number);const d=new Date(Date.UTC(y,m-1+offset,1));load(d.toISOString().slice(0,7));}
  $('calendar-filters').addEventListener('submit',e=>{e.preventDefault();load();});
  for(const id of ['mc-brand','mc-zone']) $(id).addEventListener('change',()=>load());
  for(const id of ['mc-kind','mc-status']) $(id).addEventListener('change',render);
  $('mc-prev').addEventListener('click',()=>move(-1));$('mc-next').addEventListener('click',()=>move(1));
  $('mc-today').addEventListener('click',()=>load(data.today.slice(0,7)));
  $('mc-month').addEventListener('change',()=>{if(/^\d{4}-\d{2}$/.test($('mc-month').value))load($('mc-month').value);});
  $('mc-view').addEventListener('click',()=>{agenda=!agenda;render();});render();
})();
