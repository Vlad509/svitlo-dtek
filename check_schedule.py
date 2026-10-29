"""Перевірка графіка ДТЕК КРЕМ для однієї черги. Запускається з GitHub Actions.

Змінні середовища:
  DTEK_GROUP          черга, напр. GPV3.1 (якщо порожньо — виведе список доступних черг)
  TELEGRAM_BOT_TOKEN  токен бота від @BotFather (якщо порожньо — повідомлення лише в лог)
  TELEGRAM_CHAT_ID    куди слати
  DATA_DIR            тека зі станом і schedule.json (за замовчуванням data)

Повідомлення в Telegram надходить лише тоді, коли змінився графік на якийсь день
(або з'явився графік на завтра), а також один раз при блокуванні та при відновленні.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

from dtek_krem import KYIV, DtekBlocked, DtekError, DtekKrem, group_schedule

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
MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"


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


def day_hash(slots: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(slots, sort_keys=True).encode()).hexdigest()[:16]


def day_text(d: date, slots: dict[str, str], changed: bool) -> str:
    title = f"{WEEKDAYS[d.weekday()]} {d:%d.%m}" + ("  ✏️ змінено" if changed else "")
    lines = [title]
    ivs = intervals(slots)
    if not any(iv["type"] == "off" for iv in ivs):
        lines.append("✅ відключень не заплановано")
    for iv in ivs:
        icon = "🔴" if iv["type"] == "off" else "🟡 можливо"
        lines.append(f"{icon} {iv['from']}–{iv['to']}")
    return "\n".join(lines)


# ── стан, Telegram ───────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def notify(text: str) -> None:
    print("──── повідомлення ────\n" + text + "\n──────────────────────")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("```\n" + text + "\n```\n")
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не задані — у Telegram не відправляю")
        return
    body = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status != 200:
            print(f"::warning::Telegram відповів HTTP {r.status}")


def available_groups(raw: dict) -> list[str]:
    names = set()
    for groups in ((raw.get("fact") or {}).get("data") or {}).values():
        if isinstance(groups, dict):
            names.update(groups)
    names.update(((raw.get("preset") or {}).get("sch_names") or {}).keys())
    return sorted(names)


# ── основна логіка ───────────────────────────────────────────────────────────

def record_failure(state: dict, kind: str, message: str, now: datetime) -> int:
    print(f"::warning::{kind}: {message}")
    prev = state.get("failure")
    if not prev or prev.get("kind") != kind:
        state["failure"] = {"kind": kind, "since": now.isoformat(timespec="minutes"), "message": message}
        what = "сайт ДТЕК заблокував запит (Incapsula)" if kind == "blocked" else "не вдалося отримати графік"
        notify(f"⚠️ Графік ДТЕК: {what}.\n{message}\nПовідомлю, коли знову запрацює.")
    save_json(STATE_FILE, state)
    return 1 if MANUAL_RUN else 0  # ручний запуск — червоний, за розкладом — без листів про помилку


async def run() -> int:
    group = os.environ.get("DTEK_GROUP", "").strip()
    state = load_state()
    now = datetime.now(KYIV)
    state["keepalive"] = now.strftime("%Y-%m")  # щомісячний коміт, щоб GitHub не вимкнув розклад

    try:
        async with DtekKrem() as client:
            raw = await client.region_schedule()
    except DtekBlocked as e:
        return record_failure(state, "blocked", str(e), now)
    except (DtekError, OSError) as e:
        return record_failure(state, "error", f"{type(e).__name__}: {e}", now)

    groups = available_groups(raw)
    print(f"OK: графік отримано, черг у відповіді: {len(groups)}, ДТЕК оновив: {(raw.get('fact') or {}).get('update')}")

    if not group or group not in groups:
        msg = "DTEK_GROUP не задано" if not group else f"черги {group!r} немає у відповіді"
        print(f"::warning::{msg}. Доступні черги: {', '.join(groups) or '—'}")
        save_json(STATE_FILE, state)
        return 1 if group else 0

    if state.pop("failure", None):
        notify("✅ Графік ДТЕК знову отримую.")

    sched = group_schedule(raw, group)
    today = now.date()
    days = {d: s for d, s in sched["days"].items() if d >= today}
    old_hashes = state.get("days", {})
    new_hashes = {d.isoformat(): day_hash(s) for d, s in days.items()}
    changed = [d for d in days if old_hashes.get(d.isoformat()) != new_hashes[d.isoformat()]]
    print(f"Черга {group}: днів у графіку {len(days)}, змінено: {[str(d) for d in changed] or 'нічого'}")

    if changed:
        name = group.replace("GPV", "")
        parts = [f"📅 Графік черги {name} оновлено (ДТЕК: {sched['update'] or '—'})"]
        parts += [day_text(d, s, d in changed) for d, s in sorted(days.items())]
        notify("\n\n".join(parts))

    state["days"] = new_hashes
    state["group"] = group
    save_json(STATE_FILE, state)

    slot_count = max((len(s) for s in days.values()), default=0)
    save_json(SCHEDULE_FILE, {
        "group": group,
        "dtek_update": sched["update"],
        "slot_minutes": 1440 // slot_count if slot_count else None,
        "days": {
            d.isoformat(): {"slots": list(s.values()), "intervals": intervals(s)}
            for d, s in sorted(days.items())
        },
    })
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
