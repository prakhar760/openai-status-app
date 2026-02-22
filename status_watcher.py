#!/usr/bin/env python3
"""Event-driven OpenAI status watcher.

This script watches Statuspage incidents using conditional HTTP requests
(ETag/Last-Modified), adaptive backoff, and update-level deduplication.
It prints only newly observed incident updates that affect OpenAI API products.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp


API_SIGNAL_RE = re.compile(
    r"\b(api|responses|chat completions|assistants|embeddings|images|audio|files|batch|fine[- ]?tuning)\b",
    re.IGNORECASE,
)


@dataclass
class PageState:
    etag: str | None = None
    last_modified: str | None = None
    known_updates: set[str] = field(default_factory=set)
    interval_sec: float = 20.0


class StatusPageWatcher:
    def __init__(
        self,
        page_label: str,
        base_url: str,
        min_interval_sec: float = 20.0,
        max_interval_sec: float = 300.0,
        timeout_sec: float = 20.0,
    ) -> None:
        self.page_label = page_label
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v2/incidents.json"
        self.min_interval_sec = min_interval_sec
        self.max_interval_sec = max_interval_sec
        self.timeout_sec = timeout_sec
        self.state = PageState(interval_sec=min_interval_sec)

    async def seed_baseline(self, session: aiohttp.ClientSession) -> None:
        """Load current updates into memory so only future updates are emitted."""
        payload, _ = await self._fetch(session)
        if not payload:
            return

        for incident in payload.get("incidents", []):
            for update in incident.get("incident_updates", []):
                update_id = str(update.get("id", "")).strip()
                if update_id:
                    self.state.known_updates.add(update_id)

    async def run_forever(self, session: aiohttp.ClientSession) -> None:
        while True:
            changed = False
            try:
                payload, changed = await self._fetch(session)
                if payload and changed:
                    emitted = self._process_payload(payload)
                    if emitted:
                        self.state.interval_sec = self.min_interval_sec
                    else:
                        self._backoff()
                else:
                    self._backoff()
            except Exception as exc:  # noqa: BLE001
                ts = _fmt_now()
                print(f"[{ts}] {self.page_label}: watcher error: {exc}")
                self._backoff()

            # Add small jitter to avoid synchronized bursts across many pages.
            jitter = random.uniform(0.0, max(self.state.interval_sec * 0.2, 1.0))
            await asyncio.sleep(self.state.interval_sec + jitter)

    async def _fetch(self, session: aiohttp.ClientSession) -> tuple[dict[str, Any] | None, bool]:
        headers: dict[str, str] = {}
        if self.state.etag:
            headers["If-None-Match"] = self.state.etag
        if self.state.last_modified:
            headers["If-Modified-Since"] = self.state.last_modified

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        async with session.get(self.api_url, headers=headers, timeout=timeout) as resp:
            if resp.status == 304:
                return None, False
            resp.raise_for_status()

            if resp.headers.get("ETag"):
                self.state.etag = resp.headers["ETag"]
            if resp.headers.get("Last-Modified"):
                self.state.last_modified = resp.headers["Last-Modified"]

            text = await resp.text()
            return json.loads(text), True

    def _process_payload(self, payload: dict[str, Any]) -> int:
        emitted = 0
        incidents = payload.get("incidents", [])
        for incident in incidents:
            incident_name = incident.get("name", "")
            components = incident.get("components", [])
            component_names = [c.get("name", "") for c in components if c.get("name")]

            if not self._is_api_related(incident_name, component_names):
                continue

            latest_update = self._latest_update(incident.get("incident_updates", []))
            if not latest_update:
                continue

            update_id = str(latest_update.get("id", "")).strip()
            if not update_id or update_id in self.state.known_updates:
                continue

            self.state.known_updates.add(update_id)
            emitted += 1

            product = self._format_product(component_names)
            message = latest_update.get("body") or latest_update.get("status") or "No details provided"
            timestamp = latest_update.get("updated_at") or latest_update.get("created_at")
            ts = _fmt_iso(timestamp)

            print(f"[{ts}] Product: {product}")
            print(f"Status: {message}")
            print()

        return emitted

    def _format_product(self, component_names: list[str]) -> str:
        if not component_names:
            return f"{self.page_label} API"

        cleaned = []
        for name in component_names:
            n = name.strip()
            if n and n not in cleaned:
                cleaned.append(n)

        if not cleaned:
            return f"{self.page_label} API"

        return f"{self.page_label} - {', '.join(cleaned)}"

    @staticmethod
    def _latest_update(updates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not updates:
            return None
        return max(
            updates,
            key=lambda u: _parse_iso(
                u.get("updated_at") or u.get("created_at") or "1970-01-01T00:00:00Z"
            ),
        )

    @staticmethod
    def _is_api_related(incident_name: str, component_names: list[str]) -> bool:
        if API_SIGNAL_RE.search(incident_name or ""):
            return True
        for comp in component_names:
            if API_SIGNAL_RE.search(comp):
                return True
        return False

    def _backoff(self) -> None:
        self.state.interval_sec = min(self.state.interval_sec * 1.5, self.max_interval_sec)


async def _run_watchers(watchers: list[StatusPageWatcher]) -> None:
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(w.seed_baseline(session) for w in watchers))
        await asyncio.gather(*(w.run_forever(session) for w in watchers))


def _parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _fmt_iso(value: str | None) -> str:
    if not value:
        return _fmt_now()
    try:
        dt = _parse_iso(value)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return _fmt_now()


def _fmt_now() -> str:
    return datetime.now(tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch OpenAI status page incidents and print new API-related updates."
    )
    parser.add_argument(
        "--url",
        default="https://status.openai.com",
        help="Status page base URL (default: https://status.openai.com)",
    )
    parser.add_argument(
        "--label",
        default="OpenAI API",
        help="Label shown in output (default: OpenAI API)",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=20.0,
        help="Minimum check interval in seconds (default: 20)",
    )
    parser.add_argument(
        "--max-interval",
        type=float,
        default=300.0,
        help="Maximum check interval in seconds (default: 300)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    watcher = StatusPageWatcher(
        page_label=args.label,
        base_url=args.url,
        min_interval_sec=max(5.0, args.min_interval),
        max_interval_sec=max(args.max_interval, args.min_interval),
    )
    asyncio.run(_run_watchers([watcher]))


if __name__ == "__main__":
    main()
