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
def Log(*a):
    s=' '.join(str(x) for x in a); print(s,flush=True); OUT.write(s+'\n'); OUT.flush()
def Parse(msg,adj):
    m=re.match(r'PLACE (\S+) (BUY|SELL) x(\d+) entry=(\S+) stop=(\S+) target=(\S+)',msg)
    if m:
        book,side,q,e,s,t=m.groups()
        return dict(kind='place',book=book,side=1 if side=='BUY' else -1,units=int(q)/10,
                    entry=None if e=='MKT' else float(e)-adj,sl=float(s)-adj,tp=float(t)-adj)
    if msg.startswith('CANCEL'): return dict(kind='cancel')
    if msg.startswith('FLATTEN'): return dict(kind='flatten')
    if msg.startswith('REPRICE'): return dict(kind='reprice')
    return None
def Hms(t): h,m=map(int,t[:5].split(':')); return h*3600+m*60
def Secs(c): h,m,s=map(int,c.split()[0].split(':')); return h*3600+m*60+s
def Now():
    for _ in range(8):
        c=ui.Clock()
        if re.match(r'\d\d:\d\d:\d\d',c): return Secs(c)
        time.sleep(0.3)
    raise RuntimeError('no clock')
def Running():
    a=Now(); time.sleep(1.2); return Now()!=a
def Pause():
    for _ in range(3):
        if not Running(): return
        ui.TogglePlay(); time.sleep(0.8)
    raise RuntimeError('cannot pause')
def Play():
    for _ in range(3):
        if Running(): return
        ui.TogglePlay(); time.sleep(1)
def Speed(v): ui.SetSpeed(v)
STEP=[None]
def Step(tf):
    if STEP[0]==tf: return
    ui.Ab('click','@'+ui.Ref('button "Timeframe"')); time.sleep(0.8)
    ui.Ab('click','@'+ui.Ref(f'option "{tf}"')); time.sleep(0.8); STEP[0]=tf
def FastPause():
    ui.TogglePlay(); time.sleep(0.6)
    if Running(): ui.TogglePlay(); time.sleep(0.6)
def Tab(name):
    s=ui.Snap()
    if 'button "Pending orders"' not in s: ui.Ab('click','@'+ui.Ref('button "Show positions and orders"',s)); time.sleep(1); s=ui.Snap()
    ui.Ab('click','@'+ui.Ref(f'button "{name}"',s)); time.sleep(1)
def CancelAll():
    Tab('Pending orders')
    if 'No data available' in str(ui.Js('[...document.querySelectorAll("table td")].map(c=>c.textContent.trim())')): return
    ui.Ab('click','@'+ui.Ref('button "Pending orders actions"')); time.sleep(0.8)
    try: ui.Ab('click','@'+ui.Ref('menuitem "Cancel All Orders"')); time.sleep(1)
    except KeyError: ui.Ab('press','Escape',check=False)
def OpenPosition():
    Tab('Open positions')
    rows=ui.Js('[...document.querySelectorAll("table tr")].map(r=>[...r.querySelectorAll("td")].map(c=>c.textContent.trim())).filter(r=>r.length>4)')
    net=0.0
    for r in rows:
        q=float(re.search(r'[\d.]+',r[3]).group(0)); net+= q if r[2]=='Buy' else -q
    return round(net,2)
def Market(side,units):
    s=ui.Snap()
    if 'Close order panel' in s and 'Place order' not in s: ui.Ab('click','@'+ui.Ref('button "Close order panel"',s)); time.sleep(1); s=ui.Snap()
    if 'button "Place order"' not in s: ui.Ab('click','@'+ui.Ref('button "Order"',s,last=True)); time.sleep(1.2); s=ui.Snap()
    ui.Ab('click','@'+ui.Ref(f'button "{"Buy" if side>0 else "Sell"}"',s,after='Pop out order ticket'))
    ui.Ab('click','@'+ui.Ref('button "Market"',s,after='Pop out order ticket')); time.sleep(0.5); s=ui.Snap()
    for sw in ('switch "Stop loss"','switch "Take profit"'):
        if sw+' [checked=true' in s: ui.Ab('click','@'+ui.Ref(sw,s)); time.sleep(0.3)
    r=ui.Ref('textbox "Units'); ui.Ab('click','@'+r); ui.Ab('press','Control+a'); ui.Ab('keyboard','type',f'{units:g}'); ui.Ab('press','Tab'); time.sleep(0.4)
    ui.Ab('click','@'+ui.Ref('button "Place order"')); time.sleep(1.5)
def Flatten():
    """Opposing market order (FX Replay nets positions); bracket legs die with the position."""
    CancelAll(); net=OpenPosition()
    if net: Market(-1 if net>0 else 1, abs(net))
    return net, OpenPosition()
MONTHS='Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
def ChartDate():
    """Current replay date, read from the "Go to a date" dialog (the chart draws dates on canvas)."""
    ui.Ab('click','@'+ui.Ref('button "Go to a date"')); time.sleep(1.2)
    v=ui.Js('(document.querySelector("input[type=date]")||{}).value||""')
    ui.Ab('click','@'+ui.Ref('button "Cancel"')); time.sleep(0.6)
    return dt.date.fromisoformat(v) if v else None
def TicketPrice():
    s=ui.Snap()
    if 'textbox "Entry price' not in s:
        ui.Ab('click','@'+ui.Ref('button "Order"',s,last=True)); time.sleep(1); s=ui.Snap()
    m=re.search(r'textbox "Entry price[^\n]*\]: ([\d.]+)',s); return float(m.group(1)) if m else None
def Windows(d):
    """1x only around the entries of trades the backtest actually took."""
    w=[[Hms(t['entry_time'][11:16])-PRE, Hms(t['entry_time'][11:16])+60] for t in d['trades']]
    w.sort(); m=[]
    for a,b in w:
        if m and a<=m[-1][1]: m[-1][1]=max(m[-1][1],b)
        else: m.append([a,b])
    return m
for day in days:
    d=json.load(open(f'{WORK}/sched/{day}.json'))
    adj=ADJ if dt.date.fromisoformat(day)<ROLL else 0.0
    ev=[(Hms(tm),Parse(msg,adj),msg) for tm,msg in d['events']]; ev=[e for e in ev if e[1]]
    W=Windows(d); end=max([b for a,b in W]+[e[0]+60 for e in ev])
    Log(f'=== {day} | {len(ev)} actions | {len(d["trades"])} backtest trades | 1x windows',[(f'{a//3600:02d}:{a%3600//60:02d}',f'{b//3600:02d}:{b%3600//60:02d}') for a,b in W])
    for hop in range(6):
        cd=ChartDate()
        if cd==dt.date.fromisoformat(day): break
        Log('chart is on',cd,'not',day,'-> Next Session')
        if cd and cd>dt.date.fromisoformat(day): raise SystemExit(f'overshot {day}')
        ui.Ab('click','@'+ui.Ref('button "Go to"')); time.sleep(1); ui.Ab('click','@'+ui.Ref('button "Next Session')); time.sleep(4)
    else: raise SystemExit(f'could not reach {day}')
    Log('verified chart date',cd)
    i=0; mode=None; pend={}
    fills={t['prefix']:Hms(t['entry_time'][11:16])+60 for t in d['trades']}
    end=max([end]+[Hms(t['exit_time'][11:16])+60 for t in d['trades']])
    start=Now()
    while i<len(ev) and ev[i][0]<start-30: Log('already past (done in earlier run):',ev[i][2]); i+=1
    while True:
        s=Now()
        if any(a<=s<b for a,b in W): want=('1s',0)
        else:
            nxt=[x for x in [ev[i][0] if i<len(ev) else None]+[a for a,b in W if a>s] if x is not None]
            dist=(min(nxt) if nxt else end)-s
            want=('1m',15) if dist>2400 else ('1m',0) if dist>180 else ('1s',14) if dist>20 else ('1s',0)
        if want!=mode:
            if mode is not None and mode[0]=='1m': FastPause()
            else: Pause()
            Step(want[0]); Speed(want[1]); Play(); mode=want
            s=Now(); Log(time.strftime('%H:%M:%S'),f'replay {s//3600:02d}:{s%3600//60:02d}:{s%60:02d} ->',{('1s',0):'1x (real time)',('1s',14):'approach (1s steps)',('1m',0):'skip 1m/s',('1m',15):'turbo skip'}[want])
        for pfx,ft in fills.items():
            if pfx in pend and s>=ft: pend.pop(pfx)
        if i<len(ev) and s>=ev[i][0]:
            t,e,msg=ev[i]; Pause(); c=ui.Clock(); late=Now()-t
            try:
                if e['kind']=='place':
                    px=TicketPrice(); exp=d['closes'].get(f'{(t-60)//3600:02d}:{(t-60)%3600//60:02d}')
                    if exp and px and abs((exp-px)-adj)>20:
                        Log(c,'WARNING price gap',round(exp-px-adj,2),'pts vs Rapier last close (kept offset',adj,')')
                    rid=re.search(r'\[(rp-[\w-]+)\]',msg).group(1)
                    if e['entry'] is not None: pend[rid]=e
                    typ='Market' if e['entry'] is None else 'Limit'
                    if typ=='Limit' and px and ((e['side']>0 and e['entry']>=px) or (e['side']<0 and e['entry']<=px)):
                        typ='Market'; Log(c,'limit already through market (px',px,') -> sent as market, like a marketable limit')
                    vals=ui.Place(e['side'],typ,e['units'],e['sl'],e['tp'],entry=None if typ=='Market' else e['entry'])
                    Log(c,f'(+{late}s) PLACED',msg,'| fxr', {k:e[k] for k in ('side','units','entry','sl','tp')},'| form',vals,
                        f'| px fxr={px} rapier={exp-adj if exp else None}')
                elif e['kind']=='reprice':
                    # executor replaces in place: drop the old order, keep any others resting
                    nxt=ev[i+1][2] if i+1<len(ev) else ''
                    m_=re.search(r'\[(rp-[0-9a-f]+)',nxt); base=m_.group(1) if m_ else None
                    for oid in [o for o in pend if base and o.startswith(base)]: pend.pop(oid)
                    CancelAll(); Log(c,f'(+{late}s) REPRICE -> cancelled old order |',msg)
                    for oid,o in list(pend.items()):
                        v=ui.Place(o['side'],'Limit',o['units'],o['sl'],o['tp'],entry=o['entry']); Log(c,'  re-placed still-resting',oid,v)
                elif e['kind']=='cancel':
                    rid=msg.split()[1]; pend.pop(rid,None); CancelAll(); Log(c,f'(+{late}s) CANCELLED',rid,'|',msg)
                    for oid,o in list(pend.items()):
                        v=ui.Place(o['side'],'Limit',o['units'],o['sl'],o['tp'],entry=o['entry']); Log(c,'  re-placed still-resting',oid,v)
                else:
                    was,now_=Flatten(); Log(c,f'(+{late}s) FLATTENED net {was} -> {now_} |',msg)
            except Exception as ex:
                Log(c,'ACTION FAILED',msg,repr(ex)); ui.Ab('screenshot',f'{WORK}/fail_{day}_{i}.png',check=False)
            ui.Ab('screenshot',f'{WORK}/shot_{day}_{i:02d}.png',check=False)
            i+=1; mode=None; Play(); continue
        if i>=len(ev) and s>=end:
            break
        time.sleep(0.25 if want==('1s',0) else 0.08)
    Pause(); Log(f'=== {day} done at', ui.Clock())
    if day!=days[-1]:
        ui.Ab('click','@'+ui.Ref('button "Go to"')); time.sleep(1)
        ui.Ab('click','@'+ui.Ref('button "Next Session')); time.sleep(4)
        Log('jumped to', ui.Clock())
