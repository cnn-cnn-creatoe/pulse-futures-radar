"""Calendar-day market overview from closed 5m bars (never rolling 24h)."""
import threading
import time
from .daily_journal import day_start, TZ
from datetime import datetime
def local_day(now): return datetime.fromtimestamp(now/1000,TZ).strftime("%Y-%m-%d")
from .store import is_usdt_cached_identity

class MarketOverview:
    def __init__(self, store):
        self.store=store; self.lock=threading.Lock(); self.cached=None; self.expires=0
    def get(self, now=None):
        now=int(time.time()*1000) if now is None else now
        with self.lock:
            if self.cached and now<self.expires and self.cached['date']==local_day(now):return self.cached
            start=day_start(local_day(now)); cutoff=now//300000*300000
            meta={r['symbol']:r for r in self.store.list_universe(False) if r.get('snapshot_active') and is_usdt_cached_identity(r['symbol'],r)}
            rows=self.store.market_day_rows(start,cutoff)
            items=[]
            for r in rows:
                m=meta.get(r['symbol'])
                if not m:continue
                onboard=int(m.get('onboardDate') or 0)
                begin=max(start,((onboard+299999)//300000)*300000)
                expected=max(0,(cutoff-begin)//300000)
                complete=expected>0 and r['bars']==expected and r['first_time']==begin and r['last_time']==cutoff-300000
                items.append({**r,'change_pct':(r['last_price']/r['open_price']-1)*100 if r['open_price'] else None,'complete':complete})
            valid=[r for r in items if r['complete'] and r['change_pct'] is not None]
            rank=lambda values: sorted(values,key=lambda r:(-r['turnover'],r['symbol']))[:15]
            up=[r for r in valid if r['change_pct']>0];down=[r for r in valid if r['change_pct']<0]
            listings=sorted([dict(symbol=s,onboard=int(m.get('onboardDate') or 0),status=m.get('status',''),upcoming=int(m.get('onboardDate') or 0)>now) for s,m in meta.items() if int(m.get('onboardDate') or 0)>=start-7*86400000],key=lambda r:r['onboard'],reverse=True)
            by_symbol={r['symbol']:r for r in items}
            full=[{**by_symbol.get(symbol,{}),'symbol':symbol,'complete':by_symbol.get(symbol,{}).get('complete',False)} for symbol in sorted(meta)]
            for row in full:
                if not row['complete']:
                    row['turnover']=None;row['change_pct']=None
                row.setdefault('last_price',None)
            self.cached=dict(items=full,date=local_day(now),as_of=cutoff,updated_at=now,total=len(meta),covered=len(valid),partial=len(items)-len(valid),missing=max(0,len(meta)-len(items)),up=len(up),down=len(down),flat=len(valid)-len(up)-len(down),turnover=sum(r['turnover'] for r in valid),positive=rank(up),negative=rank(down),all=rank(valid),listings=listings[:20],leaders=sorted(valid,key=lambda r:r['change_pct'],reverse=True)[:5],laggards=sorted(valid,key=lambda r:r['change_pct'])[:5])
            self.expires=now+30000
            return self.cached
