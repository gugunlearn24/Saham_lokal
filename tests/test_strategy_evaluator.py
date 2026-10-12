from __future__ import annotations

import pytest

from src.analytics.strategy_evaluator import (
    evaluate_fundamental_status,
    evaluate_strategy,
    evaluate_technical_status,
    resolve_action,
)
from src.analytics.technical_indicators import (
    compute_close_vs_weekly_ma30,
    compute_weekly_ma30,
    compute_weekly_rsi14,
)
from src.config.config_loader import load_rules_config


def _rules(
    *,
    fundamental_rules: list[dict[str, object]] | None = None,
    technical_rules: list[dict[str, object]] | None = None,
    action_matrix: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "evaluation_matrix": {
            "fundamental_rules": fundamental_rules or [],
            "technical_rules": technical_rules or [],
            "action_matrix": action_matrix or [],
        }
    }


def test_weekly_ma30_uses_trailing_30_adjusted_closes() -> None:
    adjusted_close = [float(value) for value in range(1, 41)]

    assert compute_weekly_ma30(adjusted_close) == pytest.approx(25.5)


def test_weekly_ma_uses_available_history_when_fewer_than_30_closes() -> None:
    assert compute_weekly_ma30([10, 20, 30]) == pytest.approx(20.0)


def test_weekly_ma_rejects_empty_price_series() -> None:
    with pytest.raises(ValueError, match="adjusted_close cannot be empty"):
        compute_weekly_ma30([])


def test_close_vs_weekly_ma30_returns_latest_adjusted_close_ratio() -> None:
    adjusted_close = [float(value) for value in range(1, 31)]

    assert compute_close_vs_weekly_ma30(adjusted_close) == pytest.approx(
        30.0 / 15.5
    )


def test_weekly_rsi14_uses_last_14_price_changes() -> None:
    adjusted_close = [100.0]
    for delta in [1.0, -1.0] * 7:
        adjusted_close.append(adjusted_close[-1] + delta)

    assert compute_weekly_rsi14(adjusted_close) == pytest.approx(50.0)


def test_weekly_rsi_returns_100_for_only_gains_and_zero_for_only_losses() -> None:
    assert compute_weekly_rsi14(list(range(1, 16))) == pytest.approx(100.0)
    assert compute_weekly_rsi14(list(range(15, 0, -1))) == pytest.approx(0.0)


def test_weekly_rsi_rejects_series_with_fewer_than_two_prices() -> None:
    with pytest.raises(ValueError, match="at least two values"):
        compute_weekly_rsi14([100.0])


def test_weekly_technical_metrics_are_derived_from_adjusted_close_series() -> None:
    adjusted_close = [float(value) for value in range(1, 31)]
    raw_close = [10_000.0] * len(adjusted_close)
    rules = _rules()

    result = evaluate_strategy(
        {"roe_percentage": 15.0, "der_ratio": 0.8, "net_profit_margin": 8.0},
        adjusted_close,
        rules,
    )

    assert raw_close != adjusted_close
    assert result["metrics"]["close_vs_weekly_ma30"] == pytest.approx(30.0 / 15.5)
    assert result["metrics"]["weekly_rsi14"] == pytest.approx(100.0)


def test_shipped_rules_are_loaded_and_fundamental_boundary_values_pass() -> None:
    rules = load_rules_config("config/rules.yaml")
    snapshot = {
        "roe_percentage": 12.0,
        "der_ratio": 1.5,
        "net_profit_margin": 5.01,
    }

    assert evaluate_fundamental_status(snapshot, rules) == "PASS"


def test_fundamental_rules_aggregate_fail_over_warn() -> None:
    rules = _rules(
        fundamental_rules=[
            {
                "id": "ROE",
                "metric": "roe_percentage",
                "operator": ">=",
                "threshold": 12,
                "severity": "FAIL",
            },
            {
                "id": "DER",
                "metric": "der_ratio",
                "operator": "<=",
                "threshold": 1.5,
                "severity": "WARN",
            },
        ]
    )

    assert evaluate_fundamental_status(
        {"roe_percentage": 10, "der_ratio": 2}, rules
    ) == "FAIL"
    assert evaluate_fundamental_status(
        {"roe_percentage": 14, "der_ratio": 2}, rules
    ) == "WARN"
    assert evaluate_fundamental_status(
        {"roe_percentage": 14, "der_ratio": 1}, rules
    ) == "PASS"


def test_missing_fundamental_metric_violates_its_configured_rule() -> None:
    rules = _rules(
        fundamental_rules=[
            {
                "id": "ROE",
                "metric": "roe_percentage",
                "operator": ">=",
                "threshold": 12,
                "severity": "FAIL",
            }
        ]
    )

    assert evaluate_fundamental_status({}, rules) == "FAIL"


@pytest.mark.parametrize(
    ("adjusted_close", "expected_status"),
    [
        ([10.0] * 30, "WARN"),
        ([float(value) for value in range(30, 0, -1)], "FAIL"),
    ],
)
def test_technical_status_applies_configured_ma_and_rsi_rules(
    adjusted_close: list[float],
    expected_status: str,
) -> None:
    rules = load_rules_config("config/rules.yaml")

    assert evaluate_technical_status(adjusted_close, rules) == expected_status


def test_technical_rsi_warning_does_not_override_ma_failure() -> None:
    rules = _rules(
        technical_rules=[
            {
                "id": "MA",
                "metric": "close_vs_weekly_ma30",
                "operator": ">=",
                "threshold": 1.0,
                "severity": "FAIL",
            },
            {
                "id": "RSI",
                "metric": "weekly_rsi14",
                "operator": "BETWEEN",
                "range": [40.0, 70.0],
                "severity": "WARN",
            },
        ]
    )

    assert evaluate_technical_status(list(range(30, 0, -1)), rules) == "FAIL"


@pytest.mark.parametrize(
    ("fundamental", "technical", "action"),
    [
        ("PASS", "PASS", "HOLD"),
        ("PASS", "WARN", "HOLD"),
        ("FAIL", "PASS", "EXIT"),
        ("PASS", "FAIL", "EXIT"),
        ("WARN", "FAIL", "EXIT"),
    ],
)
def test_resolves_actions_from_shipped_action_matrix(
    fundamental: str,
    technical: str,
    action: str,
) -> None:
    rules = load_rules_config("config/rules.yaml")

    assert resolve_action(fundamental, technical, rules) == action


@pytest.mark.parametrize(
    ("fundamental", "technical", "action"),
    [
        ("PASS", "PASS", "HOLD"),
        ("PASS", "WARN", "BUY_MORE"),
        ("PASS", "FAIL", "REBALANCE"),
        ("WARN", "PASS", "EXIT"),
        ("WARN", "WARN", "HOLD"),
        ("WARN", "FAIL", "BUY_MORE"),
        ("FAIL", "PASS", "REBALANCE"),
        ("FAIL", "WARN", "EXIT"),
        ("FAIL", "FAIL", "HOLD"),
    ],
)
def test_custom_action_matrix_maps_status_combinations_to_supported_signals(
    fundamental: str,
    technical: str,
    action: str,
) -> None:
    action_matrix = [
        {
            "condition": (
                f"fundamental == '{fundamental}' AND "
                f"technical == '{technical}'"
            ),
            "action": action,
        }
    ]
    rules = _rules(action_matrix=action_matrix)

    assert resolve_action(fundamental, technical, rules) == action


def test_strategy_result_contains_both_statuses_action_and_metrics() -> None:
    rules = _rules(
        fundamental_rules=[
            {
                "id": "ROE",
                "metric": "roe_percentage",
                "operator": ">=",
                "threshold": 12,
                "severity": "FAIL",
            }
        ],
        technical_rules=[],
        action_matrix=[
            {
                "condition": "fundamental == 'PASS' AND technical == 'PASS'",
                "action": "HOLD",
            }
        ],
    )

    result = evaluate_strategy({"roe_percentage": 15}, [float(x) for x in range(1, 31)], rules)

    assert result["fundamental"] == "PASS"
    assert result["technical"] == "PASS"
    assert result["action"] == "HOLD"
    assert result["metrics"] == {
        "close_vs_weekly_ma30": pytest.approx(30 / 15.5),
        "weekly_rsi14": pytest.approx(100.0),
    }


def test_empty_price_series_is_rejected_for_technical_evaluation() -> None:
    with pytest.raises(ValueError, match="at least two values"):
        evaluate_technical_status([])
