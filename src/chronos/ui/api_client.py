"""Typed HTTP client for the Chronos backend (the UI's only data path).

The Streamlit UI is a thin client: it never imports broker adapters, the
runtime, or strategy services. Every read and every DEMO rehearsal goes
through the loopback FastAPI backend with the local API token attached.
This module deliberately imports nothing beyond ``httpx`` and the settings
surface, so a UI process can never hold a broker connection by accident.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from chronos.config.settings import Settings

TOKEN_HEADER = "X-Chronos-Token"

_TOKEN_REJECTED_MESSAGE = (
    "token rejected by the backend (401). The UI token no longer matches the running "
    "backend; restart the UI so it re-reads the backend token file, or restart the "
    "backend with scripts/run_backend.py to regenerate it."
)
_READ_ONLY_MESSAGE = (
    "the backend is running read-only (409): another Chronos backend holds the "
    "single-writer lease for this database, so mutating requests are refused."
)


class ApiClientError(RuntimeError):
    """Raised for any backend transport or HTTP failure, with operator guidance."""


def _base_url_for(host: str, port: int) -> str:
    formatted_host = f"[{host}]" if ":" in host else host
    return f"http://{formatted_host}:{port}"


class ApiClient:
    """Synchronous, token-authenticated client for the loopback backend."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 10.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={TOKEN_HEADER: token},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        timeout: float = 10.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> ApiClient:
        """Build a client from validated settings, reading the backend token file."""

        token_file = settings.backend_token_file
        if not token_file.exists():
            raise ApiClientError(
                f"backend token file not found at {token_file}. Start the backend first "
                "(.venv/bin/python scripts/run_backend.py); it creates the token on first "
                "startup, then restart this UI."
            )
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ApiClientError(
                f"backend token file at {token_file} is empty. Delete it and start the "
                "backend (.venv/bin/python scripts/run_backend.py) to regenerate the token."
            )
        return cls(
            base_url=_base_url_for(settings.backend_host, settings.backend_port),
            token=token,
            timeout=timeout,
            transport=transport,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        self._client.close()

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.RequestError as error:
            raise ApiClientError(
                f"backend not reachable at {self._base_url}; start it with scripts/run_backend.py"
            ) from error
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise ApiClientError(_TOKEN_REJECTED_MESSAGE)
        if response.status_code == httpx.codes.FORBIDDEN:
            raise ApiClientError(f"the backend refused this request (403): {_detail(response)}")
        if response.status_code == httpx.codes.CONFLICT:
            raise ApiClientError(f"{_READ_ONLY_MESSAGE} Detail: {_detail(response)}")
        if response.is_error:
            raise ApiClientError(
                f"backend request failed ({response.status_code}): {_detail(response)}"
            )
        try:
            return response.json()
        except ValueError as error:
            raise ApiClientError(
                f"the backend returned a non-JSON body for {method} {path}"
            ) from error

    def _get_object(self, path: str) -> dict[str, Any]:
        return _require_object(self._request("GET", path), path)

    def _post_object(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        return _require_object(self._request("POST", path, json_body), path)

    # -- read endpoints ----------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._get_object("/health")

    def account_summary(self) -> dict[str, Any]:
        return self._get_object("/account/summary")

    def positions(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/account/positions")
        if not isinstance(payload, list):
            raise ApiClientError("the backend returned a non-array body for /account/positions")
        return [_require_object(item, "/account/positions") for item in payload]

    def reconciliation(self) -> dict[str, Any]:
        return self._get_object("/strategy/reconciliation")

    def evaluate_candidates(self, symbol: str) -> dict[str, Any]:
        return self._post_object(f"/strategy/candidates/{symbol}", {})

    def regime(self, symbol: str) -> dict[str, Any]:
        return self._get_object(f"/strategy/regime/{symbol}")

    # -- DEMO rehearsal endpoints (read-only previews; nothing submits) ----

    def risk_preview(
        self,
        symbol: str,
        selected_contract_id: int,
        total_commission_estimate: str,
    ) -> dict[str, Any]:
        return self._post_object(
            "/strategy/risk-preview",
            {
                "symbol": symbol,
                "selected_contract_id": selected_contract_id,
                "total_commission_estimate": total_commission_estimate,
            },
        )

    def demo_what_if(
        self,
        symbol: str,
        selected_contract_id: int,
        total_commission_estimate: str,
        limit_price: str,
    ) -> dict[str, Any]:
        return self._post_object(
            "/strategy/demo-what-if",
            {
                "symbol": symbol,
                "selected_contract_id": selected_contract_id,
                "total_commission_estimate": total_commission_estimate,
                "limit_price": limit_price,
            },
        )

    def demo_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_object("/strategy/demo-approval", payload)


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text if text else f"HTTP {response.status_code}"
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return str(payload)


def _require_object(payload: Any, path: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiClientError(f"the backend returned a non-object body for {path}")
    return cast(dict[str, Any], payload)
