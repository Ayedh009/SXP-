# -*- coding: utf-8 -*-
"""
بوت تيليجرام لتداول SPX500 (بيانات Yahoo Finance المجانية)
- تقرير ساعي أثناء الجلسة الأمريكية
- تنبيهات آخر 30 و 15 دقيقة مع توقع سعر الإغلاق
- إشارات: Call/Put من الأوردربلوك والفجوات، وإشارة Rejection Block (RB)
- إشارة Σ (سيغما) على فريم 5 دقائق: شمعة رفض بذيل طويل مع احتمال + نافذة زمنية + حركة متوقعة
- درجة لكل إشارة (ضعيفة / متوسطة / قوية) من تقاطع RB و OB و FVG وخط الكنترول
- سحب السيولة قبل RB، تنبيه إعادة اختبار الصندوق، رسالة إلغاء، تحذير انعكاس
- سجل صفقات + سجل توقعات الإغلاق + سجل Σ (مجلد stats) + أمر /stats + ملخص نهاية اليوم
- تنبيه عند حدوث عطل بدل الصمت، ودمج التنبيهات المتقاربة في رسالة واحدة
- أوامر يدوية: /report  /status  /stats  /help

المتغيرات البيئية المطلوبة:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  (يقبل عدة معرّفات مفصولة بفاصلة)
المتطلبات: pip install requests
"""

import os
import csv
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
# توقيت شارتك المحلي: يُعرض بجانب توقيت نيويورك لتسهيل مطابقة الشموع فقط
LOCAL = ZoneInfo(os.getenv("LOCAL_TZ", "America/Chicago"))
LOCAL_NAME = os.getenv("LOCAL_TZ_NAME", "شيكاغو")

SPX_EPIC, VIX_EPIC = "^GSPC", "^VIX"

OPEN = dtime(9, 30)
CLOSE = dtime(16, 0)
END_WINDOW = dtime(16, 31)   # آخر وقت يعمل فيه البوت بعد الإغلاق (لملخص نهاية اليوم)

# (ساعة، دقيقة، النوع)
SCHEDULE = [
    (9, 30, "report"), (10, 0, "report"), (11, 0, "report"),
    (12, 0, "report"), (13, 0, "report"), (14, 0, "report"),
    (15, 0, "report"),
    (15, 30, "close30"), (15, 45, "close15"),
    (16, 20, "eod"),      # ملخص نهاية اليوم وتسجيل النتائج
]

STATE_FILE = os.getenv("STATE_FILE", "state.json")
STATS_DIR = os.getenv("STATS_DIR", "stats")
TRADES_CSV = os.path.join(STATS_DIR, "trades.csv")
PRED_CSV = os.path.join(STATS_DIR, "close_predictions.csv")
SIGMA_CSV = os.path.join(STATS_DIR, "sigma_log.csv")
LOOP_SECONDS = 45          # دورة المراقبة
HEAVY_REFRESH = 600        # تحديث البيانات الثقيلة كل 10 دقائق
SIGNAL_COOLDOWN_MIN = 30   # أقل فاصل بين إشارتي أوردربلوك/فجوة في نفس الاتجاه
ERROR_ALERT_GAP = 1800     # أقل فاصل (ثانية) بين تنبيهي عطل متشابهين

# ── إعدادات Rejection Block (عدّل الأرقام هنا لو أردت إشارات أكثر أو أقل) ──
RB_WICK_RATIO = 0.5        # الذيل الطويل >= 50% من مدى الشمعة
RB_BODY_RATIO = 0.35       # الجسم <= 35% من مدى الشمعة
RB_MIN_RANGE_ATR = 0.8     # مدى شمعة الرفض >= 0.8 من ATR (لتجاهل الشموع الصغيرة)
RB_SWING_LOOKBACK = 10     # الرفض عند قاع/قمة محلية آخر 10 شموع
RB_STOP_ATR = 0.25         # الوقف = خلف حافة الصندوق + 0.25 ATR
RB_MIN_RISK_ATR = 0.5      # أقل مسافة للوقف عن الدخول = 0.5 ATR

# ── إعدادات إشارة Σ (سيغما) — تعمل على شموع 5 دقائق فقط ──
SIGMA_ENABLED = True
SIGMA_MIN_PROB = 45        # لا تُرسل الإشارة لو الاحتمال التقديري أقل من هذا الرقم
SIGMA_MAX_PROB = 85        # سقف الاحتمال المعروض

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
# يمكن وضع أكثر من معرّف مفصولين بفاصلة: 123456789,-1001234567890
TG_CHATS = [c.strip() for c in TG_CHAT.split(",") if c.strip()]

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


class Yahoo:
    """مصدر Yahoo Finance المجاني (بدون حساب)."""
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
    """chat=None: إرسال لكل المعرّفات في TELEGRAM_CHAT_ID. أو معرّف واحد محدد."""
    targets = [str(chat)] if chat else TG_CHATS
    for cid in targets:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": text[:4000],
                      "disable_web_page_preview": True},
                timeout=15,
            )
            if not r.ok:
                log.warning("تيليجرام رفض الإرسال إلى %s: %s", cid, r.text[:200])
        except Exception:
            log.exception("فشل إرسال رسالة تيليجرام إلى %s", cid)


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
    """+1 صاعد، -1 هابط، 0 محايد (للمعلومة فقط، لا يمنع أي إشارة)"""
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


def rb_side(cs, i, a):
    """هل الشمعة رقم i شمعة رفض (Rejection)؟
    يرجع "UP" (ذيل سفلي طويل عند قاع محلي) أو "DOWN" (ذيل علوي طويل عند قمة محلية)
    أو None. المنطقة تكون جسم الشمعة (من الفتح إلى الإغلاق)."""
    if i < RB_SWING_LOOKBACK:
        return None
    k = cs[i]
    rng = k.h - k.l
    if rng <= 0 or rng < RB_MIN_RANGE_ATR * a:
        return None
    top, bot = max(k.o, k.c), min(k.o, k.c)
    body = top - bot
    if body > RB_BODY_RATIO * rng:
        return None
    lower, upper = bot - k.l, k.h - top
    prev = cs[i - RB_SWING_LOOKBACK:i]
    if lower >= RB_WICK_RATIO * rng and lower > upper and k.l <= min(c.l for c in prev):
        return "UP"
    if upper >= RB_WICK_RATIO * rng and upper > lower and k.h >= max(c.h for c in prev):
        return "DOWN"
    return None


def sweep_info(cs, i, prev_high, prev_low, side):
    """هل سبق شمعة الرفض سحب سيولة؟ = ذيل الشمعة كسر قاعاً/قمة سابقة
    (آخر 10 شموع أو قاع/قمة اليوم السابق) وجسمها أُغلق عائداً داخلها.
    يرجع (اسم المستوى، قيمته) أو None."""
    k = cs[i]
    prev = cs[i - RB_SWING_LOOKBACK:i]
    top, bot = max(k.o, k.c), min(k.o, k.c)
    if side == "UP":
        levels = [("قاع سابق", min(c.l for c in prev))]
        if prev_low is not None:
            levels.append(("قاع اليوم السابق", prev_low))
        for name, lv in levels:
            if k.l < lv < bot:
                return name, lv
    else:
        levels = [("قمة سابقة", max(c.h for c in prev))]
        if prev_high is not None:
            levels.append(("قمة اليوم السابق", prev_high))
        for name, lv in levels:
            if k.h > lv > top:
                return name, lv
    return None


def overlaps(lo1, hi1, lo2, hi2, pad=0.0):
    return lo1 <= hi2 + pad and hi1 >= lo2 - pad


def strength(score):
    if score >= 3:
        return "قوية 🔥"
    if score == 2:
        return "متوسطة 🟡"
    return "ضعيفة ⚪"


def flags_text(fl):
    def m(ok):
        return "✅" if ok else "▫️"
    return (f"RB {m(fl['RB'])} | OB {m(fl['OB'])} | FVG {m(fl['FVG'])} | "
            f"الكنترول {m(fl['CTRL'])}")


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


def hm(dt, tz=NY):
    """HH:MM بتوقيت tz (الافتراضي نيويورك)"""
    return dt.astimezone(tz).strftime("%H:%M")


def fmt_delay(sec):
    sec = max(sec, 0)
    return f"{sec:.0f} ث" if sec < 90 else f"{sec / 60:.1f} د"


def stamp(ns, t, label="آخر شمعة", full=True):
    """يوضّح على أي شمعة بُنيت الرسالة ومتى أُرسلت وعمر البيانات.
    t = وقت فتح الشمعة (نيويورك). full=False يعرض سطر الشمعة فقط."""
    end = t + timedelta(minutes=5)
    sent = datetime.now(NY)
    is_closed = end <= sent
    line = (f"🕯 {label}: {hm(t)} → {hm(end)} نيويورك "
            f"({LOCAL_NAME} {hm(t, LOCAL)}) — {'مغلقة' if is_closed else 'جارية'}")
    if not full:
        return line
    second = f"🕒 أُرسلت {sent.strftime('%H:%M:%S')}"
    if is_closed:
        second += f" (بعد إغلاقها بـ {fmt_delay((sent - end).total_seconds())})"
    age = getattr(ns, "data_age", None)
    if age is not None:
        second += f" | عمر البيانات: {age:.1f} د " + ("✅" if age <= 6 else "⚠️ متأخرة")
    return line + "\n" + second


# ───────────────────────── السجلات (CSV) ─────────────────────────
TRADE_HEADER = ["date", "open_time_ny", "close_time_ny", "side", "kind", "score",
                "entry", "stop", "tp1", "tp2", "exit", "result", "r_multiple"]
PRED_HEADER = ["date", "alert", "alert_time_ny", "price", "predicted", "side",
               "final_close", "result"]
SIGMA_HEADER = ["date", "time_ny", "side", "entry", "target", "prob", "exp_move",
                "win_lo_min", "win_hi_min", "score", "sweep", "result"]


def csv_append(path, header, row):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(header)
            w.writerow(row)
    except Exception:
        log.exception("تعذر الكتابة في السجل %s", path)


def csv_read(path):
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []
    except Exception:
        log.exception("تعذر قراءة السجل %s", path)
        return []


def _enc(d):
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


def _dec(d, keys):
    out = dict(d)
    for k in keys:
        if out.get(k):
            out[k] = datetime.fromisoformat(out[k])
    return out


RESULT_AR = {"TP2": "حقق الهدف الثاني ✅", "TP1_BE": "حقق الهدف الأول ثم خرج عند الدخول ✅",
             "SL": "ضُرب الوقف ❌", "EOD": "أُغلقت بنهاية الجلسة ⏹"}
WIN_RESULTS = ("TP2", "TP1_BE")
SIGMA_RESULT_AR = {"HIT": "حققت الحركة المتوقعة ✅", "MISS": "لم تحقق الحركة في النافذة ❌",
                   "EOD": "انتهت الجلسة قبل الحسم ⏹"}


# ───────────────────────── البوت ─────────────────────────
class Bot:
    def __init__(self):
        self.api = Yahoo()
        self.lock = threading.Lock()
        self.heavy = None
        self.heavy_t = 0
        self.fired = set()
        self.signaled = set()
        self.touched = set()
        self.fvg_seen = set()
        self.cross_seen = set()
        self.rb_seen = set()
        self.rb_boxes = []
        self.sigma_seen = set()
        self.sigmas = []
        self.preds = []
        self.last_signal = {}
        self.trades = []
        self.outbox = []
        self.err_sent = {}
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
                "rb_seen": list(self.rb_seen),
                "rb_boxes": [_enc(b) for b in self.rb_boxes],
                "sigma_seen": list(self.sigma_seen),
                "sigmas": [_enc(s) for s in self.sigmas],
                "preds": [_enc(p) for p in self.preds],
                "last_signal": {k: v.isoformat() for k, v in self.last_signal.items()},
                "trades": [_enc(tr) for tr in self.trades],
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
            self.rb_seen = set(d.get("rb_seen", []))
            self.rb_boxes = [_dec(b, ["rej_t", "conf_t"]) for b in d.get("rb_boxes", [])]
            self.sigma_seen = set(d.get("sigma_seen", []))
            self.sigmas = [_dec(s, ["t0"]) for s in d.get("sigmas", [])]
            self.preds = [_dec(p, ["t"]) for p in d.get("preds", [])]
            self.last_signal = {k: datetime.fromisoformat(v) for k, v in d["last_signal"].items()}
            self.trades = [_dec(tr, ["from"]) for tr in d["trades"]]
            log.info("تم استرجاع حالة اليوم")
        except Exception:
            log.exception("تعذر استرجاع الحالة")

    # ---- الرسائل: دمج التنبيهات المتقاربة في رسالة واحدة ----
    def q(self, text):
        self.outbox.append(text)

    def flush(self):
        if not self.outbox:
            return
        sep = "\n\n──────────────\n\n"
        chunks, cur = [], ""
        for m in self.outbox:
            if cur and len(cur) + len(sep) + len(m) > 3800:
                chunks.append(cur)
                cur = m
            else:
                cur = cur + sep + m if cur else m
        if cur:
            chunks.append(cur)
        self.outbox = []
        for c in chunks:
            tg_send(c)

    def alert_error(self, where, e):
        key = f"{where}:{type(e).__name__}"
        if time.time() - self.err_sent.get(key, 0) < ERROR_ALERT_GAP:
            return
        self.err_sent[key] = time.time()
        tg_send(f"⚠️ عطل في البوت — {where}\n"
                f"النوع: {type(e).__name__}\n"
                f"التفاصيل: {str(e)[:300]}\n"
                f"الوقت (نيويورك): {datetime.now(NY).strftime('%Y-%m-%d %H:%M')}\n"
                "البوت يعيد المحاولة تلقائياً؛ إذا استمر العطل راجع تبويب Actions.")

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
        ns.last_t = c5[-1].t
        ns.data_age = max((now - c5[-1].t).total_seconds() / 60.0, 0.0)
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
            ns.prev_high, ns.prev_low = H, L
        else:
            ns.pivot = ns.r1 = ns.s1 = None
            ns.prev_close = today[0].o
            ns.prev_high = ns.prev_low = None
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
        L += ["", stamp(ns, ns.last_t, "آخر شمعة في البيانات")]
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
            rec, side = "CALL 🟢", "CALL"
        elif diff < -0.15 * ns.atr5:
            rec, side = "PUT 🔴", "PUT"
        else:
            rec, side = "لا ميل واضح — يُفضّل الانتظار ⚪", "NONE"

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
            stamp(ns, ns.last_t, "آخر شمعة في البيانات"),
            "",
            "⚠️ هذا تقدير إحصائي مبني على الزخم والسيولة وليس توقعاً مضموناً.",
        ]
        info = {"alert": str(minutes_left), "t": ns.now, "price": ns.price,
                "pred": pred, "side": side}
        return "\n".join(L), info

    # ---- الدرجة: تقاطع RB و OB و FVG وخط الكنترول مع نطاق الإشارة ----
    def confluence(self, ns, lo, hi, bullish):
        pad = 0.15 * ns.atr5
        fl = {"RB": False, "OB": False, "FVG": False, "CTRL": False}
        for z in ns.zones:
            if z.bullish == bullish and overlaps(lo, hi, z.lo, z.hi, pad):
                fl["OB" if z.kind.startswith("OB") else "FVG"] = True
        for b in self.rb_boxes:
            if b["active"] and (b["side"] == "UP") == bullish and \
                    overlaps(lo, hi, b["lo"], b["hi"], pad):
                fl["RB"] = True
        if overlaps(lo, hi, ns.control, ns.control, pad + 0.2 * ns.atr5):
            fl["CTRL"] = True
        return sum(fl.values()), fl

    # ---- تنبيهات لحظية ----
    def check_alerts(self, ns):
        now = ns.now
        if self.day != now.date():       # يوم جديد
            self.day, self.count = now.date(), 0
            self.signaled.clear(); self.touched.clear()
            self.fvg_seen.clear(); self.cross_seen.clear()
            self.rb_seen.clear(); self.rb_boxes.clear(); self.preds.clear()
            self.sigma_seen.clear(); self.sigmas.clear()
            self.trades.clear(); self.fired.clear(); self.last_signal.clear()
            self.outbox.clear()

        closed = ns.closed
        k = closed[-1]
        live = now.time() < CLOSE

        if live:
            # 1) فجوة جديدة
            for z in ns.zones:
                if z.kind.startswith("FVG") and z.t >= closed[-2].t and z.id not in self.fvg_seen:
                    self.fvg_seen.add(z.id)
                    self.q(f"📦 تكوّنت {z.name}\nالنطاق: {f(z.lo)} – {f(z.hi)}\n"
                           f"الفريم: 5 دقائق | السعر الآن: {f(ns.price)}\n"
                           + stamp(ns, z.t, "شمعة تكوّن الفجوة"))

            # 2) اقتراب السعر من أوردربلوك
            for z in ns.zones:
                if not z.kind.startswith("OB") or z.id in self.touched:
                    continue
                near = ns.price <= z.hi + 0.15 * ns.atr5 and ns.price >= z.lo - 0.15 * ns.atr5
                if near and z.t < k.t:
                    self.touched.add(z.id)
                    side = "شراء 🟢" if z.bullish else "بيع 🔴"
                    self.q(f"🧱 السعر عند منطقة {z.name} ({side})\n"
                           f"النطاق: {f(z.lo)} – {f(z.hi)}\nالسعر الآن: {f(ns.price)}\n"
                           + stamp(ns, ns.last_t, "آخر شمعة في البيانات"))

            # 3) اختراق خط الكنترول (إغلاق شمعة)
            tc = [c for c in ns.today if c.t + timedelta(minutes=5) <= now]
            if len(tc) >= 2 and len(ns.ctrl_series) >= len(tc):
                cs_ = ns.ctrl_series[len(tc) - 1]
                ps_ = ns.ctrl_series[len(tc) - 2]
                up = tc[-2].c <= ps_ and tc[-1].c > cs_
                dn = tc[-2].c >= ps_ and tc[-1].c < cs_
                key = tc[-1].t
                if (up or dn) and key not in self.cross_seen:
                    self.cross_seen.add(key)
                    self.q(("⚡ إغلاق شمعة فوق خط الكنترول — انحياز صاعد 🟢\n" if up else
                            "⚡ إغلاق شمعة تحت خط الكنترول — انحياز هابط 🔴\n")
                           + f"الكنترول: {f(cs_)} | الإغلاق: {f(tc[-1].c)}\n"
                           + stamp(ns, tc[-1].t, "شمعة الاختراق"))

            # 4) Rejection Block (شمعة رفض + شمعة تأكيد)
            self.check_rb(ns)

            # 4-ب) إشارة Σ سيغما (شمعة رفض 5 دقائق عند إغلاقها)
            if SIGMA_ENABLED:
                self.check_sigma(ns)

        # 5) متابعة الصناديق (إعادة اختبار / إلغاء) والصفقات المفتوحة
        self.rb_lifecycle(ns)
        self.track_trades(ns)
        self.track_sigmas(ns)

        # 6) صفقة جديدة من أوردربلوك/فجوة (بدون حد لعدد الصفقات)
        if not live:
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

    # ---- الصفقات ----
    def add_trade(self, side, kind, score, entry, sl, tp1, tp2, t, ref, box=None):
        self.count += 1
        tr = {"side": side, "kind": kind, "score": score, "entry": entry,
              "sl": sl, "sl0": sl, "risk": abs(entry - sl) or 1.0,
              "tp1": tp1, "tp2": tp2, "from": t, "ref": ref, "box": box,
              "tp1_hit": False, "done": False, "warned": False}
        self.trades.append(tr)
        return tr

    def close_trade(self, tr, result, exit_price, t):
        tr["done"] = True
        tr["result"] = result
        long = tr["side"] == "CALL"
        r = (exit_price - tr["entry"]) / tr["risk"] * (1 if long else -1)
        tr["r"] = round(r, 2)
        csv_append(TRADES_CSV, TRADE_HEADER, [
            tr["from"].strftime("%Y-%m-%d"), tr["from"].strftime("%H:%M"),
            t.astimezone(NY).strftime("%H:%M"), tr["side"], tr["kind"], tr["score"],
            f"{tr['entry']:.2f}", f"{tr['sl0']:.2f}", f"{tr['tp1']:.2f}",
            f"{tr['tp2']:.2f}", f"{exit_price:.2f}", result, f"{r:.2f}"])

    def check_rb(self, ns):
        """إشارة Rejection Block: شمعة رفض (ذيل طويل عند قاع/قمة محلية)
        ثم شمعة تأكيد تغلق خارج جسمها. المنطقة = جسم شمعة الرفض."""
        cs = ns.closed
        n = len(cs)
        a = ns.atr5
        for j in range(max(n - 3, RB_SWING_LOOKBACK + 1), n):
            conf, rej = cs[j], cs[j - 1]
            if conf.t.date() != ns.now.date() or not (OPEN <= conf.t.time() < CLOSE):
                continue
            side = rb_side(cs, j - 1, a)
            if not side:
                continue
            top, bot = max(rej.o, rej.c), min(rej.o, rej.c)
            up = side == "UP"
            ok = (conf.c > conf.o and conf.c > top) if up else (conf.c < conf.o and conf.c < bot)
            key = f"RB-{side}-{rej.t.isoformat()}"
            if not ok or key in self.rb_seen:
                continue
            self.rb_seen.add(key)

            box = {"key": key, "side": side, "lo": bot, "hi": top,
                   "ext": rej.l if up else rej.h, "rej_t": rej.t, "conf_t": conf.t,
                   "active": True, "retested": False}
            self.rb_boxes.append(box)

            sw = sweep_info(cs, j - 1, ns.prev_high, ns.prev_low, side)
            score, fl = self.confluence(ns, bot, top, up)

            # الوقف خلف حافة الصندوق + ATR، والأهداف 1R و 2R
            entry = conf.c
            if up:
                sl = min(bot - RB_STOP_ATR * a, entry - RB_MIN_RISK_ATR * a)
            else:
                sl = max(top + RB_STOP_ATR * a, entry + RB_MIN_RISK_ATR * a)
            risk = abs(entry - sl)
            sgn = 1 if up else -1
            tp1, tp2 = entry + sgn * risk, entry + sgn * 2 * risk
            tside = "CALL" if up else "PUT"
            self.add_trade(tside, "RB", score, entry, sl, tp1, tp2, conf.t,
                           ref=bot if up else top, box=key)

            rng = rej.h - rej.l
            if up:
                wick_pct = (bot - rej.l) / rng * 100
                title = "🔲 Rejection Block صعودي (RB ↑) 🟢"
                extreme = f"قاع الذيل (مكان الرفض): {f(rej.l)}"
                role = "تعمل المنطقة كدعم"
                inval = f"إغلاق شمعة تحت {f(bot)} يُلغيها"
                htf = ns.bias1h
            else:
                wick_pct = (rej.h - top) / rng * 100
                title = "🔲 Rejection Block هبوطي (RB ↓) 🔴"
                extreme = f"قمة الذيل (مكان الرفض): {f(rej.h)}"
                role = "تعمل المنطقة كمقاومة"
                inval = f"إغلاق شمعة فوق {f(top)} يُلغيها"
                htf = -ns.bias1h
            htf_txt = ("متوافق مع الفريم الكبير ✅" if htf > 0 else
                       "عكس الفريم الكبير ⚠️ (إشارة ارتداد)" if htf < 0 else
                       "الفريم الكبير محايد")
            sweep_txt = (f"💧 سبقه سحب سيولة ({sw[0]} عند {f(sw[1])})" if sw
                         else "💧 بدون سحب سيولة")
            self.q(
                f"{title}\n━━━━━━━━━━━━━━\n"
                f"⭐ الدرجة: {strength(score)} ({score}/4)\n"
                f"{flags_text(fl)}\n"
                f"{sweep_txt}\n\n"
                f"المنطقة (جسم شمعة الرفض): {f(bot)} – {f(top)}\n"
                f"{extreme}\n"
                f"طول الذيل: {wick_pct:.0f}% من الشمعة\n"
                f"{role} | {inval}\n\n"
                f"الاتجاه: {tside}\n"
                f"سعر الدخول (إغلاق التأكيد): {f(entry)}\n"
                f"وقف الخسارة: {f(sl)}\n"
                f"الهدف الأول 1R: {f(tp1)}\n"
                f"الهدف الثاني 2R: {f(tp2)}\n"
                f"{htf_txt}\n"
                f"السعر الآن: {f(ns.price)}\n\n"
                f"{stamp(ns, rej.t, 'شمعة الرفض', full=False)}\n"
                f"{stamp(ns, conf.t, 'شمعة التأكيد')}\n\n"
                "⚠️ إشارة آلية للمساعدة وليست نصيحة مالية؛ التزم بإدارة المخاطر."
            )

    # ---- إشارة Σ (سيغما) على فريم 5 دقائق ----
    def check_sigma(self, ns):
        """Σ تظهر فور إغلاق شمعة رفض (ذيل طويل عند قاع/قمة محلية) على فريم 5 دقائق،
        بدون انتظار شمعة تأكيد. تحمل: الاتجاه، احتمال تقديري، نافذة زمنية بالدقائق،
        وحركة متوقعة بالنقاط. تُفحص آخر شمعتين مغلقتين فقط."""
        cs = ns.closed
        n = len(cs)
        a = ns.atr5
        for i in range(max(n - 2, RB_SWING_LOOKBACK + 1), n):
            rej = cs[i]
            if rej.t.date() != ns.now.date() or not (OPEN <= rej.t.time() < CLOSE):
                continue
            side = rb_side(cs, i, a)
            if not side:
                continue
            key = f"SIG-{side}-{rej.t.isoformat()}"
            if key in self.sigma_seen:
                continue
            self.sigma_seen.add(key)

            up = side == "UP"
            top, bot = max(rej.o, rej.c), min(rej.o, rej.c)
            rng = rej.h - rej.l
            wick = (bot - rej.l) if up else (rej.h - top)
            wick_pct = wick / rng * 100 if rng else 0.0

            sw = sweep_info(cs, i, ns.prev_high, ns.prev_low, side)
            score, fl = self.confluence(ns, bot, top, up)

            # الاحتمال التقديري: قاعدة 45% + نقاط من جودة الشمعة والتقاطعات
            prob = 45.0
            prob += min(max(wick_pct - 50.0, 0.0) * 0.3, 8.0)     # طول الذيل
            if sw:
                prob += 8.0                                       # سحب سيولة
            prob += 6.0 * (int(fl["OB"]) + int(fl["FVG"]) + int(fl["CTRL"]))
            htf = ns.bias1h if up else -ns.bias1h
            prob += 5.0 if htf > 0 else (-5.0 if htf < 0 else 0.0)
            vols = [c.v for c in cs[max(i - 20, 0):i] if c.v > 0]
            if vols and rej.v > 1.3 * (sum(vols) / len(vols)):
                prob += 4.0                                       # حجم مرتفع
            prob = int(round(max(35.0, min(float(SIGMA_MAX_PROB), prob))))
            if prob < SIGMA_MIN_PROB:
                continue

            # الحركة المتوقعة بالنقاط (من طول الذيل و ATR، ولا تتجاوز المدى المتبقي لليوم)
            exp = 0.5 * wick + 0.5 * a
            exp = max(0.5 * a, min(2.0 * a, exp))
            exp = min(exp, max(ns.remaining, 0.5 * a))
            exp = round(exp, 1)

            # النافذة الزمنية بالدقائق (سرعة تقديرية = نصف ATR لكل شمعة 5د)
            center = exp / (0.5 * a) * 5.0
            lo = max(5, int(center * 0.5))
            hi = min(60, max(lo + 10, int(center * 1.5)))

            entry = rej.c
            target = entry + exp if up else entry - exp
            t0 = rej.t + timedelta(minutes=5)           # لحظة إغلاق شمعة الرفض
            self.sigmas.append({
                "key": key, "side": "CALL" if up else "PUT", "entry": entry,
                "target": target, "exp": exp, "prob": prob, "lo": lo, "hi": hi,
                "t0": t0, "score": score, "sweep": bool(sw), "done": False,
                "result": ""})

            emoji = "🟢" if up else "🔴"
            side_ar = "كول" if up else "بوت"
            htf_txt = ("متوافق مع الفريم الكبير ✅" if htf > 0 else
                       "عكس الفريم الكبير ⚠️" if htf < 0 else
                       "الفريم الكبير محايد")
            sweep_txt = (f"💧 سحب سيولة ({sw[0]} عند {f(sw[1])})" if sw
                         else "💧 بدون سحب سيولة")
            ext = f"{'قاع' if up else 'قمة'} الذيل: {f(rej.l if up else rej.h)}"
            self.q(
                f"Σ سيغما {side_ar} {emoji} — {prob}% — خلال {lo}-{hi} د — "
                f"متوقع {exp:g}$\n━━━━━━━━━━━━━━\n"
                f"الاتجاه: {'CALL' if up else 'PUT'} | الفريم: 5 دقائق\n"
                f"سعر الدخول (إغلاق شمعة الرفض): {f(entry)}\n"
                f"الهدف المتوقع: {f(target)} ({exp:+.1f} نقطة)\n"
                f"النافذة: من {lo} إلى {hi} دقيقة بعد الإغلاق\n"
                f"{ext} | طول الذيل: {wick_pct:.0f}% من الشمعة\n"
                f"{sweep_txt}\n"
                f"التقاطعات: {flags_text(fl)}\n"
                f"{htf_txt}\n"
                f"السعر الآن: {f(ns.price)}\n\n"
                f"{stamp(ns, rej.t, 'شمعة Σ')}\n\n"
                "⚠️ الاحتمال تقدير مبني على جودة الشمعة والتقاطعات وليس نسبة مثبتة؛ "
                "قارنه بنتائج /stats مع الوقت. وليست نصيحة مالية."
            )

    def finish_sigma(self, s, result, t):
        s["done"] = True
        s["result"] = result
        csv_append(SIGMA_CSV, SIGMA_HEADER, [
            s["t0"].strftime("%Y-%m-%d"), (s["t0"] - timedelta(minutes=5)).strftime("%H:%M"),
            s["side"], f"{s['entry']:.2f}", f"{s['target']:.2f}", s["prob"],
            f"{s['exp']:.1f}", s["lo"], s["hi"], s["score"],
            "1" if s["sweep"] else "0", result])

    def track_sigmas(self, ns):
        """يحسم كل إشارة Σ: تحققت الحركة المتوقعة داخل النافذة العليا أم لا."""
        for s in self.sigmas:
            if s["done"]:
                continue
            long = s["side"] == "CALL"
            end = s["t0"] + timedelta(minutes=s["hi"])
            hit_candle = None
            for c in ns.c5:
                if c.t < s["t0"] or c.t >= end or c.t.time() >= CLOSE:
                    continue
                if (long and c.h >= s["target"]) or ((not long) and c.l <= s["target"]):
                    hit_candle = c
                    break
            if hit_candle is not None:
                self.finish_sigma(s, "HIT", hit_candle.t)
                mins = max((hit_candle.t + timedelta(minutes=5) - s["t0"]).total_seconds() / 60.0, 5.0)
                self.q(f"Σ ✅ تحققت الحركة المتوقعة — {s['side']}\n"
                       f"الدخول: {f(s['entry'])} → الهدف: {f(s['target'])} "
                       f"({s['exp']:+.1f} نقطة)\n"
                       f"خلال أقل من {mins:.0f} دقيقة (النافذة المتوقعة {s['lo']}-{s['hi']} د)\n"
                       + stamp(ns, hit_candle.t, "عند شمعة", full=False))
            elif ns.now >= end + timedelta(minutes=5):
                self.finish_sigma(s, "MISS", ns.now)

    def rb_lifecycle(self, ns):
        """لكل صندوق RB فعّال: تنبيه إعادة اختبار (مرة واحدة)، أو إلغاء عند
        إغلاق شمعة خارج الصندوق من الجهة المعاكسة."""
        for b in self.rb_boxes:
            if not b["active"]:
                continue
            up = b["side"] == "UP"
            name = "RB ↑" if up else "RB ↓"
            for c in ns.closed:
                if c.t <= b["conf_t"] or c.t.date() != ns.now.date():
                    continue
                if (up and c.c < b["lo"]) or ((not up) and c.c > b["hi"]):
                    b["active"] = False
                    extra = ""
                    for tr in self.trades:
                        if tr.get("box") == b["key"] and not tr["done"]:
                            tr["warned"] = True
                            extra = (f"\n⚠️ الصفقة ما زالت مفتوحة ووقفها عند {f(tr['sl'])} "
                                     "— فكّر في إغلاقها يدوياً.")
                    self.q(f"❌ إلغاء {name}\n"
                           f"أُغلقت شمعة {'تحت' if up else 'فوق'} الصندوق "
                           f"({f(b['lo'])} – {f(b['hi'])}) عند {f(c.c)}\n"
                           "الإشارة لم تعد صالحة." + extra + "\n"
                           + stamp(ns, c.t, "شمعة الإلغاء"))
                    break
                touch = (c.l <= b["hi"]) if up else (c.h >= b["lo"])
                if touch and not b["retested"]:
                    b["retested"] = True
                    self.q(f"🔁 إعادة اختبار {name}\n"
                           f"السعر عاد لصندوق {f(b['lo'])} – {f(b['hi'])} وأُغلقت الشمعة "
                           f"{'فوقه' if up else 'تحته'} ({f(c.c)})\n"
                           f"الصندوق ما زال صالحاً؛ يُلغى بإغلاق "
                           f"{'تحت ' + f(b['lo']) if up else 'فوق ' + f(b['hi'])}\n"
                           + stamp(ns, c.t, "شمعة إعادة الاختبار"))

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
            ref = z.lo
        else:
            sl = max(z.hi, max(c.h for c in recent)) + 0.1 * a
            sl = min(sl, entry + 1.5 * a * 2)
            tp1, tp2 = entry - 1.0 * a * 2, entry - 1.8 * a * 2
            ref = z.hi
        self.signaled.add(z.id)
        self.last_signal[side] = k.t
        score, fl = self.confluence(ns, z.lo, z.hi, z.bullish)
        self.add_trade(side, z.kind, score, entry, sl, tp1, tp2, k.t, ref=ref)
        htf = ns.bias1h if side == "CALL" else -ns.bias1h
        htf_txt = ("متوافقة مع الفريم الكبير ✅" if htf > 0 else
                   "عكس الفريم الكبير ⚠️ (صفقة ارتداد)" if htf < 0 else
                   "الفريم الكبير محايد")
        emoji = "🟢" if side == "CALL" else "🔴"
        self.q(
            f"🚨 إشارة {side} {emoji}\n━━━━━━━━━━━━━━\n"
            f"⭐ الدرجة: {strength(score)} ({score}/4)\n"
            f"{flags_text(fl)}\n\n"
            f"السبب: ارتداد من {z.name} ({f(z.lo)} – {f(z.hi)}) مع إغلاق شمعة مؤكد\n"
            f"سعر الدخول: {f(entry)}\nوقف الخسارة: {f(sl)}\n"
            f"الهدف الأول TP1: {f(tp1)}\nالهدف الثاني TP2: {f(tp2)}\n"
            f"{htf_txt}\n\n"
            f"{stamp(ns, k.t, 'شمعة التأكيد')}\n"
            f"🧱 {z.name} على شمعة: {hm(z.t)} نيويورك\n"
            f"💱 السعر الآن: {f(ns.price)} ({ns.price - entry:+.2f} عن سعر الدخول)\n\n"
            "⚠️ إشارة آلية للمساعدة وليست نصيحة مالية؛ التزم بإدارة المخاطر."
        )

    def track_trades(self, ns):
        for tr in self.trades:
            if tr["done"]:
                continue
            long = tr["side"] == "CALL"
            tag = f"{tr['side']} ({tr.get('kind', '')})"
            for c in ns.c5:
                if c.t <= tr["from"] or c.t.time() >= CLOSE:
                    continue
                was_tp1 = tr["tp1_hit"]
                hit_sl = c.l <= tr["sl"] if long else c.h >= tr["sl"]
                hit1 = c.h >= tr["tp1"] if long else c.l <= tr["tp1"]
                hit2 = c.h >= tr["tp2"] if long else c.l <= tr["tp2"]
                if hit_sl and not was_tp1:
                    self.close_trade(tr, "SL", tr["sl"], c.t)
                    self.q(f"🛑 ضُرب وقف الخسارة لصفقة {tag}\n"
                           f"الدخول: {f(tr['entry'])} | الوقف: {f(tr['sl'])}\n"
                           + stamp(ns, c.t, "عند شمعة", full=False))
                    break
                if hit1 and not was_tp1:
                    tr["tp1_hit"] = True
                    self.q(f"🎯 تحقق الهدف الأول لصفقة {tag} ✅\n"
                           f"TP1: {f(tr['tp1'])} — يُنصح بنقل الوقف إلى سعر الدخول.\n"
                           + stamp(ns, c.t, "عند شمعة", full=False))
                    tr["sl"] = tr["entry"]
                if hit2:
                    self.close_trade(tr, "TP2", tr["tp2"], c.t)
                    self.q(f"🎯🎯 تحقق الهدف الثاني لصفقة {tag} ✅\n"
                           f"TP2: {f(tr['tp2'])} — الصفقة مكتملة.\n"
                           + stamp(ns, c.t, "عند شمعة", full=False))
                    break
                if was_tp1 and (c.l <= tr["sl"] if long else c.h >= tr["sl"]):
                    self.close_trade(tr, "TP1_BE", tr["entry"], c.t)
                    self.q(f"↩️ أُغلقت صفقة {tag} عند الدخول بعد تحقق الهدف الأول.\n"
                           + stamp(ns, c.t, "عند شمعة", full=False))
                    break

            # تحذير انعكاس: الصفقة تنقلب قبل الوصول للهدف الأول (بدون تقييد عدد الصفقات)
            if tr["done"] or tr["tp1_hit"] or tr.get("warned"):
                continue
            reason = None
            if tr.get("kind") != "RB":   # صفقات RB يغطيها تنبيه الإلغاء
                for c in ns.closed:
                    if c.t <= tr["from"] or c.t.time() >= CLOSE:
                        continue
                    if (long and c.c < tr["ref"]) or ((not long) and c.c > tr["ref"]):
                        reason = (f"كسر عكسي: إغلاق شمعة {'تحت' if long else 'فوق'} "
                                  f"حافة المنطقة {f(tr['ref'])}")
                        break
            if not reason:
                for b in self.rb_boxes:
                    if b["conf_t"] > tr["from"] and (b["side"] == "UP") != long:
                        reason = ("ظهور RB معاكس "
                                  f"({'↑' if b['side'] == 'UP' else '↓'}) "
                                  f"عند {f(b['lo'])} – {f(b['hi'])}")
                        break
            if reason:
                tr["warned"] = True
                self.q(f"⚠️ تحذير انعكاس — صفقة {tag}\n"
                       f"السبب: {reason}\n"
                       f"الدخول: {f(tr['entry'])} | الوقف: {f(tr['sl'])} | "
                       f"الهدف الأول: {f(tr['tp1'])}\n"
                       f"السعر الآن: {f(ns.price)}\n"
                       "الصفقة قد تنقلب قبل الهدف؛ فكّر في تقليل المخاطرة أو الخروج.\n"
                       + stamp(ns, ns.last_t, "آخر شمعة في البيانات"))

    # ---- نهاية اليوم: إغلاق الصفقات المتبقية وتسجيل التوقعات والملخص ----
    def end_of_day(self, ns):
        done_today = [c for c in ns.closed
                      if c.t.date() == ns.now.date() and c.t.time() < CLOSE]
        if not done_today:
            return
        final = done_today[-1].c
        for tr in self.trades:
            if not tr["done"]:
                self.close_trade(tr, "EOD", final, ns.now)
        for s in self.sigmas:
            if not s["done"]:
                self.finish_sigma(s, "EOD", ns.now)

        wins = [t for t in self.trades if t.get("result") in WIN_RESULTS]
        losses = [t for t in self.trades if t.get("result") == "SL"]
        eod = [t for t in self.trades if t.get("result") == "EOD"]
        total_r = sum(t.get("r", 0) for t in self.trades)
        L = [f"📋 ملخص نهاية اليوم — {ns.now.strftime('%Y-%m-%d')}",
             "━━━━━━━━━━━━━━",
             f"إغلاق SPX: {f(final)}", "",
             f"🔹 الصفقات: {len(self.trades)} | رابحة {len(wins)} | خاسرة {len(losses)} "
             f"| أُغلقت بنهاية الجلسة {len(eod)}",
             f"مجموع النتيجة: {total_r:+.2f}R"]
        for t in self.trades:
            L.append(f"• {t['side']} {t.get('kind', '')} (درجة {t.get('score', '-')}) "
                     f"{hm(t['from'])} → {RESULT_AR.get(t.get('result'), '-')} "
                     f"({t.get('r', 0):+.2f}R)")
        if self.sigmas:
            hits = [s for s in self.sigmas if s["result"] == "HIT"]
            miss = [s for s in self.sigmas if s["result"] == "MISS"]
            L += ["", f"🔹 إشارات Σ: {len(self.sigmas)} | حققت الحركة {len(hits)} "
                      f"| لم تحقق {len(miss)}"]
            for s in self.sigmas:
                L.append(f"• Σ {s['side']} {hm(s['t0'] - timedelta(minutes=5))} "
                         f"({s['prob']}% — متوقع {s['exp']:g}$) → "
                         f"{SIGMA_RESULT_AR.get(s['result'], '-')}")
        L += ["", "🔹 توقعات الإغلاق"]
        if not self.preds:
            L.append("لا توجد توقعات مسجلة اليوم.")
        pts = 0
        scored = 0
        for p in self.preds:
            if p["side"] == "CALL":
                res = "WIN" if final >= p["pred"] else "LOSS"
            elif p["side"] == "PUT":
                res = "WIN" if final <= p["pred"] else "LOSS"
            else:
                res = "NONE"
            if res != "NONE":
                scored += 1
                pts += res == "WIN"
            csv_append(PRED_CSV, PRED_HEADER, [
                ns.now.strftime("%Y-%m-%d"), p["alert"], p["t"].strftime("%H:%M"),
                f"{p['price']:.2f}", f"{p['pred']:.2f}", p["side"], f"{final:.2f}", res])
            res_ar = {"WIN": "نقطة ✅", "LOSS": "خسارة ❌", "NONE": "بدون توصية ⚪"}[res]
            L.append(f"• قبل {p['alert']} د: {p['side']} — المتوقع {f(p['pred'])} → {res_ar}")
        if scored:
            L.append(f"النتيجة: {pts} من {scored}")
        L += ["", "للإحصائيات التراكمية أرسل /stats"]
        tg_send("\n".join(L))

    # ---- /stats ----
    def stats_text(self):
        trades = csv_read(TRADES_CSV)
        preds = csv_read(PRED_CSV)
        sigs = csv_read(SIGMA_CSV)
        L = ["📈 إحصائيات البوت (من السجلات المحفوظة)", "━━━━━━━━━━━━━━"]
        if not trades and not preds and not sigs:
            L.append("لا توجد سجلات بعد؛ تُسجل النتائج بعد نهاية كل يوم تداول.")
        if trades:
            w = [t for t in trades if t["result"] in WIN_RESULTS]
            l = [t for t in trades if t["result"] == "SL"]
            e = [t for t in trades if t["result"] == "EOD"]
            closed = len(w) + len(l)
            rate = f"{len(w) / closed * 100:.0f}%" if closed else "—"
            tr_r = sum(float(t["r_multiple"]) for t in trades)
            days = len({t["date"] for t in trades})
            L += [f"🔹 الصفقات ({days} يوم): {len(trades)}",
                  f"رابحة {len(w)} | خاسرة {len(l)} | نهاية الجلسة {len(e)}",
                  f"نسبة النجاح (رابحة ÷ رابحة+خاسرة): {rate}",
                  f"مجموع النتيجة: {tr_r:+.2f}R"]
            by = {}
            for t in trades:
                d = by.setdefault(t["kind"], [0, 0])
                d[0] += t["result"] in WIN_RESULTS
                d[1] += t["result"] == "SL"
            L.append("حسب النوع: " + " | ".join(f"{k}: {v[0]}✅/{v[1]}❌" for k, v in by.items()))
            by = {}
            for t in trades:
                d = by.setdefault(t["score"], [0, 0])
                d[0] += t["result"] in WIN_RESULTS
                d[1] += t["result"] == "SL"
            L.append("حسب الدرجة: " + " | ".join(
                f"{k}: {v[0]}✅/{v[1]}❌" for k, v in sorted(by.items())))
        if sigs:
            sc = [s for s in sigs if s["result"] in ("HIT", "MISS")]
            ok = [s for s in sc if s["result"] == "HIT"]
            L += ["", f"🔹 إشارات Σ: {len(sigs)} (محسومة {len(sc)})"]
            if sc:
                real = len(ok) / len(sc) * 100
                try:
                    avg_p = sum(float(s["prob"]) for s in sc) / len(sc)
                    L.append(f"حققت الحركة: {len(ok)} من {len(sc)} ({real:.0f}%) "
                             f"| متوسط الاحتمال المعلن {avg_p:.0f}%")
                except Exception:
                    L.append(f"حققت الحركة: {len(ok)} من {len(sc)} ({real:.0f}%)")
                for sd, nm in (("CALL", "كول"), ("PUT", "بوت")):
                    s2 = [s for s in sc if s["side"] == sd]
                    o2 = [s for s in s2 if s["result"] == "HIT"]
                    if s2:
                        L.append(f"Σ {nm}: {len(o2)} من {len(s2)}")
                for lo_p, hi_p in ((0, 54), (55, 64), (65, 100)):
                    s3 = [s for s in sc if lo_p <= float(s["prob"]) <= hi_p]
                    o3 = [s for s in s3 if s["result"] == "HIT"]
                    if s3:
                        L.append(f"احتمال {lo_p}–{hi_p}%: {len(o3)} من {len(s3)}")
        if preds:
            sc = [p for p in preds if p["result"] in ("WIN", "LOSS")]
            ok = [p for p in sc if p["result"] == "WIN"]
            L += ["", f"🔹 توقعات الإغلاق: {len(ok)} نقطة من {len(sc)}"]
            for a in ("30", "15"):
                s2 = [p for p in sc if p["alert"] == a]
                o2 = [p for p in s2 if p["result"] == "WIN"]
                if s2:
                    L.append(f"تنبيه {a} دقيقة: {len(o2)} من {len(s2)}")
        today_open = [t for t in self.trades if not t["done"]]
        if today_open:
            L += ["", f"صفقات مفتوحة الآن: {len(today_open)}"]
        return "\n".join(L)

    # ---- الدورة الرئيسية ----
    def tick(self):
        now = datetime.now(NY)
        if now.weekday() >= 5 or not (OPEN <= now.time() <= END_WINDOW):
            return
        with self.lock:
            ns = self.snapshot()
            if ns is None:       # عطلة أو لا بيانات
                return
            try:
                self.check_alerts(ns)
            finally:
                self.flush()
            for h, m, kind in SCHEDULE:
                due = now.replace(hour=h, minute=m, second=0, microsecond=0)
                key = (now.date(), h, m)
                if key in self.fired:
                    continue
                if kind == "eod":
                    if due <= now < due + timedelta(minutes=10):
                        complete = ns.today[-1].t.time() >= dtime(15, 55)
                        if complete or now >= due + timedelta(minutes=8):
                            self.fired.add(key)
                            self.end_of_day(ns)
                    continue
                if due <= now < due + timedelta(minutes=5):
                    self.fired.add(key)
                    if kind == "report":
                        tg_send(self.report_text(ns))
                    else:
                        text, info = self.close_text(ns, 30 if kind == "close30" else 15)
                        self.preds.append(info)
                        tg_send(text)

    def run(self, until=None):
        """until: وقت التوقف (datetime بتوقيت نيويورك)، أو None للتشغيل الدائم"""
        while until is None or datetime.now(NY) < until:
            try:
                self.tick()
                self.save_state()
            except Exception as e:
                log.exception("خطأ في الدورة")
                self.alert_error("الدورة الرئيسية", e)
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
                    if chat not in TG_CHATS:
                        continue
                    if text.startswith("/report"):
                        with self.lock:
                            ns = self.snapshot()
                        tg_send(self.report_text(ns) if ns else
                                "⏸️ لا توجد بيانات جلسة اليوم (السوق مغلق أو عطلة).", chat)
                    elif text.startswith("/stats"):
                        tg_send(self.stats_text(), chat)
                    elif text.startswith("/status"):
                        tg_send(f"✅ البوت يعمل\nالوقت (نيويورك): "
                                f"{datetime.now(NY).strftime('%Y-%m-%d %H:%M')}\n"
                                f"صفقات اليوم: {self.count}", chat)
                    elif text.startswith(("/start", "/help")):
                        tg_send("الأوامر:\n/report — تقرير فوري\n/stats — إحصائيات النتائج\n"
                                "/status — حالة البوت", chat)
            except Exception as e:
                log.exception("خطأ في استقبال الأوامر")
                self.alert_error("استقبال الأوامر", e)
                time.sleep(5)


def main():
    need = {"TELEGRAM_TOKEN": TG_TOKEN, "TELEGRAM_CHAT_ID": TG_CHAT}
    missing = [k for k, v in need.items() if not v]
    if missing:
        raise SystemExit("متغيرات بيئية ناقصة: " + ", ".join(missing))
    now = datetime.now(NY)
    test = os.getenv("TEST_MINUTES")
    if test:                                   # تشغيل تجريبي يدوي
        until = now + timedelta(minutes=int(test))
    elif os.getenv("CI_WINDOWS") == "1":       # تشغيل GitHub Actions على مرحلتين
        t = now.time()
        if now.weekday() >= 5 or t >= dtime(16, 35) or t < dtime(8, 55):
            log.info("خارج نافذة التشغيل؛ إنهاء.")
            return
        stop = dtime(12, 40) if t < dtime(12, 40) else dtime(16, 35)
        until = now.replace(hour=stop.hour, minute=stop.minute, second=0, microsecond=0)
    else:
        until = None                           # تشغيل دائم على خادم عادي
    bot = Bot()
    threading.Thread(target=bot.commands, daemon=True).start()
    tg_send("✅ البوت يعمل الآن. أرسل /report للحصول على تقرير فوري.")
    bot.run(until)


if __name__ == "__main__":
    main()
