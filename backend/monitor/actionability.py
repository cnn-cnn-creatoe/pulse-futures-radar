"""Causal structural ranking, separate from liquidity verification and win probability."""
import math


def finite(value):
    return isinstance(value, (int,float)) and not isinstance(value,bool) and math.isfinite(value)


def assess_inputs(signal, values, plan, position):
    result=dict(version=2,score=None,label="数据不足",blockers=[],components={},research_only=True,probability=False,
                basis="结构分 / 100；盘口验证独立显示",execution_status="待验证",missing=[])
    blockers=result["blockers"]
    atr=values.get("atr")
    closes=values.get("closes") or []
    if values.get("status")!="ready" or not finite(atr) or atr<=0 or len(closes)<4 or not all(finite(c) and c>0 for c in closes) or values.get("as_of")!=signal.get("close_time"):
        blockers.append("缺少信号当时连续已收盘 K 线或 ATR")
        return result
    spread,liquidity=signal.get("spread_bps"),signal.get("liquidity_percentile")
    if not finite(spread) or spread<0: result["missing"].append("当时价差")
    elif spread>8: blockers.append("价差超过 8 bps")
    if not finite(liquidity) or not 0<=liquidity<=1:
        bounds=signal.get('liquidity_bounds')
        valid=isinstance(bounds,(list,tuple)) and len(bounds)==2 and all(finite(v) for v in bounds) and 0<=bounds[0]<=bounds[1]<=1
        if valid:
            result['liquidity_bounds']=list(bounds)
            if bounds[1]<.4:blockers.append('当时流动性分位范围整体低于40%')
            elif bounds[0]<.4:result['missing'].append('当时流动性范围跨越40%门槛，无法确定')
        else:result["missing"].append("当时流动性分位")
    elif liquidity<.4: blockers.append("流动性分位低于 40%")
    result["execution_status"]="不通过" if blockers else "待验证" if result["missing"] else "通过"
    result['execution_reasons']=blockers[:]
    if not plan or not finite(plan.get("risk_reward")) or not finite(plan.get("stop_loss")):
        result.update(score=0,label="等待结构")
        blockers.append("没有已确认的止损位和目标空间")
        return result
    rr=plan["risk_reward"]
    extension=abs(closes[-1]-closes[-4])/atr
    risk_atr=abs(closes[-1]-plan["stop_loss"])/atr
    if rr<1.5: blockers.append("成本后目标 / 风险不足 1.5")
    if position and position.get("blocked"): blockers.append(position.get("advice") or "位置不适合追价")
    if extension>3: blockers.append("最近三根累计位移超过 3 ATR，等待回落")
    if risk_atr>3: blockers.append("结构止损距离超过 3 ATR")
    if risk_atr<.25: blockers.append("结构止损距离小于 0.25 ATR，容易被正常波动触发")
    direction=plan.get("direction")
    aligned=values.get("trend")==({"long":"up","short":"down"}.get(direction,"unknown"))
    parts=dict(space=round(max(0,min(1,rr/3))*40,1),
               timing=round(max(0,1-max(0,extension-.5)/3.5)*30,1),
               risk_distance=round(max(0,1-max(0,risk_atr-1)/3)*20,1),
               trend=10 if aligned else 0)
    raw=round(sum(parts.values()),1)
    result.update(score=min(raw,59) if blockers else raw,raw_score=raw,components=parts,
                  label="暂不操作" if blockers else "结构候选 · 盘口待验证" if result["missing"] else "结构候选",
                  extension_atr=round(extension,3),stop_atr=round(risk_atr,3),cost_adjusted_rr=round(rr,3),
                  explanation="结构分不是胜率。空间40、时机30、止损距离20、趋势10；风险阻断时最高59分。缺失盘口不补造，独立标为待验证。")
    return result


def assess(signal,candles,context,plan,position):
    values=dict(atr=context.get("supertrend",{}).get("atr"),status=context.get("status"),
                trend=context.get("supertrend",{}).get("direction"),closes=[c.close for c in candles[-4:]],
                as_of=candles[-1].close_time if candles else None)
    return assess_inputs(signal,values,plan,position)


def score_projection(projection):
    # Small immutable inputs only; avoid decoding complete indicator histories in list APIs.
    fields={"status":"$.indicators.status","atr":"$.indicators.supertrend.atr","trend":"$.indicators.supertrend.direction","as_of":"$.candles[#-1].close_time"}
    pairs=",".join("'"+key+"',json_extract(payload,'"+path+"')" for key,path in fields.items())
    closes=",".join("json_extract(payload,'$.candles[#-"+str(i)+"].close')" for i in (4,3,2,1))
    return "json_set("+projection+",'$.actionability_inputs',COALESCE(json_extract(payload,'$.actionability_inputs'),json_object("+pairs+",'closes',json_array("+closes+"))))"


def refresh_saved(signal):
    values=signal.pop("actionability_inputs",None)
    if (signal.get("actionability") or {}).get("version")==2: return signal
    if values is None:
        context=signal.get("indicators") or signal.get("indicator_context") or {}
        candles=[c for c in signal.get("candles",[]) if isinstance(c,dict)]
        values=dict(status=context.get("status"),atr=context.get("supertrend",{}).get("atr"),trend=context.get("supertrend",{}).get("direction"),
                    as_of=candles[-1].get("close_time") if candles else None,closes=[c.get("close") for c in candles[-4:]])
    score=assess_inputs(signal,values,signal.get("trade_plan"),signal.get("position_assessment"))
    score["recalculated_from_saved_snapshot"]=True
    signal["actionability"]=score
    return signal
