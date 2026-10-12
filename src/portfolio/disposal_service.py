from __future__ import annotations

from src.portfolio.fifo_lot_engine import liquidate_fifo_lots


def dispose_lots(connection, *, ticker: str, shares_to_sell: int, disposal_entry_id: str, selling_price_per_share: float, disposal_date: str | None = None):
    return liquidate_fifo_lots(
        connection,
        ticker=ticker,
        shares_to_sell=shares_to_sell,
        disposal_entry_id=disposal_entry_id,
        selling_price_per_share=selling_price_per_share,
        disposal_date=disposal_date,
    )
