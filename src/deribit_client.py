import logging
import time
from typing import Any, Dict, List, Optional
import httpx
from src.config import DERIBIT_API_BASE

logger = logging.getLogger("deribit_client")


class DeribitClient:
    def __init__(self, base_url: str = DERIBIT_API_BASE, timeout: float = 15.0, max_retries: int = 4):
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        delay = 1.0
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.get(endpoint, params=params)
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    raise RuntimeError(f"Deribit API Error: {data['error']}")
                return data.get("result")
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Deribit API attempt {attempt}/{self.max_retries} failed for {endpoint} (params={params}): {e}"
                )
                if attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2.0

        logger.error(f"Deribit API request completely failed after {self.max_retries} retries: {endpoint}")
        raise RuntimeError(f"Deribit API call failed: {last_error}")

    def get_index_price(self, index_name: str = "btc_usd") -> Dict[str, Any]:
        """Fetch current index price and estimated delivery price."""
        return self._request("/get_index_price", {"index_name": index_name})

    def get_instruments(self, currency: str = "BTC", kind: str = "option", expired: bool = False) -> List[Dict[str, Any]]:
        """Fetch active option instruments."""
        return self._request(
            "/get_instruments",
            {"currency": currency, "kind": kind, "expired": str(expired).lower()},
        )

    def get_book_summary_by_currency(self, currency: str = "BTC", kind: str = "option") -> List[Dict[str, Any]]:
        """Fetch book summary (OI, volume, mark_iv, etc.) for all option instruments."""
        return self._request(
            "/get_book_summary_by_currency",
            {"currency": currency, "kind": kind},
        )

    def get_ticker(self, instrument_name: str) -> Dict[str, Any]:
        """Fetch ticker with greeks for a single instrument."""
        return self._request("/ticker", {"instrument_name": instrument_name})

    def get_dvol(self, currency: str = "BTC") -> Optional[float]:
        """Fetch latest 1h close DVOL (Deribit Volatility Index)."""
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 24 * 3600 * 1000  # last 24h
        try:
            res = self._request(
                "/get_volatility_index_data",
                {
                    "currency": currency,
                    "start_timestamp": start_ms,
                    "end_timestamp": now_ms,
                    "resolution": "1h",
                },
            )
            data = res.get("data", [])
            if data and len(data) > 0:
                # [timestamp, open, high, low, close]
                latest_close = data[-1][4]
                return float(latest_close)
        except Exception as e:
            logger.error(f"Failed to fetch DVOL: {e}")
        return None

    def get_delivery_prices(self, index_name: str = "btc_usd", count: int = 100) -> List[Dict[str, Any]]:
        """Fetch historical daily delivery / settlement prices."""
        res = self._request("/get_delivery_prices", {"index_name": index_name, "count": count})
        return res.get("data", [])
