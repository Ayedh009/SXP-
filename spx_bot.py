# -*- coding: utf-8 -*-
"""
بوت تيليجرام لتداول SPX500 (بيانات Yahoo Finance أو Capital.com)
- تقرير ساعي أثناء الجلسة الأمريكية
- تنبيهات آخر 30 و 15 دقيقة مع توقع سعر الإغلاق
- إشارات لحظية: صفقات Call/Put، أوردربلوك، فجوات FVG، اختراق خط الكنترول، الأهداف
- أوامر يدوية: /report  /status  /help

المتغيرات البيئية المطلوبة:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
مصدر البيانات الافتراضي: Yahoo Finance (^GSPC و ^VIX) بدون أي حساب.
لاستخدام Capital.com بدلاً منه: DATA_SOURCE=capital مع
  CAPITAL_API_KEY, CAPITAL_IDENTIFIER (الإيميل), CAPITAL_PASSWORD
  واختياري CAPITAL_DEMO=1 (تجريبي، الافتراضي) أو 0 (حقيقي)
المتطلبات: pip install requests
"""

import os
import json
import time
import math
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time as dtime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import requests

# ───────────────────────── الإعدادات ─────────────────────────
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

# مصدر البيانات: yahoo (الافتراضي، بدون حساب) أو capital
DATA_SOURCE = os.getenv("DATA_SOURCE", "yahoo").lower()
if DATA_SOURCE == "capital":
    SPX_EPIC, VIX_EPIC = "US500", "VIX"
else:
    SPX_EPIC, VIX_EPIC = "^GSPC", "^VIX"

OPEN = dtime(9, 30)
CLOSE = dtime(16, 0)

# (ساعة، دقيقة، النوع)
SCHEDULE = [
    (9, 30, "report"), (10, 0, "report"), (11, 0, "report"),
    (12, 0, "report"), (13, 0, "report"), (14, 0, "report"),
    (15, 0, "report"),
    (15, 30, "close30"), (15, 45, "close15"),
]

STATE_FILE = os.getenv("STATE_FILE", "state.json")
LOOP_SECONDS = 45          # دورة المراقبة
HEAVY_REFRESH = 600        # تحديث البيانات الثقيلة كل 10 دقائق
MAX_TRADES_PER_DAY = 6
SIGNAL_COOLDOWN_MIN = 30   # أقل فاصل بين إشارتين في نفس الاتجاه

API_KEY = os.getenv("CAPITAL_API_KEY", "")
API_ID = os.getenv("CAPITAL_IDENTIFIER", "")
API_PW = os.getenv("CAPITAL_PASSWORD", "")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
DEMO = os.getenv("CAPITAL_DEMO", "1") != "0"
BASE = ("https://demo-api-capital.backend-capital.com" if DEMO
        else "https://api-capital.backend-capital.com")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spxbot")


# ───────────────────────── جلب البيانات ─────────────────────────
@dataclass
class Candle:
    t: datetime   # وقت فتح الشمعة بتوقيت نيويورك
    o: float
    h: float
    l: float
    c: float
    v: float


def _mid(d):
    return (d["bid"] + d["ask"]) / 2.0


class Capital:
    def __init__(self):
        self.headers = {}

    def login(self):
        r = requests.post(
            BASE + "/api/v1/session",
            headers={"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"},
            json={"identifier": API_ID, "password": API_PW, "encryptedPassword": False},
            timeout=15,
        )
        r.raise_for_status()
        self.headers = {
            "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"],
            "CST": r.headers["CST"],
        }
        log.info("تم تسجيل الدخول إلى Capital.com")

    def candles(self, epic, resolution, n):
        for _ in range(2):
            if not self.headers:
                self.login()
            r = requests.get(
                BASE + "/api/v1/prices/" + epic,
                headers=self.headers,
                params={"resolution": resolution, "max": n},
                timeout=20,
            )
            if r.status_code == 401:      # انتهت الجلسة
                self.headers = {}
                continue
            r.raise_for_status()
            out = []
            for p in r.json().get("prices", []):
                t = (datetime.fromisoformat(p["snapshotTimeUTC"])
                     .replace(tzinfo=UTC).astimezone(NY))
                out.append(Candle(t, _mid(p["openPrice"]), _mid(p["highPrice"]),
                                  _mid(p["lowPrice"]), _mid(p["closePrice"]),
                                  float(p.get("lastTradedVolume") or 0)))
            out.sort(key=lambda x: x.t)
            return out
        raise RuntimeError("فشل جلب البيانات من Capital.com")


class Yahoo:
    """مصدر Yahoo Finance المجاني (بدون حساب). نفس واجهة Capital.candles."""
    UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
    MAP = {"MINUTE_5": ("5m", "5d"), "HOUR": ("60m", "1mo"),
           "HOUR_4": ("60m", "1mo"), "DAY": ("1d", "1mo")}

    def _fetch(self, symbol, interval, rng):
        last_err = None
        for host in ("query1", "query2"):
            for attempt in range(2):
                try:
                    r = requests.get(
                        f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                        params={"interval": interval, "range": rng},
                        headers=self.UA, timeout=20)
                    if r.status_code == 429:
                        time.sleep(3)
                        continue
                    r.raise_for_status()
                    res = r.json()["chart"]["result"][0]
                    ts = res.get("timestamp") or []
                    q = res["indicators"]["quote"][0]
                    out = []
                    for i, t in enumerate(ts):
                        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                        if None in (o, h, l, c):
                            continue
                        out.append(Candle(
                            datetime.fromtimestamp(t, tz=UTC).astimezone(NY),
                            o, h, l, c, float((q["volume"][i] or 0))))
                    return out
                except Exception as e:
                    last_err = e
                    time.sleep(1)
        raise RuntimeError(f"فشل جلب البيانات من Yahoo: {last_err}")

    def candles(self, symbol, resolution, n):
        interval, rng = self.MAP[resolution]
        cs = self._fetch(symbol, interval, rng)
        if resolution == "HOUR_4":   # تجميع كل 4 شموع ساعة في اليوم
            agg, by_day = [], {}
            for c in cs:
                by_day.setdefault(c.t.date(), []).append(c)
            for day in sorted(by_day):
                g = by_day[day]
                for i in range(0, len(g), 4):
                    ch = g[i:i + 4]
                    agg.append(Candle(ch[0].t, ch[0].o, max(x.h for x in ch),
                                      min(x.l for x in ch), ch[-1].c,
                                      sum(x.v for x in ch)))
            cs = agg
        return cs[-n:]


# ───────────────────────── تيليجرام ─────────────────────────
def tg_send(text, chat=None):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": chat or TG_CHAT, "text": text,
                  "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception:
        log.exception("فشل إرسال رسالة تيليجرام")


# ───────────────────────── أدوات حسابية ─────────────────────────
def atr(cs, n=14):
    if len(cs) < 2:
        return 0.0
    trs = []
    for i in range(1, len(cs)):
        trs.append(max(cs[i].h - cs[i].l,
                       abs(cs[i].h - cs[i - 1].c),
                       abs(cs[i].l - cs[i - 1].c)))
    trs = trs[-n:]
    return sum(trs) / len(trs)


def ema(vals, n):
    k = 2.0 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def htf_bias(cs):
    """+1 صاعد، -1 هابط، 0 محايد (فلتر استدلالي فقط)"""
    closes = [c.c for c in cs]
    if len(closes) < 30:
        return 0
    e_now, e_prev = ema(closes, 20), ema(closes[:-3], 20)
    if closes[-1] > e_now and e_now > e_prev:
        return 1
    if closes[-1] < e_now and e_now < e_prev:
        return -1
    return 0


def bias_text(b):
    return {1: "صاعد 🟢", -1: "هابط 🔴", 0: "محايد ⚪"}[b]


def slope(vals):
    """ميل الانحدار الخطي (نقاط لكل شمعة)"""
    n = len(vals)
    if n < 2:
        return 0.0
    xm, ym = (n - 1) / 2.0, sum(vals) / n
    den = sum((i - xm) ** 2 for i in range(n))
    return sum((i - xm) * (v - ym) for i, v in enumerate(vals)) / den if den else 0.0


def control_series(cs):
    """خط الكنترول = متوسط (VWAP الجلسة ، منتصف مدى اليوم المتراكم)"""
    out, pv, vol = [], 0.0, 0.0
    hi, lo = -1e18, 1e18
    vw_list = []
    for c in cs:
        tp = (c.h + c.l + c.c) / 3.0
        w = c.v if c.v > 0 else 1.0
        pv += tp * w
        vol += w
        hi, lo = max(hi, c.h), min(lo, c.l)
        vw = pv / vol
        vw_list.append(vw)
        out.append((vw + (hi + lo) / 2.0) / 2.0)
    return out, (vw_list[-1] if vw_list else 0.0)


@dataclass
class Zone:
    kind: str    # OB_D طلب، OB_S عرض، FVG_B صاعدة، FVG_S هابطة
    lo: float
    hi: float
    t: datetime

    @property
    def id(self):
        return f"{self.kind}-{self.t.isoformat()}-{self.lo:.2f}"

    @property
    def bullish(self):
        return self.kind in ("OB_D", "FVG_B")

    @property
    def name(self):
        return {"OB_D": "أوردربلوك طلب", "OB_S": "أوردربلوك عرض",
                "FVG_B": "فجوة سعرية صاعدة", "FVG_S": "فجوة سعرية هابطة"}[self.kind]


def find_obs(cs, a):
    d, s = [], []
    for i in range(len(cs) - 1):
        x, y = cs[i], cs[i + 1]
        later = cs[i + 2:]
        if x.c < x.o and y.c > y.o and (y.c - y.o) >= 1.2 * a and y.c > x.h:
            if not any(z.c < x.l for z in later):          # لم يُكسر
                d.append(Zone("OB_D", x.l, x.h, x.t))
        if x.c > x.o and y.c < y.o and (y.o - y.c) >= 1.2 * a and y.c < x.l:
            if not any(z.c > x.h for z in later):
                s.append(Zone("OB_S", x.l, x.h, x.t))
    return d[-4:] + s[-4:]


def find_fvgs(cs, a):
    out = []
    for i in range(2, len(cs)):
        c0, c2 = cs[i - 2], cs[i]
        later = cs[i + 1:]
        if c2.l > c0.h and (c2.l - c0.h) >= 0.2 * a:
            if not any(z.l <= c0.h for z in later):         # لم تُملأ
                out.append(Zone("FVG_B", c0.h, c2.l, c2.t))
        if c2.h < c0.l and (c0.l - c2.h) >= 0.2 * a:
            if not any(z.h >= c0.l for z in later):
                out.append(Zone("FVG_S", c2.h, c0.l, c2.t))
    return out[-6:]


def vix_reading(v):
    if v is None:
        return "غير متاح"
    if v < 15:
        return "هدوء وتفاؤل"
    if v < 20:
        return "طبيعي"
    if v < 30:
        return "قلق مرتفع"
    return "ذعر وتذبذب حاد"


def f(x):
    return f"{x:,.2f}"


# ───────────────────────── البوت ─────────────────────────
class Bot:
    def __init__(self):
        self.api = Capital() if DATA_SOURCE == "capital" else Yahoo()
        self.lock = threading.Lock()
        self.heavy = None
        self.heavy_t = 0
        self.fired = set()
        self.signaled = set()
        self.touched = set()
        self.fvg_seen = set()
        self.cross_seen = set()
        self.last_signal = {}
        self.trades = []
        self.day = None
        self.count = 0
        self.load_state()

    # ---- حفظ/استرجاع الحالة (للتنقل بين مرحلتي GitHub Actions) ----
    def save_state(self):
        try:
            data = {
                "day": str(self.day), "count": self.count,
                "fired": [[str(d), h, m] for d, h, m in self.fired],
                "signaled": list(self.signaled), "touched": list(self.touched),
                "fvg_seen": list(self.fvg_seen),
                "cross_seen": [t.isoformat() for t in self.cross_seen],
                "last_signal": {k: v.isoformat() for k, v in self.last_signal.items()},
                "trades": [dict(tr, **{"from": tr["from"].isoformat()}) for tr in self.trades],
            }
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
        except Exception:
            log.exception("تعذر حفظ الحالة")

    def load_state(self):
        try:
            if not os.path.exists(STATE_FILE):
                return
            with open(STATE_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
            today = datetime.now(NY).date()
            if d.get("day") != str(today):
                return
            self.day, self.count = today, d["count"]
            self.fired = {(datetime.fromisoformat(a).date(), h, m) for a, h, m in d["fired"]}
            self.signaled, self.touched = set(d["signaled"]), set(d["touched"])
            self.fvg_seen = set(d["fvg_seen"])
            self.cross_seen = {datetime.fromisoformat(t) for t in d["cross_seen"]}
            self.last_signal = {k: datetime.fromisoformat(v) for k, v in d["last_signal"].items()}
            self.trades = [dict(tr, **{"from": datetime.fromisoformat(tr["from"])})
                           for tr in d["trades"]]
            log.info("تم استرجاع حالة اليوم")
        except Exception:
            log.exception("تعذر استرجاع الحالة")

    # ---- بيانات ثقيلة تُحدَّث كل 10 دقائق ----
    def _heavy(self):
        if self.heavy and time.time() - self.heavy_t < HEAVY_REFRESH:
            return self.heavy
        h = SimpleNamespace(daily=[], h1=[], h4=[], vix=None, vix_chg=None)
        h.daily = self.api.candles(SPX_EPIC, "DAY", 16)
        h.h1 = self.api.candles(SPX_EPIC, "HOUR", 120)
        h.h4 = self.api.candles(SPX_EPIC, "HOUR_4", 120)
        try:
            v = self.api.candles(VIX_EPIC, "DAY", 3)
            if v:
                h.vix = v[-1].c
                if len(v) > 1 and v[-2].c:
                    h.vix_chg = (v[-1].c - v[-2].c) / v[-2].c * 100
        except Exception:
            log.warning("تعذر جلب VIX")
        self.heavy, self.heavy_t = h, time.time()
        return h

    # ---- لقطة السوق ----
    def snapshot(self):
        now = datetime.now(NY)
        c5 = self.api.candles(SPX_EPIC, "MINUTE_5", 1000)
        if not c5:
            return None
        closed = [c for c in c5 if c.t + timedelta(minutes=5) <= now]
        sess = [c for c in c5 if OPEN <= c.t.time() < CLOSE]
        today = [c for c in sess if c.t.date() == now.date()]
        if not today or len(closed) < 20:
            return None
        pdates = sorted({c.t.date() for c in sess if c.t.date() < now.date()})
        prev = [c for c in sess if pdates and c.t.date() == pdates[-1]]
        hv = self._heavy()

        ns = SimpleNamespace()
        ns.now, ns.c5, ns.closed, ns.today = now, c5, closed, today
        ns.price = c5[-1].c
        ns.atr5 = atr(closed[-60:]) or 0.5
        ns.atr1h = atr(hv.h1[-30:]) if hv.h1 else ns.atr5 * 3
        ns.vix, ns.vix_chg = hv.vix, hv.vix_chg
        ns.bias1h, ns.bias4h = htf_bias(hv.h1), htf_bias(hv.h4)

        ns.day_high = max(c.h for c in today)
        ns.day_low = min(c.l for c in today)
        ns.mid = (ns.day_high + ns.day_low) / 2.0
        rng = ns.day_high - ns.day_low

        rngs = [d.h - d.l for d in hv.daily[:-1]][-14:]
        ns.adr = sum(rngs) / len(rngs) if rngs else (
            (prev and max(c.h for c in prev) - min(c.l for c in prev)) or rng or 1.0)
        ns.range_today = rng
        ns.consumed = rng / ns.adr * 100 if ns.adr else 0
        ns.remaining = max(ns.adr - rng, 0.0)

        if prev:
            H, L, C = max(c.h for c in prev), min(c.l for c in prev), prev[-1].c
            ns.pivot = (H + L + C) / 3.0
            ns.r1, ns.s1 = 2 * ns.pivot - L, 2 * ns.pivot - H
            ns.prev_close = C
        else:
            ns.pivot = ns.r1 = ns.s1 = None
            ns.prev_close = today[0].o
        ns.change_pct = (ns.price - ns.prev_close) / ns.prev_close * 100

        ctrl, vwap = control_series(today)
        ns.ctrl_series, ns.control, ns.vwap = ctrl, ctrl[-1], vwap

        half = (ns.day_high - ns.day_low) / 2.0
        ns.buy_tp = (ns.mid + 0.5 * half, ns.day_high)   # جني أرباح الشراء
        ns.sell_tp = (ns.day_low, ns.mid - 0.5 * half)   # جني أرباح البيع

        zc = closed[-300:]
        ns.zones = find_obs(zc, ns.atr5) + find_fvgs(zc, ns.atr5)
        return ns

    # ---- التقرير الساعي ----
    def report_text(self, ns):
        t = ns.now.strftime("%H:%M")
        ctrl_side = "فوق" if ns.price > ns.control else "تحت"
        if ns.price > ns.control and ns.bias1h >= 0:
            direction = "صاعد 🟢"
        elif ns.price < ns.control and ns.bias1h <= 0:
            direction = "هابط 🔴"
        else:
            direction = "متذبذب ⚪"
        vix_line = "غير متاح"
        if ns.vix is not None:
            chg = f" ({ns.vix_chg:+.2f}%)" if ns.vix_chg is not None else ""
            vix_line = f"{ns.vix:.2f}{chg} — {vix_reading(ns.vix)}"

        L = [
            f"📊 تقرير الجلسة الأمريكية — {t} (توقيت نيويورك)",
            "━━━━━━━━━━━━━━",
            "🩺 نبض السوق",
            f"السعر: {f(ns.price)}",
            f"الاتجاه: {direction}  |  التغير: {ns.change_pct:+.2f}%",
            f"السعر {ctrl_side} خط الكنترول ({f(ns.control)})",
            f"VIX: {vix_line}",
            f"الفريم الكبير: 1س {bias_text(ns.bias1h)} | 4س {bias_text(ns.bias4h)}",
            "",
            "📏 نطاقات الحركة",
            f"المدى اليومي المعتاد (ADR): {f(ns.adr)} نقطة",
            f"المستهلك: {f(ns.range_today)} نقطة ({ns.consumed:.0f}%)",
            f"المتبقي المتاح: {f(ns.remaining)} نقطة",
            f"متوسط المدى الحقيقي (ATR) لساعة: {f(ns.atr1h)}",
        ]
        if ns.remaining < ns.atr1h:
            L.append("⚠️ الحركة المتبقية محدودة؛ اختر عقداً أقرب للسعر وهدفاً أصغر.")
        else:
            L.append(f"💡 يمكن استهداف حركة حتى نحو {f(ns.remaining)} نقطة من المدى المتبقي.")
        L += ["", "🎯 مستويات التداول اليومية"]
        if ns.pivot is not None:
            L += [f"المقاومة: {f(ns.r1)}", f"نقطة التحول (Pivot): {f(ns.pivot)}",
                  f"الدعم: {f(ns.s1)}"]
        else:
            L.append("غير متاحة (لا توجد بيانات جلسة سابقة)")
        L += [
            "", "⚖️ مستويات MGT للتوازن",
            f"سعر التوازن (منتصف مدى اليوم): {f(ns.mid)}",
            f"منطقة جني أرباح الشراء: {f(ns.buy_tp[0])} – {f(ns.buy_tp[1])}",
            f"منطقة جني أرباح البيع: {f(ns.sell_tp[0])} – {f(ns.sell_tp[1])}",
            "", "🏁 أقرب الأهداف",
            f"قمة اليوم: {f(ns.day_high)} (تبعد {f(ns.day_high - ns.price)} نقطة)",
            f"قاع اليوم: {f(ns.day_low)} (يبعد {f(ns.price - ns.day_low)} نقطة)",
        ]
        return "\n".join(L)

    # ---- تنبيهات آخر 30 / 15 دقيقة ----
    def close_text(self, ns, minutes_left):
        last6 = [c.c for c in ns.closed if c.t.date() == ns.now.date()][-6:]
        s = slope(last6)
        steps = max(minutes_left / 5.0, 1.0)
        pred = ns.price + s * steps * 0.5 + 0.15 * (ns.vwap - ns.price)
        lim = 1.5 * ns.atr5 * math.sqrt(steps)
        pred = max(ns.price - lim, min(ns.price + lim, pred))

        thr = 0.1 * ns.atr5
        if s > thr:
            direction = "صعود 🟢"
        elif s < -thr:
            direction = "هبوط 🔴"
        else:
            direction = "عرضي ⚪"
        diff = pred - ns.price
        if diff > 0.15 * ns.atr5:
            rec = "CALL 🟢"
        elif diff < -0.15 * ns.atr5:
            rec = "PUT 🔴"
        else:
            rec = "لا ميل واضح — يُفضّل الانتظار ⚪"

        L = [
            f"⚡ تنبيه الإغلاق — باقي {minutes_left} دقيقة",
            "━━━━━━━━━━━━━━",
            f"السعر الحالي: {f(ns.price)}",
            f"الاتجاه الأخير (زخم آخر 30 دقيقة): {direction}",
            f"السعر مقابل VWAP: {'فوقه' if ns.price > ns.vwap else 'تحته'} ({f(ns.vwap)})",
            f"السعر المتوقع للإغلاق: {f(pred)}  ({diff:+.2f} نقطة)",
            f"أقرب سيولة: قمة اليوم {f(ns.day_high)} | قاع اليوم {f(ns.day_low)}",
            f"توصية عقد الربع ساعة الأخيرة: {rec}",
            "",
            "⚠️ هذا تقدير إحصائي مبني على الزخم والسيولة وليس توقعاً مضموناً.",
        ]
        return "\n".join(L)

    # ---- تنبيهات لحظية ----
    def check_alerts(self, ns):
        now = ns.now
        if self.day != now.date():       # يوم جديد
            self.day, self.count = now.date(), 0
            self.signaled.clear(); self.touched.clear()
            self.fvg_seen.clear(); self.cross_seen.clear()
            self.trades.clear(); self.fired.clear(); self.last_signal.clear()

        closed = ns.closed
        k = closed[-1]

        # 1) فجوة جديدة
        for z in ns.zones:
            if z.kind.startswith("FVG") and z.t >= closed[-2].t and z.id not in self.fvg_seen:
                self.fvg_seen.add(z.id)
                tg_send(f"📦 تكوّنت {z.name}\nالنطاق: {f(z.lo)} – {f(z.hi)}\n"
                        f"الفريم: 5 دقائق | السعر الآن: {f(ns.price)}")

        # 2) اقتراب السعر من أوردربلوك
        for z in ns.zones:
            if not z.kind.startswith("OB") or z.id in self.touched:
                continue
            near = ns.price <= z.hi + 0.15 * ns.atr5 and ns.price >= z.lo - 0.15 * ns.atr5
            if near and z.t < k.t:
                self.touched.add(z.id)
                side = "شراء 🟢" if z.bullish else "بيع 🔴"
                tg_send(f"🧱 السعر عند منطقة {z.name} ({side})\n"
                        f"النطاق: {f(z.lo)} – {f(z.hi)}\nالسعر الآن: {f(ns.price)}")

        # 3) اختراق خط الكنترول (إغلاق شمعة)
        idx = len(ns.today)
        tc = [c for c in ns.today if c.t + timedelta(minutes=5) <= now]
        if len(tc) >= 2 and len(ns.ctrl_series) >= len(tc):
            cs_ = ns.ctrl_series[len(tc) - 1]
            ps_ = ns.ctrl_series[len(tc) - 2]
            up = tc[-2].c <= ps_ and tc[-1].c > cs_
            dn = tc[-2].c >= ps_ and tc[-1].c < cs_
            key = tc[-1].t
            if (up or dn) and key not in self.cross_seen:
                self.cross_seen.add(key)
                tg_send(("⚡ إغلاق شمعة فوق خط الكنترول — انحياز صاعد 🟢\n" if up else
                         "⚡ إغلاق شمعة تحت خط الكنترول — انحياز هابط 🔴\n")
                        + f"الكنترول: {f(cs_)} | الإغلاق: {f(tc[-1].c)}")

        # 4) متابعة الصفقات المفتوحة
        self.track_trades(ns)

        # 5) صفقة جديدة
        if now.time() >= CLOSE or self.count >= MAX_TRADES_PER_DAY:
            return
        for z in sorted(ns.zones, key=lambda z: abs(ns.price - (z.lo + z.hi) / 2)):
            if z.id in self.signaled or z.t >= k.t:
                continue
            mid = (z.lo + z.hi) / 2
            if z.bullish and k.l <= z.hi and k.c > k.o and k.c > mid:
                self.open_trade("CALL", z, k, ns)
                break
            if (not z.bullish) and k.h >= z.lo and k.c < k.o and k.c < mid:
                self.open_trade("PUT", z, k, ns)
                break

    def open_trade(self, side, z, k, ns):
        last = self.last_signal.get(side)
        if last and (k.t - last) < timedelta(minutes=SIGNAL_COOLDOWN_MIN):
            return
        a = ns.atr5
        recent = ns.closed[-3:]
        entry = k.c
        if side == "CALL":
            sl = min(z.lo, min(c.l for c in recent)) - 0.1 * a
            sl = max(sl, entry - 1.5 * a * 2)
            tp1, tp2 = entry + 1.0 * a * 2, entry + 1.8 * a * 2
        else:
            sl = max(z.hi, max(c.h for c in recent)) + 0.1 * a
            sl = min(sl, entry + 1.5 * a * 2)
            tp1, tp2 = entry - 1.0 * a * 2, entry - 1.8 * a * 2
        self.signaled.add(z.id)
        self.last_signal[side] = k.t
        self.count += 1
        self.trades.append({"side": side, "entry": entry, "sl": sl, "tp1": tp1,
                            "tp2": tp2, "from": k.t, "tp1_hit": False, "done": False})
        htf = ns.bias1h if side == "CALL" else -ns.bias1h
        htf_txt = ("متوافقة مع الفريم الكبير ✅" if htf > 0 else
                   "مخالفة للفريم الكبير ⚠️ (صفقة ارتداد)" if htf < 0 else
                   "الفريم الكبير محايد")
        emoji = "🟢" if side == "CALL" else "🔴"
        tg_send(
            f"🚨 إشارة {side} {emoji}\n━━━━━━━━━━━━━━\n"
            f"السبب: ارتداد من {z.name} ({f(z.lo)} – {f(z.hi)}) مع إغلاق شمعة مؤكد\n"
            f"سعر الدخول: {f(entry)}\nوقف الخسارة: {f(sl)}\n"
            f"الهدف الأول TP1: {f(tp1)}\nالهدف الثاني TP2: {f(tp2)}\n"
            f"{htf_txt}\n\n⚠️ إشارة آلية للمساعدة وليست نصيحة مالية؛ التزم بإدارة المخاطر."
        )

    def track_trades(self, ns):
        for tr in self.trades:
            if tr["done"]:
                continue
            for c in ns.c5:
                if c.t <= tr["from"]:
                    continue
                long = tr["side"] == "CALL"
                hit_sl = c.l <= tr["sl"] if long else c.h >= tr["sl"]
                hit1 = c.h >= tr["tp1"] if long else c.l <= tr["tp1"]
                hit2 = c.h >= tr["tp2"] if long else c.l <= tr["tp2"]
                if hit_sl and not tr["tp1_hit"]:
                    tr["done"] = True
                    tg_send(f"🛑 ضُرب وقف الخسارة لصفقة {tr['side']}\n"
                            f"الدخول: {f(tr['entry'])} | الوقف: {f(tr['sl'])}")
                    break
                if hit1 and not tr["tp1_hit"]:
                    tr["tp1_hit"] = True
                    tg_send(f"🎯 تحقق الهدف الأول لصفقة {tr['side']} ✅\n"
                            f"TP1: {f(tr['tp1'])} — يُنصح بنقل الوقف إلى سعر الدخول.")
                    tr["sl"] = tr["entry"]
                if hit2:
                    tr["done"] = True
                    tg_send(f"🎯🎯 تحقق الهدف الثاني لصفقة {tr['side']} ✅\n"
                            f"TP2: {f(tr['tp2'])} — الصفقة مكتملة.")
                    break
                if tr["tp1_hit"] and (c.l <= tr["sl"] if long else c.h >= tr["sl"]):
                    tr["done"] = True
                    tg_send(f"↩️ أُغلقت صفقة {tr['side']} عند الدخول بعد تحقق الهدف الأول.")
                    break

    # ---- الدورة الرئيسية ----
    def tick(self):
        now = datetime.now(NY)
        if now.weekday() >= 5 or not (OPEN <= now.time() <= dtime(16, 1)):
            return
        with self.lock:
            ns = self.snapshot()
            if ns is None:       # عطلة أو لا بيانات
                return
            self.check_alerts(ns)
            for h, m, kind in SCHEDULE:
                due = now.replace(hour=h, minute=m, second=0, microsecond=0)
                key = (now.date(), h, m)
                if due <= now < due + timedelta(minutes=5) and key not in self.fired:
                    self.fired.add(key)
                    if kind == "report":
                        tg_send(self.report_text(ns))
                    else:
                        tg_send(self.close_text(ns, 30 if kind == "close30" else 15))

    def run(self, until=None):
        """until: وقت التوقف (datetime بتوقيت نيويورك)، أو None للتشغيل الدائم"""
        while until is None or datetime.now(NY) < until:
            try:
                self.tick()
                self.save_state()
            except Exception:
                log.exception("خطأ في الدورة")
            time.sleep(LOOP_SECONDS)
        log.info("انتهت مرحلة التشغيل الحالية")

    # ---- الأوامر اليدوية ----
    def commands(self):
        offset = None
        try:   # تجاهل الأوامر القديمة المتراكمة عند بدء التشغيل
            r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                             params={"offset": -1}, timeout=15).json()
            if r.get("result"):
                offset = r["result"][-1]["update_id"] + 1
        except Exception:
            pass
        while True:
            try:
                r = requests.get(
                    f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                    params={"timeout": 30, "offset": offset}, timeout=40).json()
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    m = u.get("message") or {}
                    chat = str(m.get("chat", {}).get("id", ""))
                    text = (m.get("text") or "").strip().lower()
                    if chat != str(TG_CHAT):
                        continue
                    if text.startswith("/report"):
                        with self.lock:
                            ns = self.snapshot()
                        tg_send(self.report_text(ns) if ns else
                                "⏸️ لا توجد بيانات جلسة اليوم (السوق مغلق أو عطلة).", chat)
                    elif text.startswith("/status"):
                        tg_send(f"✅ البوت يعمل\nالوقت (نيويورك): "
                                f"{datetime.now(NY).strftime('%Y-%m-%d %H:%M')}\n"
                                f"صفقات اليوم: {self.count}", chat)
                    elif text.startswith(("/start", "/help")):
                        tg_send("الأوامر:\n/report — تقرير فوري\n/status — حالة البوت", chat)
            except Exception:
                log.exception("خطأ في استقبال الأوامر")
                time.sleep(5)


def setup_mode():
    """وضع الإعداد: يتحقق من التوكن ويطبع رقم المحادثة (Chat ID) في سجل التشغيل."""
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    try:
        me = requests.get(base + "/getMe", timeout=15).json()
    except Exception as e:
        log.error("تعذر الاتصال بتيليجرام: %s", e)
        return
    if not me.get("ok"):
        log.error("❌ التوكن غير صحيح (%s). انسخه من BotFather ← /mybots ← API Token "
                  "والصقه في السر TELEGRAM_TOKEN بدون مسافات.", me.get("description"))
        return
    log.info("✅ التوكن صحيح — البوت: @%s", me["result"].get("username"))
    ups = requests.get(base + "/getUpdates", timeout=15).json().get("result", [])
    found = {}
    for u in ups:
        for key in ("message", "edited_message", "my_chat_member", "channel_post"):
            chat = (u.get(key) or {}).get("chat")
            if chat:
                found[chat["id"]] = chat.get("first_name") or chat.get("title") or chat.get("username") or ""
    if not found:
        log.warning("⚠️ لا توجد رسائل. أرسل /start لبوتك من حسابك ثم أعد تشغيل الـ workflow.")
        return
    for cid, name in found.items():
        log.info("🆔 CHAT_ID = %s   (%s)", cid, name)
    log.info("انسخ الرقم أعلاه وضعه في السر TELEGRAM_CHAT_ID ثم شغّل الـ workflow مرة أخرى.")


def main():
    need = {"TELEGRAM_TOKEN": TG_TOKEN}
    if DATA_SOURCE == "capital":
        need.update({"CAPITAL_API_KEY": API_KEY, "CAPITAL_IDENTIFIER": API_ID,
                     "CAPITAL_PASSWORD": API_PW})
    missing = [k for k, v in need.items() if not v]
    if missing:
        raise SystemExit("متغيرات بيئية ناقصة: " + ", ".join(missing))
    if not TG_CHAT:            # لم يُضبط Chat ID بعد → وضع الإعداد
        setup_mode()
        return
    now = datetime.now(NY)
    test = os.getenv("TEST_MINUTES")
    if test:                                   # تشغيل تجريبي يدوي
        until = now + timedelta(minutes=int(test))
    elif os.getenv("CI_WINDOWS") == "1":       # تشغيل GitHub Actions على مرحلتين
        t = now.time()
        if now.weekday() >= 5 or t >= dtime(16, 5) or t < dtime(8, 55):
            log.info("خارج نافذة التشغيل؛ إنهاء.")
            return
        stop = dtime(12, 40) if t < dtime(12, 40) else dtime(16, 5)
        until = now.replace(hour=stop.hour, minute=stop.minute, second=0, microsecond=0)
    else:
        until = None                           # تشغيل دائم على خادم عادي
    bot = Bot()
    threading.Thread(target=bot.commands, daemon=True).start()
    tg_send("✅ البوت يعمل الآن. أرسل /report للحصول على تقرير فوري.")
    bot.run(until)


if __name__ == "__main__":
    main()
