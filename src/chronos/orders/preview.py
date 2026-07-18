"""What-if / order preview (never transmits).

Builds an ``OrderRequest`` with ``transmit=False`` and calls the broker's
``preview_order`` (IBKR whatIf) through the serialized connection. The request
is asserted non-transmitting before it ever reaches the broker, and a preview
the broker declines is surfaced as not-accepted rather than silently ignored.
"""

from __future__ import annotations

from datetime import datetime

from chronos.broker.connection import BrokerConnectionManager
from chronos.domain.models import ChronosModel, OrderPreview, OrderRequest
from chronos.orders.intent import WheelOrderIntent


class OrderPreviewResult(ChronosModel):
    request: OrderRequest
    broker_preview: OrderPreview
    accepted: bool
    warnings: tuple[str, ...]
    previewed_at: datetime


class OrderPreviewService:
    """Run a non-transmitting broker what-if for a proposed intent."""

    def __init__(self, connection: BrokerConnectionManager) -> None:
        self._connection = connection

    def preview(self, intent: WheelOrderIntent) -> OrderPreviewResult:
        request = intent.to_order_request(transmit=False)
        # Defensive: a preview must never carry a transmit flag.
        if request.transmit:  # pragma: no cover - to_order_request(transmit=False) guarantees this
            raise RuntimeError("preview requests must never set transmit=True")
        preview = self._connection.run(self._connection.broker.preview_order(request))
        return OrderPreviewResult(
            request=request,
            broker_preview=preview,
            accepted=preview.accepted,
            warnings=preview.warnings,
            previewed_at=preview.previewed_at,
        )
