"""
اختبارات محلية: python test_vox.py
- اختبارات منطق (الكراسي الكويسة، عدم التكرار، حالة الـ status)
- اختبار كامل على موقع VOX وهمي بيتشغل على جهازك (بدون إنترنت ولا تليجرام حقيقي)
الاختبارات دي بتثبت إن المنطق سليم، لكنها ما بتثبتش إن شكل موقع VOX الحقيقي متوافق.
ده بيتأكد منه أول تشغيل debug على GitHub.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import vox_watcher as vw

CFG = dict(vw.DEFAULT_CONFIG)
ROWS = "ABCDEFGHIJKL"  # 12 صف، A = الأمام
PER_ROW = 14


def grid(open_codes):
    return [
        {"row": r, "num": str(n), "ok": f"{r}{n}" in open_codes, "kind": "seat"}
        for r in ROWS for n in range(1, PER_ROW + 1)
    ]


# ───────────── اختبارات المنطق ─────────────
def test_good_seats():
    seats = grid({"A1", "A7", "C5", "H7", "H8", "K6", "L1", "L14", "G14"})
    got = {vw.seat_code(s) for s in vw.good_seats(seats, CFG)}
    assert got == {"H7", "H8", "K6"}, got  # الأمام/الأطراف مستبعدة
    assert vw.good_seats(seats, CFG)[0]["row"] in ("H", "K")


def test_party_size():
    cfg = dict(CFG, party_size=2)
    seats = grid({"H7", "H8", "K6", "J3", "J9"})
    got = {vw.seat_code(s) for s in vw.good_seats(seats, cfg)}
    assert got == {"H7", "H8"}, got  # الكراسي المتجاورة بس


def test_skip_wheelchair():
    seats = grid({"H7"})
    for s in seats:
        if vw.seat_code(s) == "H7":
            s["kind"] = "wheelchair"
    assert vw.good_seats(seats, CFG) == []


def test_status():
    f = vw.status_is_available
    assert f("Available") and f("empty") and f(True)
    assert f("unavailable") is False and f("Sold") is False and f("reserved") is False
    assert f("???") is None


def test_dedupe_and_renotify():
    good = [{"row": "H", "num": "7"}, {"row": "H", "num": "8"}]
    state = {"seats": {"k": ["H7"]}}
    fresh, notified = vw.fresh_seats(state, "k", good, renotify=False)
    assert [vw.seat_code(s) for s in fresh] == ["H8"] and notified == {"H7", "H8"}
    # H7 اتحجز وبعدين رجع: مع الإعداد الافتراضي ما يتبعتش تاني
    state = {"seats": {"k": ["H7", "H9"]}}
    fresh, _ = vw.fresh_seats(state, "k", [{"row": "H", "num": "7"}], renotify=False)
    assert fresh == []
    # مع renotify: H9 اتحجز (مش في الفاضي دلوقتي) فبيتشال، ولو رجع يتبعت
    fresh, notified = vw.fresh_seats(state, "k", [{"row": "H", "num": "7"}], renotify=True)
    assert fresh == [] and notified == {"H7"}


def test_experience_and_showtime():
    it = {"href": "https://x/booking/100123", "text": "18:00 MAX",
          "ctx": "City Centre Almaza 18:00 MAX 21:00"}
    st = vw.to_showtime(it, "Fri 18 Dec", CFG)
    assert st["key"] == "100123" and st["exp"] == "MAX" and st["time"] == "18:00"
    other = {"href": "https://x/booking/100124", "text": "20:00", "ctx": "Mall of Egypt 20:00"}
    assert vw.to_showtime(other, "", CFG) is None
    # نص كبير بيذكر فرعين: بناخد أقرب نص بيذكر فرع واحد بس
    ctxs = ["20:00", "Mall of Egypt 20:00", "Mall of Egypt 20:00 City Centre Almaza 18:00"]
    assert vw.pick_ctx(ctxs, CFG) == "Mall of Egypt 20:00"
    it2 = {"href": "https://x/booking/100125", "text": "20:00", "ctxs": ctxs}
    assert vw.to_showtime(it2, "", CFG) is None


def test_is_foreign():
    assert vw.is_foreign("welcome to vox mall of egypt screen 3", CFG)
    assert not vw.is_foreign("city centre almaza screen 3 | mall of egypt", CFG)
    assert not vw.is_foreign("screen 3 seats", CFG)  # مفيش ذكر لأي فرع -> نفترض سليم


# ───────────── موقع VOX وهمي ─────────────
SITE = {
    "showtimes": {
        "Fri 18 Dec": [
            ("City Centre Almaza", [("100001", "18:00", "MAX"), ("100002", "21:00", "")]),
            ("Mall of Egypt", [("100003", "20:00", "")]),
        ],
        "Sat 19 Dec": [("City Centre Almaza", [("100004", "19:00", "")])],
    },
    # الكراسي الفاضية لكل حفلة (الباقي محجوز)
    "open": {
        "100001": {"H7", "H8", "K6", "A1", "C5", "L1"},
        "100002": set(),
        "100003": {"H7"},
        "100004": {"J7"},
        "100005": {"F6"},
    },
    "cinema_of": {"100003": "Mall of Egypt"},
}

MOVIE_HTML = """<html><body><h1>Avengers: Doomsday</h1>
<div class="dates">%s</div><div id="list"></div>
<script>
const DATA = %s;
function show(d){document.getElementById('list').innerHTML = (DATA[d]||[]).map(c =>
 '<section class="cinema"><h3>'+c[0]+'</h3><ul>'+c[1].map(t=>
 '<li><a href="/booking/'+t[0]+'">'+t[1]+' '+t[2]+'</a></li>').join('')+'</ul></section>').join('');}
document.querySelectorAll('.date-tab').forEach(b => b.onclick = () => show(b.textContent));
show(Object.keys(DATA)[0]);
</script></body></html>"""


def booking_html(sid):
    cinema = SITE["cinema_of"].get(sid, "City Centre Almaza")
    opened = SITE["open"].get(sid, set())
    cells = []
    for r in ROWS:
        for n in range(1, PER_ROW + 1):
            ok = f"{r}{n}" in opened
            cells.append(
                f'<div class="seat {"available" if ok else "sold"}" data-row="{r}" data-seat="{n}" '
                f'aria-label="Row {r} seat {n} {"available" if ok else "sold"}"></div>'
            )
    return f"<html><body><h2>{cinema}</h2>{''.join(cells)}</body></html>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/movies/avengers-doomsday"):
            tabs = "".join(f'<button class="date-tab">{d}</button>' for d in SITE["showtimes"])
            body = MOVIE_HTML % (tabs, json.dumps(SITE["showtimes"]))
        elif self.path.startswith("/booking/"):
            body = booking_html(self.path.rsplit("/", 1)[1])
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


def run_watcher(env_extra):
    env = {**os.environ, "DRY_RUN": "1", "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost", **env_extra}
    p = subprocess.run([sys.executable, "vox_watcher.py"], env=env, capture_output=True,
                       text=True, timeout=240, cwd=Path(__file__).parent)
    if p.returncode:
        print(p.stdout, p.stderr)
    assert p.returncode == 0, p.stderr
    return p.stdout


def messages(out):
    return re.findall(r"──── \[DRY_RUN telegram\] ────\n(.*?)\n────────", out, re.S)


def test_end_to_end():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp())
    (tmp / "cfg.json").write_text(json.dumps({"heartbeat_hour": 0, "movie_slug": "avengers-doomsday"}))
    env = {"VOX_BASE_URL": f"http://127.0.0.1:{srv.server_port}",
           "VOX_STATE_FILE": str(tmp / "state.json"), "VOX_CONFIG_FILE": str(tmp / "cfg.json"),
           "VOX_DEBUG_DIR": str(tmp / "debug")}

    # تشغيلة 1: baseline + كراسي كويسة (H7 H8 K6 في 100001 و J7 في 100004)
    out1 = run_watcher(env)
    m1 = messages(out1)
    seat1 = [m for m in m1 if "🚨" in m]
    assert any("المراقبة بدأت" in m for m in m1), "لازم رسالة البداية"
    assert not any("حفلات جديدة" in m for m in m1), "الـ baseline ما يبعتش تنبيه حفلات جديدة"
    assert len(seat1) == 2, out1
    a = next(m for m in seat1 if "18:00" in m)
    assert "H" in a and "7" in a and "8" in a and "K" in a
    assert "A1" not in a and "صف <b>A</b>" not in a and "صف <b>C</b>" not in a and "صف <b>L</b>" not in a
    assert "/booking/100001" in a, "لينك الحجز لازم يكون في الرسالة"
    assert "MAX" in a and "Fri 18 Dec" in a
    assert not any("100003" in m for m in m1), "فرع مول مصر لازم يتستبعد"
    assert sum("شغال" in m for m in m1) == 1, "heartbeat مرة واحدة"

    # تشغيلة 2: مفيش تغيير -> مفيش ولا رسالة
    out2 = run_watcher(env)
    assert messages(out2) == [], out2

    # تشغيلة 3: يوم جديد 24 + حفلة جديدة + كرسي جديد K9 في 100001
    SITE["showtimes"]["Tue 24 Dec"] = [("City Centre Almaza", [("100005", "22:00", "MAX")])]
    SITE["open"]["100001"].add("K9")
    out3 = run_watcher(env)
    m3 = messages(out3)
    new_msg = [m for m in m3 if "حفلات جديدة" in m]
    assert len(new_msg) == 1 and "Tue 24 Dec" in new_msg[0] and "/booking/100005" in new_msg[0]
    assert "أيام جديدة" in new_msg[0]
    seat3 = [m for m in m3 if "🚨" in m]
    assert len(seat3) == 2, out3
    k = next(m for m in seat3 if "18:00" in m)
    assert "9" in k and "صف <b>H</b>" not in k, "الكراسي القديمة ما تتبعتش تاني"
    assert any("22:00" in m and "F" in m for m in seat3)

    # تشغيلة 4: ثبات
    assert messages(run_watcher(env)) == []
    print("e2e OK")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
    print("كل الاختبارات نجحت")
