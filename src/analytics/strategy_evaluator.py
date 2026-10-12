from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.config.config_loader import load_rules_config

_DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.yaml"

VALID_STATUSES = {"PASS", "WARN", "FAIL"}


def _normalize_status(value: str | None) -> str:
    normalized = (value or "").strip().upper()
    if normalized not in VALID_STATUSES:
        raise ValueError(f"Unsupported status value '{value}'.")
    return normalized


def _evaluate_numeric_rule(metric_value: Any, rule: Mapping[str, Any]) -> bool:
    if metric_value is None:
        return False

    value = float(metric_value)
    operator = str(rule["operator"])
    if operator == ">=":
        return value >= float(rule["threshold"])
    if operator == "<=":
        return value <= float(rule["threshold"])
    if operator == ">":
        return value > float(rule["threshold"])
    if operator == "<":
        return value < float(rule["threshold"])
    if operator == "==":
        return value == float(rule["threshold"])
    if operator == "!=":
        return value != float(rule["threshold"])
    if operator == "BETWEEN":
        low, high = rule["range"]
        return float(low) <= value <= float(high)
    raise ValueError(f"Unsupported operator '{operator}' in evaluation rule.")


def _rule_severity(rule: Mapping[str, Any]) -> str:
    severity = str(rule.get("severity") or "WARN").upper()
    if severity not in VALID_STATUSES:
        raise ValueError(f"Unsupported severity '{severity}' in rule '{rule.get('id')}'.")
    return severity


def evaluate_fundamental_status(snapshot: Mapping[str, Any], rules: Mapping[str, Any] | None = None) -> str:
    """Return the aggregate fundamental status for a snapshot, using the configured rules."""
    loaded_rules = load_rules_config(_DEFAULT_RULES_PATH) if rules is None else rules
    fundamental_rules = loaded_rules.get("evaluation_matrix", {}).get("fundamental_rules", [])
    if not fundamental_rules:
        return "PASS"

    fail_seen = False
    warn_seen = False
    for rule in fundamental_rules:
        metric = str(rule["metric"])
        metric_value = snapshot.get(metric)
        if not _evaluate_numeric_rule(metric_value, rule):
            severity = _rule_severity(rule)
            if severity == "FAIL":
                fail_seen = True
            elif severity == "WARN":
                warn_seen = True

    if fail_seen:
        return "FAIL"
    if warn_seen:
        return "WARN"
    return "PASS"


def evaluate_technical_status(adjusted_close: Sequence[float | int], rules: Mapping[str, Any] | None = None) -> str:
    """Return the aggregate technical status for an adjusted close series, using the configured rules."""
    loaded_rules = load_rules_config(_DEFAULT_RULES_PATH) if rules is None else rules
    technical_rules = loaded_rules.get("evaluation_matrix", {}).get("technical_rules", [])
    if not technical_rules:
        return "PASS"

    metrics = {
        "close_vs_weekly_ma30": (adjusted_close[-1] / compute_weekly_ma30(adjusted_close)) if adjusted_close else 0.0,
        "weekly_rsi14": compute_weekly_rsi14(adjusted_close),
    }

    fail_seen = False
    warn_seen = False
    for rule in technical_rules:
        metric = str(rule["metric"])
        metric_value = metrics.get(metric)
        if not _evaluate_numeric_rule(metric_value, rule):
            severity = _rule_severity(rule)
            if severity == "FAIL":
                fail_seen = True
            elif severity == "WARN":
                warn_seen = True

    if fail_seen:
        return "FAIL"
    if warn_seen:
        return "WARN"
    return "PASS"


def evaluate_condition(condition: str, fundamental_status: str, technical_status: str) -> bool:
    """Safely evaluate a simple A AND B / A OR B condition using the status variables."""
    expression = (
        condition
        .replace("fundamental", "fundamental_status")
        .replace("technical", "technical_status")
        .replace("AND", " and ")
        .replace("OR", " or ")
    )
    namespace = {"fundamental_status": _normalize_status(fundamental_status), "technical_status": _normalize_status(technical_status), "__builtins__": {}}
    return bool(eval(expression, namespace, {}))


def resolve_action(fundamental_status: str, technical_status: str, rules: Mapping[str, Any] | None = None) -> str:
    """Resolve the final portfolio action from the configured action matrix."""
    loaded_rules = load_rules_config(_DEFAULT_RULES_PATH) if rules is None else rules
    action_matrix = loaded_rules.get("evaluation_matrix", {}).get("action_matrix", [])
    for entry in action_matrix:
        condition = str(entry.get("condition", ""))
        if evaluate_condition(condition, fundamental_status, technical_status):
            return str(entry["action"]).upper()
    return "HOLD"


def evaluate_strategy(
    fundamental_snapshot: Mapping[str, Any],
    adjusted_close: Sequence[float | int],
    rules: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate both fundamental and technical dimensions and return the final action."""
    loaded_rules = load_rules_config(_DEFAULT_RULES_PATH) if rules is None else rules
    fundamental_status = evaluate_fundamental_status(fundamental_snapshot, loaded_rules)
    technical_status = evaluate_technical_status(adjusted_close, loaded_rules)
    action = resolve_action(fundamental_status, technical_status, loaded_rules)

    return {
        "fundamental": fundamental_status,
        "technical": technical_status,
        "action": action,
        "metrics": {
            "close_vs_weekly_ma30": (adjusted_close[-1] / compute_weekly_ma30(adjusted_close)) if adjusted_close else 0.0,
            "weekly_rsi14": compute_weekly_rsi14(adjusted_close),
        },
    }


# Lazy import to avoid circular dependency on the indicator helpers.
def compute_weekly_ma30(adjusted_close: Sequence[float | int]) -> float:
    from .technical_indicators import compute_weekly_ma30 as _compute_weekly_ma30

    return _compute_weekly_ma30(adjusted_close)


def compute_weekly_rsi14(adjusted_close: Sequence[float | int], period: int = 14) -> float:
    from .technical_indicators import compute_weekly_rsi14 as _compute_weekly_rsi14

    return _compute_weekly_rsi14(adjusted_close, period=period)
