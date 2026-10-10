"""Thirty-day evidence retention, with lossless compression of recent snapshots."""
import json
import zlib

RETENTION_DAYS = 30
DAY_MS = 86_400_000
DETAIL_FIELDS = ("candles", "indicators", "indicator_context")
DETAIL_SCHEMA = """CREATE TABLE IF NOT EXISTS alert_details (
    event_key TEXT PRIMARY KEY, close_time INTEGER NOT NULL, payload BLOB NOT NULL
)"""
DETAIL_INDEX = "CREATE INDEX IF NOT EXISTS alert_details_close ON alert_details(close_time)"


def candle_cutoff_sql(now_ms):
    # Long-period live indicators need a small warm-up window beyond 30 days.
    # Keep the existing computation window so housekeeping never changes signals.
    from .engine import LOOKBACK, TIMEFRAME_MS
    clauses = []
    for timeframe, window in LOOKBACK.items():
        keep = max(RETENTION_DAYS * DAY_MS, window['days'] * DAY_MS + 3 * TIMEFRAME_MS[timeframe])
        clauses.append("WHEN '" + timeframe + "' THEN " + str(int(now_ms - keep)))
    return 'CASE timeframe ' + ' '.join(clauses) + ' ELSE ' + str(int(now_ms - RETENTION_DAYS * DAY_MS)) + ' END'


def split_alert(payload, *, expired=False, encoder=None):
    """Keep queryable metadata separate; do not mutate the caller's signal."""
    summary = dict(payload)
    if "actionability_inputs" not in summary:
        context = payload.get("indicators") or payload.get("indicator_context") or {}
        candles = [c for c in payload.get("candles", []) if isinstance(c, dict)]
        trend = context.get("supertrend") or {}
        summary["actionability_inputs"] = dict(
            status=context.get("status"), atr=trend.get("atr"), trend=trend.get("direction"),
            as_of=candles[-1].get("close_time") if candles else None,
            closes=[c.get("close") for c in candles[-4:]])
    details = {key: summary.pop(key) for key in DETAIL_FIELDS if key in summary}
    if expired:
        summary = expired_summary(summary)
    encoded = (encoder(summary).decode('utf-8') if encoder else
               json.dumps(summary, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    blob = None
    if not expired:
        raw = (encoder(details) if encoder else
               json.dumps(details, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        blob = zlib.compress(raw, level=1)
    return encoded, blob


def expired_summary(summary):
    summary = dict(summary)
    for key in (*DETAIL_FIELDS, "policy_snapshot", "custom_indicator_snapshot"):
        summary.pop(key, None)
    summary["snapshot_expired"] = True
    return summary


def restore_alert(summary, blob):
    if blob is not None:
        summary = {**summary, **json.loads(zlib.decompress(blob))}
    return summary
