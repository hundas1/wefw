"""STEP 1 - Work out exactly which orders Rapier would send on one trading day.

Usage:  python tools/fxreplay/make_schedule.py 2026-09-10
It replays that day minute by minute through the real engine + executor + simulated
broker (same as rapier/replay.py) and saves $FXR_WORK/sched/<day>.json with:
  events  - [time, "PLACE ... / CANCEL ... / REPRICE ... / FLATTEN ..."]
  trades  - the trades the simulated broker filled (the backtest answer)
  closes  - 1m closes, used to sanity-check FX Replay's prices
Prices are Rapier's roll-ADJUSTED prices. run_days.py converts them back to raw
contract prices (subtract 279.25 before the 2026-09-14 roll, 0 after).
"""
import os, sys, json, pandas as pd
from rapier import data as D, replay as R
from rapier.backtest import Market
from rapier.brokers.base import SimBroker
from rapier.executor import Executor, ExecConfig
from rapier.live import engine_view
from rapier.system import load_params
day=sys.argv[1]; adj=float(sys.argv[2]) if len(sys.argv)>2 else 0.0
WORK=os.environ.get("FXR_WORK","fxreplay_work"); os.makedirs(f"{WORK}/sched",exist_ok=True)
d=D.load_nq(('1h','1m','5m','15m'),refresh=False); fr={k:d[k] for k in ('1h','1m','5m','15m')}
books=('teacher-1m','scalp-5m','ote-1h'); p=R.live_params(load_params(),books)
sim=SimBroker(slip=0.25); ex=Executor(sim,ExecConfig(live_books=books,stale_after=pd.Timedelta('10min')))
m1=fr['1m']; ev=[]
for t in m1.loc[f'{day} 08:00':f'{day} 16:45'].index:
    r=m1.loc[t]; sim.on_bar(t,r.open,r.high,r.low,r.close)
    cut=t+pd.Timedelta('1min')
    sl={k:f[(f.index<cut)&(f.index>=cut-pd.Timedelta(days=4 if k!='1h' else 300))] for k,f in fr.items()}
    v,_=engine_view(Market.from_1h(sl['1h'],{k:sl[k] for k in ('1m','5m','15m')},base_tf='1m'),p,lookback_days=3)
    for msg in ex.step(v,wall=cut+pd.Timedelta(seconds=3)):
        if msg.startswith(('PLACE','CANCEL','REPRICE','FLATTEN','KILL')): ev.append((str(cut.time())[:5],msg))
for tm,msg in ev: print(tm,msg)
print('SIM TRADES'); 
for x in sim.trades: print({k:(round(v,2) if isinstance(v,float) else str(v)) for k,v in x.items()})
json.dump({'events':[(tm,msg) for tm,msg in ev],'trades':[{k:(float(v) if hasattr(v,'__float__') and not isinstance(v,str) else str(v)) for k,v in x.items()} for x in sim.trades],'closes':{str(t.time())[:5]:float(m1.loc[t].close) for t in m1.loc[f'{day} 08:00':f'{day} 16:45'].index}},open(f'{WORK}/sched/{day}.json','w'))
print(f'saved {WORK}/sched/{day}.json (prices are roll-adjusted; run_days.py converts them)')
