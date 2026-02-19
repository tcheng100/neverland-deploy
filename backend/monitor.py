#!/usr/bin/env python3
"""OpenSea-only rank and undercut monitor for Neverland marketplace listings."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


API_URL = "https://app.neverland.money/api/marketplace/opensea"
DEFAULT_SLUG = "voting-escrow-dust"
MONITOR_TITLE = "Neverland OpenSea Monitor"


@dataclass(frozen=True)
class NormalizedListing:
    rank: int
    token_id: str
    order_hash: str
    seller: str
    contract: str
    price_native: float
    price_wei: int
    currency: str
    asset_url: str

    def to_state_dict(self) -> Dict[str, Any]:
        return {
            "rank": self.rank,
            "token_id": self.token_id,
            "order_hash": self.order_hash,
            "seller": self.seller,
            "contract": self.contract,
            "price_native": self.price_native,
            "price_wei": self.price_wei,
            "currency": self.currency,
            "asset_url": self.asset_url,
        }


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{now_utc_iso()}] {message}", flush=True)


def format_mon(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def canonical_wallets(csv_wallets: str) -> set[str]:
    wallets = {part.strip().lower() for part in csv_wallets.split(",") if part.strip()}
    return wallets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Neverland marketplace rank/undercut changes using OpenSea data only."
    )
    parser.add_argument("--slug", default=DEFAULT_SLUG, help="OpenSea collection slug used by Neverland API.")
    parser.add_argument("--wallets", default="", help="Comma-separated seller wallet addresses to monitor.")
    parser.add_argument("--poll-seconds", type=int, default=20, help="Polling interval in seconds.")
    parser.add_argument("--limit", type=int, default=200, help="Listings fetched per API page.")
    parser.add_argument("--max-pages", type=int, default=20, help="Maximum pages to fetch each poll.")
    parser.add_argument("--top-n", type=int, default=25, help="Track rank changes for top N listings.")
    parser.add_argument(
        "--state-file",
        default=str(Path.home() / ".neverland_opensea_monitor_state.json"),
        help="Path to JSON file used to persist previous snapshot and dedupe alerts.",
    )
    parser.add_argument(
        "--min-undercut-mon",
        type=float,
        default=0.0,
        help="Only alert undercuts >= this MON amount.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=20, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=4, help="Retries for API calls.")
    parser.add_argument("--discord-webhook-url", default="", help="Optional Discord webhook URL for alerts.")
    parser.add_argument(
        "--on-undercut-cmd",
        default="",
        help="Optional shell command to execute for undercut events; event JSON is passed in NEVERLAND_EVENT_JSON.",
    )
    parser.add_argument(
        "--on-rank-change-cmd",
        default="",
        help="Optional shell command to execute when top-N ranking changes; event JSON in NEVERLAND_EVENT_JSON.",
    )
    parser.add_argument(
        "--notify-mac",
        action="store_true",
        help="Show macOS desktop notifications via osascript.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll iteration and exit.",
    )
    parser.add_argument(
        "--print-top",
        type=int,
        default=5,
        help="Print current top K listings each poll.",
    )
    return parser.parse_args()


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"last_snapshot": None, "seen_events": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"last_snapshot": None, "seen_events": {}}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen_events = state.get("seen_events", {})
    if isinstance(seen_events, dict) and len(seen_events) > 5000:
        trimmed_keys = sorted(seen_events.keys())[-5000:]
        state["seen_events"] = {k: seen_events[k] for k in trimmed_keys}
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def request_json_with_retry(
    session: requests.Session,
    params: Dict[str, Any],
    timeout_seconds: int,
    retries: int,
) -> Dict[str, Any]:
    delay = 1.0
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(API_URL, params=params, timeout=timeout_seconds)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise requests.HTTPError(
                    f"retryable status={response.status_code}: {response.text[:300]}",
                    response=response,
                )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object response.")
            return payload
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == retries:
                break
            time.sleep(delay)
            delay = min(delay * 2, 20.0)
    raise RuntimeError(f"Failed request after {retries} retries: {last_exc}")


def fetch_all_opensea_listings(
    session: requests.Session,
    slug: str,
    limit: int,
    max_pages: int,
    timeout_seconds: int,
    retries: int,
) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    next_cursor: Optional[str] = None
    for _ in range(max_pages):
        params: Dict[str, Any] = {"slug": slug, "limit": str(limit)}
        if next_cursor:
            params["next"] = next_cursor
        payload = request_json_with_retry(session, params, timeout_seconds, retries)
        rows = payload.get("listings") or []
        if not isinstance(rows, list):
            rows = []
        all_rows.extend(row for row in rows if isinstance(row, dict))
        next_cursor = payload.get("next")
        if not next_cursor:
            break
    return all_rows


def to_native_price(value_wei: Any, decimals: Any) -> Tuple[int, float]:
    try:
        wei = int(str(value_wei))
    except (TypeError, ValueError):
        wei = 0
    try:
        dec = int(decimals)
    except (TypeError, ValueError):
        dec = 18
    if dec < 0:
        dec = 18
    return wei, wei / (10**dec)


def parse_listing(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    status = str(raw.get("status", "")).upper()
    if status and status != "ACTIVE":
        return None

    remaining_qty = raw.get("remaining_quantity", 1)
    try:
        if int(remaining_qty) <= 0:
            return None
    except (TypeError, ValueError):
        pass

    params = (((raw.get("protocol_data") or {}).get("parameters")) or {})
    offer = params.get("offer") or []
    if not offer or not isinstance(offer, list):
        return None
    offer0 = offer[0] if isinstance(offer[0], dict) else {}
    token_id = str(offer0.get("identifierOrCriteria", "")).strip()
    if not token_id:
        return None

    contract = str(offer0.get("token", "")).strip().lower()
    seller = str(params.get("offerer", "")).strip().lower()
    order_hash = str(raw.get("order_hash", "")).strip().lower()
    current = ((raw.get("price") or {}).get("current")) or {}
    price_wei, price_native = to_native_price(current.get("value"), current.get("decimals"))
    currency = str(current.get("currency", "MON"))
    chain = str(raw.get("chain", "monad")).lower()
    asset_url = f"https://opensea.io/assets/{chain}/{contract}/{token_id}" if contract else ""
    return {
        "token_id": token_id,
        "order_hash": order_hash,
        "seller": seller,
        "contract": contract,
        "price_native": price_native,
        "price_wei": price_wei,
        "currency": currency,
        "asset_url": asset_url,
    }


def normalize_rows(raw_listings: Iterable[Dict[str, Any]]) -> List[NormalizedListing]:
    dedup: Dict[str, Dict[str, Any]] = {}
    for raw in raw_listings:
        parsed = parse_listing(raw)
        if not parsed:
            continue
        token_id = parsed["token_id"]
        existing = dedup.get(token_id)
        if existing is None:
            dedup[token_id] = parsed
            continue
        if parsed["price_native"] < existing["price_native"]:
            dedup[token_id] = parsed
            continue
        if parsed["price_native"] == existing["price_native"] and parsed["order_hash"] < existing["order_hash"]:
            dedup[token_id] = parsed

    ordered = sorted(dedup.values(), key=lambda row: (row["price_native"], row["token_id"]))
    results: List[NormalizedListing] = []
    for idx, row in enumerate(ordered, start=1):
        results.append(
            NormalizedListing(
                rank=idx,
                token_id=row["token_id"],
                order_hash=row["order_hash"],
                seller=row["seller"],
                contract=row["contract"],
                price_native=row["price_native"],
                price_wei=row["price_wei"],
                currency=row["currency"],
                asset_url=row["asset_url"],
            )
        )
    return results


def make_snapshot(listings: List[NormalizedListing]) -> Dict[str, Any]:
    return {
        "captured_at": now_utc_iso(),
        "listings": [item.to_state_dict() for item in listings],
    }


def listings_from_snapshot(snapshot: Optional[Dict[str, Any]]) -> List[NormalizedListing]:
    if not snapshot:
        return []
    rows = snapshot.get("listings") or []
    parsed: List[NormalizedListing] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            parsed.append(
                NormalizedListing(
                    rank=int(row["rank"]),
                    token_id=str(row["token_id"]),
                    order_hash=str(row["order_hash"]),
                    seller=str(row["seller"]).lower(),
                    contract=str(row["contract"]).lower(),
                    price_native=float(row["price_native"]),
                    price_wei=int(row["price_wei"]),
                    currency=str(row["currency"]),
                    asset_url=str(row.get("asset_url", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return parsed


def detect_top_rank_change(
    previous: List[NormalizedListing], current: List[NormalizedListing], top_n: int
) -> Optional[Dict[str, Any]]:
    prev_top = [row.token_id for row in previous[:top_n]]
    curr_top = [row.token_id for row in current[:top_n]]
    if prev_top == curr_top:
        return None

    moved_positions: List[Dict[str, Any]] = []
    max_len = min(top_n, max(len(prev_top), len(curr_top)))
    for idx in range(max_len):
        prev_token = prev_top[idx] if idx < len(prev_top) else None
        curr_token = curr_top[idx] if idx < len(curr_top) else None
        if prev_token != curr_token:
            moved_positions.append(
                {
                    "position": idx + 1,
                    "before_token": prev_token,
                    "after_token": curr_token,
                }
            )
        if len(moved_positions) >= 12:
            break

    event_id = f"rank_top_{top_n}:{'|'.join(prev_top)}=>{'|'.join(curr_top)}"
    return {
        "event_type": "rank_change",
        "event_id": event_id,
        "top_n": top_n,
        "moved_positions": moved_positions,
        "before_top": prev_top[:10],
        "after_top": curr_top[:10],
    }


def detect_wallet_rank_changes(
    previous: List[NormalizedListing],
    current: List[NormalizedListing],
    wallets: set[str],
) -> List[Dict[str, Any]]:
    if not wallets:
        return []
    prev_map = {row.token_id: row for row in previous if row.seller in wallets}
    curr_map = {row.token_id: row for row in current if row.seller in wallets}

    events: List[Dict[str, Any]] = []

    for token_id, row in curr_map.items():
        prev = prev_map.get(token_id)
        if prev is None:
            events.append(
                {
                    "event_type": "wallet_listing_new",
                    "event_id": f"wallet_new:{token_id}:{row.order_hash}",
                    "token_id": token_id,
                    "new_rank": row.rank,
                    "new_price": row.price_native,
                    "seller": row.seller,
                    "asset_url": row.asset_url,
                }
            )
            continue
        if prev.rank != row.rank:
            events.append(
                {
                    "event_type": "wallet_rank_changed",
                    "event_id": f"wallet_rank:{token_id}:{prev.rank}->{row.rank}:{row.order_hash}",
                    "token_id": token_id,
                    "old_rank": prev.rank,
                    "new_rank": row.rank,
                    "old_price": prev.price_native,
                    "new_price": row.price_native,
                    "seller": row.seller,
                    "asset_url": row.asset_url,
                }
            )

    for token_id, row in prev_map.items():
        if token_id not in curr_map:
            events.append(
                {
                    "event_type": "wallet_listing_missing",
                    "event_id": f"wallet_missing:{token_id}:{row.order_hash}",
                    "token_id": token_id,
                    "old_rank": row.rank,
                    "old_price": row.price_native,
                    "seller": row.seller,
                    "asset_url": row.asset_url,
                }
            )
    return events


def detect_undercuts(
    current: List[NormalizedListing],
    wallets: set[str],
    min_undercut_mon: float,
) -> List[Dict[str, Any]]:
    if not wallets:
        return []
    events: List[Dict[str, Any]] = []
    for row in current:
        if row.seller not in wallets:
            continue
        if row.rank <= 1:
            continue
        above = current[row.rank - 2]
        if above.seller in wallets:
            continue
        if above.price_native >= row.price_native:
            continue
        undercut_by = row.price_native - above.price_native
        if undercut_by + 1e-12 < min_undercut_mon:
            continue
        undercut_pct = (undercut_by / row.price_native * 100) if row.price_native > 0 else 0.0
        event_id = (
            f"undercut:{row.token_id}:{row.order_hash}:{above.order_hash}:"
            f"{format_mon(above.price_native)}:{format_mon(row.price_native)}"
        )
        events.append(
            {
                "event_type": "undercut",
                "event_id": event_id,
                "token_id": row.token_id,
                "seller": row.seller,
                "self_rank": row.rank,
                "self_price": row.price_native,
                "self_order_hash": row.order_hash,
                "self_asset_url": row.asset_url,
                "competitor_rank": above.rank,
                "competitor_token_id": above.token_id,
                "competitor_seller": above.seller,
                "competitor_price": above.price_native,
                "competitor_order_hash": above.order_hash,
                "competitor_asset_url": above.asset_url,
                "undercut_by": undercut_by,
                "undercut_pct": undercut_pct,
            }
        )
    return events


def post_discord(webhook_url: str, message: str) -> None:
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"content": message[:1900]}, timeout=10)
    except Exception as exc:  # noqa: BLE001
        log(f"Discord webhook error: {exc}")


def mac_notify(message: str) -> None:
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    title = MONITOR_TITLE.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{escaped}" with title "{title}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception as exc:  # noqa: BLE001
        log(f"macOS notification error: {exc}")


def run_event_command(cmd: str, event: Dict[str, Any]) -> None:
    if not cmd:
        return
    env = os.environ.copy()
    env["NEVERLAND_EVENT_JSON"] = json.dumps(event)
    env["NEVERLAND_EVENT_TYPE"] = str(event.get("event_type", ""))
    try:
        subprocess.run(cmd, shell=True, check=False, env=env)
    except Exception as exc:  # noqa: BLE001
        log(f"Event command error: {exc}")


def event_message(event: Dict[str, Any]) -> str:
    event_type = event.get("event_type")
    if event_type == "undercut":
        return (
            "Undercut detected: token #{token} now rank #{rank}, competitor at #{comp_rank} is "
            "{comp_price} MON vs your {self_price} MON (delta {delta} MON / {pct:.3f}%)."
        ).format(
            token=event["token_id"],
            rank=event["self_rank"],
            comp_rank=event["competitor_rank"],
            comp_price=format_mon(event["competitor_price"]),
            self_price=format_mon(event["self_price"]),
            delta=format_mon(event["undercut_by"]),
            pct=float(event["undercut_pct"]),
        )
    if event_type == "rank_change":
        return (
            "Top-{top_n} ranking changed. New top: {after}"
        ).format(top_n=event["top_n"], after=", ".join(event.get("after_top", [])))
    if event_type == "wallet_rank_changed":
        return (
            "Your token #{token} moved rank {old} -> {new} ({old_price} -> {new_price} MON)."
        ).format(
            token=event["token_id"],
            old=event["old_rank"],
            new=event["new_rank"],
            old_price=format_mon(event["old_price"]),
            new_price=format_mon(event["new_price"]),
        )
    if event_type == "wallet_listing_new":
        return "Your token #{token} appeared at rank #{rank} ({price} MON).".format(
            token=event["token_id"], rank=event["new_rank"], price=format_mon(event["new_price"])
        )
    if event_type == "wallet_listing_missing":
        return "Your token #{token} listing disappeared (was rank #{rank}).".format(
            token=event["token_id"], rank=event["old_rank"]
        )
    return json.dumps(event, ensure_ascii=True)


def print_top(listings: List[NormalizedListing], count: int) -> None:
    if count <= 0:
        return
    top = listings[:count]
    if not top:
        log("No listings returned.")
        return
    simple = ", ".join(f"#{row.rank} token {row.token_id}: {format_mon(row.price_native)} MON" for row in top)
    log(f"Top {len(top)}: {simple}")


def should_emit(event_id: str, seen_events: Dict[str, str]) -> bool:
    return event_id not in seen_events


def main() -> int:
    args = parse_args()
    state_path = Path(args.state_file).expanduser().resolve()
    wallets = canonical_wallets(args.wallets)
    if args.wallets and not wallets:
        log("No valid wallet addresses parsed from --wallets.")
        return 2

    state = load_state(state_path)
    last_snapshot = state.get("last_snapshot")
    seen_events = state.get("seen_events")
    if not isinstance(seen_events, dict):
        seen_events = {}
        state["seen_events"] = seen_events

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "neverland-opensea-monitor/1.0"})

    log(f"Monitoring slug={args.slug} wallets={len(wallets)} poll={args.poll_seconds}s")

    while True:
        try:
            raw_rows = fetch_all_opensea_listings(
                session=session,
                slug=args.slug,
                limit=args.limit,
                max_pages=args.max_pages,
                timeout_seconds=args.timeout_seconds,
                retries=args.retries,
            )
            listings = normalize_rows(raw_rows)
            print_top(listings, args.print_top)

            previous = listings_from_snapshot(last_snapshot)
            events: List[Dict[str, Any]] = []
            if previous:
                rank_event = detect_top_rank_change(previous, listings, args.top_n)
                if rank_event:
                    events.append(rank_event)
                events.extend(detect_wallet_rank_changes(previous, listings, wallets))
                events.extend(detect_undercuts(listings, wallets, args.min_undercut_mon))
            else:
                log("Baseline snapshot created. Next poll will alert on changes.")

            emitted = 0
            for event in events:
                event_id = str(event.get("event_id", ""))
                if not event_id or not should_emit(event_id, seen_events):
                    continue
                seen_events[event_id] = now_utc_iso()
                message = event_message(event)
                log(message)
                post_discord(args.discord_webhook_url, message)
                if args.notify_mac:
                    mac_notify(message)
                if event.get("event_type") == "undercut":
                    run_event_command(args.on_undercut_cmd, event)
                elif event.get("event_type") == "rank_change":
                    run_event_command(args.on_rank_change_cmd, event)
                emitted += 1

            if events and emitted == 0:
                log("Changes detected but all were already emitted previously.")

            last_snapshot = make_snapshot(listings)
            state["last_snapshot"] = last_snapshot
            save_state(state_path, state)
        except KeyboardInterrupt:
            log("Stopping monitor.")
            return 0
        except Exception as exc:  # noqa: BLE001
            log(f"Poll error: {exc}")

        if args.once:
            return 0
        time.sleep(max(3, args.poll_seconds))


if __name__ == "__main__":
    sys.exit(main())
