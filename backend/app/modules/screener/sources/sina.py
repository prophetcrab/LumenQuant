"""Full-market A-share snapshot source (Sina ranking endpoint).

Why this source: screening needs the *whole* market in a handful of requests, and
the quote endpoints the trading path uses (`qt.gtimg.cn`) only answer for symbols
you already name. Sina's market-centre JSON returns paginated rows with price,
change%, turnover, PE, PB and market cap — everything a filter needs — and it
stays available while the alternative (East Money `push2`) blocks non-browser
clients intermittently.

Layering note: this module knows nothing about the screener's business rules. It
returns plain dicts; turning them into filterable records is `service.py`'s job.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

logger = logging.getLogger(__name__)

SINA_RANK_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
# Board suffixes map 1:1 onto the a-share prefixes Sina uses in `symbol`.
PAGE_SIZE = 100
# The whole market is ~5,400 names; 60 pages of 100 covers it with headroom.
MAX_PAGES = 70


class SinaUnavailable(RuntimeError):
    """The upstream ranking endpoint could not serve the request."""


class SinaMarketSource:
    name = "sina-market"

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        page_size: int = PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self.timeout = timeout
        self._transport = transport
        self.page_size = max(10, min(page_size, 100))
        self.max_pages = max(1, max_pages)

    def _headers(self) -> dict[str, str]:
        # The endpoint returns 403 without a finance.sina.com.cn referer.
        return {
            "User-Agent": "Mozilla/5.0 (compatible; LumenQuant/0.1)",
            "Referer": "https://finance.sina.com.cn/",
        }

    def _page(self, page: int, sort: str) -> list[dict]:
        try:
            with httpx.Client(
                timeout=self.timeout, transport=self._transport, headers=self._headers()
            ) as client:
                response = client.get(
                    SINA_RANK_URL,
                    params={
                        "page": page,
                        "num": self.page_size,
                        "sort": sort,
                        "asc": 0,
                        "node": "hs_a",
                        "symbol": "",
                        "_s_r_a": "page",
                    },
                )
        except httpx.HTTPError as exc:
            raise SinaUnavailable(f"新浪行情不可达：{exc}") from exc
        if response.status_code >= 400:
            raise SinaUnavailable(f"新浪行情返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SinaUnavailable("新浪行情返回非 JSON") from exc
        if not isinstance(payload, list):
            raise SinaUnavailable("新浪行情返回结构异常")
        return [row for row in payload if isinstance(row, dict)]

    def snapshot(self, *, sort: str = "symbol", max_rows: int | None = None) -> list[dict]:
        """Every A-share row, or up to `max_rows`.

        Pages are fetched concurrently. Sequentially this took ~47s for the full
        market (56 pages, each ~0.8s) — far too slow for a filter the user is about
        to tweak. Concurrency is bounded to 6 so the upstream does not start
        refusing connections; a page that fails is retried once, then skipped with
        a warning, because a partial universe still screens usefully.
        """
        target = max_rows if max_rows and max_rows > 0 else self.page_size * self.max_pages
        pages_needed = min(self.max_pages, (target + self.page_size - 1) // self.page_size)

        def fetch(page: int) -> list[dict]:
            for attempt in range(2):
                try:
                    return self._page(page, sort)
                except SinaUnavailable:
                    if attempt == 1:
                        raise
                    time.sleep(0.3)
            return []

        rows: list[dict] = []
        failed = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = pool.map(fetch, range(1, pages_needed + 1))
            for chunk in results:
                if not chunk:
                    failed += 1
                    continue
                rows.extend(chunk)
                if len(rows) >= target:
                    break
        if failed:
            logger.warning("screener snapshot lost %s page(s) to upstream errors", failed)
        if not rows:
            raise SinaUnavailable("新浪行情所有分页均失败")
        return rows[:target]

    def health(self) -> tuple[bool, str, float]:
        started = time.perf_counter()
        try:
            rows = self._page(1, "symbol")
        except Exception as exc:
            return False, str(exc)[:200], round((time.perf_counter() - started) * 1000, 1)
        if not rows:
            return False, "新浪行情返回空列表", round((time.perf_counter() - started) * 1000, 1)
        return True, "ok", round((time.perf_counter() - started) * 1000, 1)
