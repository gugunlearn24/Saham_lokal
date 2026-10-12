from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from src.config.config_loader import load_brokers_config, load_rules_config
from src.config.schema_validator import (
    validate_brokers_config,
    validate_rules_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BROKERS_CONFIG_PATH = PROJECT_ROOT / "config" / "brokers.yaml"
RULES_CONFIG_PATH = PROJECT_ROOT / "config" / "rules.yaml"
REQUIRED_BROKER_MAPPINGS = {
    "trade_date",
    "ticker",
    "action",
    "shares",
    "price_per_share",
    "net_amount",
}
VALID_ACTIONS = {"HOLD", "BUY_MORE", "REBALANCE", "EXIT"}


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        payload = yaml.safe_load(config_file)
    assert isinstance(payload, dict)
    return payload


def _broker_payload() -> dict[str, Any]:
    return _read_yaml(BROKERS_CONFIG_PATH)


def _rules_payload() -> dict[str, Any]:
    return _read_yaml(RULES_CONFIG_PATH)


def test_loads_and_validates_shipped_broker_adapters() -> None:
    brokers = load_brokers_config(BROKERS_CONFIG_PATH)

    assert {broker["broker_id"] for broker in brokers} == {"STOCKBIT", "IPOT"}
    for broker in brokers:
        assert isinstance(broker["broker_name"], str)
        assert isinstance(broker["delimiter"], str)
        assert isinstance(broker["date_format"], str)
        assert isinstance(broker["encoding"], str)
        assert REQUIRED_BROKER_MAPPINGS <= broker["mappings"].keys()
        assert all(
            isinstance(broker["mappings"][key], str)
            and broker["mappings"][key].strip()
            for key in REQUIRED_BROKER_MAPPINGS
        )
        assert broker["conversions"]["lot_multiplier"] > 0


@pytest.mark.parametrize(
    ("broker_id", "expected_delimiter", "expected_multiplier"),
    [("STOCKBIT", ",", 100), ("IPOT", ";", 1)],
)
def test_shipped_broker_metadata_and_conversion_types(
    broker_id: str, expected_delimiter: str, expected_multiplier: int
) -> None:
    brokers = load_brokers_config(BROKERS_CONFIG_PATH)
    broker = next(item for item in brokers if item["broker_id"] == broker_id)

    assert broker["delimiter"] == expected_delimiter
    assert broker["conversions"]["lot_multiplier"] == expected_multiplier
    assert isinstance(broker["conversions"]["decimal_separator"], str)
    assert isinstance(broker["conversions"]["thousand_separator"], str)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["brokers"][0]["mappings"].pop("trade_date"),
            "trade_date",
        ),
        (
            lambda payload: payload["brokers"][0]["mappings"].update(
                {"ticker": None}
            ),
            "ticker",
        ),
        (
            lambda payload: payload["brokers"][0]["mappings"].update(
                {"action": ""}
            ),
            "action",
        ),
        (
            lambda payload: payload["brokers"][0]["mappings"].pop("shares"),
            "shares",
        ),
        (
            lambda payload: payload["brokers"][0]["mappings"].pop(
                "price_per_share"
            ),
            "price_per_share",
        ),
        (
            lambda payload: payload["brokers"][0]["mappings"].pop("net_amount"),
            "net_amount",
        ),
    ],
)
def test_rejects_missing_or_invalid_required_broker_mappings(
    mutate: Any, message: str
) -> None:
    payload = deepcopy(_broker_payload())
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        validate_brokers_config(payload)


@pytest.mark.parametrize(
    "multiplier",
    [0, -1, 0.0, -0.5, True, "100", None, float("nan"), float("inf"), -float("inf")],
)
def test_rejects_non_positive_or_non_numeric_lot_multiplier(
    multiplier: Any,
) -> None:
    payload = deepcopy(_broker_payload())
    payload["brokers"][0]["conversions"]["lot_multiplier"] = multiplier

    with pytest.raises(ValueError, match="lot_multiplier"):
        validate_brokers_config(payload)


@pytest.mark.parametrize(
    "field",
    ["broker_id", "broker_name", "delimiter", "date_format", "encoding"],
)
def test_rejects_missing_required_broker_metadata(field: str) -> None:
    payload = deepcopy(_broker_payload())
    payload["brokers"][0].pop(field)

    with pytest.raises(ValueError, match=field):
        validate_brokers_config(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload["brokers"][0].update({"broker_id": 123}), "broker_id"),
        (
            lambda payload: payload["brokers"][0]["conversions"].update(
                {"decimal_separator": 1}
            ),
            "decimal_separator",
        ),
        (
            lambda payload: payload["brokers"][0]["conversions"].update(
                {"thousand_separator": ""}
            ),
            "thousand_separator",
        ),
    ],
)
def test_rejects_invalid_broker_field_types(mutate: Any, message: str) -> None:
    payload = deepcopy(_broker_payload())
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        validate_brokers_config(payload)


def test_rejects_empty_broker_list_and_invalid_broker_container() -> None:
    with pytest.raises(ValueError, match="non-empty 'brokers' list"):
        validate_brokers_config({"brokers": []})

    with pytest.raises(ValueError, match="brokers"):
        validate_brokers_config({"brokers": {"broker_id": "STOCKBIT"}})


def test_loads_and_validates_shipped_fundamental_technical_and_action_rules() -> None:
    rules = load_rules_config(RULES_CONFIG_PATH)
    matrix = rules["evaluation_matrix"]

    assert rules["version"]
    assert rules["strategy_name"]
    assert matrix["fundamental_rules"]
    assert matrix["technical_rules"]
    assert matrix["action_matrix"]

    for group in ("fundamental_rules", "technical_rules"):
        for rule in matrix[group]:
            assert rule["id"]
            assert rule["metric"]
            assert rule["operator"]
            assert rule["severity"] in {"FAIL", "WARN"}
            if rule["operator"] == "BETWEEN":
                assert len(rule["range"]) == 2
            else:
                assert isinstance(rule["threshold"], (int, float))

    for action_rule in matrix["action_matrix"]:
        assert isinstance(action_rule["condition"], str)
        assert action_rule["condition"].strip()
        assert action_rule["action"] in VALID_ACTIONS


@pytest.mark.parametrize("group", ["fundamental_rules", "technical_rules"])
def test_rejects_missing_or_empty_evaluation_rule_groups(group: str) -> None:
    missing_group = deepcopy(_rules_payload())
    missing_group["evaluation_matrix"].pop(group)
    with pytest.raises(ValueError, match=group):
        validate_rules_config(missing_group)

    empty_group = deepcopy(_rules_payload())
    empty_group["evaluation_matrix"][group] = []
    with pytest.raises(ValueError, match=group):
        validate_rules_config(empty_group)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda rule: rule.pop("id"),
            "missing 'id'",
        ),
        (
            lambda rule: rule.update({"operator": "APPROX"}),
            "unsupported operator",
        ),
        (
            lambda rule: rule.pop("threshold"),
            "threshold",
        ),
        (
            lambda rule: rule.update({"threshold": True}),
            "threshold",
        ),
        (
            lambda rule: rule.update({"severity": "INFO"}),
            "severity",
        ),
        (
            lambda rule: rule.update({"operator": ["APPROX"]}),
            "unsupported operator",
        ),
        (
            lambda rule: rule.update({"severity": []}),
            "severity",
        ),
        (
            lambda rule: rule.update({"threshold": float("nan")}),
            "finite",
        ),
    ],
)
def test_rejects_invalid_fundamental_or_technical_rule(
    mutate: Any, message: str
) -> None:
    payload = deepcopy(_rules_payload())
    rule = payload["evaluation_matrix"]["fundamental_rules"][0]
    mutate(rule)

    with pytest.raises(ValueError, match=message):
        validate_rules_config(payload)


@pytest.mark.parametrize(
    "invalid_range",
    [None, [], [1], [1, 2, 3], ["low", 70]],
)
def test_rejects_invalid_between_rule_range(invalid_range: Any) -> None:
    payload = deepcopy(_rules_payload())
    rule = payload["evaluation_matrix"]["technical_rules"][1]
    rule["range"] = invalid_range

    with pytest.raises(ValueError, match="range"):
        validate_rules_config(payload)


def test_rejects_missing_or_invalid_action_matrix_entries() -> None:
    missing = deepcopy(_rules_payload())
    missing["evaluation_matrix"].pop("action_matrix")
    with pytest.raises(ValueError, match="action_matrix"):
        validate_rules_config(missing)

    invalid_action = deepcopy(_rules_payload())
    invalid_action["evaluation_matrix"]["action_matrix"][0]["action"] = "SELL_ALL"
    with pytest.raises(ValueError, match="action"):
        validate_rules_config(invalid_action)

    invalid_action_type = deepcopy(_rules_payload())
    invalid_action_type["evaluation_matrix"]["action_matrix"][0]["action"] = []
    with pytest.raises(ValueError, match="action"):
        validate_rules_config(invalid_action_type)

    invalid_condition = deepcopy(_rules_payload())
    invalid_condition["evaluation_matrix"]["action_matrix"][0]["condition"] = ""
    with pytest.raises(ValueError, match="condition"):
        validate_rules_config(invalid_condition)


@pytest.mark.parametrize(
    ("loader", "filename", "contents", "message"),
    [
        (load_brokers_config, "brokers.yaml", "brokers: [", "Invalid YAML syntax"),
        (load_rules_config, "rules.yaml", "[]", "version"),
        (load_brokers_config, "empty.yaml", "", "Broker config"),
        (load_rules_config, "missing-structure.yaml", "version: '1.0'", "strategy_name"),
    ],
)
def test_loader_rejects_invalid_yaml_contract(
    tmp_path: Path,
    loader: Any,
    filename: str,
    contents: str,
    message: str,
) -> None:
    config_path = tmp_path / filename
    config_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        loader(config_path)


@pytest.mark.parametrize("loader", [load_brokers_config, load_rules_config])
def test_loader_reports_missing_config_file(tmp_path: Path, loader: Any) -> None:
    missing_path = tmp_path / "missing.yaml"

    with pytest.raises(FileNotFoundError, match="Configuration file not found"):
        loader(missing_path)
