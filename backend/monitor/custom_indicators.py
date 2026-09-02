"""Safe, causal custom-indicator expressions and an atomic JSON registry.

The expression language is deliberately small.  It is parsed with :mod:`ast`
and interpreted by the functions in this module; Python ``eval``/``exec``,
imports, attributes, subscripts and user-selected call targets are never used.
Only completed, contiguous candle prefixes are accepted, so a rule cannot
refer to a future bar or silently run across a history gap.

Example::

    {
        "id": "rsi_pullback",
        "name": "RSI pullback",
        "description": "Momentum confirmation",
        "long_expression": "rsi(14) < 35 and close > ema(20)",
        "short_expression": "rsi(14) > 65 and close < ema(20)"
    }

Series-aware forms such as ``sma(volume, 20)`` are also supported.  A one
argument moving function uses its conventional source: close for SMA/EMA/RSI,
high for highest and low for lowest.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from statistics import fmean
import tempfile
from threading import RLock
from typing import Any, Mapping, Sequence

from .engine import Candle, TIMEFRAME_MS


REGISTRY_VERSION = 1
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "data" / "custom_indicators.json"
MAX_EXPRESSION_LENGTH = 2_000
MAX_AST_NODES = 128
MAX_AST_DEPTH = 24
MAX_PERIOD = 500
MAX_ABS_CONSTANT = 1_000_000_000_000.0

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_RESERVED_IDS = frozenset({"supertrend", "smc", "ema"})
_SERIES_NAMES = frozenset({"open", "high", "low", "close", "volume"})
_FUNCTION_NAMES = frozenset({"sma", "ema", "rsi", "atr", "highest", "lowest"})
_DEFAULT_SOURCE = {"sma": "close", "ema": "close", "rsi": "close", "highest": "high", "lowest": "low"}
_BINARY_OPERATORS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_COMPARISON_OPERATORS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


class IndicatorValidationError(ValueError):
    """The user-authored indicator specification did not pass validation."""

    def __init__(self, errors: Sequence[Mapping[str, str]]):
        self.errors = [dict(error) for error in errors]
        message = "; ".join(f"{error.get('field', 'spec')}: {error.get('message', 'invalid')}" for error in self.errors)
        super().__init__(message or "invalid indicator specification")


class IndicatorRegistryError(RuntimeError):
    """The registry could not be safely read or written."""


class IndicatorNotValidatedError(IndicatorRegistryError):
    """A draft indicator was requested for execution."""


class _InsufficientHistory(Exception):
    pass


@dataclass(frozen=True)
class _FunctionCall:
    name: str
    source: str | None
    period: int


@dataclass(frozen=True)
class _ParsedExpression:
    source: str
    tree: ast.Expression
    calls: tuple[_FunctionCall, ...]
    required_history: int


def _error(field: str, code: str, message: str) -> dict[str, str]:
    return {"field": field, "code": code, "message": message}


def _text(value: Any, field: str, *, minimum: int, maximum: int, allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise IndicatorValidationError([_error(field, "invalid_type", "必须是文本")])
    result = value.strip()
    if not allow_blank and len(result) < minimum:
        raise IndicatorValidationError([_error(field, "required", "不能为空")])
    if len(result) > maximum:
        raise IndicatorValidationError([_error(field, "too_long", f"最多 {maximum} 个字符")])
    return result


def _normalise_metadata(spec: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(spec, Mapping):
        raise IndicatorValidationError([_error("spec", "invalid_type", "指标必须是对象")])
    indicator_id = _text(spec.get("id"), "id", minimum=2, maximum=64)
    if not _ID_PATTERN.fullmatch(indicator_id):
        raise IndicatorValidationError([_error("id", "invalid_id", "仅支持小写字母、数字和下划线，并以字母开头")])
    if indicator_id in _RESERVED_IDS:
        raise IndicatorValidationError([_error("id", "reserved_id", "该名称属于内置指标")])
    return {
        "id": indicator_id,
        "name": _text(spec.get("name"), "name", minimum=1, maximum=80),
        "description": _text(spec.get("description", ""), "description", minimum=0, maximum=1_000, allow_blank=True),
        "long_expression": _text(spec.get("long_expression", ""), "long_expression", minimum=0, maximum=MAX_EXPRESSION_LENGTH, allow_blank=True),
        "short_expression": _text(spec.get("short_expression", ""), "short_expression", minimum=0, maximum=MAX_EXPRESSION_LENGTH, allow_blank=True),
    }


def _node_depth(node: ast.AST) -> int:
    children = tuple(ast.iter_child_nodes(node))
    return 1 if not children else 1 + max(_node_depth(child) for child in children)


def _period_argument(node: ast.AST, field: str) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        period = node.value
    else:
        raise IndicatorValidationError([_error(field, "invalid_period", "周期必须是固定整数")])
    if not 1 <= period <= MAX_PERIOD:
        raise IndicatorValidationError([_error(field, "invalid_period", f"周期必须在 1 到 {MAX_PERIOD} 之间")])
    return period


class _ExpressionValidator:
    def __init__(self, field: str):
        self.field = field
        self.calls: list[_FunctionCall] = []

    def check(self, node: ast.AST) -> str:
        """Validate *node* and return its static type (``number`` or ``bool``)."""
        if isinstance(node, ast.Expression):
            return self.check(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return "bool"
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                value = float(node.value)
                if math.isfinite(value) and abs(value) <= MAX_ABS_CONSTANT:
                    return "number"
                raise IndicatorValidationError([_error(self.field, "invalid_number", "数值必须有限且不能过大")])
            raise IndicatorValidationError([_error(self.field, "forbidden_syntax", "仅支持数字和布尔常量")])
        if isinstance(node, ast.Name):
            if node.id not in _SERIES_NAMES:
                raise IndicatorValidationError([_error(self.field, "unknown_name", f"不支持名称 {node.id}")])
            return "number"
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _BINARY_OPERATORS):
                raise IndicatorValidationError([_error(self.field, "forbidden_operator", "仅支持 +、-、*、/")])
            if self.check(node.left) != "number" or self.check(node.right) != "number":
                raise IndicatorValidationError([_error(self.field, "type_error", "数值运算两侧必须是数字")])
            return "number"
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                if self.check(node.operand) != "bool":
                    raise IndicatorValidationError([_error(self.field, "type_error", "not 后必须是条件")])
                return "bool"
            if isinstance(node.op, (ast.UAdd, ast.USub)):
                if self.check(node.operand) != "number":
                    raise IndicatorValidationError([_error(self.field, "type_error", "正负号后必须是数字")])
                return "number"
            raise IndicatorValidationError([_error(self.field, "forbidden_operator", "不支持该一元运算")])
        if isinstance(node, ast.BoolOp):
            if not isinstance(node.op, (ast.And, ast.Or)):
                raise IndicatorValidationError([_error(self.field, "forbidden_operator", "仅支持 and、or、not")])
            if any(self.check(value) != "bool" for value in node.values):
                raise IndicatorValidationError([_error(self.field, "type_error", "and/or 两侧必须是条件")])
            return "bool"
        if isinstance(node, ast.Compare):
            if self.check(node.left) != "number" or any(self.check(item) != "number" for item in node.comparators):
                raise IndicatorValidationError([_error(self.field, "type_error", "比较两侧必须是数字")])
            if any(not isinstance(operator, _COMPARISON_OPERATORS) for operator in node.ops):
                raise IndicatorValidationError([_error(self.field, "forbidden_operator", "仅支持常规数值比较")])
            return "bool"
        if isinstance(node, ast.Call):
            return self._check_call(node)
        # Attribute, Subscript, Lambda, comprehensions, containers, formatted
        # strings, assignments and every statement node all stop here.
        raise IndicatorValidationError([_error(self.field, "forbidden_syntax", f"不支持 {type(node).__name__}")])

    def _check_call(self, node: ast.Call) -> str:
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTION_NAMES:
            raise IndicatorValidationError([_error(self.field, "forbidden_call", "只允许调用 sma/ema/rsi/atr/highest/lowest")])
        if node.keywords:
            raise IndicatorValidationError([_error(self.field, "forbidden_call", "函数不支持命名参数")])
        name = node.func.id
        source: str | None
        if name == "atr":
            if len(node.args) != 1:
                raise IndicatorValidationError([_error(self.field, "invalid_arguments", "atr 只接收一个周期")])
            source = None
            period = _period_argument(node.args[0], self.field)
        elif len(node.args) == 1:
            source = _DEFAULT_SOURCE[name]
            period = _period_argument(node.args[0], self.field)
        elif len(node.args) == 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in _SERIES_NAMES:
            source = node.args[0].id
            period = _period_argument(node.args[1], self.field)
        else:
            raise IndicatorValidationError([_error(self.field, "invalid_arguments", f"{name} 使用 周期 或 数据源, 周期")])
        self.calls.append(_FunctionCall(name, source, period))
        return "number"


def _parse_expression(source: str, field: str) -> _ParsedExpression:
    if len(source) > MAX_EXPRESSION_LENGTH:
        raise IndicatorValidationError([_error(field, "too_long", f"最多 {MAX_EXPRESSION_LENGTH} 个字符")])
    try:
        tree = ast.parse(source, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        raise IndicatorValidationError([_error(field, "invalid_syntax", "表达式语法不完整或无效")]) from exc
    try:
        nodes = tuple(ast.walk(tree))
        depth = _node_depth(tree)
    except RecursionError as exc:
        raise IndicatorValidationError([_error(field, "too_complex", "表达式嵌套过深")]) from exc
    if len(nodes) > MAX_AST_NODES or depth > MAX_AST_DEPTH:
        raise IndicatorValidationError([_error(field, "too_complex", "表达式过于复杂")])
    validator = _ExpressionValidator(field)
    if validator.check(tree) != "bool":
        raise IndicatorValidationError([_error(field, "not_condition", "买卖规则最终必须得到真或假")])
    required = max((call.period + (1 if call.name == "rsi" else 0) for call in validator.calls), default=1)
    return _ParsedExpression(source, tree, tuple(validator.calls), required)


def _compile_spec(spec: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, _ParsedExpression]]:
    normalised: dict[str, Any] = _normalise_metadata(spec)
    if not normalised["long_expression"] and not normalised["short_expression"]:
        raise IndicatorValidationError([_error("expressions", "required", "至少填写一个做多或做空规则")])
    parsed: dict[str, _ParsedExpression] = {}
    errors: list[dict[str, str]] = []
    for side in ("long", "short"):
        field = f"{side}_expression"
        if not normalised[field]:
            continue
        try:
            parsed[side] = _parse_expression(normalised[field], field)
        except IndicatorValidationError as exc:
            errors.extend(exc.errors)
    if errors:
        raise IndicatorValidationError(errors)
    normalised["required_history"] = max((item.required_history for item in parsed.values()), default=1)
    normalised["language_version"] = 1
    return normalised, parsed


def validate_indicator_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalised safe spec or raise :class:`IndicatorValidationError`."""
    normalised, _ = _compile_spec(spec)
    return deepcopy(normalised)


def validation_report(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return an API-friendly validation report without executing the rule."""
    try:
        normalised, _ = _compile_spec(spec)
    except IndicatorValidationError as exc:
        return {"valid": False, "errors": deepcopy(exc.errors), "required_history": None}
    return {"valid": True, "errors": [], "required_history": normalised["required_history"], "normalised": deepcopy(normalised)}


def _series_status(candles: Sequence[Candle]) -> str:
    if not candles:
        return "insufficient_history"
    last = candles[-1]
    if not isinstance(last, Candle):
        return "invalid_data"
    interval = TIMEFRAME_MS.get(last.timeframe)
    if interval is None:
        return "unsupported_timeframe"
    for index, candle in enumerate(candles):
        if not isinstance(candle, Candle):
            return "invalid_data"
        if candle.symbol != last.symbol or candle.timeframe != last.timeframe:
            return "mixed_series"
        if candle.closed is not True:
            return "open_candle"
        if isinstance(candle.open_time, bool) or isinstance(candle.close_time, bool) or not isinstance(candle.open_time, int) or not isinstance(candle.close_time, int):
            return "invalid_timestamp"
        if candle.open_time % interval or candle.close_time != candle.open_time + interval - 1:
            return "invalid_timestamp"
        if index and candle.open_time != candles[index - 1].open_time + interval:
            return "gap"
        prices = (candle.open, candle.high, candle.low, candle.close)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in prices):
            return "invalid_data"
        if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(candle.open, candle.close, candle.high):
            return "invalid_data"
        if isinstance(candle.volume, bool) or not isinstance(candle.volume, (int, float)) or not math.isfinite(candle.volume) or candle.volume < 0:
            return "invalid_data"
    return "ready"


class _ExpressionInterpreter:
    def __init__(self, candles: Sequence[Candle]):
        self.candles = candles
        last = candles[-1]
        self.latest = {name: float(getattr(last, name)) for name in _SERIES_NAMES}
        self.cache: dict[_FunctionCall, float] = {}

    def run(self, parsed: _ParsedExpression) -> bool:
        result = self._node(parsed.tree.body)
        if not isinstance(result, bool):
            raise RuntimeError("validated condition did not return bool")
        return result

    def _node(self, node: ast.AST) -> float | bool:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self.latest[node.id]
        if isinstance(node, ast.BinOp):
            left, right = self._number(self._node(node.left)), self._number(self._node(node.right))
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            else:
                if right == 0:
                    raise ArithmeticError("division by zero")
                value = left / right
            if not math.isfinite(value):
                raise ArithmeticError("non-finite arithmetic result")
            return value
        if isinstance(node, ast.UnaryOp):
            value = self._node(node.operand)
            if isinstance(node.op, ast.Not):
                return not self._boolean(value)
            number = self._number(value)
            return number if isinstance(node.op, ast.UAdd) else -number
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                for item in node.values:
                    if not self._boolean(self._node(item)):
                        return False
                return True
            for item in node.values:
                if self._boolean(self._node(item)):
                    return True
            return False
        if isinstance(node, ast.Compare):
            left = self._number(self._node(node.left))
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._number(self._node(comparator))
                if isinstance(operator, ast.Eq):
                    passed = left == right
                elif isinstance(operator, ast.NotEq):
                    passed = left != right
                elif isinstance(operator, ast.Lt):
                    passed = left < right
                elif isinstance(operator, ast.LtE):
                    passed = left <= right
                elif isinstance(operator, ast.Gt):
                    passed = left > right
                else:
                    passed = left >= right
                if not passed:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            call = self._call_key(node)
            if call not in self.cache:
                self.cache[call] = self._function(call)
            return self.cache[call]
        # This is unreachable after validation and intentionally has no generic
        # Python execution fallback.
        raise RuntimeError("unexpected validated node")

    @staticmethod
    def _number(value: float | bool) -> float:
        if isinstance(value, bool):
            raise ArithmeticError("boolean used as number")
        result = float(value)
        if not math.isfinite(result):
            raise ArithmeticError("non-finite number")
        return result

    @staticmethod
    def _boolean(value: float | bool) -> bool:
        if not isinstance(value, bool):
            raise ArithmeticError("number used as condition")
        return value

    @staticmethod
    def _call_key(node: ast.Call) -> _FunctionCall:
        name = node.func.id  # validated as ast.Name
        if name == "atr":
            return _FunctionCall(name, None, int(node.args[0].value))
        if len(node.args) == 1:
            return _FunctionCall(name, _DEFAULT_SOURCE[name], int(node.args[0].value))
        return _FunctionCall(name, node.args[0].id, int(node.args[1].value))

    def _function(self, call: _FunctionCall) -> float:
        if call.name == "atr":
            return self._atr(call.period)
        values = [float(getattr(candle, call.source)) for candle in self.candles]
        if call.name == "sma":
            return fmean(values[-call.period :])
        if call.name == "ema":
            alpha = 2 / (call.period + 1)
            result = values[0]
            for value in values[1:]:
                result += alpha * (value - result)
            return result
        if call.name == "highest":
            return max(values[-call.period :])
        if call.name == "lowest":
            return min(values[-call.period :])
        return self._rsi(values, call.period)

    def _atr(self, period: int) -> float:
        ranges: list[float] = []
        for index, candle in enumerate(self.candles):
            previous_close = self.candles[index - 1].close if index else candle.close
            ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
        result = fmean(ranges[:period])
        for value in ranges[period:]:
            result = (result * (period - 1) + value) / period
        return result

    @staticmethod
    def _rsi(values: Sequence[float], period: int) -> float:
        changes = [values[index] - values[index - 1] for index in range(1, len(values))]
        gains = [max(change, 0.0) for change in changes]
        losses = [max(-change, 0.0) for change in changes]
        average_gain, average_loss = fmean(gains[:period]), fmean(losses[:period])
        for gain, loss in zip(gains[period:], losses[period:]):
            average_gain = (average_gain * (period - 1) + gain) / period
            average_loss = (average_loss * (period - 1) + loss) / period
        if average_loss == 0:
            return 50.0 if average_gain == 0 else 100.0
        return 100 - 100 / (1 + average_gain / average_loss)


def evaluate_indicator(spec: Mapping[str, Any], candles: Sequence[Candle]) -> dict[str, Any]:
    """Evaluate a validated rule against the last candle in a closed prefix.

    Input-integrity and warm-up failures return ``ready=False`` instead of a
    guessed signal. Runtime arithmetic errors also fail closed.
    """
    normalised, parsed = _compile_spec(spec)
    status = _series_status(candles)
    base = {
        "indicator_id": normalised["id"],
        "ready": False,
        "status": status,
        "long": False,
        "short": False,
        "signal": "unavailable",
        "required_history": normalised["required_history"],
        "available_history": len(candles),
        "close_time": candles[-1].close_time if candles and isinstance(candles[-1], Candle) else None,
    }
    if status != "ready":
        return base
    if len(candles) < normalised["required_history"]:
        return {**base, "status": "insufficient_history"}
    interpreter = _ExpressionInterpreter(candles)
    try:
        long_signal = interpreter.run(parsed["long"]) if "long" in parsed else False
        short_signal = interpreter.run(parsed["short"]) if "short" in parsed else False
    except (ArithmeticError, _InsufficientHistory):
        return {**base, "status": "calculation_error"}
    signal = "conflict" if long_signal and short_signal else "long" if long_signal else "short" if short_signal else "neutral"
    return {**base, "ready": True, "status": "ready", "long": long_signal, "short": short_signal, "signal": signal}


class CustomIndicatorRegistry:
    """Thread-safe registry using replace-on-success JSON persistence."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
        self._lock = RLock()

    def validate(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        return validation_report(spec)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._read_unlocked()["indicators"])

    def list_indicators(self) -> list[dict[str, Any]]:
        return self.list()

    def get(self, indicator_id: str) -> dict[str, Any] | None:
        if not isinstance(indicator_id, str):
            return None
        with self._lock:
            for item in self._read_unlocked()["indicators"]:
                if item["id"] == indicator_id:
                    return deepcopy(item)
        return None

    def get_indicator(self, indicator_id: str) -> dict[str, Any] | None:
        return self.get(indicator_id)

    def save_draft(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        metadata = _normalise_metadata(spec)
        report = validation_report(metadata)
        item: dict[str, Any] = {
            **metadata,
            "status": "draft",
            "required_history": report.get("required_history"),
            "validation": {"valid": report["valid"], "errors": report["errors"]},
            "language_version": 1,
        }
        return self._upsert(item)

    def save_validated(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        normalised = validate_indicator_spec(spec)
        item: dict[str, Any] = {
            **normalised,
            "status": "validated",
            "validation": {"valid": True, "errors": []},
        }
        return self._upsert(item)

    def save(self, spec: Mapping[str, Any], status: str = "draft") -> dict[str, Any]:
        if status == "draft":
            return self.save_draft(spec)
        if status == "validated":
            return self.save_validated(spec)
        raise ValueError("status must be draft or validated")

    def evaluate(self, indicator_id: str, candles: Sequence[Candle]) -> dict[str, Any]:
        item = self.get(indicator_id)
        if item is None:
            raise KeyError(indicator_id)
        if item.get("status") != "validated" or item.get("validation", {}).get("valid") is not True:
            raise IndicatorNotValidatedError(f"indicator {indicator_id!r} is not validated")
        return evaluate_indicator(item, candles)

    def _upsert(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            document = self._read_unlocked()
            existing = next((row for row in document["indicators"] if row["id"] == item["id"]), None)
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            saved = deepcopy(item)
            saved["created_at"] = existing.get("created_at", now) if existing else now
            saved["updated_at"] = now
            rows = [row for row in document["indicators"] if row["id"] != item["id"]]
            rows.append(saved)
            rows.sort(key=lambda row: row["id"])
            self._write_unlocked({"version": REGISTRY_VERSION, "indicators": rows})
            return deepcopy(saved)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": REGISTRY_VERSION, "indicators": []}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IndicatorRegistryError("custom indicator registry is unreadable") from exc
        if not isinstance(document, dict) or document.get("version") != REGISTRY_VERSION or not isinstance(document.get("indicators"), list):
            raise IndicatorRegistryError("custom indicator registry has an unsupported format")
        seen: set[str] = set()
        for item in document["indicators"]:
            try:
                metadata = _normalise_metadata(item)
            except IndicatorValidationError as exc:
                raise IndicatorRegistryError("custom indicator registry contains invalid metadata") from exc
            if metadata["id"] in seen or item.get("status") not in {"draft", "validated"}:
                raise IndicatorRegistryError("custom indicator registry contains invalid entries")
            seen.add(metadata["id"])
            if item.get("status") == "validated" and not validation_report(metadata)["valid"]:
                raise IndicatorRegistryError("validated custom indicator failed integrity validation")
        return document

    def _write_unlocked(self, document: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp",
            ) as handle:
                temporary = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, TypeError, ValueError) as exc:
            raise IndicatorRegistryError("custom indicator registry could not be saved atomically") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
