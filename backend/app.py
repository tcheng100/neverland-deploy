#!/usr/bin/env python3
"""Production-ready dashboard backend for Neverland marketplace monitoring."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

OPENSEA_PROXY_URL = "https://app.neverland.money/api/marketplace/opensea"
VEDUST_URL_TEMPLATE = "https://app.neverland.money/api/vedust/{token_id}"
DUST_TOKEN_URL = "https://app.neverland.money/api/neverland/dust/token"

DEFAULT_SLUG = "voting-escrow-dust"
DEFAULT_LIMIT = 200
DEFAULT_MAX_PAGES = 20
METADATA_CACHE_TTL = 60 * 30
PRICE_CACHE_TTL = 20

@dataclass
class DiscountListing:
    rank_discount: int
    token_id: str
    seller: str
    order_hash: str
    price_mon: float
    price_wei: int
    dust_locked: float
    dust_value_usd: float
    listing_value_usd: float
    discount_pct: float
    dust_per_mon: float
    asset_url: str

class NeverlandDataService:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.metadata_cache: Dict[str, Tuple[float, float]] = {}
        self.price_cache: Optional[Tuple[float, Dict[str, float]]] = None
        self.lock = threading.Lock()

    def _now(self) -> float:
        return time.time()

    def _fetch_json(self, url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = self.session.get(url, params=params, timeout=timeout)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError(f"Unexpected JSON type from {url}")
                return payload
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.35 * (attempt + 1) + random.random() * 0.2)
        raise RuntimeError(f"Failed to fetch JSON from {url}: {last_exc}")

    def _fetch_mon_usd(self) -> float:
        candidates = [
            ("https://api.coingecko.com/api/v3/simple/price", {"ids": "wrapped-monad", "vs_currencies": "usd"}),
            ("https://api.coingecko.com/api/v3/simple/price", {"ids": "monad", "vs_currencies": "usd"}),
        ]
        for url, params in candidates:
            try:
                payload = self._fetch_json(url, params=params, timeout=10)
                for key in ("wrapped-monad", "monad"):
                    row = payload.get(key)
                    if isinstance(row, dict) and row.get("usd"):
                        return float(row["usd"])
            except Exception:
                continue

        fallback = self._fetch_json(
            "https://coins.llama.fi/prices/current/coingecko:wrapped-monad,coingecko:monad", timeout=10
        )
        coins = fallback.get("coins", {})
        if isinstance(coins, dict):
            for key in ("coingecko:wrapped-monad", "coingecko:monad"):
                row = coins.get(key)
                if isinstance(row, dict) and row.get("price"):
                    return float(row["price"])

        raise RuntimeError("Unable to fetch MON/USD price.")

    def _fetch_dust_usd(self) -> float:
        payload = self._fetch_json(DUST_TOKEN_URL)
        if payload.get("priceUsdNorm") is not None:
            return float(payload["priceUsdNorm"])
        if payload.get("priceUsd") is not None:
            return float(payload["priceUsd"]) / 1e8
        raise RuntimeError("Unable to fetch DUST/USD price.")

    def get_prices(self) -> Tuple[Dict[str, float], bool, List[str]]:
        stale_prices: Optional[Dict[str, float]] = None
        with self.lock:
            if self.price_cache and self._now() - self.price_cache[0] < PRICE_CACHE_TTL:
                return self.price_cache[1], False, []
            if self.price_cache:
                stale_prices = self.price_cache[1]

        dust_usd: Optional[float] = None
        mon_usd: Optional[float] = None
        fallback_fields: List[str] = []

        try:
            dust_usd = self._fetch_dust_usd()
        except Exception:
            if stale_prices:
                dust_usd = float(stale_prices["dust_usd"])
                fallback_fields.append("dust_usd")
            else:
                raise

        try:
            mon_usd = self._fetch_mon_usd()
        except Exception:
            if stale_prices:
                mon_usd = float(stale_prices["mon_usd"])
                fallback_fields.append("mon_usd")
            else:
                raise

        prices = {"dust_usd": dust_usd, "mon_usd": mon_usd}
        with self.lock:
            self.price_cache = (self._now(), prices)
        return prices, len(fallback_fields) > 0, fallback_fields

    @staticmethod
    def _parse_floatish(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).replace(",", "").strip()
        if not text:
            return 0.0
        return float(text)

    def _extract_dust_locked(self, metadata: Dict[str, Any]) -> float:
        attributes = metadata.get("attributes") or []
        if not isinstance(attributes, list):
            return 0.0
        for trait in ("Treasury (DUST)", "Amount Locked (DUST)", "Locked DUST"):
            for row in attributes:
                if not isinstance(row, dict):
                    continue
                if row.get("trait_type") == trait:
                    try:
                        return max(self._parse_floatish(row.get("value")), 0.0)
                    except Exception:
                        continue
        return 0.0

    def get_dust_locked(self, token_id: str) -> float:
        now = self._now()
        with self.lock:
            cached = self.metadata_cache.get(token_id)
            if cached and now - cached[0] < METADATA_CACHE_TTL:
                return cached[1]

        payload = self._fetch_json(VEDUST_URL_TEMPLATE.format(token_id=token_id))
        dust_locked = self._extract_dust_locked(payload)
        with self.lock:
            self.metadata_cache[token_id] = (now, dust_locked)
        return dust_locked

    def get_dust_locked_many(self, token_ids: List[str], max_workers: int = 16) -> Dict[str, float]:
        out: Dict[str, float] = {}
        if not token_ids:
            return out
        workers = max(4, min(max_workers, 24))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(self.get_dust_locked, token_id): token_id for token_id in token_ids}
            for future in as_completed(future_map):
                token_id = future_map[future]
                try:
                    out[token_id] = future.result()
                except Exception:
                    out[token_id] = 0.0
        return out

    def fetch_opensea_listings(self, slug: str, limit: int, max_pages: int) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        next_cursor: Optional[str] = None
        for _ in range(max_pages):
            params = {"slug": slug, "limit": str(limit)}
            if next_cursor:
                params["next"] = next_cursor
            payload = self._fetch_json(OPENSEA_PROXY_URL, params=params)
            page = payload.get("listings") or []
            if isinstance(page, list):
                rows.extend(r for r in page if isinstance(r, dict))
            next_cursor = payload.get("next")
            if not next_cursor:
                break
        return rows

    @staticmethod
    def _parse_listing(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if str(raw.get("status", "")).upper() != "ACTIVE":
            return None
        params = ((raw.get("protocol_data") or {}).get("parameters")) or {}
        offer = params.get("offer") or []
        if not offer or not isinstance(offer[0], dict):
            return None
        offer0 = offer[0]
        token_id = str(offer0.get("identifierOrCriteria", "")).strip()
        if not token_id:
            return None
        seller = str(params.get("offerer", "")).lower().strip()
        if not seller:
            return None
        current = ((raw.get("price") or {}).get("current")) or {}
        try:
            price_wei = int(str(current.get("value", "0")))
            decimals = int(current.get("decimals", 18))
        except ValueError:
            return None
        if decimals < 0:
            decimals = 18
        price_mon = float(Decimal(price_wei) / (Decimal(10) ** decimals))
        contract = str(offer0.get("token", "")).lower()
        chain = str(raw.get("chain", "monad")).lower()
        return {
            "token_id": token_id,
            "seller": seller,
            "order_hash": str(raw.get("order_hash", "")).lower(),
            "price_wei": price_wei,
            "price_mon": price_mon,
            "asset_url": f"https://opensea.io/assets/{chain}/{contract}/{token_id}" if contract else "",
        }

    def build_discount_rankings(self, slug: str, limit: int, max_pages: int) -> Dict[str, Any]:
        raw_rows = self.fetch_opensea_listings(slug=slug, limit=limit, max_pages=max_pages)
        parsed = [item for item in (self._parse_listing(r) for r in raw_rows) if item]

        best_by_token: Dict[str, Dict[str, Any]] = {}
        for row in parsed:
            token_id = row["token_id"]
            old = best_by_token.get(token_id)
            if old is None or row["price_wei"] < old["price_wei"]:
                best_by_token[token_id] = row

        prices, using_fallback_source, fallback_fields = self.get_prices()
        dust_usd = prices["dust_usd"]
        mon_usd = prices["mon_usd"]

        token_ids = list(best_by_token.keys())
        dust_by_token = self.get_dust_locked_many(token_ids)

        ranked: List[DiscountListing] = []
        for token_id, row in best_by_token.items():
            dust_locked = dust_by_token.get(token_id, 0.0)
            if dust_locked <= 0:
                continue
            dust_value_usd = dust_locked * dust_usd
            listing_value_usd = row["price_mon"] * mon_usd
            if dust_value_usd <= 0:
                continue
            discount_pct = (dust_value_usd - listing_value_usd) / dust_value_usd * 100.0
            dust_per_mon = (dust_locked / row["price_mon"]) if row["price_mon"] > 0 else 0.0
            ranked.append(
                DiscountListing(
                    rank_discount=0,
                    token_id=token_id,
                    seller=row["seller"],
                    order_hash=row["order_hash"],
                    price_mon=row["price_mon"],
                    price_wei=row["price_wei"],
                    dust_locked=dust_locked,
                    dust_value_usd=dust_value_usd,
                    listing_value_usd=listing_value_usd,
                    discount_pct=discount_pct,
                    dust_per_mon=dust_per_mon,
                    asset_url=row["asset_url"],
                )
            )

        ranked.sort(key=lambda x: (x.discount_pct, -x.price_mon), reverse=True)
        for idx, item in enumerate(ranked, start=1):
            item.rank_discount = idx

        return {
            "captured_at": int(time.time()),
            "slug": slug,
            "prices": prices,
            "using_fallback_source": using_fallback_source,
            "fallback_fields": fallback_fields,
            "total_listings": len(ranked),
            "listings": [asdict(item) for item in ranked],
        }

class DashboardHandler(BaseHTTPRequestHandler):
    data_service = NeverlandDataService()
    cors_origin = os.environ.get("CORS_ORIGIN", "*")

    @staticmethod
    def _is_wallet(value: str) -> bool:
        return value.startswith("0x") and len(value) == 42

    @classmethod
    def _extract_wallets(cls, params: Dict[str, List[str]]) -> List[str]:
        raw_parts: List[str] = []
        raw_parts.extend(params.get("wallet", []))
        raw_parts.extend(params.get("wallets", []))
        if not raw_parts:
            return []

        cleaned: List[str] = []
        seen: set[str] = set()
        for part in raw_parts:
            chunk = part.replace("\n", ",").replace(";", ",")
            for item in chunk.split(","):
                wallet = item.strip().lower()
                if not wallet:
                    continue
                if cls._is_wallet(wallet) and wallet not in seen:
                    seen.add(wallet)
                    cleaned.append(wallet)
        return cleaned

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", self.cors_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/snapshot":
            self._serve_snapshot(parsed.query)
            return
        if parsed.path == "/health":
            self._json({"status": "ok"})
            return
        self._json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def _serve_snapshot(self, query: str) -> None:
        params = parse_qs(query)
        wallets = self._extract_wallets(params)
        slug = (params.get("slug", [DEFAULT_SLUG])[0] or DEFAULT_SLUG).strip()
        try:
            limit = int(params.get("limit", [str(DEFAULT_LIMIT)])[0])
            max_pages = int(params.get("max_pages", [str(DEFAULT_MAX_PAGES)])[0])
        except ValueError:
            self._json({"error": "limit and max_pages must be integers"}, status=HTTPStatus.BAD_REQUEST)
            return

        if not wallets:
            self._json(
                {"error": "wallet/wallets query param must contain at least one valid 0x address"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            snapshot = self.data_service.build_discount_rankings(slug=slug, limit=limit, max_pages=max_pages)
        except Exception as exc:
            self._json({"error": f"snapshot_failed: {exc}"}, status=HTTPStatus.BAD_GATEWAY)
            return

        listings = snapshot["listings"]
        wallet_set = set(wallets)
        mine = [row for row in listings if row["seller"] in wallet_set]
        threats = []
        for row in mine:
            rank = int(row["rank_discount"])
            if rank <= 1:
                continue
            above = listings[rank - 2]
            if above["seller"] not in wallet_set:
                threats.append(
                    {
                        "my_token": row["token_id"],
                        "my_seller": row["seller"],
                        "my_rank": row["rank_discount"],
                        "my_discount_pct": row["discount_pct"],
                        "my_price_mon": row["price_mon"],
                        "my_dust_per_mon": row.get("dust_per_mon", 0.0),
                        "competitor_token": above["token_id"],
                        "competitor_rank": above["rank_discount"],
                        "competitor_discount_pct": above["discount_pct"],
                        "competitor_price_mon": above["price_mon"],
                        "competitor_dust_per_mon": above.get("dust_per_mon", 0.0),
                        "competitor_seller": above["seller"],
                    }
                )

        payload = {
            **snapshot,
            "wallet": wallets[0],
            "wallets": wallets,
            "tracked_wallet_count": len(wallets),
            "my_listing_count": len(mine),
            "my_best_rank": min((row["rank_discount"] for row in mine), default=None),
            "my_worst_rank": max((row["rank_discount"] for row in mine), default=None),
            "my_listings": mine,
            "threats": threats,
        }
        self._json(payload)

    def _json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", self.cors_origin)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return

def main() -> int:
    port = int(os.environ.get("PORT", 8787))
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"Dashboard server running on port {port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
