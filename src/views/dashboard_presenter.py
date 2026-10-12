from __future__ import annotations

from typing import Any


def present_dashboard(dashboard: dict[str, Any]) -> dict[str, Any]:
    """Return a UI-facing dashboard payload with the primary and unsettled sections separated."""
    primary_view = {
        "nav_total": dashboard.get("nav_total", 0.0),
        "unrealized_pnl_total": dashboard.get("unrealized_pnl_total", 0.0),
        "positions": dashboard.get("active_positions", []),
        "stale_data_warning_tickers": dashboard.get("stale_data_warning_tickers", []),
    }

    unsettled_view = {
        "warning_label": "Unsettled / Pending RDN",
        "positions": dashboard.get("pending_clearance", []),
    }

    return {
        "primary_view": primary_view,
        "unsettled_view": unsettled_view,
        "warnings": [warning for warning in dashboard.get("warnings", []) if warning],
    }
