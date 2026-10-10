"""Binance UTC candle boundaries, including Monday weeks and calendar months."""
from datetime import datetime, timezone, timedelta
NATIVE = ("1s", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")
FIXED = {"1s":1000,"1m":60000,"5m":300000,"15m":900000,"30m":1800000,"1h":3600000,"2h":7200000,"4h":14400000,"1d":86400000,"1w":604800000}
def floor_bar(value, tf):
    if tf == "1M":
        d=datetime.fromtimestamp(value/1000,timezone.utc)
        return int(d.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()*1000)
    offset=345600000 if tf=="1w" else 0
    return (value-offset)//FIXED[tf]*FIXED[tf]+offset

def shift(value, tf, bars=1):
    if tf != "1M": return value+bars*FIXED[tf]
    d=datetime.fromtimestamp(value/1000,timezone.utc)
    m=d.year*12+d.month-1+bars
    return int(d.replace(year=m//12,month=m%12+1,day=1).timestamp()*1000)

def ceil_bar(value, tf):
    f=floor_bar(value,tf)
    return f if f==value else shift(f,tf)

def count_bars(first,last,tf):
    if first>last:return 0
    if tf!="1M":return (last-first)//FIXED[tf]+1
    a=datetime.fromtimestamp(first/1000,timezone.utc);b=datetime.fromtimestamp(last/1000,timezone.utc)
    return (b.year-a.year)*12+b.month-a.month+1
