"""Перевірка графіків ДТЕК для кількох адрес. Запускається з GitHub Actions.

Змінні середовища:
  TELEGRAM_BOT_TOKEN  токен бота від @BotFather (якщо порожньо — повідомлення лише в лог)
  TELEGRAM_CHAT_ID    чат за замовчуванням, якщо в targets.json немає "chats"
  ADMIN_CHAT_ID       (необов'язково) куди слати технічні попередження; інакше — у перший чат
  TARGETS_JSON        (необов'язково) налаштування; інакше читається файл targets.json,
                      а якщо і його немає — одна адреса з DTEK_GROUP (Київська область)
  DATA_DIR            тека зі станом і schedule.json (за замовчуванням data)

targets.json: {"chats": [...], "admin_chat": "...", "targets": [...]} або просто [...].
"chats" — особисті чати людей з ботом та/або групи; кожна адреса може мати власні "chats".

Для кожної адреси бот:
  * пише повідомлення, коли графік змінився або з'явився графік на завтра;
  * тримає одне закріплене повідомлення «актуальний графік» і тихо оновлює його.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from dtek_krem import KYIV, SITES, DtekBlocked, DtekClient, DtekError, group_schedule, house_entry, houses

# Кожен слот ділиться на дві половини: (перша, друга)
HALVES = {
    "yes": ("ok", "ok"),
    "no": ("off", "off"),
    "first": ("off", "ok"),
    "second": ("ok", "off"),
    "maybe": ("maybe", "maybe"),
    "mfirst": ("maybe", "ok"),
    "msecond": ("ok", "maybe"),
}
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
STATE_FILE = DATA_DIR / "state.json"
SCHEDULE_FILE = DATA_DIR / "schedule.json"
# ручний запуск кнопкою — помилка робить запуск червоним; запуск від cron-job.org — ні
MANUAL_RUN = (
    os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    and os.environ.get("RUN_SOURCE", "manual") != "cron"
)
_ENV_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CHATS: list[str] = [_ENV_CHAT] if _ENV_CHAT else []
ADMIN_CHAT = os.environ.get("ADMIN_CHAT_ID", "").strip() or _ENV_CHAT


def _chat_list(value) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [str(x).strip() for x in items if str(x or "").strip()]


# ── налаштування адрес ───────────────────────────────────────────────────────

def load_targets() -> list[dict]:
    global CHATS, ADMIN_CHAT
    raw = os.environ.get("TARGETS_JSON", "").strip()
    if not raw and Path("targets.json").exists():
        raw = Path("targets.json").read_text(encoding="utf-8")
    if raw:
        cfg = json.loads(raw)
        if isinstance(cfg, dict):
            CHATS = _chat_list(cfg.get("chats") or cfg.get("chat")) or CHATS
            ADMIN_CHAT = str(cfg.get("admin_chat") or os.environ.get("ADMIN_CHAT_ID") or "").strip() \
                or (CHATS[0] if CHATS else "")
            targets = cfg.get("targets") or []
        else:
            targets = cfg
    else:  # стара схема: одна черга з DTEK_GROUP
        targets = [{"id": "home", "name": "Дім", "site": "krem", "group": os.environ.get("DTEK_GROUP", "").strip()}]

    seen = set()
    for t in targets:
        t.setdefault("site", "krem")
        t.setdefault("name", t.get("id", "?"))
        if not t.get("id") or t["id"] in seen:
            raise SystemExit(f"У targets кожна адреса має мати унікальний id: {t}")
        if t["site"] not in SITES:
            raise SystemExit(f"{t['id']}: site має бути одним із {sorted(SITES)}")
        if not t.get("group") and not (t.get("street") and t.get("house")):
            raise SystemExit(f"{t['id']}: вкажи або group, або street + house (і city, якщо не Київ)")
        t["chats"] = _chat_list(t.get("chats")) or list(CHATS)
        seen.add(t["id"])
    return targets


# ── розбір графіка ───────────────────────────────────────────────────────────

def intervals(slots: dict[str, str]) -> list[dict]:
    """Слоти → інтервали [{"from": "18:00", "to": "21:30", "type": "off"|"maybe"}]."""
    values = list(slots.values())
    if not values:
        return []
    half = 1440 // (len(values) * 2)  # 24 слоти → 30 хв, 48 слотів → 15 хв
    units = [h for v in values for h in HALVES.get(v, ("maybe", "maybe"))]
    result, start = [], None
    for i, kind in enumerate(units + ["ok"]):
        prev = units[i - 1] if i else "ok"
        if kind != prev:
            if prev != "ok":
                result.append({"from": _hhmm(start * half), "to": _hhmm(i * half), "type": prev})
            start = i
    return result


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def short_group(g: str) -> str:
    return g.replace("GPV", "")


def day_hash(groups_slots: dict[str, dict]) -> str:
    # для однієї черги — той самий хеш, що й у попередній версії (без зайвих повідомлень після оновлення)
    body = next(iter(groups_slots.values())) if len(groups_slots) == 1 else groups_slots
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def day_lines(d: date, groups_slots: dict[str, dict], changed: bool = False) -> str:
    lines = [f"{WEEKDAYS[d.weekday()]} {d:%d.%m}" + ("  ✏️ змінено" if changed else "")]
    for g, slots in groups_slots.items():
        if len(groups_slots) > 1:
            lines.append(f"Черга {short_group(g)}:")
        ivs = intervals(slots)
        if not any(iv["type"] == "off" for iv in ivs):
            lines.append("✅ відключень не заплановано")
        for iv in ivs:
            icon = "🔴" if iv["type"] == "off" else "🟡 можливо"
            lines.append(f"{icon} {iv['from']}–{iv['to']}")
    return "\n".join(lines)


def groups_label(groups: list[str]) -> str:
    return ("черга " if len(groups) == 1 else "черги ") + ", ".join(short_group(g) for g in groups)


# ── Telegram ─────────────────────────────────────────────────────────────────

def tg(method: str, **params) -> dict | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not params.get("chat_id"):
        return None
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("result")
    except urllib.error.HTTPError as e:
        desc = json.loads(e.read() or b"{}").get("description", str(e))
        if "message is not modified" in desc:
            return {"not_modified": True}
        print(f"::warning::Telegram {method}: {desc}")
        return None


def notify(text: str, chat: str, silent: bool = False) -> int | None:
    print(f"──── повідомлення → {chat or 'лише лог'} ────\n{text}\n──────────────────────")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("```\n" + text + "\n```\n")
    res = tg("sendMessage", chat_id=chat, text=text, disable_web_page_preview=True, disable_notification=silent)
    return res.get("message_id") if isinstance(res, dict) else None


def update_pinned(tstate: dict, text: str, chats: list[str]) -> None:
    """У кожному чаті одне закріплене повідомлення на адресу: редагуємо, а якщо не вийшло — створюємо нове."""
    h = hashlib.sha256(text.encode()).hexdigest()[:16]
    pins = tstate.setdefault("pins", {})
    tstate.pop("pin_id", None), tstate.pop("pin_hash", None)  # формат попередньої версії
    if not chats:
        print(f"(закріплене без Telegram)\n{text}")
    for chat in chats:
        pin = pins.get(chat) or {}
        if pin.get("hash") == h:
            continue
        mid = pin.get("id")
        ok = mid and tg("editMessageText", chat_id=chat, message_id=mid, text=text, disable_web_page_preview=True)
        if not ok:
            # у новий чат — зі звуком, щоб людина помітила; перестворення — тихо
            mid = notify(text, chat, silent=bool(pin))
            if mid:
                tg("pinChatMessage", chat_id=chat, message_id=mid, disable_notification=True)
        if mid:
            pins[chat] = {"id": mid, "hash": h}
    for chat in [c for c in pins if c not in chats]:  # чат прибрали з налаштувань
        del pins[chat]


# ── стан ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        state = {}
    if "days" in state:  # міграція зі старої версії (одна черга)
        state.setdefault("targets", {})["home"] = {"days": state.pop("days"), "groups": [state.pop("group", "")]}
        if "failure" in state:
            state.setdefault("failures", {})["krem"] = state.pop("failure")
    state.setdefault("targets", {})
    state.setdefault("failures", {})
    return state


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def site_failure(state: dict, site: str, kind: str, message: str, now: datetime) -> None:
    print(f"::warning::{site} {kind}: {message}")
    prev = state["failures"].get(site)
    if prev and prev.get("kind") == kind:
        return
    state["failures"][site] = {"kind": kind, "since": now.isoformat(timespec="minutes"), "message": message}
    what = {
        "blocked": "сайт ДТЕК заблокував запит (Incapsula)",
        "address": "не вдалося знайти адресу або її графік",
    }.get(kind, "не вдалося отримати графік")
    notify(f"⚠️ dtek-{site}: {what}.\n{message}\nПовідомлю, коли знову запрацює.", chat=ADMIN_CHAT)


def site_ok(state: dict, site: str) -> None:
    if state["failures"].pop(site, None):
        notify(f"✅ dtek-{site}: графік знову отримую.", chat=ADMIN_CHAT)


# ── основна логіка ───────────────────────────────────────────────────────────

async def fetch_target(t: dict, client: DtekClient, region_cache: dict) -> tuple[list[str], dict]:
    """Повертає (черги адреси, відповідь з графіком)."""
    if t.get("group"):
        if t["site"] not in region_cache:
            region_cache[t["site"]] = await client.region_schedule()
        return [t["group"]], region_cache[t["site"]]

    city = await client.resolve_city(t.get("city"))
    print(f"[{t['id']}] {client.streets_debug}")
    raw, city = await client.home_num_any(city, t["street"])
    print(f"[{t['id']}] getHomeNum: ключі {list(raw)[:12]}, будинків {len(houses(raw))}, місто в запиті {city!r}")
    try:
        entry = house_entry(raw, str(t["house"]))
    except DtekError as e:
        hint = client.similar_streets(city, t["street"])
        raise DtekError(f"{e}" + (f"; схожі вулиці: {hint}" if hint else "")) from e
    groups = [str(g) for g in (entry.get("sub_type_reason") or [])]
    print(f"[{t['id']}] {city or '(без міста)'}, {t['street']}, {t['house']} → {groups or 'черг немає'}")
    print(f"[{t['id']}] запис будинку: {json.dumps(entry, ensure_ascii=False)[:500]}")
    if not (raw.get("fact") or {}).get("data"):  # графік не прийшов разом з адресою — беремо загальний
        if t["site"] not in region_cache:
            region_cache[t["site"]] = await client.region_schedule()
        raw = region_cache[t["site"]]
    return groups, raw


def process(t: dict, groups: list[str], raw: dict, state: dict, now: datetime) -> dict | None:
    """Порівнює графік із попереднім, шле повідомлення. Повертає графік першої черги для ESP."""
    fact_groups = set()
    for g in ((raw.get("fact") or {}).get("data") or {}).values():
        if isinstance(g, dict):
            fact_groups.update(g)
    missing = [g for g in groups if g not in fact_groups]
    if not groups or missing:
        raise DtekError(
            f"{t['name']}: у графіку немає черг {missing or groups or '—'}; "
            f"є: {', '.join(sorted(fact_groups)[:40]) or '—'}"
        )

    today = now.date()
    scheds = {g: group_schedule(raw, g) for g in groups}
    update = next(iter(scheds.values()))["update"]
    all_days = sorted({d for s in scheds.values() for d in s["days"] if d >= today})
    days = {d: {g: scheds[g]["days"][d] for g in groups if d in scheds[g]["days"]} for d in all_days}

    tstate = state["targets"].setdefault(t["id"], {})
    first_time = "days" not in tstate
    old = tstate.get("days", {})
    new = {d.isoformat(): day_hash(gs) for d, gs in days.items()}
    changed = [d for d in days if old.get(d.isoformat()) != new[d.isoformat()]]
    old_groups = [g for g in tstate.get("groups", []) if g]
    print(f"[{t['id']}] {groups_label(groups)}: днів {len(days)}, змінено: {[str(d) for d in changed] or 'нічого'}")

    header = f"{t['name']} · {groups_label(groups)}"
    group_changed = bool(old_groups) and old_groups != groups
    if (changed or group_changed) and not first_time:
        parts = [f"📅 {header}: графік оновлено (ДТЕК: {update or '—'})"]
        if group_changed:
            parts[0] += f"\n⚠️ Черга змінилась: {', '.join(map(short_group, old_groups))} → {', '.join(map(short_group, groups))}"
        parts += [day_lines(d, gs, d in changed) for d, gs in days.items()]
        for chat in t["chats"]:
            notify("\n\n".join(parts), chat)

    body = [day_lines(d, gs) for d, gs in days.items()] or ["Графік ще не опубліковано"]
    pinned = "\n\n".join([f"📌 {header} — актуальний графік"] + body + [f"Дані ДТЕК: {update or '—'}"])
    update_pinned(tstate, pinned, t["chats"])

    tstate["days"] = new
    tstate["groups"] = groups
    g0 = groups[0]
    slot_count = max((len(s) for s in scheds[g0]["days"].values()), default=0)
    return {
        "target": t["id"],
        "group": g0,
        "dtek_update": update,
        "slot_minutes": 1440 // slot_count if slot_count else None,
        "days": {
            d.isoformat(): {"slots": list(gs[g0].values()), "intervals": intervals(gs[g0])}
            for d, gs in days.items() if g0 in gs
        },
    }


async def run() -> int:
    targets = load_targets()
    state = load_state()
    now = datetime.now(KYIV)
    state["keepalive"] = now.strftime("%Y-%m")
    failed = False
    esp_target = next((t["id"] for t in targets if t.get("esp")), targets[0]["id"])

    for site in sorted({t["site"] for t in targets}):
        site_targets = [t for t in targets if t["site"] == site]
        region_cache: dict = {}
        try:
            async with DtekClient(site) as client:
                results = []
                for t in site_targets:
                    try:
                        groups, raw = await fetch_target(t, client, region_cache)
                    except DtekBlocked:
                        raise
                    except DtekError as e:
                        site_failure(state, site, "address", f"{t['name']}: {e}", now)
                        failed = True
                        continue
                    results.append((t, groups, raw))
        except DtekBlocked as e:
            site_failure(state, site, "blocked", str(e), now)
            failed = True
            continue
        except (DtekError, OSError) as e:
            site_failure(state, site, "error", f"{type(e).__name__}: {e}", now)
            failed = True
            continue

        site_has_errors = len(results) < len(site_targets)
        for t, groups, raw in results:
            try:
                esp = process(t, groups, raw, state, now)
            except DtekError as e:
                site_failure(state, site, "address", str(e), now)
                failed = site_has_errors = True
                continue
            if t["id"] == esp_target and esp:
                save_json(SCHEDULE_FILE, esp)
        if not site_has_errors:
            site_ok(state, site)

    state["targets"] = {k: v for k, v in state["targets"].items() if k in {t["id"] for t in targets}}
    save_json(STATE_FILE, state)
    return 1 if (failed and MANUAL_RUN) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
