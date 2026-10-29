"""Мінімальний клієнт графіків ДТЕК: Київські регіональні (krem) і Київські (kem) електромережі.

Протокол взято з dtek-blackout-client 0.1.12 (MIT) і спрощено:
  * GET  /ua/shutdowns  -> cookies Incapsula + CSRF-токен (meta csrf-token)
  * POST /ua/ajax        -> JSON (form-urlencoded, формат jQuery serializeArray)
      method=getStreets                 -> {"streets": {місто: [вулиці]}}
      method=getHomeNum + city, street  -> {"data": {будинок: {"sub_type_reason": ["GPV3.1"]}}, ...}
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

SITES = {
    "krem": "https://www.dtek-krem.com.ua",  # Київська область
    "kem": "https://www.dtek-kem.com.ua",    # місто Київ
}
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


class DtekClient:
    def __init__(self, site: str = "krem", cookies: dict[str, str] | None = None):
        if site not in SITES:
            raise ValueError(f"невідомий сайт {site!r}, є: {sorted(SITES)}")
        self.site = site
        self.base = SITES[site]
        self.page = f"{self.base}/ua/shutdowns"
        self.ajax = f"{self.base}/ua/ajax"
        self._s = AsyncSession(
            impersonate="chrome",
            timeout=20,
            headers={"Accept-Language": "uk,en;q=0.8", "Referer": self.page, "Origin": self.base},
        )
        domain = "." + self.base.split("//www.")[-1]
        for k, v in (cookies or {}).items():
            self._s.cookies.set(k, v, domain=domain)
        self._csrf: str | None = None
        self._streets: dict | None = None
        self.streets_debug = ""

    async def __aenter__(self) -> DtekClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._s.close()

    def cookies(self) -> dict[str, str]:
        """Поточні cookies — зберігай між запусками, щоб не проходити WAF щоразу."""
        return dict(self._s.cookies.items())

    async def warm_up(self) -> None:
        r = await self._s.get(self.page)
        m = _CSRF_RE.search(r.text)
        if m:  # справжня сторінка (Incapsula вставляє свій скрипт і в неї — це нормально)
            self._csrf = m.group(1)
            return
        if r.status_code in (403, 429) or any(x in r.text for x in _WAF_MARKERS):
            raise DtekBlocked(f"WAF на {self.page}: HTTP {r.status_code}, {len(r.text)} байт")
        self._csrf = ""  # сторінка без токена — пробуємо AJAX без нього

    async def _post(self, data: dict[str, str]) -> dict:
        if self._csrf is None:
            await self.warm_up()
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        r = await self._s.post(self.ajax, data=data, headers=headers)
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if payload is None:  # не JSON: або челендж WAF, або інша помилка сайту
            if r.status_code in (400, 403, 429) or any(x in r.text for x in _WAF_MARKERS):
                raise DtekBlocked(f"WAF на {self.ajax}: HTTP {r.status_code}, {len(r.text)} байт")
            raise DtekError(f"HTTP {r.status_code}, не JSON: {r.text[:200]!r}")
        if r.status_code >= 400:
            raise DtekError(f"HTTP {r.status_code}: {str(payload)[:200]}")
        if not isinstance(payload, dict) or payload.get("result") is False:
            raise DtekError(f"result=false або не об'єкт: {str(payload)[:200]}")
        return payload

    async def all_streets(self) -> dict[str, list[str]]:
        """{місто: [вулиці]} для всього сайту (кешується на час сесії).

        На dtek-krem вулиці згруповані за містами. На dtek-kem (Київ) може прийти просто
        список вулиць — тоді місто записуємо як "" (запит без міста).
        """
        if self._streets is None:
            resp = await self._post({"method": "getStreets"})
            raw = resp.get("streets") or resp.get("data") or {}
            self.streets_debug = (
                f"getStreets: ключі відповіді {list(resp)[:10]}, "
                f"тип streets={type(resp.get('streets')).__name__}, тип data={type(resp.get('data')).__name__}"
            )
            if isinstance(raw, list):
                raw = {"": [str(x) for x in raw]}
            elif isinstance(raw, dict) and raw and all(isinstance(v, str) for v in raw.values()):
                raw = {"": list(raw.values())}  # {id: назва вулиці}
            self._streets = raw if isinstance(raw, dict) else {}
            n = sum(len(v) for v in self._streets.values() if isinstance(v, list))
            self.streets_debug += f"; міст {len(self._streets)}, вулиць {n}"
        return self._streets

    async def streets(self, city: str) -> list[str]:
        return list((await self.all_streets()).get(city, []))

    async def resolve_city(self, city: str | None) -> str:
        """Назва міста точно як на сайті. Для Києва місто можна не вказувати."""
        cities = list(await self.all_streets())
        if cities == [""]:
            return ""  # сайт без поділу на міста
        if city and city in cities:
            return city
        if city:
            near = [c for c in cities if city.lower().replace("м. ", "") in c.lower()]
            if len(near) == 1:
                return near[0]
            raise DtekError(f"місто {city!r} не знайдено; схожі: {near[:15] or cities[:15]}")
        if len(cities) == 1:
            return cities[0]
        kyiv = [c for c in cities if c.lower() in ("м. київ", "київ")]
        if kyiv:
            return kyiv[0]
        if not cities:
            return ""  # список вулиць не прийшов — спробуємо запит без міста
        raise DtekError(f"вкажи місто; на сайті є: {cities[:20]}")

    def similar_streets(self, city: str, street: str) -> list[str]:
        """Вулиці з тим самим головним словом — підказка, якщо назву написано не так, як на сайті."""
        words = [w for w in street.lower().replace(".", " ").split() if len(w) > 3]
        pool = (self._streets or {}).get(city, [])
        return [s for s in pool if any(w in s.lower() for w in words)][:15]

    async def home_num(self, city: str, street: str) -> dict:
        """Сира відповідь getHomeNum: будинки вулиці (+ часто графік fact/preset)."""
        fields = [("city", city), ("street", street)] if city else [("street", street)]
        return await self._post(_form("getHomeNum", fields))

    async def home_num_any(self, city: str, street: str) -> tuple[dict, str]:
        """getHomeNum з кількома варіантами міста (для Києва формат запиту може відрізнятися)."""
        variants = [city] + ([v for v in ("м. Київ", "") if v != city] if self.site == "kem" else [])
        raw: dict = {}
        for v in variants:
            raw = await self.home_num(v, street)
            if houses(raw):
                return raw, v
        return raw, city

    async def find_groups(self, city: str, street: str, house: str) -> list[str]:
        """Черги для адреси, напр. ['GPV3.1']."""
        return house_entry(await self.home_num(city, street), house).get("sub_type_reason") or []

    async def region_schedule(self) -> dict:
        """Графік усіх черг сайту. Адреса не потрібна."""
        return await self._post({"method": "checkDisconUpdate", "update": "01.01.2000 00:00"})


class DtekKrem(DtekClient):
    """Зворотна сумісність: клієнт Київської області."""

    def __init__(self, cookies: dict[str, str] | None = None):
        super().__init__("krem", cookies)


def houses(raw: dict) -> dict[str, dict]:
    data = raw.get("data") or {}
    if isinstance(data, list):  # інший формат, який теж трапляється
        data = {str(h.get("num") or h.get("name") or h.get("id")): h for h in data if isinstance(h, dict)}
    return data if isinstance(data, dict) else {}


def house_entry(raw: dict, house: str) -> dict:
    data = houses(raw)
    if house in data:
        return data[house]
    norm = {k.replace(" ", "").lower(): k for k in data}
    key = norm.get(house.replace(" ", "").lower())
    if key:
        return data[key]
    similar = [k for k in data if k[:1] == house[:1]] or list(data)
    street = "вулиця не знайдена або порожня" if not data else f"схожі номери: {sorted(similar)[:30]}"
    raise DtekError(f"будинок {house!r} не знайдено; {street}")


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

    USAGE = """Використання:
  python dtek_krem.py [krem|kem] cities                       — міста на сайті
  python dtek_krem.py [krem|kem] streets "м. Київ"            — вулиці міста
  python dtek_krem.py [krem|kem] "м. Київ" "вул. Хрещатик" 1  — черги для адреси
  python dtek_krem.py [krem|kem] GPV3.1                       — графік черги"""

    async def main(args: list[str]) -> None:
        site = args.pop(0) if args and args[0] in SITES else "krem"
        async with DtekClient(site) as c:
            if args == ["cities"]:
                print("\n".join(await c.all_streets()))
            elif len(args) == 2 and args[0] == "streets":
                print("\n".join(await c.streets(await c.resolve_city(args[1]))))
            elif len(args) == 3:
                city = await c.resolve_city(args[0])
                raw = await c.home_num(city, args[1])
                entry = house_entry(raw, args[2])
                print("Місто:", city)
                print("Запис будинку:", json.dumps(entry, ensure_ascii=False))
                print("Черги:", entry.get("sub_type_reason"))
            elif len(args) == 1:
                group = args[0]
                sched = group_schedule(await c.region_schedule(), group)
                print(f"{group}, оновлено: {sched['update']}, hash {schedule_hash(sched)}")
                for d, slots in sched["days"].items():
                    print(d, render_bar(slots))
            else:
                print(USAGE)

    asyncio.run(main(sys.argv[1:]))
