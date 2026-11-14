#!/usr/bin/env python3
"""
VOX Egypt watcher (City Centre Almaza) - تشغيلة واحدة كل مرة، مصمم لـ GitHub Actions.

كل تشغيلة:
  1. تفتح صفحة الفيلم وتجمع كل الحفلات في فرع الماظة (كل الأيام).
  2. لو ظهرت حفلة/يوم جديد  -> تنبيه تليجرام فيه لينك الحجز.
  3. تفتح خريطة كراسي كل حفلة وتفلتر الكراسي الكويسة بس
     (مش الصفوف الأمامية ومش أطراف الصف) -> تنبيه بالصف والكرسي واللينك.
  4. أي كرسي اتبعت عنه تنبيه قبل كده مش بيتبعت تاني (الحالة محفوظة في state.json).
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
CAIRO = ZoneInfo("Africa/Cairo")
BASE_URL = os.environ.get("VOX_BASE_URL", "https://egy.voxcinemas.com").rstrip("/")
STATE_FILE = Path(os.environ.get("VOX_STATE_FILE", ROOT / "state.json"))
CONFIG_FILE = Path(os.environ.get("VOX_CONFIG_FILE", ROOT / "config.json"))
DEBUG_DIR = Path(os.environ.get("VOX_DEBUG_DIR", ROOT / "debug"))
DEBUG = os.environ.get("DEBUG") == "1"
DRY_RUN = os.environ.get("DRY_RUN") == "1"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_CONFIG = {
    "movie_slug": "avengers-doomsday",
    "movie_name": "Avengers: Doomsday",
    "movie_url": "{base}/movies/{slug}",
    "cinema_name": "City Centre Almaza",
    "cinema_keywords": ["almaza", "الماظة", "المظة"],
    "other_cinema_keywords": [
        "mall of egypt", "alexandria", "مول مصر", "الإسكندرية", "الاسكندرية",
    ],
    # الكراسي الكويسة: نسبة العمق (0 = أول صف، 1 = آخر صف) ونسبة الوسط في الصف
    "min_depth": 0.40,
    "max_depth": 1.0,
    "ideal_depth": 0.65,
    "center_band": [0.20, 0.80],
    "party_size": 1,
    "skip_seat_kinds": ["wheelchair", "companion", "disabled", "handicap"],
    "front_to_back": True,
    "renotify_after_taken": False,
    "notify_new_showtimes": True,
    "max_seats_listed": 12,
    "max_showtimes_per_run": 30,
    "max_date_tabs": 40,
    "max_alert_messages_per_run": 10,
    "failure_alert_after_runs": 3,
    "deadline_seconds": 480,
    "heartbeat_hour": 12,  # رسالة "شغال" يومية بعد الساعة دي بتوقيت القاهرة (null لإلغائها)
}

UNAVAILABLE_WORDS = (
    "unavailable", "sold", "taken", "reserved", "booked", "occupied",
    "blocked", "disabled", "hold", "locked", "unselectable",
)
AVAILABLE_WORDS = {"available", "open", "free", "vacant", "empty", "selectable", "1", "true"}
EXPERIENCES = ["IMAX", "4DX", "MAX", "GOLD", "THEATRE", "KIDS"]
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(AM|PM|am|pm|ص|م)?")
ROW_SEAT_RE = re.compile(r"row\s*([A-Za-z]{1,2})\W+(?:seat\s*)?(\d+)", re.I)
ID_SEAT_RE = re.compile(r"(?<![A-Za-z])([A-Z]{1,2})[-_ ]?(\d{1,2})(?!\d)")
DATE_RES = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b", re.I
    ),
]


# ───────────────────────── إعدادات وحالة ─────────────────────────
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    return cfg


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def log(msg: str) -> None:
    print(f"[{datetime.now(CAIRO):%H:%M:%S}] {msg}", flush=True)


# ───────────────────────── تليجرام ─────────────────────────
def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if DRY_RUN:
        print("──── [DRY_RUN telegram] ────\n" + text + "\n────────────────────────────")
        return True
    if not token or not chat:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID مش متضبطين")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True
        hint = {
            401: "التوكن غلط",
            400: "الـ Chat ID غلط، أو لسه ما ضغطتش Start على البوت",
            403: "البوت متبلوك أو ما بدأتش معاه محادثة (ابعتله /start)",
        }.get(r.status_code, "")
        log(f"فشل إرسال تليجرام: {r.status_code} {r.text[:200]} {hint}")
    except Exception as e:  # noqa: BLE001
        log(f"خطأ تليجرام: {e}")
    return False


def test_telegram() -> int:
    """اختبار سريع: هل التوكن والـ Chat ID شغالين؟ (بيتشغل من زرار في GitHub)"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("❌ الـ Secrets مش متضافة: TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID")
        return 1
    try:
        me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        if me.status_code != 200:
            print(f"❌ التوكن مش مقبول ({me.status_code}). راجعه من BotFather.")
            return 1
        print(f"✅ التوكن سليم - البوت: @{me.json()['result'].get('username')}")
    except Exception as e:  # noqa: BLE001
        print(f"❌ مقدرتش أوصل لتليجرام: {e}")
        return 1
    ok = send_telegram(
        "✅ <b>اختبار VOX watcher</b>\nلو الرسالة دي وصلتك يبقى التوكن والـ Chat ID سليمين "
        "والتنبيهات هتوصلك."
    )
    print("✅ الرسالة اتبعتت، شوف تليجرام" if ok else "❌ الإرسال فشل (شوف السبب فوق)")
    return 0 if ok else 1


def write_summary(lines: list[str]) -> None:
    """ملخص كل تشغيلة يظهر في صفحة الـ run على GitHub."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def maybe_heartbeat(state: dict, cfg: dict, now: datetime, stats: dict) -> None:
    """رسالة 'شغال' مرة في اليوم. لو يوم عدى من غيرها يبقى السكريبت واقف."""
    hour = cfg.get("heartbeat_hour")
    today = now.date().isoformat()
    if hour is None or now.hour < hour or state.get("last_heartbeat") == today:
        return
    if stats["showtimes"]:
        body = (
            f"📋 حفلات متابَعة في الماظة: {stats['showtimes']}\n"
            f"🔍 اتفحص منهم النهاردة: {stats['checked']} | كراسي كويسة فاضية حاليًا: {stats['good']}"
        )
    else:
        body = "📋 لسه مفيش حفلات ظاهرة للفيلم (هبعتلك أول ما تظهر)"
    if send_telegram(
        f"💓 <b>VOX watcher شغال</b>\n🎬 {h(cfg['movie_name'])} - {h(cfg['cinema_name'])}\n"
        f"🕐 آخر فحص: {now:%H:%M}\n{body}"
    ):
        state["last_heartbeat"] = today


def split_message(lines: list[str], limit: int = 3500) -> list[str]:
    chunks, cur = [], ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + ln
    if cur:
        chunks.append(cur)
    return chunks


def send_lines(lines: list[str]) -> bool:
    return all(send_telegram(c) for c in split_message(lines))


# ───────────────────────── منطق الكراسي ─────────────────────────
def status_is_available(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    s = str(v).strip().lower()
    if not s:
        return None
    if any(w in s for w in UNAVAILABLE_WORDS):
        return False
    if s in AVAILABLE_WORDS or "available" in s:
        return True
    return None


def natkey(s: str):
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


def seat_code(s: dict) -> str:
    return f"{s['row']}{s['num']}"


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _pick(d: dict, *names):
    low = {k.lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, ""):
            return v
    return None


def dedupe_seats(seats: list[dict]) -> list[dict]:
    uniq: dict[tuple, dict] = {}
    for s in seats:
        uniq[(s["row"], s["num"])] = s
    return list(uniq.values())


def seats_from_json(payloads: list) -> list[dict]:
    seats = []
    for p in payloads:
        for d in _walk(p):
            row = _pick(d, "row", "rowLabel", "rowName", "rowLetter", "row_label", "rowId")
            num = _pick(d, "seat", "seatNumber", "seatLabel", "seatName", "number",
                        "seat_number", "column", "col")
            status = _pick(d, "status", "availability", "state", "available",
                           "isAvailable", "seatStatus")
            if row is None or num is None or status is None:
                continue
            if any(isinstance(x, (dict, list)) for x in (row, num)):
                continue
            ok = status_is_available(status)
            if ok is None:
                continue
            kind = str(_pick(d, "type", "seatType", "category", "area", "areaName") or "")
            seats.append({"row": str(row).strip().upper(), "num": str(num).strip(),
                          "ok": ok, "kind": kind.lower()})
    return dedupe_seats(seats)


def seats_from_dom(items: list[dict]) -> list[dict]:
    seats = []
    for it in items:
        row = (it.get("row") or "").strip().upper()
        num = (it.get("num") or "").strip()
        label = f"{it.get('aria', '')} {it.get('title', '')}"
        if not row or not num:
            m = ROW_SEAT_RE.search(label) or ID_SEAT_RE.search(it.get("id", ""))
            if not m:
                continue
            row, num = m.group(1).upper(), m.group(2)
        blob = f"{it.get('cls', '')} {label}".lower()
        ok = status_is_available(it["status"]) if it.get("status") else None
        if ok is None:
            if any(w in blob for w in UNAVAILABLE_WORDS):
                ok = False
            elif "available" in blob:
                ok = True
        if ok is None:
            continue
        seats.append({"row": row, "num": num, "ok": ok, "kind": blob})
    return dedupe_seats(seats)


def good_seats(seats: list[dict], cfg: dict) -> list[dict]:
    """الكراسي الفاضية الكويسة بس، مرتبة من الأحسن للأقل."""
    rows: dict[str, list[dict]] = {}
    for s in seats:
        rows.setdefault(s["row"], []).append(s)
    order = sorted(rows, key=lambda r: (len(r), r))
    if not cfg["front_to_back"]:
        order.reverse()
    n = len(order)
    lo, hi = cfg["center_band"]
    party = max(1, int(cfg["party_size"]))
    out: list[dict] = []
    for i, r in enumerate(order):
        depth = (i + 1) / n
        if depth < cfg["min_depth"] or depth > cfg["max_depth"]:
            continue
        row_seats = sorted(rows[r], key=lambda s: natkey(s["num"]))
        m = len(row_seats)
        cand = []
        for j, s in enumerate(row_seats):
            pos = 0.5 if m == 1 else j / (m - 1)
            skipped = any(k in s["kind"] for k in cfg["skip_seat_kinds"])
            cand.append(bool(s["ok"]) and lo <= pos <= hi and not skipped)
        keep = set()
        if party == 1:
            keep = {j for j, c in enumerate(cand) if c}
        else:
            run: list[int] = []
            for j, c in enumerate(cand):
                contiguous = (
                    not run
                    or not (row_seats[j]["num"].isdigit() and row_seats[run[-1]]["num"].isdigit())
                    or int(row_seats[j]["num"]) - int(row_seats[run[-1]]["num"]) == 1
                )
                if c and contiguous:
                    run.append(j)
                else:
                    if len(run) >= party:
                        keep.update(run)
                    run = [j] if c else []
            if len(run) >= party:
                keep.update(run)
        for j in keep:
            pos = 0.5 if m == 1 else j / (m - 1)
            out.append({**row_seats[j], "depth": depth, "pos": pos,
                        "score": abs(depth - cfg["ideal_depth"]) + abs(pos - 0.5)})
    out.sort(key=lambda s: s["score"])
    return out


def fresh_seats(state: dict, key: str, good: list[dict], renotify: bool):
    """يرجع (الكراسي الجديدة، مجموعة المتبعت عنها بعد التحديث)."""
    notified = set(state.get("seats", {}).get(key, []))
    cur = {seat_code(s) for s in good}
    if renotify:
        notified &= cur
    fresh = [s for s in good if seat_code(s) not in notified]
    return fresh, notified | cur


# ───────────────────────── الحفلات ─────────────────────────
def make_key(href: str) -> str:
    u = urlparse(href)  # الرقم من المسار/الـ query بس، مش من الـ host أو الـ port
    tail = f"{u.path}?{u.query}"
    m = re.search(r"(\d{5,})", tail)
    return m.group(1) if m else hashlib.sha1(tail.encode()).hexdigest()[:10]


def detect_experience(text: str, ctx: str) -> str:
    def find(s: str) -> list[str]:
        up = s.upper()
        return [e for e in EXPERIENCES if re.search(rf"(?<![A-Z0-9]){e}(?![A-Z0-9])", up)]

    hit = find(text)
    if hit:
        return hit[0]
    hit = find(ctx)
    return hit[0] if len(set(hit)) == 1 else "STANDARD"


def cinema_hits(text: str, cfg: dict) -> set[str]:
    t = text.lower()
    hits = {"TARGET"} if any(k.lower() in t for k in cfg["cinema_keywords"]) else set()
    hits |= {k for k in cfg["other_cinema_keywords"] if k.lower() in t}
    return hits


def pick_ctx(ctxs: list[str], cfg: dict) -> str:
    """أكبر نص حوالين اللينك بيذكر فرع واحد بس (من الأقرب للأبعد)."""
    best = ""
    for c in ctxs:
        if len(cinema_hits(c, cfg)) > 1:
            break
        best = c
    return best


def is_foreign(body: str, cfg: dict) -> bool:
    """صفحة الحجز بتاعة فرع تاني؟ (بتذكر فرع تاني ومبتذكرش الماظة)"""
    return "TARGET" not in cinema_hits(body, cfg) and bool(cinema_hits(body, cfg))


def to_showtime(it: dict, label: str, cfg: dict, ctx_date: bool = False) -> dict | None:
    m = TIME_RE.search(it.get("text", ""))
    if not m or not it.get("href"):
        return None
    it = dict(it, ctx=pick_ctx(it.get("ctxs") or [it.get("ctx", "")], cfg))
    hits = cinema_hits(f"{it['href']} {it['ctx']}", cfg)
    if hits and "TARGET" not in hits:
        return None  # الحفلة دي لفرع تاني
    time_str = f"{m.group(1)}:{m.group(2)}" + (f" {m.group(3)}" if m.group(3) else "")
    date = label
    if not date and ctx_date:  # احتياطي لو مفيش تابات أيام في الصفحة
        for rx in DATE_RES:
            dm = rx.search(it.get("ctx", ""))
            if dm:
                date = dm.group(0)
                break
    return {
        "key": make_key(it["href"]),
        "url": it["href"],
        "time": time_str,
        "exp": detect_experience(it.get("text", ""), it.get("ctx", "")),
        "date": date,
    }


SHOWTIME_JS = r"""
() => {
  const out = [];
  for (const a of document.querySelectorAll("a[href]")) {
    const href = a.href || "";
    if (!/book|session|showtime|ticket|seat|checkout|order/i.test(href)) continue;
    const text = (a.innerText || a.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80);
    const ctxs = [];
    let n = a.parentElement;
    for (let i = 0; i < 10 && n; i++, n = n.parentElement) {
      const t = (n.innerText || "").replace(/\s+/g, " ").trim();
      if (t.length > 1500) break;
      ctxs.push(t);
    }
    out.push({href, text, ctxs});
  }
  return out;
}
"""

SEAT_JS = r"""
() => {
  const out = [];
  const sel = "[data-row],[data-seat],[data-seat-id],[aria-label*='eat' i],[title*='eat' i],[class*='seat' i],[id*='seat' i]";
  for (const e of document.querySelectorAll(sel)) {
    if (e.children.length > 3) continue;
    const d = e.dataset || {};
    out.push({
      row: d.row || d.rowLabel || "",
      num: d.seat || d.number || d.label || "",
      status: d.status || d.availability || "",
      cls: e.getAttribute("class") || "",
      aria: e.getAttribute("aria-label") || "",
      title: e.getAttribute("title") || "",
      id: e.id || "",
    });
  }
  return out;
}
"""

DATE_SELECTOR = (
    "[data-date], [role=tab], [class*='date' i] a, [class*='date' i] button, "
    "[class*='day' i] a, [class*='day' i] button"
)
ADVANCE_RE = re.compile(r"continue|next|select seats|choose seats|proceed|متابعة|التالي|اختيار", re.I)


async def settle(page, quick: bool = False) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=4000 if quick else 12000)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(600 if quick else 1500)


async def select_cinema(page, cfg: dict) -> bool:
    for kw in cfg["cinema_keywords"]:
        try:
            loc = page.get_by_text(re.compile(re.escape(kw), re.I)).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=3000)
                await settle(page, quick=True)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def collect_showtimes(page, cfg: dict) -> list[dict]:
    found: dict[str, dict] = {}

    async def grab(label: str, ctx_date: bool = False) -> None:
        for it in await page.evaluate(SHOWTIME_JS):
            st = to_showtime(it, label, cfg, ctx_date)
            if not st:
                continue
            old = found.get(st["key"])
            if old is None:
                found[st["key"]] = st
            elif not old["date"] and st["date"]:
                old["date"] = st["date"]

    await grab("")
    tabs = page.locator(DATE_SELECTOR)
    total = min(await tabs.count(), cfg["max_date_tabs"])
    seen: set[str] = set()
    for i in range(total):
        try:
            el = tabs.nth(i)
            label = re.sub(r"\s+", " ", await el.inner_text(timeout=2000)).strip()[:30]
            if not label or label in seen:
                continue
            seen.add(label)
            await el.click(timeout=3000)
            await settle(page, quick=True)
            await grab(label)
        except Exception:  # noqa: BLE001
            continue
    if not seen:
        await grab("", ctx_date=True)
    return list(found.values())


async def read_seats(page, st: dict, captured: list) -> tuple[list[dict], str]:
    start = len(captured)
    await page.goto(st["url"], wait_until="domcontentloaded", timeout=45000)
    await settle(page)

    async def collect() -> list[dict]:
        from_json = seats_from_json([p for _, p in captured[start:]])
        from_dom = seats_from_dom(await page.evaluate(SEAT_JS))
        return max((from_json, from_dom), key=len)

    seats = await collect()
    for _ in range(2):
        if seats:
            break
        btn = page.locator("button, a").filter(has_text=ADVANCE_RE).first
        try:
            if not await btn.count():
                break
            await btn.click(timeout=3000)
        except Exception:  # noqa: BLE001
            break
        await settle(page)
        seats = await collect()
    body = (await page.inner_text("body")).lower()
    return seats, body


# ───────────────────────── الرسائل ─────────────────────────
def h(s) -> str:
    return html.escape(str(s))


def fmt_exp(exp: str) -> str:
    return "⭐ " + exp if exp in ("MAX", "IMAX") else ("🎞 Standard" if exp == "STANDARD" else "🎞 " + exp)


def msg_seats(st: dict, fresh: list[dict], cfg: dict) -> str:
    lim = cfg["max_seats_listed"]
    shown = fresh[:lim]
    by_row: dict[str, list[str]] = {}
    for s in shown:
        by_row.setdefault(s["row"], []).append(s["num"])
    rows = sorted(by_row, key=lambda r: min(x["score"] for x in shown if x["row"] == r))
    lines = [
        "🚨 <b>كراسي كويسة فاضية دلوقتي!</b>",
        f"🎬 {h(cfg['movie_name'])}",
        f"📍 {h(cfg['cinema_name'])}",
        f"📅 <b>{h(st.get('date') or '؟')}</b>   ⏰ <b>{h(st['time'])}</b>   {h(fmt_exp(st['exp']))}",
        "",
    ]
    for r in rows:
        nums = sorted(by_row[r], key=natkey)
        lines.append(f"🪑 صف <b>{h(r)}</b> : " + "  ".join(h(n) for n in nums))
    if len(fresh) > lim:
        lines.append(f"(+{len(fresh) - lim} كراسي كويسة تانية)")
    lines += ["", f'👉 <a href="{h(st["url"])}"><b>احجز الحفلة دي فورًا</b></a>']
    return "\n".join(lines)


def msg_new_showtimes(new: list[dict], new_dates: set[str], cfg: dict) -> list[str]:
    lines = [
        f"🆕 <b>حفلات جديدة اتفتحت - {h(cfg['movie_name'])}</b>",
        f"📍 {h(cfg['cinema_name'])}",
    ]
    if new_dates:
        lines.append("📆 أيام جديدة: " + "، ".join(h(d) for d in sorted(new_dates)))
    by_date: dict[str, list[dict]] = {}
    for st in new:
        by_date.setdefault(st.get("date") or "؟", []).append(st)
    for d, group in by_date.items():
        lines += ["", f"📅 <b>{h(d)}</b>"]
        for st in sorted(group, key=lambda x: x["time"]):
            lines.append(
                f'   ⏰ {h(st["time"])} · {h(fmt_exp(st["exp"]))} - '
                f'<a href="{h(st["url"])}">احجز</a>'
            )
    return lines


# ───────────────────────── debug ─────────────────────────
def dump(name: str, content: str | bytes) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    p = DEBUG_DIR / name
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")


async def dump_page(page, name: str) -> None:
    try:
        dump(f"{name}.html", await page.content())
        dump(f"{name}.png", await page.screenshot(full_page=True))
    except Exception as e:  # noqa: BLE001
        log(f"debug dump فشل: {e}")


# ───────────────────────── التشغيل ─────────────────────────
async def run() -> None:
    from playwright.async_api import async_playwright  # lazy: الاختبارات المنطقية مش محتاجاه

    cfg = load_config()
    state = load_state()
    started = time.monotonic()
    now = datetime.now(CAIRO)
    captured: list[tuple[str, object]] = []
    first_run = "initialized" not in state and "started_at" not in state
    sent_ok = True
    stats = {"showtimes": 0, "checked": 0, "good": 0, "alerts": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=UA, locale="ar-EG", timezone_id="Africa/Cairo",
            viewport={"width": 1366, "height": 900},
        )
        page = await ctx.new_page()

        async def on_response(resp):
            try:
                if "json" in (resp.headers.get("content-type") or "") and len(captured) < 300:
                    captured.append((resp.url, await resp.json()))
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)

        # ── 1) صفحة الفيلم ──
        url = cfg["movie_url"].format(base=BASE_URL, slug=cfg["movie_slug"])
        log(f"فتح {url}")
        status = 0
        showtimes: list[dict] = []
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status = resp.status if resp else 0
            await settle(page)
            if status < 400:
                await select_cinema(page, cfg)
                showtimes = await collect_showtimes(page, cfg)
        except Exception as e:  # noqa: BLE001
            log(f"خطأ في فتح صفحة الفيلم: {e}")
        log(f"status={status} | حفلات الماظة: {len(showtimes)}")

        if DEBUG or not showtimes:
            await dump_page(page, "movie_page")
            dump("json_responses.json", json.dumps(
                [{"url": u, "data": d} for u, d in captured][:80],
                ensure_ascii=False, default=str)[:3_000_000])

        # ── 2) فشل / مش موجود ──
        if not showtimes:
            if status in (200, 404):
                state["saw_unlisted"] = True  # أول ظهور بعد كده يتبعت كتنبيه مش baseline
            state["fail_count"] = state.get("fail_count", 0) + 1
            if (state.get("fail_count", 0) >= cfg["failure_alert_after_runs"]
                    and not state.get("fail_alerted")):
                why = {404: "الفيلم لسه مش موجود على الموقع (404)",
                       403: "الموقع رافض الطلب من سيرفر GitHub (403)",
                       0: "الصفحة مفتحتش"}.get(
                    status, f"الصفحة اتفتحت (status {status}) بس مفيش حفلات للماظة ظاهرة")
                if send_telegram(
                    f"⚠️ <b>VOX watcher</b>: مفيش حفلات لـ {h(cfg['movie_name'])} من "
                    f"{state['fail_count']} تشغيلات ورا بعض.\nالسبب المحتمل: {h(why)}.\n"
                    "ده طبيعي لو الحجز لسه مفتحش. هبعتلك فور ظهور أول حفلة."
                ):
                    state["fail_alerted"] = True
            state.setdefault("started_at", now.isoformat(timespec="minutes"))
            if first_run:
                send_telegram(f"✅ المراقبة بدأت لـ <b>{h(cfg['movie_name'])}</b> - {h(cfg['cinema_name'])}.\n"
                              "لسه مفيش حفلات ظاهرة، هبعتلك أول ما تظهر.")
            maybe_heartbeat(state, cfg, now, stats)
            write_summary([f"### VOX watcher - {now:%Y-%m-%d %H:%M}",
                           f"- status الصفحة: {status} | مفيش حفلات ظاهرة (تشغيلات ورا بعض: {state['fail_count']})"])
            save_state(state)
            await browser.close()
            return

        # ── 3) حفلات جديدة (بعد التأكد إنها فعلًا فرع الماظة) ──
        foreign: set[str] = set(state.get("foreign", []))
        foreign_added: set[str] = set()
        showtimes = [s for s in showtimes if s["key"] not in foreign]
        cap = cfg["max_showtimes_per_run"]
        cache: dict[str, tuple[list[dict], str] | None] = {}
        dumped = False

        async def page_data(st: dict):
            """يفتح صفحة الحفلة مرة واحدة في التشغيلة (بيرجع الكراسي + نص الصفحة)."""
            nonlocal dumped
            if st["key"] in cache:
                return cache[st["key"]]
            try:
                data = await read_seats(page, st, captured)
            except Exception as e:  # noqa: BLE001
                log(f"فشل قراءة {st['key']}: {e}")
                data = None
            cache[st["key"]] = data
            if data and DEBUG and not dumped:
                await dump_page(page, "booking_page")
                dumped = True
            return data

        known: dict = state.setdefault("showtimes", {})
        baseline = (not state.get("initialized")) and not state.get("saw_unlisted")
        new_raw = [s for s in showtimes if s["key"] not in known]
        new, deferred = [], set()
        for idx, s in enumerate(new_raw):
            if idx < cap and time.monotonic() - started < cfg["deadline_seconds"]:
                data = await page_data(s)
                if data and is_foreign(data[1], cfg):
                    foreign.add(s["key"])
                    foreign_added.add(s["key"])
                    log(f"{s['key']}: فرع تاني، تجاهل")
                    continue
                new.append(s)
            elif baseline:
                new.append(s)  # أول تشغيلة: بنسجل الباقي بدون تنبيه
            else:
                deferred.add(s["key"])  # هيتفحص في التشغيلة الجاية قبل التنبيه
        showtimes = [s for s in showtimes if s["key"] not in foreign and s["key"] not in deferred]
        old_dates = {v.get("date") for v in known.values() if v.get("date")}
        new_dates = {s["date"] for s in new if s.get("date") and s["date"] not in old_dates}

        if state.get("fail_alerted"):
            send_telegram("✅ VOX watcher: الحفلات ظهرت ورجع يشتغل طبيعي.")
        state["fail_count"], state["fail_alerted"] = 0, False
        stats["showtimes"] = len(showtimes)

        if baseline:
            days = sorted({s["date"] for s in showtimes if s.get("date")})
            sent_ok &= send_telegram(
                f"✅ المراقبة بدأت لـ <b>{h(cfg['movie_name'])}</b> - {h(cfg['cinema_name'])}\n"
                f"الحفلات الحالية: {len(showtimes)}"
                + (f" في الأيام: {h('، '.join(days))}" if days else "")
                + "\nهبعتلك على أي حفلة/يوم جديد، وعلى الكراسي الكويسة الفاضية."
            )
            to_register = new
        elif new and cfg["notify_new_showtimes"]:
            ok = send_lines(msg_new_showtimes(new, new_dates, cfg))
            sent_ok &= ok
            to_register = new if ok else []
        else:
            to_register = new
        for s in to_register:
            known[s["key"]] = {"date": s.get("date", ""), "time": s["time"],
                               "exp": s["exp"], "url": s["url"]}
        for s in showtimes:
            if s["key"] in known:
                known[s["key"]]["last_seen"] = now.date().isoformat()
        state["initialized"] = True
        state.setdefault("started_at", now.isoformat(timespec="minutes"))

        # ── 4) الكراسي ──
        keys_new = {s["key"] for s in new}
        by_key = {s["key"]: s for s in showtimes}
        others = sorted(k for k in by_key if k not in keys_new)
        cur = state.get("cursor", 0) % max(1, len(others))
        rotated = others[cur:] + others[:cur]
        queue = (sorted(keys_new) + rotated)[:cap]
        state["cursor"] = (cur + max(0, len(queue) - len(keys_new))) % max(1, len(others))

        alerts = 0
        parsed_any = False
        for key in queue:
            if time.monotonic() - started > cfg["deadline_seconds"]:
                log("وصلنا للحد الزمني، الباقي في التشغيلة الجاية")
                break
            st = by_key[key]
            data = await page_data(st)
            if data is None:
                continue
            seats, body = data
            if is_foreign(body, cfg):
                foreign.add(key)
                foreign_added.add(key)
                known.pop(key, None)
                state.get("seats", {}).pop(key, None)
                log(f"{key}: فرع تاني، تجاهل")
                continue
            if not seats:
                log(f"{key}: مقدرتش أقرا خريطة الكراسي")
                continue
            parsed_any = True
            good = good_seats(seats, cfg)
            stats["checked"] += 1
            stats["good"] += len(good)
            fresh, notified = fresh_seats(state, key, good, cfg["renotify_after_taken"])
            log(f"{key} {st['date']} {st['time']}: {sum(s['ok'] for s in seats)} فاضي، "
                f"{len(good)} كويس، {len(fresh)} جديد")
            if fresh and alerts < cfg["max_alert_messages_per_run"]:
                if send_telegram(msg_seats(st, fresh, cfg)):
                    state.setdefault("seats", {})[key] = sorted(notified)
                    alerts += 1
                    stats["alerts"] += 1
                else:
                    sent_ok = False
            elif not fresh:
                state.setdefault("seats", {})[key] = sorted(notified)

        if queue and not parsed_any:
            state["seat_fail_runs"] = state.get("seat_fail_runs", 0) + 1
            if state["seat_fail_runs"] == cfg["failure_alert_after_runs"]:
                send_telegram(
                    "⚠️ <b>VOX watcher</b>: لاقي الحفلات بس مش قادر يقرا خريطة الكراسي "
                    f"({state['seat_fail_runs']} تشغيلات). شغّل الـ workflow يدوي مع debug وابعتلي الملفات."
                )
        else:
            state["seat_fail_runs"] = 0

        # ── 5) تنظيف الحالة القديمة ──
        cutoff = (now - timedelta(days=14)).date().isoformat()
        for k in [k for k, v in known.items() if v.get("last_seen", "9999") < cutoff]:
            known.pop(k, None)
            state.get("seats", {}).pop(k, None)

        # حماية: لو كل الصفحات اتصنفت "فرع تاني" غالبًا الكلمات المفتاحية غلط، مش الحفلات
        any_ok = any(d and not is_foreign(d[1], cfg) for d in cache.values())
        if len(foreign_added) >= 3 and not any_ok:
            foreign -= foreign_added
            if not state.get("foreign_warned") and send_telegram(
                "⚠️ <b>VOX watcher</b>: كل صفحات الحجز اتصنفت فرع تاني. غالبًا اسم الفرع في الموقع "
                "مختلف عن <code>cinema_keywords</code> في config.json. شغّل debug وابعتلي الملفات."
            ):
                state["foreign_warned"] = True
        state["foreign"] = sorted(foreign)
        stats["showtimes"] = len([s for s in showtimes if s["key"] not in foreign])
        maybe_heartbeat(state, cfg, now, stats)
        write_summary([
            f"### VOX watcher - {now:%Y-%m-%d %H:%M}",
            f"- حفلات الماظة: **{stats['showtimes']}** (جديد: {len(new)})",
            f"- خرائط كراسي اتفحصت: **{stats['checked']}** من {len(queue)}",
            f"- كراسي كويسة فاضية: **{stats['good']}** | تنبيهات اتبعتت: **{stats['alerts']}**",
        ])
        save_state(state)
        await browser.close()
    if not sent_ok:
        log("في رسائل متبعتتش، هتتحاول تاني في التشغيلة الجاية")


if __name__ == "__main__":
    if "--test-telegram" in sys.argv:
        sys.exit(test_telegram())
    asyncio.run(run())
