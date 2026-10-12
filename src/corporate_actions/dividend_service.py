from __future__ import annotations

import uuid
from numbers import Integral


class DividendBookingError(ValueError):
    pass


def book_cash_dividend(
    connection,
    *,
    ticker: str,
    payment_date: str,
    net_cash_received: int,
    reference_id: str,
    description: str | None = None,
) -> str:
    """Book dividend only when cash has actually arrived in the RDN account."""
    if (
        isinstance(net_cash_received, bool)
        or not isinstance(net_cash_received, Integral)
        or net_cash_received <= 0
    ):
        raise DividendBookingError("net_cash_received must be a positive integer.")

    normalized_ticker = ticker.upper()
    if description is None:
        description = f"Cash dividend for {normalized_ticker}"

    entry_id = str(uuid.uuid4())
    income_account_id = f"REVENUE:DIVIDEND_INCOME:{normalized_ticker}"
    connection.execute(
        """
        INSERT OR IGNORE INTO accounts (
            account_id, account_name, account_type, currency
        ) VALUES (?, ?, 'REVENUE', 'IDR')
        """,
        (income_account_id, f"{normalized_ticker} Dividend Income"),
    )
    connection.execute(
        "INSERT INTO journal_entries (entry_id, entry_hash, entry_date, trade_date, source_type, reference_id, description) VALUES (?, ?, ?, ?, 'DIVIDEND', ?, ?)",
        (entry_id, f"dividend-{normalized_ticker}-{reference_id}-{payment_date}", payment_date, payment_date, reference_id, description),
    )

    connection.execute(
        "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, 'ASSET:RDN_CASH', ?, 0)",
        (str(uuid.uuid4()), entry_id, net_cash_received),
    )

    connection.execute(
        "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, ?, 0, ?)",
        (str(uuid.uuid4()), entry_id, income_account_id, net_cash_received),
    )

    return entry_id
