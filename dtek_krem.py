"""Мінімальний клієнт графіків ДТЕК Київські регіональні електромережі.

Протокол взято з dtek-blackout-client 0.1.12 (MIT) і спрощено:
  * GET  /ua/shutdowns  -> cookies Incapsula + CSRF-токен (meta csrf-token)
  * POST /ua/ajax        -> JSON (form-urlencoded, формат jQuery serializeArray)
      method=getStreets                 -> {"streets": {місто: [вулиці]}}
      method=getHomeNum + city, street  -> {"data": {будинок: {"sub_type_reason": ["GPV3.1"]}}}
      method=checkDisconUpdate          -> {"preset": ..., "fact": ...} графік УСІХ черг

Incapsula пропускає curl_cffi з імітацією TLS-відбитка Chrome.
Якщо ні — DtekBlocked, і тоді cookies треба отримати браузером (Playwright).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from curl_cffi.requests import AsyncSession

BASE = "https://www.dtek-krem.com.ua"
PAGE = f"{BASE}/ua/shutdowns"
AJAX = f"{BASE}/ua/ajax"
KYIV = ZoneInfo("Europe/Kyiv")

_CSRF_RE = re.compile(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"')
_WAF_MARKERS = ("_Incapsula_Resource", "visid_incap", "incap_ses")

# Статуси клітинок графіка
OUTAGE = {"no", "first", "second"}         # точно без світла (повністю / перша / друга половина)
MAYBE = {"maybe", "mfirst", "msecond"}     # можливе відключення


class DtekError(Exception):
    """Неочікувана відповідь сайту."""


class DtekBlocked(DtekError):
    """Incapsula показала челендж — потрібні cookies з реального браузера."""


def _form(method: str, fields: list[tuple[str, str]] = ()) -> dict[str, str]:
    form = {"method": method}
    for i, (name, value) in enumerate(fields):
        form[f"data[{i}][name]"] = name
        form[f"data[{i}][value]"] = value
    return form


class DtekKrem:
    def __init__(self, cookies: dict[str, str] | None = None):
        self._s = AsyncSession(
            impersonate="chrome",
            timeout=20,
            headers={"Accept-Language": "uk,en;q=0.8", "Referer": PAGE, "Origin": BASE},
        )
        for k, v in (cookies or {}).items():
            self._s.cookies.set(k, v, domain=".dtek-krem.com.ua")
        self._csrf: str | None = None

    async def __aenter__(self) -> DtekKrem:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._s.close()

    def cookies(self) -> dict[str, str]:
        """Поточні cookies — зберігай між запусками, щоб не проходити WAF щоразу."""
        return dict(self._s.cookies.items())

    async def warm_up(self) -> None:
        r = await self._s.get(PAGE)
        if r.status_code in (403, 429) or any(m in r.text for m in _WAF_MARKERS):
            raise DtekBlocked(f"WAF на {PAGE}: HTTP {r.status_code}, {len(r.text)} байт")
        m = _CSRF_RE.search(r.text)
        self._csrf = m.group(1) if m else ""

    async def _post(self, data: dict[str, str]) -> dict:
        if self._csrf is None:
            await self.warm_up()
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        r = await self._s.post(AJAX, data=data, headers=headers)
        if r.status_code in (400, 403, 429) or any(m in r.text[:3000] for m in _WAF_MARKERS):
            raise DtekBlocked(f"WAF на {AJAX}: HTTP {r.status_code}")
        if r.status_code >= 400:
            raise DtekError(f"HTTP {r.status_code}")
        try:
            payload = r.json()
        except ValueError as e:
            raise DtekError(f"не JSON: {r.text[:200]!r}") from e
        if not isinstance(payload, dict) or payload.get("result") is False:
            raise DtekError(f"result=false або не об'єкт: {str(payload)[:200]}")
        return payload

    async def streets(self, city: str) -> list[str]:
        raw = (await self._post({"method": "getStreets"})).get("streets", {})
        return list(raw.get(city, []))

    async def find_groups(self, city: str, street: str, house: str) -> list[str]:
        """Черги для адреси, напр. ['GPV3.1']. Викликається один раз при налаштуванні."""
        raw = await self._post(_form("getHomeNum", [("city", city), ("street", street)]))
        data = raw.get("data", {})
        if isinstance(data, list):  # інший формат, який теж трапляється
            data = {str(h.get("num") or h.get("name") or h.get("id")): h for h in data}
        if house not in data:
            raise DtekError(f"будинок {house!r} не знайдено; є: {sorted(data)[:30]}")
        return list(data[house].get("sub_type_reason") or [])

    async def region_schedule(self) -> dict:
        """Графік усіх черг області. Саме це опитуємо періодично — адреса не потрібна."""
        return await self._post({"method": "checkDisconUpdate", "update": "01.01.2000 00:00"})


def group_schedule(raw: dict, group: str) -> dict:
    """Виділяє з відповіді region_schedule дані однієї черги.

    Повертає {"update": "29.10.2026 18:40", "labels": {...}, "days": {date: {слот: статус}}}.
    """
    fact = raw.get("fact") or {}
    preset = raw.get("preset") or {}
    labels = preset.get("time_zone") or {}
    days = {}
    for ts, groups in (fact.get("data") or {}).items():
        if isinstance(groups, dict) and group in groups:
            d = datetime.fromtimestamp(int(ts), KYIV).date()
            days[d] = dict(sorted(groups[group].items(), key=lambda kv: int(kv[0])))
    return {
        "update": fact.get("update"),
        "labels": {k: (v[0] if isinstance(v, list) else v) for k, v in labels.items()},
        "days": days,
    }


def schedule_hash(sched: dict) -> str:
    """Хеш лише клітинок графіка: змінюється тільки при реальній зміні, не від поля update."""
    body = json.dumps({str(d): s for d, s in sched["days"].items()}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def render_bar(slots: dict[str, str]) -> str:
    """Текстова смужка: 🟢 є, 🔴 нема, 🟠 частково, 🟡 можливо."""
    icon = {"yes": "🟢", "no": "🔴", "first": "🟠", "second": "🟠"}
    return "".join(icon.get(v, "🟡" if v in MAYBE else "⚪") for v in slots.values())


if __name__ == "__main__":
    import asyncio
    import sys

    async def main() -> None:
        async with DtekKrem() as c:
            if len(sys.argv) == 4:
                print("Черги:", await c.find_groups(*sys.argv[1:4]))
                return
            group = sys.argv[1] if len(sys.argv) > 1 else "GPV1.1"
            sched = group_schedule(await c.region_schedule(), group)
            print(f"{group}, оновлено: {sched['update']}, hash {schedule_hash(sched)}")
            for d, slots in sched["days"].items():
                print(d, render_bar(slots))

    asyncio.run(main())
