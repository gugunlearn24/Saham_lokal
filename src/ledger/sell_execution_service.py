from __future__ import annotations

import uuid
from typing import Any

from src.ledger.transaction_service import LedgerValidationError, create_journal_entry


def execute_sell_journal(
    connection,
    *,
    ticker: str,
    shares: int,
    selling_price_per_share: float,
    gross_amount: int,
    broker_fee: int,
    exchange_levy: int,
    pph_final: int,
    cost_basis: int,
    trade_date: str,
    source_type: str = "BROKER_CSV",
    reference_id: str = "SELL-EXEC",
    description: str | None = None,
) -> str:
    """Create a balanced multi-leg sell entry for a trade execution."""
    if shares <= 0:
        raise ValueError("shares must be positive.")

    net_cash_inflow = gross_amount - broker_fee - exchange_levy - pph_final
    realized_gain_loss = gross_amount - cost_basis

    if description is None:
        description = f"Sell {shares} shares of {ticker} at {selling_price_per_share}"

    legs = [
        {
            "account_id": "ASSET:RDN_CASH",
            "debit_amount": net_cash_inflow,
            "credit_amount": 0,
        },
        {
            "account_id": "EXPENSE:BROKER_FEE",
            "debit_amount": broker_fee,
            "credit_amount": 0,
        },
        {
            "account_id": "EXPENSE:EXCHANGE_LEVY",
            "debit_amount": exchange_levy,
            "credit_amount": 0,
        },
        {
            "account_id": "EXPENSE:TAX_PPH_FINAL",
            "debit_amount": pph_final,
            "credit_amount": 0,
        },
        {
            "account_id": f"ASSET:PORTFOLIO:{ticker}",
            "debit_amount": 0,
            "credit_amount": cost_basis,
        },
        {
            "account_id": "REVENUE:REALIZED_GAIN_LOSS",
            "debit_amount": 0,
            "credit_amount": max(realized_gain_loss, 0),
        },
    ]

    if realized_gain_loss < 0:
        legs[-1] = {
            "account_id": "REVENUE:REALIZED_GAIN_LOSS",
            "debit_amount": abs(realized_gain_loss),
            "credit_amount": 0,
        }

    total_debits = sum(leg["debit_amount"] for leg in legs)
    total_credits = sum(leg["credit_amount"] for leg in legs)
    if total_debits != total_credits:
        raise LedgerValidationError(
            "Sell journal failed: debits do not match credits for multi-leg sell entry."
        )

    return create_journal_entry(
        connection,
        entry_date=trade_date,
        source_type=source_type,
        reference_id=reference_id,
        description=description,
        trade_date=trade_date,
        legs=legs,
    )
