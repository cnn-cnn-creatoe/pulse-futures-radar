"""Read-only smoke check: real Binance stream, fake delivery, temporary DB."""
import asyncio,json,tempfile,time
from pathlib import Path
from types import SimpleNamespace
import aiohttp
from monitor.collector import MonitorService
from monitor.store import Store
from monitor.engine import EngineConfig
from monitor.smc_live import SmcLive
from monitor.telegram import normalize_preferences

async def main():
    store=Store(':memory:');market=MonitorService(store,EngineConfig())
    market._session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),trust_env=True)
    data=await market._request('/fapi/v1/exchangeInfo')
    family=data.get('futuresType')
    market._metadata={r['symbol']:{**r,**({'futuresType':family} if family else {})} for r in data['symbols']}
    p=normalize_preferences(dict(signals_enabled=True,rule_settings=[dict(kind='indicator',rule_id='smc_equity_structure',timeframes=['1m'],symbols=['MUUSDT'])]))
    tg=SimpleNamespace(_accept_events=True,state=SimpleNamespace(subscribers=lambda:[dict(preferences=p)]),enqueue=lambda *args:True)
    with tempfile.TemporaryDirectory() as temp:
        live=SmcLive(market,tg,Path(temp)/'smc.db');live.start()
        try:
            for _ in range(35):
                await asyncio.sleep(1)
                state=live.status()
                if any(i.get('last_event') for i in state['items']):
                    print(json.dumps(dict(ok=True,items=state['items'],observed_age_ms=market.now()-state['items'][0]['last_event']),ensure_ascii=True));break
            else:print(json.dumps(dict(ok=False,status=live.status()),ensure_ascii=True));raise RuntimeError('No live kline within 35 seconds')
        finally:await live.close();await market._session.close();store.close()
asyncio.run(main())
