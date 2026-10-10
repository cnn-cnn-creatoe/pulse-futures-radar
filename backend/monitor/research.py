"""Local-only bounded dataset and research-job storage, separate from live alerts."""
import asyncio
from dataclasses import asdict
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import time
import uuid
from .history_data import download_binance_month, import_history, validate_series
from .backtest import run_backtest
from .policy import Settings

MAX_RESEARCH_BARS = 120_000

POLICY_LABELS = {
    "strict_full_policy": "完整实时门槛（历史字段缺失时严格拒绝）",
    "price_only_ablation": "K线价格指标消融（不代表完整实盘策略）",
}
PARTITION_LABELS = {"train": "训练集", "holdout": "时间外验证集",
                    "walk_forward": "滚动时间外验证"}
EXIT_LABELS = {"target": "到达目标位", "stop_loss": "触发止损",
               "stop_first_ambiguous": "同根触及止盈止损，按止损计",
               "max_holding": "达到最长持有", "end_of_data": "数据结束"}


def _iso_utc(value):
    if not isinstance(value, (int, float)):
        return ""
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _csv_text(headers, rows):
    buffer = io.StringIO(newline="")
    # Use a single LF in memory. Path.write_text translates it once on Windows;
    # emitting csv's default CRLF here would otherwise become CRCRLF and show
    # an empty spreadsheet row between every result.
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return "\ufeff" + buffer.getvalue()


def _source_label(result):
    provenance = result.get("provenance", {}).get("provenance", result.get("provenance", {}))
    return provenance.get("type", "") if isinstance(provenance, dict) else ""


def _sample_conclusion(metrics, sufficient):
    if not sufficient:
        return "样本不足，不能据此下单"
    expectancy = metrics.get("expectancy_net_pct")
    if expectancy is None or expectancy <= 0:
        return "样本达标但扣除成本后期望不为正"
    return "该历史分段扣除成本后为正；仍需未来未见数据验证"


def backtest_summary_csv(job):
    """Return a Chinese, spreadsheet-safe summary for all 7 predeclared rows."""
    result = job.get("result", job)
    dataset = result.get("dataset", {})
    study = result.get("indicator_study", {})
    execution = study.get("execution_assumptions", {})
    rows = []
    for indicator in study.get("rows", []):
        policies = indicator.get("policies") or {
            indicator.get("evaluation_policy", "price_only_ablation"): indicator}
        for policy_name, policy in policies.items():
            for partition_name in ("train", "holdout", "walk_forward"):
                partition = policy.get(partition_name)
                if not isinstance(partition, dict):
                    continue
                metrics = partition.get("metrics", {})
                trades = metrics.get("trades", 0) or 0
                wins = metrics.get("wins", 0) or 0
                sufficient = bool(partition.get("sample_sufficient"))
                rows.append({
                    "标的": dataset.get("symbol", ""), "周期": dataset.get("timeframe", ""),
                    "数据起始UTC": _iso_utc(dataset.get("first_close_time")),
                    "数据结束UTC": _iso_utc(dataset.get("last_close_time")),
                    "K线根数": dataset.get("bars", ""), "数据来源": _source_label(result),
                    "指标编号": indicator.get("id", ""), "指标组合": indicator.get("label", ""),
                    "指标数量": indicator.get("combination_size", ""),
                    "策略口径": POLICY_LABELS.get(policy_name, policy_name),
                    "验证分段": PARTITION_LABELS[partition_name],
                    "异常倍数门槛": study.get("threshold", ""),
                    "成交笔数": trades, "胜笔": wins, "负笔": max(0, trades - wins),
                    "胜率%": "" if metrics.get("win_rate") is None else metrics["win_rate"] * 100,
                    "单笔净期望%": metrics.get("expectancy_net_pct", ""),
                    "盈利因子": metrics.get("profit_factor", ""),
                    "最大回撤%": metrics.get("max_drawdown_pct", ""),
                    "假设1倍复投收益%": metrics.get("hypothetical_fixed_1x_return_pct", ""),
                    "平均持仓K线": metrics.get("average_holding_bars", ""),
                    "累计手续费%": metrics.get("fees_pct_sum", ""),
                    "单边手续费bps": execution.get("fee_bps_per_side", ""),
                    "单边滑点bps": execution.get("slippage_bps_per_side", ""),
                    "样本是否充足": "是" if sufficient else "否",
                    "滚动折数": partition.get("fold_count", ""),
                    "每折最低笔数": partition.get("minimum_closed_trades_per_fold", ""),
                    "最低总笔数": partition.get("minimum_total_closed_trades",
                                         partition.get("minimum_closed_trades", "")),
                    "各折均充足": ("是" if partition.get("sample_sufficient_all_folds") else "否"
                                   if "sample_sufficient_all_folds" in partition else ""),
                    "结论": _sample_conclusion(metrics, sufficient),
                    "政策哈希": result.get("policy_hash", ""),
                })
    headers = ["标的", "周期", "数据起始UTC", "数据结束UTC", "K线根数", "数据来源",
               "指标编号", "指标组合", "指标数量", "策略口径", "验证分段", "异常倍数门槛",
               "成交笔数", "胜笔", "负笔", "胜率%", "单笔净期望%", "盈利因子", "最大回撤%",
               "假设1倍复投收益%", "平均持仓K线", "累计手续费%", "单边手续费bps",
               "单边滑点bps", "样本是否充足", "滚动折数", "每折最低笔数", "最低总笔数",
               "各折均充足", "结论", "政策哈希"]
    return _csv_text(headers, rows)


def _trade_rows(result):
    dataset = result.get("dataset", {})
    study = result.get("indicator_study", {})
    common = {"标的": dataset.get("symbol", ""), "周期": dataset.get("timeframe", ""),
              "政策哈希": result.get("policy_hash", "")}
    found = False
    for indicator in study.get("rows", []):
        for policy_name, policy in (indicator.get("policies") or {}).items():
            for partition_name in ("train", "holdout"):
                for trade in policy.get(partition_name, {}).get("trades", []) or []:
                    found = True
                    yield common | _trade_row(indicator, policy_name, PARTITION_LABELS[partition_name], "", trade)
            walk = policy.get("walk_forward", {})
            for fold in walk.get("folds", []) or []:
                for trade in fold.get("validation", {}).get("trades", []) or []:
                    found = True
                    yield common | _trade_row(indicator, policy_name, PARTITION_LABELS["walk_forward"],
                                              fold.get("fold", ""), trade)
    if found:
        return
    # Compatibility for reports produced before per-indicator ledgers existed.
    for trade in result.get("strategy", {}).get("price_only_ablation", {}).get("trades", []) or []:
        yield common | _trade_row({"id": "legacy_policy", "label": "旧版全策略"},
                                  "price_only_ablation", "全样本", "", trade)


def _trade_row(indicator, policy_name, partition, fold, trade):
    direction = {"long": "做多", "short": "做空"}.get(trade.get("direction"), trade.get("direction", ""))
    return {
        "指标编号": indicator.get("id", ""), "指标组合": indicator.get("label", ""),
        "策略口径": POLICY_LABELS.get(policy_name, policy_name), "验证分段": partition,
        "滚动折": fold, "信号收线UTC": _iso_utc(trade.get("signal_close_time")),
        "入场UTC": _iso_utc(trade.get("entry_time")), "出场UTC": _iso_utc(trade.get("exit_time")),
        "方向": direction, "两根K线形态": trade.get("pattern", ""), "设置": trade.get("setup", ""),
        "异常倍数": trade.get("volatility_ratio", ""), "活跃度": trade.get("activity_score", ""),
        "入场价": trade.get("entry_price", ""), "出场价": trade.get("exit_price", ""),
        "止损价": trade.get("stop_loss", ""), "目标价": trade.get("target", ""),
        "出场原因": EXIT_LABELS.get(trade.get("exit_reason"), trade.get("exit_reason", "")),
        "持仓K线": trade.get("holding_bars", ""),
        "滑点后毛收益%": trade.get("gross_return_after_slippage_pct", ""),
        "手续费%": trade.get("fee_pct", ""), "净收益%": trade.get("net_return_pct", ""),
        "最大有利波动%": trade.get("mfe_pct", ""), "最大不利波动%": trade.get("mae_pct", ""),
        "前瞻1根净收益%": trade.get("forward_returns_net_pct", {}).get("1", ""),
        "前瞻2根净收益%": trade.get("forward_returns_net_pct", {}).get("2", ""),
        "前瞻3根净收益%": trade.get("forward_returns_net_pct", {}).get("3", ""),
        "前瞻5根净收益%": trade.get("forward_returns_net_pct", {}).get("5", ""),
    }


def backtest_trades_csv(job):
    result = job.get("result", job)
    headers = ["标的", "周期", "指标编号", "指标组合", "策略口径", "验证分段", "滚动折",
               "信号收线UTC", "入场UTC", "出场UTC", "方向", "两根K线形态", "设置", "异常倍数",
               "活跃度", "入场价", "出场价", "止损价", "目标价", "出场原因", "持仓K线",
               "滑点后毛收益%", "手续费%", "净收益%", "最大有利波动%", "最大不利波动%",
               "前瞻1根净收益%", "前瞻2根净收益%", "前瞻3根净收益%", "前瞻5根净收益%", "政策哈希"]
    return _csv_text(headers, list(_trade_rows(result)))


def compact_backtest_result(result):
    """Remove ledgers after durable CSV creation while retaining every metric."""
    removed = 0
    for strategy in result.get("strategy", {}).values():
        if isinstance(strategy, dict):
            removed += len(strategy.pop("trades", []) or [])
    for indicator in result.get("indicator_study", {}).get("rows", []):
        for policy in (indicator.get("policies") or {}).values():
            for name in ("train", "holdout"):
                partition = policy.get(name, {})
                removed += len(partition.pop("trades", []) or [])
            walk = policy.get("walk_forward", {})
            removed += len(walk.pop("trades", []) or [])
            for fold in walk.get("folds", []) or []:
                for name in ("train", "validation"):
                    partition = fold.get(name, {})
                    removed += len(partition.pop("trades", []) or [])
        # Primary aliases refer to the policy objects above, but tolerate
        # deserialized/copy-based reports too.
        for name in ("train", "holdout"):
            partition = indicator.get(name, {})
            removed += len(partition.pop("trades", []) or [])
        walk = indicator.get("walk_forward", {})
        removed += len(walk.pop("trades", []) or [])
        for fold in walk.get("folds", []) or []:
            removed += len(fold.get("validation", {}).pop("trades", []) or [])
    result["trade_ledger"] = {"storage": "separate_csv", "removed_embedded_rows": removed}
    return result

class Research:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.jobs = {}
        self.tasks = set()
        self.busy = False

    def datasets(self):
        result = []
        for path in self.root.glob('*.dataset.json'):
            try:
                result.append(json.loads(path.read_text('utf-8')))
            except (OSError,ValueError):
                continue
        return sorted(result,key=lambda x:x.get('created_at',0),reverse=True)

    def register(self,candles,provenance,label=None):
        info = validate_series(candles)
        if not candles or len(candles)>MAX_RESEARCH_BARS:
            raise ValueError(f'每个数据集需有 1～{MAX_RESEARCH_BARS:,} 根已收盘 K 线')
        raw = json.dumps([asdict(c) for c in candles],ensure_ascii=False,separators=(',',':'))
        ident = hashlib.sha256(raw.encode()).hexdigest()[:24]
        record = {**info,'id':ident,'label':label or f'{info["symbol"]} · {info["timeframe"]} · {info["bars"]:,} 根',
                  'created_at':int(time.time()*1000),'sha256':hashlib.sha256(raw.encode()).hexdigest(),
                  'provenance':provenance,'has_historical_oi':False,'has_historical_spread':False}
        self._atomic_write(self.root/f'{ident}.json',raw)
        self._atomic_write(self.root/f'{ident}.dataset.json',json.dumps(record,ensure_ascii=False,indent=2))
        return record

    @staticmethod
    def _atomic_write(path,content):
        temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        # Write exact bytes: on Windows text mode would expand CSV CRLF into
        # CRCRLF and create apparent blank rows in spreadsheet programs.
        temporary.write_bytes(content.encode('utf-8'))
        temporary.replace(path)

    def load(self,ident):
        if not re.fullmatch(r'[a-f0-9]{24}',ident):
            raise ValueError('无效的数据集 ID')
        manifest = self.root/f'{ident}.dataset.json'
        if not manifest.exists():
            raise ValueError('数据集不存在')
        meta = json.loads(manifest.read_text('utf-8'))
        source = self.root/f'{ident}.json'
        if hashlib.sha256(source.read_bytes()).hexdigest()!=meta['sha256']:
            raise ValueError('本地数据校验失败，请重新导入')
        return import_history(source,meta['symbol'],meta['timeframe']),meta

    def start(self,kind,payload,settings):
        if self.busy:
            raise ValueError('已有下载或回测正在运行，请等待当前任务完成')
        self.busy = True
        ident = uuid.uuid4().hex
        job = {'id':ident,'kind':kind,'state':'running','created_at':int(time.time()*1000),'message':'正在下载校验历史数据' if kind=='download' else '正在按时间顺序回测；不读取未来 K 线'}
        self.jobs[ident] = job
        while len(self.jobs)>12:
            self.jobs.pop(next(iter(self.jobs)))
        task = asyncio.create_task(self._execute(job,payload,settings.model_dump()))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job.copy()

    async def _execute(self,job,payload,snapshot):
        try:
            result = await asyncio.to_thread(self._work,job['kind'],payload,snapshot)
            job.update(state='completed',result=result,message='完成',completed_at=int(time.time()*1000))
            if job["kind"] == "backtest":
                summary_name = f'job-{job["id"]}.summary.csv'
                trades_name = f'job-{job["id"]}.trades.csv'
                summary_csv = backtest_summary_csv(job)
                trades_csv = backtest_trades_csv(job)
                self._atomic_write(self.root / summary_name, summary_csv)
                self._atomic_write(self.root / trades_name, trades_csv)
                compact_backtest_result(result)
                result["exports"] = {"summary_csv": summary_name, "trades_csv": trades_name,
                                     "encoding": "UTF-8 with BOM"}
            # Completed research reports survive a browser refresh/server restart.
            self._atomic_write(self.root/f'job-{job["id"]}.report.json',
                               json.dumps(job,ensure_ascii=False,allow_nan=False))
        except Exception as error:
            job.update(state='failed',message=str(error)[:500],completed_at=int(time.time()*1000))
        finally:
            self.busy = False

    def _work(self,kind,payload,snapshot):
        if kind=='download':
            candles, sources = [],[]
            for month in sorted(set(payload['months'])):
                data = download_binance_month(payload['symbol'],payload['timeframe'],month,self.root/'archives')
                candles.extend(data['candles'])
                sources.append(data['metadata'])
            return self.register(candles,{'type':'binance_official_archive','sources':sources,'verified_checksum':True})
        candles,meta = self.load(payload['dataset_id'])
        from .policy import migrate_saved_settings
        settings = Settings(**migrate_saved_settings(snapshot))
        result = run_backtest(candles,settings.engine(),settings.indicator_config(),settings.quality.model_dump(),
            {'max_holding_bars':payload['max_holding_bars'],'compare_thresholds':True,
             'threshold_profile':settings.threshold_profile,'fee_bps_per_side':settings.quality.fee_bps_per_side,
             'slippage_bps_per_side':settings.quality.slippage_bps_per_side})
        result['provenance'] = meta
        result['policy_snapshot'] = snapshot
        result['policy_hash'] = settings.fingerprint()
        return result

    def get_job(self,ident):
        if not re.fullmatch(r'[a-f0-9]{32}',ident):
            return None
        if ident in self.jobs:
            return self.jobs[ident]
        path = self.root/f'job-{ident}.report.json'
        return json.loads(path.read_text('utf-8')) if path.exists() else None

    def reports(self):
        result=[]
        paths=sorted(self.root.glob('job-*.report.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:12]
        for path in paths:
            try:
                record=json.loads(path.read_text('utf-8'))
                if record.get('kind')=='backtest':
                    summary={key:record.get(key) for key in ('id','kind','state','created_at','completed_at','message')}
                    summary['exports']=record.get('result',{}).get('exports',{})
                    result.append(summary)
            except (OSError,ValueError):
                continue
        return result

    async def close(self):
        # Await active local writes before lifespan closes the application.
        if self.tasks:
            await asyncio.gather(*self.tasks,return_exceptions=True)
