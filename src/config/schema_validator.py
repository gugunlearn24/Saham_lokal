from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from numbers import Real
from typing import Any

BROKER_REQUIRED_MAPPINGS = {
    "trade_date",
    "ticker",
    "action",
    "shares",
    "price_per_share",
    "net_amount",
}

RULE_OPERATORS = {">=", "<=", ">", "<", "==", "!=", "BETWEEN"}
VALID_ACTIONS = {"HOLD", "BUY_MORE", "REBALANCE", "EXIT"}
VALID_RULE_SEVERITIES = {"FAIL", "WARN"}


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping/object.")
    return value


def _as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return value


def _validate_numeric(value: Any, label: str, *, positive: bool = False) -> Real:
    if not isinstance(value, Real) or isinstance(value, bool):
        raise ValueError(f"{label} must be a valid number.")
    if not isfinite(value):
        raise ValueError(f"{label} must be a finite number.")
    if positive and value <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return value


def validate_brokers_config(data: Any) -> list[dict[str, Any]]:
    """Validate the broker adapter config and return a normalized list of brokers."""
    root = _as_mapping(data, "Broker config")
    brokers = root.get("brokers")
    if not isinstance(brokers, list) or not brokers:
        raise ValueError("Broker config must contain a non-empty 'brokers' list.")

    normalized: list[dict[str, Any]] = []
    for idx, broker in enumerate(brokers, start=1):
        broker_map = _as_mapping(broker, f"Broker #{idx}")

        for field in ("broker_id", "broker_name", "delimiter", "date_format", "encoding"):
            value = broker_map.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Broker #{idx} field '{field}' must be a non-empty string."
                )

        mappings = _as_mapping(broker_map.get("mappings"), f"Broker #{idx} mappings")
        missing_mappings = sorted(BROKER_REQUIRED_MAPPINGS - set(mappings.keys()))
        if missing_mappings:
            raise ValueError(
                f"Broker #{idx} is missing required mappings: {', '.join(missing_mappings)}."
            )
        for mapping_name in BROKER_REQUIRED_MAPPINGS:
            column_name = mappings[mapping_name]
            if not isinstance(column_name, str) or not column_name.strip():
                raise ValueError(
                    f"Broker #{idx} mapping '{mapping_name}' must be a non-empty string."
                )

        conversions = _as_mapping(broker_map.get("conversions"), f"Broker #{idx} conversions")
        multiplier = conversions.get("lot_multiplier")
        if multiplier is None:
            raise ValueError(f"Broker #{idx} conversions.lot_multiplier is required.")
        _validate_numeric(multiplier, f"Broker #{idx} conversions.lot_multiplier", positive=True)

        for field_name in ("decimal_separator", "thousand_separator"):
            separator = conversions.get(field_name)
            if not isinstance(separator, str) or not separator:
                raise ValueError(
                    f"Broker #{idx} conversions.{field_name} must be a non-empty string."
                )

        normalized.append(dict(broker_map))
    return normalized


def _validate_rule_entry(rule: Any, rule_type: str, index: int) -> dict[str, Any]:
    rule_map = _as_mapping(rule, f"{rule_type} rule #{index}")
    for field in ("id", "metric", "operator"):
        if not rule_map.get(field):
            raise ValueError(f"{rule_type} rule #{index} is missing '{field}'.")

    operator = rule_map["operator"]
    if not isinstance(operator, str) or operator not in RULE_OPERATORS:
        raise ValueError(
            f"{rule_type} rule '{rule_map.get('id')}' has unsupported operator '{operator}'."
        )

    if operator == "BETWEEN":
        value_range = rule_map.get("range")
        if not isinstance(value_range, Sequence) or len(value_range) != 2:
            raise ValueError(
                f"{rule_type} rule '{rule_map.get('id')}' must define 'range' as a two-item list for BETWEEN operations."
            )
        for item in value_range:
            _validate_numeric(item, f"{rule_type} rule '{rule_map.get('id')}' range value")
    else:
        if "threshold" not in rule_map:
            raise ValueError(
                f"{rule_type} rule '{rule_map.get('id')}' is missing the 'threshold' field."
            )
        _validate_numeric(rule_map["threshold"], f"{rule_type} rule '{rule_map.get('id')}' threshold")

    severity = rule_map.get("severity")
    if severity is not None and (
        not isinstance(severity, str) or severity not in VALID_RULE_SEVERITIES
    ):
        raise ValueError(
            f"{rule_type} rule '{rule_map.get('id')}' has invalid severity '{severity}'."
        )

    return dict(rule_map)


def validate_rules_config(data: Any) -> dict[str, Any]:
    """Validate the strategy rules YAML and return the normalized structure."""
    root = _as_mapping(data, "Rules config")
    for field in ("version", "strategy_name", "evaluation_matrix"):
        if field not in root or root[field] is None:
            raise ValueError(f"Rules config is missing required field '{field}'.")

    evaluation_matrix = dict(_as_mapping(root["evaluation_matrix"], "evaluation_matrix"))
    for rule_group_name in ("fundamental_rules", "technical_rules"):
        if rule_group_name not in evaluation_matrix:
            raise ValueError(f"evaluation_matrix is missing '{rule_group_name}'.")

        rules = _as_list(evaluation_matrix[rule_group_name], f"evaluation_matrix.{rule_group_name}")
        if not rules:
            raise ValueError(f"evaluation_matrix.{rule_group_name} must not be empty.")

        normalized_group = []
        for idx, rule in enumerate(rules, start=1):
            normalized_group.append(_validate_rule_entry(rule, rule_group_name[:-6].upper(), idx))
        evaluation_matrix[rule_group_name] = normalized_group

    action_matrix = evaluation_matrix.get("action_matrix")
    if action_matrix is None:
        raise ValueError("evaluation_matrix is missing 'action_matrix'.")

    action_entries = _as_list(action_matrix, "evaluation_matrix.action_matrix")
    if not action_entries:
        raise ValueError("evaluation_matrix.action_matrix must not be empty.")
    normalized_actions = []
    for idx, action in enumerate(action_entries, start=1):
        entry = _as_mapping(action, f"action_matrix entry #{idx}")
        condition = entry.get("condition")
        action_name = entry.get("action")
        if not isinstance(condition, str) or not condition.strip():
            raise ValueError(f"action_matrix entry #{idx} is missing a valid 'condition'.")
        if not isinstance(action_name, str) or action_name not in VALID_ACTIONS:
            raise ValueError(
                f"action_matrix entry #{idx} has invalid action '{action_name}' "
                f"(expected one of: {', '.join(sorted(VALID_ACTIONS))})."
            )
        normalized_actions.append(dict(entry))
    evaluation_matrix["action_matrix"] = normalized_actions

    normalized = dict(root)
    normalized["evaluation_matrix"] = evaluation_matrix
    return normalized
