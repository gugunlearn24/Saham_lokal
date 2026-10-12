"""Portfolio allocation and FIFO disposal logic."""

from .disposal_service import dispose_lots
from .fifo_lot_engine import (
    FIFOValidationError,
    create_stock_lot,
    get_open_lots,
    liquidate_fifo_lots,
)
