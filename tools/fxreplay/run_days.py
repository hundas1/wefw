"""STEP 2 - Walk the FX Replay chart through Rapier's order schedule, one day at a time.

Usage:  python tools/fxreplay/run_days.py 2026-09-10 2026-09-11 ...
Needs:  schedules from make_schedule.py, and an FX Replay session already open and
        logged in inside agent-browser (see tools/fxreplay/README.md).

Speed rule (what the user asked for): skip fast between setups, slow down ~2 min
before each order, play at real 1x speed for the last 20 s and through each
backtested entry, then skip to the next one.
Every place/cancel/flatten happens while the replay is PAUSED, then it resumes.
Log: $FXR_WORK/run.log  (FXR_WORK defaults to ./fxreplay_work)
"""
import os, json, re, sys, time, datetime as dt
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__))); import browser as ui
WORK=os.environ.get("FXR_WORK","fxreplay_work"); os.makedirs(f"{WORK}/sched",exist_ok=True)
PRE=120        # seconds of real-time (1x) play before each backtested entry
FAST=15        # slider value for skipping
ROLL=dt.date(2026,9,14)   # FX Replay NQ1 vs Rapier back-adjusted series
ADJ=279.25
days=sys.argv[1:]
OUT=open(f'{WORK}/run.log','a')
def log(*a):
    s=' '.join(str(x) for x in a); print(s,flush=True); OUT.write(s+'\n'); OUT.flush()
def parse(msg,adj):
    m=re.match(r'PLACE (\S+) (BUY|SELL) x(\d+) entry=(\S+) stop=(\S+) target=(\S+)',msg)
    if m:
        book,side,q,e,s,t=m.groups()
        return dict(kind='place',book=book,side=1 if side=='BUY' else -1,units=int(q)/10,
                    entry=None if e=='MKT' else float(e)-adj,sl=float(s)-adj,tp=float(t)-adj)
    if msg.startswith('CANCEL'): return dict(kind='cancel')
    if msg.startswith('FLATTEN'): return dict(kind='flatten')
    if msg.startswith('REPRICE'): return dict(kind='reprice')
    return None
def hms(t): h,m=map(int,t[:5].split(':')); return h*3600+m*60
def secs(c): h,m,s=map(int,c.split()[0].split(':')); return h*3600+m*60+s
def now():
    for _ in range(8):
        c=ui.clock()
        if re.match(r'\d\d:\d\d:\d\d',c): return secs(c)
        time.sleep(0.3)
    raise RuntimeError('no clock')
def running():
    a=now(); time.sleep(1.2); return now()!=a
def pause():
    for _ in range(3):
        if not running(): return
        ui.toggle_play(); time.sleep(0.8)
    raise RuntimeError('cannot pause')
def play():
    for _ in range(3):
        if running(): return
        ui.toggle_play(); time.sleep(1)
def speed(v): ui.set_speed(v)
STEP=[None]
def step(tf):
    if STEP[0]==tf: return
    ui.ab('click','@'+ui.ref('button "Timeframe"')); time.sleep(0.8)
    ui.ab('click','@'+ui.ref(f'option "{tf}"')); time.sleep(0.8); STEP[0]=tf
def fast_pause():
    ui.toggle_play(); time.sleep(0.6)
    if running(): ui.toggle_play(); time.sleep(0.6)
def tab(name):
    s=ui.snap()
    if 'button "Pending orders"' not in s: ui.ab('click','@'+ui.ref('button "Show positions and orders"',s)); time.sleep(1); s=ui.snap()
    ui.ab('click','@'+ui.ref(f'button "{name}"',s)); time.sleep(1)
def cancel_all():
    tab('Pending orders')
    if 'No data available' in str(ui.js('[...document.querySelectorAll("table td")].map(c=>c.textContent.trim())')): return
    ui.ab('click','@'+ui.ref('button "Pending orders actions"')); time.sleep(0.8)
    try: ui.ab('click','@'+ui.ref('menuitem "Cancel All Orders"')); time.sleep(1)
    except KeyError: ui.ab('press','Escape',check=False)
def open_position():
    tab('Open positions')
    rows=ui.js('[...document.querySelectorAll("table tr")].map(r=>[...r.querySelectorAll("td")].map(c=>c.textContent.trim())).filter(r=>r.length>4)')
    net=0.0
    for r in rows:
        q=float(re.search(r'[\d.]+',r[3]).group(0)); net+= q if r[2]=='Buy' else -q
    return round(net,2)
def market(side,units):
    s=ui.snap()
    if 'Close order panel' in s and 'Place order' not in s: ui.ab('click','@'+ui.ref('button "Close order panel"',s)); time.sleep(1); s=ui.snap()
    if 'button "Place order"' not in s: ui.ab('click','@'+ui.ref('button "Order"',s,last=True)); time.sleep(1.2); s=ui.snap()
    ui.ab('click','@'+ui.ref(f'button "{"Buy" if side>0 else "Sell"}"',s,after='Pop out order ticket'))
    ui.ab('click','@'+ui.ref('button "Market"',s,after='Pop out order ticket')); time.sleep(0.5); s=ui.snap()
    for sw in ('switch "Stop loss"','switch "Take profit"'):
        if sw+' [checked=true' in s: ui.ab('click','@'+ui.ref(sw,s)); time.sleep(0.3)
    r=ui.ref('textbox "Units'); ui.ab('click','@'+r); ui.ab('press','Control+a'); ui.ab('keyboard','type',f'{units:g}'); ui.ab('press','Tab'); time.sleep(0.4)
    ui.ab('click','@'+ui.ref('button "Place order"')); time.sleep(1.5)
def flatten():
    """Opposing market order (FX Replay nets positions); bracket legs die with the position."""
    cancel_all(); net=open_position()
    if net: market(-1 if net>0 else 1, abs(net))
    return net, open_position()
MONTHS='Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
def chart_date():
    """Current replay date, read from the "Go to a date" dialog (the chart draws dates on canvas)."""
    ui.ab('click','@'+ui.ref('button "Go to a date"')); time.sleep(1.2)
    v=ui.js('(document.querySelector("input[type=date]")||{}).value||""')
    ui.ab('click','@'+ui.ref('button "Cancel"')); time.sleep(0.6)
    return dt.date.fromisoformat(v) if v else None
def ticket_price():
    s=ui.snap()
    if 'textbox "Entry price' not in s:
        ui.ab('click','@'+ui.ref('button "Order"',s,last=True)); time.sleep(1); s=ui.snap()
    m=re.search(r'textbox "Entry price[^\n]*\]: ([\d.]+)',s); return float(m.group(1)) if m else None
def windows(d):
    """1x only around the entries of trades the backtest actually took."""
    w=[[hms(t['entry_time'][11:16])-PRE, hms(t['entry_time'][11:16])+60] for t in d['trades']]
    w.sort(); m=[]
    for a,b in w:
        if m and a<=m[-1][1]: m[-1][1]=max(m[-1][1],b)
        else: m.append([a,b])
    return m
for day in days:
    d=json.load(open(f'{WORK}/sched/{day}.json'))
    adj=ADJ if dt.date.fromisoformat(day)<ROLL else 0.0
    ev=[(hms(tm),parse(msg,adj),msg) for tm,msg in d['events']]; ev=[e for e in ev if e[1]]
    W=windows(d); end=max([b for a,b in W]+[e[0]+60 for e in ev])
    log(f'=== {day} | {len(ev)} actions | {len(d["trades"])} backtest trades | 1x windows',[(f'{a//3600:02d}:{a%3600//60:02d}',f'{b//3600:02d}:{b%3600//60:02d}') for a,b in W])
    for hop in range(6):
        cd=chart_date()
        if cd==dt.date.fromisoformat(day): break
        log('chart is on',cd,'not',day,'-> Next Session')
        if cd and cd>dt.date.fromisoformat(day): raise SystemExit(f'overshot {day}')
        ui.ab('click','@'+ui.ref('button "Go to"')); time.sleep(1); ui.ab('click','@'+ui.ref('button "Next Session')); time.sleep(4)
    else: raise SystemExit(f'could not reach {day}')
    log('verified chart date',cd)
    i=0; mode=None; pend={}
    fills={t['prefix']:hms(t['entry_time'][11:16])+60 for t in d['trades']}
    end=max([end]+[hms(t['exit_time'][11:16])+60 for t in d['trades']])
    start=now()
    while i<len(ev) and ev[i][0]<start-30: log('already past (done in earlier run):',ev[i][2]); i+=1
    while True:
        s=now()
        if any(a<=s<b for a,b in W): want=('1s',0)
        else:
            nxt=[x for x in [ev[i][0] if i<len(ev) else None]+[a for a,b in W if a>s] if x is not None]
            dist=(min(nxt) if nxt else end)-s
            want=('1m',15) if dist>2400 else ('1m',0) if dist>180 else ('1s',14) if dist>20 else ('1s',0)
        if want!=mode:
            if mode is not None and mode[0]=='1m': fast_pause()
            else: pause()
            step(want[0]); speed(want[1]); play(); mode=want
            s=now(); log(time.strftime('%H:%M:%S'),f'replay {s//3600:02d}:{s%3600//60:02d}:{s%60:02d} ->',{('1s',0):'1x (real time)',('1s',14):'approach (1s steps)',('1m',0):'skip 1m/s',('1m',15):'turbo skip'}[want])
        for pfx,ft in fills.items():
            if pfx in pend and s>=ft: pend.pop(pfx)
        if i<len(ev) and s>=ev[i][0]:
            t,e,msg=ev[i]; pause(); c=ui.clock(); late=now()-t
            try:
                if e['kind']=='place':
                    px=ticket_price(); exp=d['closes'].get(f'{(t-60)//3600:02d}:{(t-60)%3600//60:02d}')
                    if exp and px and abs((exp-px)-adj)>20:
                        log(c,'WARNING price gap',round(exp-px-adj,2),'pts vs Rapier last close (kept offset',adj,')')
                    rid=re.search(r'\[(rp-[\w-]+)\]',msg).group(1)
                    if e['entry'] is not None: pend[rid]=e
                    typ='Market' if e['entry'] is None else 'Limit'
                    if typ=='Limit' and px and ((e['side']>0 and e['entry']>=px) or (e['side']<0 and e['entry']<=px)):
                        typ='Market'; log(c,'limit already through market (px',px,') -> sent as market, like a marketable limit')
                    vals=ui.place(e['side'],typ,e['units'],e['sl'],e['tp'],entry=None if typ=='Market' else e['entry'])
                    log(c,f'(+{late}s) PLACED',msg,'| fxr', {k:e[k] for k in ('side','units','entry','sl','tp')},'| form',vals,
                        f'| px fxr={px} rapier={exp-adj if exp else None}')
                elif e['kind']=='reprice':
                    # executor replaces in place: drop the old order, keep any others resting
                    nxt=ev[i+1][2] if i+1<len(ev) else ''
                    m_=re.search(r'\[(rp-[0-9a-f]+)',nxt); base=m_.group(1) if m_ else None
                    for oid in [o for o in pend if base and o.startswith(base)]: pend.pop(oid)
                    cancel_all(); log(c,f'(+{late}s) REPRICE -> cancelled old order |',msg)
                    for oid,o in list(pend.items()):
                        v=ui.place(o['side'],'Limit',o['units'],o['sl'],o['tp'],entry=o['entry']); log(c,'  re-placed still-resting',oid,v)
                elif e['kind']=='cancel':
                    rid=msg.split()[1]; pend.pop(rid,None); cancel_all(); log(c,f'(+{late}s) CANCELLED',rid,'|',msg)
                    for oid,o in list(pend.items()):
                        v=ui.place(o['side'],'Limit',o['units'],o['sl'],o['tp'],entry=o['entry']); log(c,'  re-placed still-resting',oid,v)
                else:
                    was,now_=flatten(); log(c,f'(+{late}s) FLATTENED net {was} -> {now_} |',msg)
            except Exception as ex:
                log(c,'ACTION FAILED',msg,repr(ex)); ui.ab('screenshot',f'{WORK}/fail_{day}_{i}.png',check=False)
            ui.ab('screenshot',f'{WORK}/shot_{day}_{i:02d}.png',check=False)
            i+=1; mode=None; play(); continue
        if i>=len(ev) and s>=end:
            break
        time.sleep(0.25 if want==('1s',0) else 0.08)
    pause(); log(f'=== {day} done at', ui.clock())
    if day!=days[-1]:
        ui.ab('click','@'+ui.ref('button "Go to"')); time.sleep(1)
        ui.ab('click','@'+ui.ref('button "Next Session')); time.sleep(4)
        log('jumped to', ui.clock())
