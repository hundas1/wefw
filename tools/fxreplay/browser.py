"""Low-level helpers that click around FX Replay's web page via the agent-browser CLI.

Nothing here knows about trading strategy; it only knows FX Replay's buttons:
snapshot the page, find a button by its label, read the replay clock, set speed,
play/pause, fill the order ticket and place an order. If FX Replay changes its UI,
this is the file to fix. No passwords live here - log in by hand first.
"""
import json, re, subprocess, time
def dismiss():
    """Close FX Replay's feedback/promo popups that intercept clicks."""
    subprocess.run(['agent-browser','eval','[...document.querySelectorAll("section.sfd__bar button, [role=dialog] button")].filter(b=>/^(Later|Close|Dismiss|No thanks)$/i.test(b.innerText.trim())||b.getAttribute("aria-label")=="Close").forEach(b=>b.click())'],capture_output=True,text=True,timeout=30)
def ab(*a, check=True):
    for attempt in range(3):
        r=subprocess.run(['agent-browser',*a],capture_output=True,text=True,timeout=60)
        if r.returncode and 'covered by' in r.stderr and attempt<2:
            dismiss(); time.sleep(0.8); continue
        break
    if check and r.returncode: raise RuntimeError(f"{a}: {r.stderr[-300:]}")
    return r.stdout
def snap(): return ab('snapshot','-i')
def ref(label, text=None, after=None, last=False):
    s=text or snap()
    if after: s=s[s.index(after):]
    hits=[l for l in s.splitlines() if label in l and 'ref=' in l]
    if not hits: raise KeyError(label)
    return re.search(r'ref=(e\d+)',hits[-1 if last else 0]).group(1)
def js(code): return json.loads(ab('eval',code) or 'null')
def clock():
    return js('(document.querySelector("button.fxr-footer-btn")||{}).textContent||""').strip()
def playing():
    return js('!!document.querySelector("[aria-label=\'Play / Pause\'] [class*=pause], [aria-label=\'Play / Pause\'][aria-pressed=true]")')
def set_speed(v):
    js(f'''(()=>{{const e=document.querySelector("input[aria-label=Speed]");const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set;s.call(e,"{v}");e.dispatchEvent(new Event("input",{{bubbles:true}}));e.dispatchEvent(new Event("change",{{bubbles:true}}));return e.value}})()''')
def toggle_play(): ab('click','@'+ref('button "Play / Pause"'))
def fill(label,val):
    r=ref(label); ab('click','@'+r); ab('press','Control+a'); ab('keyboard','type',str(val)); ab('press','Tab')
def place(side,typ,units,sl,tp,entry=None):
    s=snap()
    if 'button "Place order"' not in s:
        ab('click','@'+ref('button "Order"',s,last=True)); time.sleep(1); s=snap()
    ab('click','@'+ref(f'button "{"Buy" if side>0 else "Sell"}"',s,after='Pop out order ticket'))
    ab('click','@'+ref(f'button "{typ}"',s,after='Pop out order ticket')); time.sleep(0.5)
    s=snap()
    for sw in ('switch "Stop loss"','switch "Take profit"'):
        if sw+' [checked=false' in s: ab('click','@'+ref(sw,s))
    time.sleep(0.5)
    if entry is not None: fill('textbox "Entry price',f'{entry:.2f}')
    fill('textbox "Stop loss price',f'{sl:.2f}')
    fill('textbox "Take profit price',f'{tp:.2f}')
    fill('textbox "Units',f'{units:g}')
    time.sleep(0.5)
    form=snap()
    vals={k:re.search(k+r'[^\n]*\]: ([\d.]+)',form) for k in ('Units','Entry price','Stop loss price','Take profit price')}
    vals={k:(float(v.group(1)) if v else None) for k,v in vals.items()}
    ab('click','@'+ref('button "Place order"',form)); time.sleep(1.5)
    return vals
