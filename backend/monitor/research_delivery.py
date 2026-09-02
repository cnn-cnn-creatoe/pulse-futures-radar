"""Normalize, merge and publish year-batch CSV/report artifacts."""
from __future__ import annotations

import argparse
import csv
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean, median
import time

from .research import (Research, _csv_text, backtest_summary_csv,
                       backtest_trades_csv, compact_backtest_result, _iso_utc)


def _read_csv(path: str | Path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        rows = [row for row in reader if any(value not in (None, "") for value in row.values())]
    if not headers:
        raise ValueError(f"CSV 没有表头：{path}")
    return headers, rows


def _write_exact(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content.encode("utf-8"))
    temporary.replace(path)


def _copy_exact(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(source.read_bytes())
    temporary.replace(destination)


def validate_csv_bytes(path: str | Path) -> dict:
    data = Path(path).read_bytes()
    if not data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"CSV 缺少 UTF-8 BOM：{path}")
    if b"\r\r\n" in data:
        raise ValueError(f"CSV 含 CRCRLF：{path}")
    headers, rows = _read_csv(path)
    return {"headers": headers, "rows": len(rows), "bytes": len(data),
            "utf8_bom": True, "crcrlf": False}


def normalize_csv(path: str | Path) -> dict:
    path = Path(path)
    headers, rows = _read_csv(path)
    _write_exact(path, _csv_text(headers, rows))
    return validate_csv_bytes(path)


def merge_csv(paths: list[str | Path], destination: str | Path) -> dict:
    if not paths:
        raise ValueError("至少需要一个 CSV")
    headers = None
    combined = []
    for path in paths:
        current, rows = _read_csv(path)
        if headers is None:
            headers = current
        elif current != headers:
            raise ValueError(f"CSV 表头不一致：{path}")
        combined.extend(rows)
    destination = Path(destination)
    _write_exact(destination, _csv_text(headers, combined))
    return validate_csv_bytes(destination)


def _number(value):
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def derive_long_event_study(report_path: str | Path, trades_path: str | Path,
                            events_path: str | Path, summary_path: str | Path) -> dict:
    """Derive one de-duplicated Supertrend+SMC long-only event study.

    Existing train/holdout/walk-forward ledgers intentionally overlap.  The
    causal signal close is the stable event key; duplicates must agree on entry
    and every forward result or delivery fails rather than silently choosing.
    """
    wrapper = json.loads(Path(report_path).read_text("utf-8"))
    result = wrapper["result"]
    dataset, config = result["dataset"], result.get("indicator_config", {})
    _, ledger = _read_csv(trades_path)
    selected = [row for row in ledger
                if row.get("指标编号") == "supertrend_smc"
                and row.get("策略口径", "").startswith("K线价格指标消融")
                and row.get("方向") == "做多"]
    priority = {"训练集": 0, "时间外验证集": 1, "滚动时间外验证": 2}
    unique = {}
    compare_fields = ("参考", "入场UTC", "入场价", "两根K线形态", "异常倍数",
                      "前瞻1根净收益%", "前瞻2根净收益%", "前瞻3根净收益%", "前瞻5根净收益%")
    # ``参考`` is a harmless sentinel so the tuple cannot accidentally be empty.
    for row in selected:
        key = (row.get("标的"), row.get("周期"), row.get("信号收线UTC"))
        comparable = ("next_open",) + tuple(row.get(field, "") for field in compare_fields[1:])
        if key in unique:
            existing = unique[key]
            prior_comparable = ("next_open",) + tuple(existing.get(field, "") for field in compare_fields[1:])
            if comparable != prior_comparable:
                raise ValueError(f"同一买点在重复验证分段中的结果不一致：{key}")
            if priority.get(row.get("验证分段"), 99) < priority.get(existing.get("验证分段"), 99):
                unique[key] = row
        else:
            unique[key] = row
    costs = result.get("indicator_study", {}).get("execution_assumptions", {})
    fee = costs.get("fee_bps_per_side", result.get("quality", {}).get("fee_bps_per_side", ""))
    slip = costs.get("slippage_bps_per_side", result.get("quality", {}).get("slippage_bps_per_side", ""))
    parameters = (f"Supertrend ATR周期={config.get('atr_period', 10)}，倍数={config.get('atr_multiplier', 3)}，"
                  f"ATR方法={config.get('atr_method', 'wilder')}；SMC内部长度={config.get('smc_internal_length', 5)}，"
                  f"摆动长度={config.get('smc_swing_length', 50)}；两指标必须同时看多")
    cost_basis = f"参考买入和观察收盘均计单边滑点{slip}bps，并计买卖双边手续费各{fee}bps；成本后收益>0计胜"
    event_rows = []
    for _, row in sorted(unique.items(), key=lambda item: item[0][2]):
        event = {
            "标的": row.get("标的", dataset.get("symbol", "")), "周期": row.get("周期", dataset.get("timeframe", "")),
            "数据起始UTC": _iso_utc(dataset.get("first_close_time")),
            "数据结束UTC": _iso_utc(dataset.get("last_close_time")),
            "信号收线UTC": row.get("信号收线UTC", ""), "参考买入UTC": row.get("入场UTC", ""),
            "参考买入价（含买入滑点）": row.get("入场价", ""), "参考买入规则": "信号收线后的下一根K线开盘",
            "两根K线形态": row.get("两根K线形态", ""), "异常倍数": row.get("异常倍数", ""),
            "活跃度": row.get("活跃度", ""), "Supertrend判断": "看多", "SMC判断": "看多",
            "固定参数": parameters, "研究口径": "K线价格指标消融；严格因果；不含做空和卖点",
            "成本口径": cost_basis, "政策哈希": row.get("政策哈希", result.get("policy_hash", "")),
        }
        for horizon in (1, 2, 3, 5):
            value = _number(row.get(f"前瞻{horizon}根净收益%"))
            event[f"后{horizon}根K线成本后收益%"] = "" if value is None else value
            event[f"后{horizon}根K线结果"] = "" if value is None else "胜" if value > 0 else "负或持平"
        event_rows.append(event)
    event_headers = ["标的", "周期", "数据起始UTC", "数据结束UTC", "信号收线UTC", "参考买入UTC",
                     "参考买入价（含买入滑点）", "参考买入规则", "两根K线形态", "异常倍数", "活跃度",
                     "Supertrend判断", "SMC判断", "固定参数", "研究口径", "成本口径",
                     "后1根K线成本后收益%", "后1根K线结果", "后2根K线成本后收益%", "后2根K线结果",
                     "后3根K线成本后收益%", "后3根K线结果", "后5根K线成本后收益%", "后5根K线结果", "政策哈希"]
    _write_exact(Path(events_path), _csv_text(event_headers, event_rows))
    summary_rows = []
    for horizon in (1, 2, 3, 5):
        values = [_number(row[f"后{horizon}根K线成本后收益%"] ) for row in event_rows]
        values = [value for value in values if value is not None]
        wins = sum(value > 0 for value in values)
        enough = len(values) >= 30
        summary_rows.append({
            "标的": dataset.get("symbol", ""), "周期": dataset.get("timeframe", ""),
            "数据起始UTC": _iso_utc(dataset.get("first_close_time")),
            "数据结束UTC": _iso_utc(dataset.get("last_close_time")),
            "信号条件": "Supertrend + SMC 同时看多", "观察窗口": f"买入后第{horizon}根K线收盘",
            "全部去重买点数": len(event_rows), "有效结果数": len(values), "盈利次数": wins,
            "负或持平次数": len(values) - wins,
            "胜率%": "" if not values else wins / len(values) * 100,
            "平均成本后收益%": "" if not values else fmean(values),
            "中位成本后收益%": "" if not values else median(values),
            "最低成本后收益%": "" if not values else min(values),
            "最高成本后收益%": "" if not values else max(values),
            "最低样本要求": 30, "样本是否充足": "是" if enough else "否",
            "结论": ("样本不足，胜率仅作描述，不能据此下单" if not enough
                     else "历史事件统计；仍需未来未见数据验证，不能保证收益"),
            "固定参数": parameters, "研究口径": "每个买点仅一次；next-bar open；只做多；严格因果",
            "成本口径": cost_basis, "政策哈希": result.get("policy_hash", "")})
    summary_headers = ["标的", "周期", "数据起始UTC", "数据结束UTC", "信号条件", "观察窗口",
                       "全部去重买点数", "有效结果数", "盈利次数", "负或持平次数", "胜率%",
                       "平均成本后收益%", "中位成本后收益%", "最低成本后收益%", "最高成本后收益%",
                       "最低样本要求", "样本是否充足", "结论", "固定参数", "研究口径", "成本口径", "政策哈希"]
    _write_exact(Path(summary_path), _csv_text(summary_headers, summary_rows))
    return {"events": validate_csv_bytes(events_path), "summary": validate_csv_bytes(summary_path),
            "unique_buy_points": len(event_rows), "summary_rows": summary_rows}


def derive_directional_entry_study(report_path: str | Path, trades_path: str | Path,
                                   events_path: str | Path, summary_path: str | Path) -> dict:
    """Derive de-duplicated same-direction Supertrend+SMC long and short entries."""
    wrapper = json.loads(Path(report_path).read_text("utf-8"))
    result = wrapper["result"]
    dataset, config = result["dataset"], result.get("indicator_config", {})
    _, ledger = _read_csv(trades_path)
    selected = [row for row in ledger
                if row.get("指标编号") == "supertrend_smc"
                and row.get("策略口径", "").startswith("K线价格指标消融")
                and row.get("方向") in {"做多", "做空"}]
    priority = {"训练集": 0, "时间外验证集": 1, "滚动时间外验证": 2}
    compare_fields = ("入场UTC", "入场价", "两根K线形态", "异常倍数",
                      "前瞻1根净收益%", "前瞻2根净收益%", "前瞻3根净收益%", "前瞻5根净收益%")
    unique = {}
    for row in selected:
        key = (row.get("标的"), row.get("周期"), row.get("方向"), row.get("信号收线UTC"))
        comparable = tuple(row.get(field, "") for field in compare_fields)
        if key in unique:
            existing = unique[key]
            if comparable != tuple(existing.get(field, "") for field in compare_fields):
                raise ValueError(f"同一方向入场点在重复验证分段中的结果不一致：{key}")
            if priority.get(row.get("验证分段"), 99) < priority.get(existing.get("验证分段"), 99):
                unique[key] = row
        else:
            unique[key] = row
    costs = result.get("indicator_study", {}).get("execution_assumptions", {})
    fee = costs.get("fee_bps_per_side", result.get("quality", {}).get("fee_bps_per_side", ""))
    slip = costs.get("slippage_bps_per_side", result.get("quality", {}).get("slippage_bps_per_side", ""))
    parameters = (f"Supertrend ATR周期={config.get('atr_period', 10)}，倍数={config.get('atr_multiplier', 3)}，"
                  f"ATR方法={config.get('atr_method', 'wilder')}；SMC内部长度={config.get('smc_internal_length', 5)}，"
                  f"摆动长度={config.get('smc_swing_length', 50)}；两指标必须同向确认")
    cost_basis = f"参考入场和观察收盘均计单边不利滑点{slip}bps，并计开平双边手续费各{fee}bps；方向调整后的成本后收益>0计胜"
    event_rows = []
    direction_order = {"做多": 0, "做空": 1}
    for _, row in sorted(unique.items(), key=lambda item: (item[0][3], direction_order[item[0][2]])):
        direction = row["方向"]
        judgment = "看多" if direction == "做多" else "看空"
        action = "买入做多" if direction == "做多" else "卖出做空"
        event = {
            "标的": row.get("标的", dataset.get("symbol", "")), "周期": row.get("周期", dataset.get("timeframe", "")),
            "方向": direction, "参考入场动作": action,
            "数据起始UTC": _iso_utc(dataset.get("first_close_time")),
            "数据结束UTC": _iso_utc(dataset.get("last_close_time")),
            "信号收线UTC": row.get("信号收线UTC", ""), "参考入场UTC": row.get("入场UTC", ""),
            "参考入场价（含不利滑点）": row.get("入场价", ""), "参考入场规则": "信号收线后的下一根K线开盘",
            "两根K线形态": row.get("两根K线形态", ""), "异常倍数": row.get("异常倍数", ""),
            "活跃度": row.get("活跃度", ""), "Supertrend判断": judgment, "SMC判断": judgment,
            "固定参数": parameters, "研究口径": "K线价格指标消融；严格因果；只研究入场事件，不研究退出策略",
            "成本口径": cost_basis, "政策哈希": row.get("政策哈希", result.get("policy_hash", "")),
        }
        for horizon in (1, 2, 3, 5):
            value = _number(row.get(f"前瞻{horizon}根净收益%"))
            event[f"后{horizon}根K线成本后收益%"] = "" if value is None else value
            event[f"后{horizon}根K线结果"] = "" if value is None else "胜" if value > 0 else "负或持平"
        event_rows.append(event)
    event_headers = ["标的", "周期", "方向", "参考入场动作", "数据起始UTC", "数据结束UTC", "信号收线UTC",
                     "参考入场UTC", "参考入场价（含不利滑点）", "参考入场规则", "两根K线形态", "异常倍数", "活跃度",
                     "Supertrend判断", "SMC判断", "固定参数", "研究口径", "成本口径",
                     "后1根K线成本后收益%", "后1根K线结果", "后2根K线成本后收益%", "后2根K线结果",
                     "后3根K线成本后收益%", "后3根K线结果", "后5根K线成本后收益%", "后5根K线结果", "政策哈希"]
    _write_exact(Path(events_path), _csv_text(event_headers, event_rows))
    summary_rows = []
    for direction in ("做多", "做空"):
        direction_events = [row for row in event_rows if row["方向"] == direction]
        for horizon in (1, 2, 3, 5):
            values = [_number(row[f"后{horizon}根K线成本后收益%"]) for row in direction_events]
            values = [value for value in values if value is not None]
            wins = sum(value > 0 for value in values)
            enough = len(values) >= 30
            summary_rows.append({
                "标的": dataset.get("symbol", ""), "周期": dataset.get("timeframe", ""), "方向": direction,
                "参考入场动作": "买入做多" if direction == "做多" else "卖出做空",
                "数据起始UTC": _iso_utc(dataset.get("first_close_time")),
                "数据结束UTC": _iso_utc(dataset.get("last_close_time")),
                "信号条件": f"Supertrend + SMC 同时{'看多' if direction == '做多' else '看空'}",
                "观察窗口": f"入场后第{horizon}根K线收盘", "该方向去重入场点数": len(direction_events),
                "有效结果数": len(values), "盈利次数": wins, "负或持平次数": len(values) - wins,
                "胜率%": "" if not values else wins / len(values) * 100,
                "平均成本后收益%": "" if not values else fmean(values),
                "中位成本后收益%": "" if not values else median(values),
                "最低成本后收益%": "" if not values else min(values),
                "最高成本后收益%": "" if not values else max(values),
                "最低样本要求": 30, "样本是否充足": "是" if enough else "否",
                "结论": ("样本不足，胜率仅作描述，不能据此下单" if not enough
                         else "历史入场事件统计；仍需未来未见数据验证，不能保证收益"),
                "固定参数": parameters, "研究口径": "每个方向入场点仅一次；next-bar open；严格因果；不研究退出策略",
                "成本口径": cost_basis, "政策哈希": result.get("policy_hash", "")})
    summary_headers = ["标的", "周期", "方向", "参考入场动作", "数据起始UTC", "数据结束UTC", "信号条件", "观察窗口",
                       "该方向去重入场点数", "有效结果数", "盈利次数", "负或持平次数", "胜率%",
                       "平均成本后收益%", "中位成本后收益%", "最低成本后收益%", "最高成本后收益%",
                       "最低样本要求", "样本是否充足", "结论", "固定参数", "研究口径", "成本口径", "政策哈希"]
    _write_exact(Path(summary_path), _csv_text(summary_headers, summary_rows))
    counts = {direction: sum(row["方向"] == direction for row in event_rows) for direction in ("做多", "做空")}
    return {"events": validate_csv_bytes(events_path), "summary": validate_csv_bytes(summary_path),
            "unique_entries": len(event_rows), "direction_counts": counts, "summary_rows": summary_rows}


def _job_id(result: dict) -> str:
    dataset = result.get("dataset", {})
    identity = "|".join(str(value) for value in (
        "year-backtest-v2", dataset.get("symbol"), dataset.get("timeframe"),
        dataset.get("first_close_time"), dataset.get("last_close_time"),
        result.get("policy_hash")))
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def publish_report(report_path: str | Path, research_root: str | Path) -> dict:
    report_path, research_root = Path(report_path), Path(research_root)
    research_root.mkdir(parents=True, exist_ok=True)
    batch = json.loads(report_path.read_text("utf-8"))
    if batch.get("kind") != "backtest" or batch.get("state") != "completed":
        raise ValueError(f"不是已完成回测报告：{report_path}")
    result = deepcopy(batch["result"])
    dataset = result.get("dataset", {})
    symbol, timeframe = dataset.get("symbol"), dataset.get("timeframe")
    if timeframe not in {"5m", "15m"} or not isinstance(symbol, str):
        raise ValueError("报告不是 5m/15m 单标的结果")
    source_trades = report_path.with_name(report_path.name.replace(".report.json", ".trades.csv"))
    if not source_trades.is_file():
        # Only an uncompacted report can reconstruct a missing trade ledger.
        reconstructed = backtest_trades_csv({"result": result})
        _write_exact(source_trades, reconstructed)
    normalize_csv(source_trades)
    job_id = _job_id(result)
    summary_name, trades_name = f"job-{job_id}.summary.csv", f"job-{job_id}.trades.csv"
    summary_path, trades_path = research_root / summary_name, research_root / trades_name
    job_for_export = {"kind": "backtest", "state": "completed", "result": result}
    _write_exact(summary_path, backtest_summary_csv(job_for_export))
    _copy_exact(source_trades, trades_path)
    validate_csv_bytes(summary_path)
    validate_csv_bytes(trades_path)
    compact_backtest_result(result)
    result["exports"] = {"summary_csv": summary_name, "trades_csv": trades_name,
                         "encoding": "UTF-8 with BOM", "merged": False}
    completed_at = int(report_path.stat().st_mtime * 1000)
    elapsed = 0
    manifest_path = report_path.parent / "batch.manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text("utf-8"))
        elapsed = manifest.get("items", {}).get(f"{symbol}:{timeframe}", {}).get("elapsed_seconds", 0) or 0
    job = {"id": job_id, "kind": "backtest", "state": "completed",
           "created_at": completed_at - int(elapsed * 1000), "completed_at": completed_at,
           "message": f"最近一年 {symbol} {timeframe} 回测完成；含成本与时间外验证",
           "result": result}
    target_report = research_root / f"job-{job_id}.report.json"
    _write_exact(target_report, json.dumps(job, ensure_ascii=False, allow_nan=False))
    return {"id": job_id, "symbol": symbol, "timeframe": timeframe,
            "report": str(target_report), "summary_csv": str(summary_path),
            "trades_csv": str(trades_path)}


def deliver_batch(batch_root: str | Path, research_root: str | Path) -> dict:
    batch_root, research_root = Path(batch_root), Path(research_root)
    reports = sorted(batch_root.glob("*.report.json"))
    if not reports:
        raise ValueError("批次目录没有已完成报告")
    published = [publish_report(path, research_root) for path in reports]
    summary_sources, trade_sources, long_event_sources, long_summary_sources = [], [], [], []
    entry_event_sources, entry_summary_sources = [], []
    long_studies, directional_studies = [], []
    for path, published_item in zip(reports, published):
        batch = json.loads(path.read_text("utf-8"))
        # Regenerate every source summary with the current stable schema.
        summary = path.with_name(path.name.replace(".report.json", ".summary.csv"))
        _write_exact(summary, backtest_summary_csv(batch))
        normalize_csv(summary)
        trades = path.with_name(path.name.replace(".report.json", ".trades.csv"))
        normalize_csv(trades)
        summary_sources.append(summary)
        trade_sources.append(trades)
        long_events = path.with_name(path.name.replace(".report.json", ".long-events.csv"))
        long_summary = path.with_name(path.name.replace(".report.json", ".long-summary.csv"))
        long_meta = derive_long_event_study(path, trades, long_events, long_summary)
        long_event_sources.append(long_events)
        long_summary_sources.append(long_summary)
        long_studies.append({"timeframe": published_item["timeframe"],
                             "unique_buy_points": long_meta["unique_buy_points"],
                             "summary_rows": long_meta["summary_rows"]})
        entry_events = path.with_name(path.name.replace(".report.json", ".entry-events.csv"))
        entry_summary = path.with_name(path.name.replace(".report.json", ".entry-summary.csv"))
        entry_meta = derive_directional_entry_study(path, trades, entry_events, entry_summary)
        entry_event_sources.append(entry_events)
        entry_summary_sources.append(entry_summary)
        directional_studies.append({"timeframe": published_item["timeframe"],
                                    "unique_entries": entry_meta["unique_entries"],
                                    "direction_counts": entry_meta["direction_counts"],
                                    "summary_rows": entry_meta["summary_rows"]})
        research_events_name = f"job-{published_item['id']}.long-events.csv"
        research_summary_name = f"job-{published_item['id']}.long-summary.csv"
        _copy_exact(long_events, research_root / research_events_name)
        _copy_exact(long_summary, research_root / research_summary_name)
        research_entry_events_name = f"job-{published_item['id']}.entry-events.csv"
        research_entry_summary_name = f"job-{published_item['id']}.entry-summary.csv"
        _copy_exact(entry_events, research_root / research_entry_events_name)
        _copy_exact(entry_summary, research_root / research_entry_summary_name)
        saved_job_path = Path(published_item["report"])
        saved_job = json.loads(saved_job_path.read_text("utf-8"))
        saved_job["result"].setdefault("exports", {}).update(
            long_events_csv=research_events_name, long_summary_csv=research_summary_name,
            entry_events_csv=research_entry_events_name, entry_summary_csv=research_entry_summary_name)
        saved_job["result"]["long_only_event_study"] = {
            "signal": "Supertrend + SMC both bullish", "direction": "long_only",
            "entry": "next_bar_open", "horizons": [1, 2, 3, 5],
            "deduplicated_buy_points": long_meta["unique_buy_points"],
            "sample_sufficient": long_meta["unique_buy_points"] >= 30,
            "warning": "样本不足，胜率仅作描述，不能据此下单" if long_meta["unique_buy_points"] < 30 else "仍需未来未见数据验证"}
        saved_job["result"]["directional_entry_study"] = {
            "signal": "Supertrend + SMC same direction", "directions": ["long", "short"],
            "entry": "next_bar_open", "horizons": [1, 2, 3, 5],
            "deduplicated_entries": entry_meta["unique_entries"],
            "direction_counts": entry_meta["direction_counts"],
            "exit_strategy_evaluated": False,
            "warning": "各方向少于30个样本时，胜率仅作描述，不能据此下单"}
        _write_exact(saved_job_path, json.dumps(saved_job, ensure_ascii=False, allow_nan=False))
    first = json.loads(reports[0].read_text("utf-8"))["result"]["dataset"]
    last = json.loads(reports[-1].read_text("utf-8"))["result"]["dataset"]
    symbol = first["symbol"] if all(item["symbol"] == first["symbol"] for item in published) else "multi-symbol"
    start, end = min(first["first_close_time"], last["first_close_time"]), max(first["last_close_time"], last["last_close_time"])
    stem = f"{symbol}-5m_15m-{start}_{end}"
    merged_summary, merged_trades = batch_root / f"{stem}.summary.csv", batch_root / f"{stem}.trades.csv"
    summary_meta = merge_csv(summary_sources, merged_summary)
    trades_meta = merge_csv(trade_sources, merged_trades)
    research_summary, research_trades = research_root / merged_summary.name, research_root / merged_trades.name
    _copy_exact(merged_summary, research_summary)
    _copy_exact(merged_trades, research_trades)
    merged_long_events = batch_root / f"{stem}.long-events.csv"
    merged_long_summary = batch_root / f"{stem}.long-summary.csv"
    long_events_meta = merge_csv(long_event_sources, merged_long_events)
    long_summary_meta = merge_csv(long_summary_sources, merged_long_summary)
    _copy_exact(merged_long_events, research_root / merged_long_events.name)
    _copy_exact(merged_long_summary, research_root / merged_long_summary.name)
    merged_entry_events = batch_root / f"{stem}.entry-events.csv"
    merged_entry_summary = batch_root / f"{stem}.entry-summary.csv"
    entry_events_meta = merge_csv(entry_event_sources, merged_entry_events)
    entry_summary_meta = merge_csv(entry_summary_sources, merged_entry_summary)
    _copy_exact(merged_entry_events, research_root / merged_entry_events.name)
    _copy_exact(merged_entry_summary, research_root / merged_entry_summary.name)
    delivery = {"published_jobs": published,
                "merged_summary_csv": str(merged_summary), "merged_trades_csv": str(merged_trades),
                "merged_summary": summary_meta, "merged_trades": trades_meta,
                "long_only_studies": long_studies,
                "merged_long_events_csv": str(merged_long_events),
                "merged_long_summary_csv": str(merged_long_summary),
                "merged_long_events": long_events_meta, "merged_long_summary": long_summary_meta,
                "directional_entry_studies": directional_studies,
                "merged_entry_events_csv": str(merged_entry_events),
                "merged_entry_summary_csv": str(merged_entry_summary),
                "merged_entry_events": entry_events_meta, "merged_entry_summary": entry_summary_meta,
                "research_reports_visible": len(Research(research_root).reports()) >= len(published),
                "delivered_at": int(time.time() * 1000)}
    _write_exact(batch_root / "delivery.manifest.json",
                 json.dumps(delivery, ensure_ascii=False, allow_nan=False, indent=2))
    return delivery


def main(argv=None):
    parser = argparse.ArgumentParser(description="归一化、合并并发布最近一年回测 CSV")
    parser.add_argument("--batch-root", required=True)
    parser.add_argument("--research-root", required=True)
    args = parser.parse_args(argv)
    result = deliver_batch(args.batch_root, args.research_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
