from __future__ import annotations

import uuid


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
    if net_cash_received <= 0:
        raise DividendBookingError("net_cash_received must be a positive integer.")

    if description is None:
        description = f"Cash dividend for {ticker.upper()}"

    entry_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO journal_entries (entry_id, entry_hash, entry_date, trade_date, source_type, reference_id, description) VALUES (?, ?, ?, ?, 'DIVIDEND', ?, ?)",
        (entry_id, f"dividend-{ticker.upper()}-{reference_id}-{payment_date}", payment_date, payment_date, reference_id, description),
    )

    connection.execute(
        "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, 'ASSET:RDN_CASH', ?, 0)",
        (str(uuid.uuid4()), entry_id, net_cash_received),
    )

    connection.execute(
        "INSERT INTO journal_legs (leg_id, entry_id, account_id, debit_amount, credit_amount) VALUES (?, ?, ?, 0, ?)",
        (str(uuid.uuid4()), entry_id, f"REVENUE:DIVIDEND_INCOME:{ticker.upper()}", net_cash_received),
    )

    return entry_id
