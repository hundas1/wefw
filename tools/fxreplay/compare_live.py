"""STEP 3 - Read FX Replay's "Closed positions" table and match it to Rapier's backtest trades.

Usage:  python tools/fxreplay/compare_live.py
Matches on same day + same side + entry within 2 minutes; prints each pair, fills
that only happened in FX Replay, and totals. Writes $FXR_WORK/compare.json.
"""
import os, json, glob, sys, time, re, datetime as dt
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__))); import browser as ui
WORK=os.environ.get("FXR_WORK","fxreplay_work"); os.makedirs(f"{WORK}/sched",exist_ok=True)
def rows():
    s=ui.snap()
    if 'button "Closed positions"' not in s: ui.ab('click','@'+ui.ref('button "Show positions and orders"',s)); time.sleep(1); s=ui.snap()
    ui.ab('click','@'+ui.ref('button "Closed positions"',s)); time.sleep(1.5)
    out=[]; seen=set()
    for page in range(30):
        r=ui.js('[...document.querySelectorAll("table tr")].map(r=>[...r.querySelectorAll("td")].map(c=>c.textContent.trim())).filter(r=>r.length>10)')
        new=[x for x in r if tuple(x) not in seen]
        if not new: break
        for x in new: seen.add(tuple(x)); out.append(x)
        nxt=ui.js('(()=>{const b=[...document.querySelectorAll("button")].filter(b=>b.querySelector("i,svg")&&b.closest("[class*=paginator]")).pop();if(!b||b.disabled)return false;b.click();return true})()')
        if not nxt: break
        time.sleep(1.2)
    return out
fx=rows()
recs=[]
for x in fx:
    # ['', asset, side, start, end, entry, sl, tp, rr, size, close, realized, commission, journal]
    st=dt.datetime.strptime(x[3],'%m/%d/%y, %I:%M %p'); recs.append(dict(day=st.date().isoformat(),t=st.strftime('%H:%M'),side=x[2],entry=float(x[5]),rr=x[8],size=x[9],pnl=float(re.sub(r'[^\d.-]','',x[11]))))
bt=[]
days=sorted({r['day'] for r in recs}|set(sys.argv[1:]))
for day in days:
    try: d=json.load(open(f'{WORK}/sched/{day}.json'))
    except FileNotFoundError: continue
    for t in d['trades']: bt.append(dict(day=day,t=t['entry_time'][11:16],side='Buy' if t['side']=='1' else 'Sell',pnl=float(t['pnl']),reason=t['reason']))
used=set(); lines=[]; agree=0; both=0
for b in bt:
    m=[i for i,r in enumerate(recs) if i not in used and r['day']==b['day'] and r['side']==b['side'] and abs(int(r['t'][:2])*60+int(r['t'][3:])-int(b['t'][:2])*60-int(b['t'][3:]))<=2]
    if m:
        i=m[0]; used.add(i); r=recs[i]; both+=1; same=(r['pnl']>0)==(b['pnl']>0); agree+=same
        lines.append(f"{b['day']} {b['t']} {b['side']:4s} backtest {b['pnl']:+8.1f} ({b['reason']:7s}) | FXR {r['pnl']:+8.1f} {r['rr']:>6s} {'OK' if same else 'DIFFERENT OUTCOME'}")
    else: lines.append(f"{b['day']} {b['t']} {b['side']:4s} backtest {b['pnl']:+8.1f} ({b['reason']:7s}) | FXR -- no fill --")
for i,r in enumerate(recs):
    if i not in used: lines.append(f"{r['day']} {r['t']} {r['side']:4s} backtest   (none)           | FXR {r['pnl']:+8.1f} {r['rr']:>6s} EXTRA FILL")
print('\n'.join(sorted(lines)))
fxp=sum(r['pnl'] for r in recs); btp=sum(b['pnl'] for b in bt if b['day'] in {r['day'] for r in recs} or True)
print(f"\nbacktest trades {len(bt)} | FX Replay fills {len(recs)} | matched {both} | same win/loss {agree}/{both}")
print(f"win rate  backtest {sum(b['pnl']>0 for b in bt)/max(len(bt),1):.0%}  FX Replay {sum(r['pnl']>0 for r in recs)/max(len(recs),1):.0%}")
print(f"net P&L   backtest {sum(b['pnl'] for b in bt):+.0f}  FX Replay {fxp:+.0f}")
json.dump(dict(fx=recs,bt=bt),open(f'{WORK}/compare.json','w'),indent=1)
