# -*- coding: utf-8 -*-
"""
بوت الأخبار الاقتصادية الأمريكية لتيليجرام (مجاني وبدون مفتاح)
المصدر: تقويم Forex Factory المجاني (أخبار أمريكا USD فقط، الأهمية العالية والمتوسطة)

المواعيد (بتوقيتك المحلي LOCAL_TZ — الافتراضي شيكاغو):
- ملخص الأسبوع: كل اثنين 12:00 منتصف الليل (00:00) — فيه كل أخبار الأسبوع
- ملخص اليوم: كل يوم عمل 7:00 صباحاً — أخبار اليوم
- تذكير قبل كل خبر بـ 15 دقيقة
- "صدر الآن" عند موعد الخبر
- نتيجة الخبر (الفعلي مقابل التوقع) إذا وفّرها المصدر

طريقة التشغيل (لحل مشكلة التأخير):
  NEWS_LOOP_MINUTES=330  → يبقى البوت شغالاً هذه المدة ويفحص كل 20 ثانية
                           (بدل تشغيلة قصيرة كل 5 دقائق يتأخر جدولها في GitHub)
  بدون المتغير → تمرير واحد فقط (السلوك القديم)
المتغيرات: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (عدة معرّفات مفصولة بفاصلة)
اختياري: NEWS_TEST=1 لإرسال ملخص الأسبوع واليوم فوراً للتجربة
المتطلبات: pip install requests
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
LOCAL = ZoneInfo(os.getenv("LOCAL_TZ", "America/Chicago"))
LOCAL_NAME = os.getenv("LOCAL_TZ_NAME", "شيكاغو")

FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_FILE = os.getenv("NEWS_STATE_FILE", "news_state.json")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHATS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]

# ── المواعيد بتوقيتك المحلي (ساعة، دقيقة) ──
WEEKLY_AT = (0, 0)         # الاثنين 12:00 منتصف الليل
WEEKLY_UNTIL_HOUR = 12     # لو تأخر التشغيل يُرسل حتى الساعة 12 ظهراً من نفس اليوم
DAILY_AT = (7, 0)          # كل يوم عمل 7:00 صباحاً
DAILY_UNTIL_HOUR = 11      # لو تأخر التشغيل يُرسل حتى الساعة 11 صباحاً

REMIND_MIN = 15            # التذكير قبل الخبر بـ 15 دقيقة
RELEASE_WINDOW_MIN = 30    # أقصى تأخر لإرسال "صدر الآن"
RESULT_WINDOW_MIN = 90     # كم دقيقة بعد الخبر نبحث فيها عن النتيجة الفعلية
CHECK_SECONDS = 20         # فاصل الفحص في وضع التشغيل المستمر
FEED_REFRESH = 300         # تحديث التقويم كل 5 دقائق (المصدر يحجب الطلبات الأكثر)
FEED_REFRESH_WAIT = 150    # تحديث أسرع بعد الخبر أثناء انتظار الرقم الفعلي
FAIL_ALERT_AFTER = 20 * 60 # تنبيه عطل لو فشل جلب التقويم هذه المدة
ERROR_GAP = 3 * 3600       # أقل فاصل بين تنبيهي عطل

DAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
IMPACT = {"High": ("🔴", "عالية"), "Medium": ("🟠", "متوسطة")}

# ── ترجمة أسماء الأخبار (أي خبر غير موجود هنا يظهر بالإنجليزي كما هو) ──
TITLES = {
    "CPI": "مؤشر أسعار المستهلكين (التضخم)",
    "Core CPI": "التضخم الأساسي (CPI)",
    "PPI": "مؤشر أسعار المنتجين",
    "Core PPI": "أسعار المنتجين الأساسية",
    "Core PCE Price Index": "مؤشر PCE الأساسي (تضخم الفيدرالي المفضل)",
    "PCE Price Index": "مؤشر أسعار PCE",
    "Non-Farm Employment Change": "الوظائف غير الزراعية (NFP)",
    "ADP Non-Farm Employment Change": "وظائف القطاع الخاص ADP",
    "Unemployment Rate": "معدل البطالة",
    "Unemployment Claims": "طلبات إعانة البطالة الأسبوعية",
    "Average Hourly Earnings": "متوسط الأجر بالساعة",
    "JOLTS Job Openings": "الوظائف الشاغرة JOLTS",
    "Federal Funds Rate": "قرار الفائدة الأمريكية",
    "FOMC Statement": "بيان الفيدرالي FOMC",
    "FOMC Press Conference": "مؤتمر الفيدرالي الصحفي",
    "FOMC Meeting Minutes": "محضر اجتماع الفيدرالي",
    "FOMC Economic Projections": "توقعات الفيدرالي الاقتصادية",
    "Fed Chair Powell Speaks": "كلمة رئيس الفيدرالي باول",
    "Retail Sales": "مبيعات التجزئة",
    "Core Retail Sales": "مبيعات التجزئة الأساسية",
    "GDP": "الناتج المحلي الإجمالي",
    "Advance GDP": "الناتج المحلي الإجمالي",
    "ISM Manufacturing PMI": "مؤشر ISM للتصنيع",
    "ISM Services PMI": "مؤشر ISM للخدمات",
    "Flash Manufacturing PMI": "مؤشر مديري المشتريات للتصنيع",
    "Flash Services PMI": "مؤشر مديري المشتريات للخدمات",
    "UoM Consumer Sentiment": "ثقة المستهلك (جامعة ميشيغان)",
    "UoM Inflation Expectations": "توقعات التضخم (جامعة ميشيغان)",
    "CB Consumer Confidence": "ثقة المستهلك (كونفرنس بورد)",
    "Durable Goods Orders": "طلبيات السلع المعمرة",
    "Core Durable Goods Orders": "طلبيات السلع المعمرة الأساسية",
    "Empire State Manufacturing Index": "مؤشر إمباير ستيت للتصنيع",
    "Philly Fed Manufacturing Index": "مؤشر فيلادلفيا الفيدرالي للتصنيع",
    "Crude Oil Inventories": "مخزونات النفط الخام",
    "Natural Gas Storage": "مخزونات الغاز الطبيعي",
    "Existing Home Sales": "مبيعات المنازل القائمة",
    "New Home Sales": "مبيعات المنازل الجديدة",
    "Pending Home Sales": "مبيعات المنازل المعلقة",
    "Building Permits": "تصاريح البناء",
    "Housing Starts": "بدء إنشاء المنازل",
    "Trade Balance": "الميزان التجاري",
    "Personal Income": "الدخل الشخصي",
    "Personal Spending": "الإنفاق الشخصي",
    "Industrial Production": "الإنتاج الصناعي",
    "Factory Orders": "طلبيات المصانع",
    "Import Prices": "أسعار الواردات",
    "Employment Cost Index": "مؤشر تكلفة التوظيف",
    "Challenger Job Cuts": "تسريحات الوظائف Challenger",
    "Congressional Elections": "انتخابات الكونغرس الأمريكي",
    "Presidential Election": "الانتخابات الرئاسية الأمريكية",
}
SUFFIX = {"m/m": "(شهري)", "y/y": "(سنوي)", "q/q": "(ربع سنوي)"}
PREFIX = {"Prelim": "أولي", "Final": "نهائي", "Flash": "مبدئي",
          "Revised": "معدّل", "Advance": "مبدئي"}


def ar(title):
    """الاسم بالعربي مع الأصل الإنجليزي بين قوسين؛ أو الإنجليزي فقط إن لم يُعرف."""
    t = title.strip()
    suffix = ""
    for k, v in SUFFIX.items():
        if t.endswith(" " + k):
            t, suffix = t[:-len(k) - 1], " " + v
            break
    base = TITLES.get(t)
    if base is None:
        first, _, rest = t.partition(" ")
        if first in PREFIX and rest in TITLES:
            base = f"{TITLES[rest]} ({PREFIX[first]})"
    if base is None and t.endswith(" Speaks"):
        base = f"كلمة {t[:-len(' Speaks')]}"
    if base is None:
        return title
    return f"{base}{suffix} ({title})"


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("newsbot")


def now_ny():
    return datetime.now(NY)


# ───────────────────────── تيليجرام ─────────────────────────
def tg_send(text):
    """يرجع True إذا وصلت الرسالة لكل المعرّفات."""
    ok_all = True
    for cid in TG_CHATS:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": text[:4000],
                      "disable_web_page_preview": True},
                timeout=15)
            if not r.ok:
                ok_all = False
                log.warning("تيليجرام رفض الإرسال إلى %s: %s", cid, r.text[:200])
        except Exception:
            ok_all = False
            log.exception("فشل الإرسال إلى %s", cid)
    return ok_all


# ───────────────────────── الحالة ─────────────────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            s = json.load(fh)
    except Exception:
        s = {}
    s.setdefault("weekly", "")
    s.setdefault("daily", "")
    s.setdefault("reminded", [])
    s.setdefault("released", [])
    s.setdefault("results", [])
    s.setdefault("err_t", 0)
    return s


def save_state(s):
    try:
        s["reminded"] = s["reminded"][-300:]
        s["released"] = s["released"][-300:]
        s["results"] = s["results"][-300:]
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False)
    except Exception:
        log.exception("تعذر حفظ الحالة")


# ───────────────────────── جلب الأخبار ─────────────────────────
def fetch_events(attempts=3):
    """يرجع (قائمة الأحداث، هل المصدر يوفّر حقل actual)."""
    last = None
    for attempt in range(attempts):
        try:
            r = requests.get(FEED, timeout=20,
                             headers={"User-Agent": "Mozilla/5.0 (news-bot)"})
            if r.status_code == 429:
                last = RuntimeError("المصدر طلب الانتظار (429)")
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            rows = r.json()
            has_actual = any("actual" in e for e in rows)
            out = []
            for e in rows:
                if e.get("country") != "USD" or e.get("impact") not in IMPACT:
                    continue
                t = datetime.fromisoformat(e["date"]).astimezone(NY)
                out.append({
                    "id": f"{e['date']}|{e.get('title', '')}",
                    "t": t,
                    "title": (e.get("title") or "").strip(),
                    "impact": e["impact"],
                    "forecast": (e.get("forecast") or "").strip(),
                    "previous": (e.get("previous") or "").strip(),
                    "actual": (e.get("actual") or "").strip(),
                })
            out.sort(key=lambda x: x["t"])
            return out, has_actual
        except Exception as ex:
            last = ex
            time.sleep(2)
    raise RuntimeError(f"تعذر جلب تقويم الأخبار: {last}")


# ───────────────────────── الرسائل ─────────────────────────
def when(e):
    return (f"{e['t'].strftime('%H:%M')} نيويورك "
            f"({LOCAL_NAME} {e['t'].astimezone(LOCAL).strftime('%H:%M')})")


def line(e):
    emo, _ = IMPACT[e["impact"]]
    return f"{emo} {when(e)} — {ar(e['title'])}"


def weekly_text(events):
    if not events:
        return "📅 ملخص الأسبوع\nلا توجد أخبار أمريكية مهمة هذا الأسبوع."
    L = ["📅 ملخص أخبار الأسبوع الأمريكية المؤثرة على SPX",
         "(🔴 أهمية عالية | 🟠 متوسطة)", "━━━━━━━━━━━━━━"]
    day = None
    for e in events:
        loc = e["t"].astimezone(LOCAL)
        if loc.date() != day:
            day = loc.date()
            L += ["", f"📌 {DAYS_AR[loc.weekday()]} {loc.strftime('%d/%m')}"]
        L.append(line(e))
    L += ["", "⚠️ التذبذب يزيد عادة حول الأخبار الحمراء؛ انتبه للإشارات قبلها وبعدها."]
    return "\n".join(L)


def daily_text(events, today):
    ev = [e for e in events if e["t"].astimezone(LOCAL).date() == today]
    if not ev:
        return "📰 أخبار اليوم\nلا توجد أخبار أمريكية مهمة اليوم ✅"
    L = [f"📰 أخبار اليوم الأمريكية ({DAYS_AR[today.weekday()]} "
         f"{today.strftime('%d/%m')})", "━━━━━━━━━━━━━━"]
    L += [line(e) for e in ev]
    L += ["", "سيصلك تذكير قبل كل خبر بـ 15 دقيقة، ورسالة عند صدوره ثم النتيجة."]
    return "\n".join(L)


def remind_text(e, mins):
    emo, name = IMPACT[e["impact"]]
    mins = max(int(round(mins)), 1)
    L = [f"⏰ خبر بعد {mins} دقيقة {emo}",
         f"📌 {ar(e['title'])}",
         f"🕒 {when(e)}",
         f"الأهمية: {name}"]
    if e["forecast"] or e["previous"]:
        L.append(f"التوقع: {e['forecast'] or '—'} | السابق: {e['previous'] or '—'}")
    L.append("⚠️ تجنّب الدخول قبل الخبر؛ التذبذب قد يكون حاداً.")
    return "\n".join(L)


def release_text(e, has_actual):
    emo, name = IMPACT[e["impact"]]
    L = [f"📢 صدر الآن {emo}",
         f"📌 {ar(e['title'])}",
         f"🕒 {when(e)}",
         f"التوقع: {e['forecast'] or '—'} | السابق: {e['previous'] or '—'}"]
    if has_actual:
        L.append("⏳ ستصلك النتيجة (الفعلي مقابل التوقع) فور توفرها في المصدر.")
    else:
        L.append("القيمة الفعلية غير متوفرة في المصدر المجاني؛ راجع التقويم أو الشارت.")
    L.append("⚠️ توقّع تذبذباً حاداً في الدقائق القادمة.")
    return "\n".join(L)


def _num(v):
    """يحوّل نص مثل 3.1% أو 150K أو -0.2B إلى رقم (للمقارنة فقط)."""
    if not v:
        return None
    s = v.replace(",", "").replace("%", "").strip()
    mult = 1.0
    if s and s[-1] in "KMBT":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1]]
        s = s[:-1]
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    return float(s) * mult


def _cmp(a, b, label):
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return None
    if abs(x - y) < 1e-9:
        return f"{label}: مطابق ➖"
    return f"{label}: {'أعلى ⬆️' if x > y else 'أقل ⬇️'}"


def result_text(e):
    emo, _ = IMPACT[e["impact"]]
    L = [f"📊 نتيجة الخبر {emo}",
         f"📌 {ar(e['title'])}",
         f"🕒 {when(e)}",
         f"الفعلي: {e['actual']}",
         f"التوقع: {e['forecast'] or '—'} | السابق: {e['previous'] or '—'}"]
    c1 = _cmp(e["actual"], e["forecast"], "مقارنة بالتوقع")
    c2 = _cmp(e["actual"], e["previous"], "مقارنة بالسابق")
    L += [c for c in (c1, c2) if c]
    L.append("⚠️ ارتفاع الرقم ليس صعوداً أو هبوطاً للسوق دائماً؛ راقب ردة فعل الشارت.")
    return "\n".join(L)


# ───────────────────────── البوت ─────────────────────────
class NewsBot:
    def __init__(self):
        self.state = load_state()
        self.events = []
        self.has_actual = False
        self.next_fetch = 0.0
        self.fail_since = None
        self.logged = False

    def _waiting_actual(self, now):
        """هل هناك خبر صدر قبل قليل وننتظر رقمه الفعلي؟"""
        if not self.has_actual:
            return False
        for e in self.events:
            m = (now - e["t"]).total_seconds() / 60.0
            if 0 <= m <= RESULT_WINDOW_MIN and not e["actual"]:
                return True
        return False

    def refresh(self, now, strict=False):
        t = time.time()
        if not strict and t < self.next_fetch:
            return
        try:
            self.events, self.has_actual = fetch_events()
            self.fail_since = None
            self.next_fetch = t + (FEED_REFRESH_WAIT if self._waiting_actual(now)
                                   else FEED_REFRESH)
            if not self.logged:
                self.logged = True
                log.info("تم جلب %d خبراً؛ المصدر يوفّر الرقم الفعلي: %s",
                         len(self.events), self.has_actual)
        except Exception as ex:
            if strict:
                raise
            if self.fail_since is None:
                self.fail_since = t
            self.next_fetch = t + 60
            log.warning("فشل تحديث التقويم (سأعيد المحاولة): %s", ex)
            if t - self.fail_since > FAIL_ALERT_AFTER:
                alert_error(self.state, ex)

    def step(self, now=None, test=False, strict=False):
        now = now or now_ny()
        self.refresh(now, strict=strict)
        ev = self.events
        if not ev:
            return
        st = self.state
        loc = now.astimezone(LOCAL)
        msgs = []   # (نص، دالة تسجّل أنه أُرسل)

        # ملخص الأسبوع: الاثنين 12:00 منتصف الليل بتوقيتك (مرة واحدة)
        wk = loc.strftime("%G-W%V")
        if test or (loc.weekday() == 0 and (loc.hour, loc.minute) >= WEEKLY_AT
                    and loc.hour < WEEKLY_UNTIL_HOUR and st["weekly"] != wk):
            wstart = loc.date() - timedelta(days=loc.weekday())
            wend = wstart + timedelta(days=6)
            wev = [e for e in ev if wstart <= e["t"].astimezone(LOCAL).date() <= wend]
            if wev or test:   # لو التقويم لم يتحدث لأسبوع جديد بعد ننتظر التحديث
                msgs.append((weekly_text(wev), lambda: st.update(weekly=wk)))

        # ملخص اليوم: أيام العمل 7:00 صباحاً بتوقيتك (مرة واحدة)
        today = loc.date()
        if test or (loc.weekday() < 5 and (loc.hour, loc.minute) >= DAILY_AT
                    and loc.hour < DAILY_UNTIL_HOUR and st["daily"] != str(today)):
            msgs.append((daily_text(ev, today), lambda: st.update(daily=str(today))))

        for e in ev:
            mins = (e["t"] - now).total_seconds() / 60.0
            eid = e["id"]
            # تذكير قبل الخبر بـ 15 دقيقة
            if 0 < mins <= REMIND_MIN and eid not in st["reminded"]:
                msgs.append((remind_text(e, mins),
                             lambda i=eid: st["reminded"].append(i)))
            # صدر الآن (يُتخطى لو النتيجة جاهزة فنرسل النتيجة مباشرة)
            if (-RELEASE_WINDOW_MIN <= mins <= 0 and eid not in st["released"]
                    and not (self.has_actual and e["actual"])):
                msgs.append((release_text(e, self.has_actual),
                             lambda i=eid: st["released"].append(i)))
            # النتيجة الفعلية
            if (self.has_actual and e["actual"] and -RESULT_WINDOW_MIN <= mins <= 0
                    and eid not in st["results"]):
                def mark(i=eid):
                    st["results"].append(i)
                    if i not in st["released"]:
                        st["released"].append(i)
                msgs.append((result_text(e), mark))

        if not msgs:
            return
        # دمج رسائل نفس اللحظة في رسالة واحدة
        text = "\n\n──────────────\n\n".join(m for m, _ in msgs)
        if len(text) > 3800:
            for m, mark in msgs:
                if tg_send(m):
                    mark()
        elif tg_send(text):
            for _, mark in msgs:
                mark()


def alert_error(state, e):
    if time.time() - state["err_t"] < ERROR_GAP:
        return
    state["err_t"] = time.time()
    tg_send("⚠️ عطل في بوت الأخبار\n"
            f"{type(e).__name__}: {str(e)[:300]}\n"
            "قد لا تصلك تنبيهات الأخبار حتى يعود المصدر؛ تابع التقويم يدوياً.")


def main():
    if not TG_TOKEN or not TG_CHATS:
        raise SystemExit("متغيرات بيئية ناقصة: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID")
    bot = NewsBot()
    test = os.getenv("NEWS_TEST") == "1"
    loop_min = os.getenv("NEWS_LOOP_MINUTES")
    try:
        if loop_min and not test:
            end = time.time() + float(loop_min) * 60
            log.info("وضع التشغيل المستمر لمدة %s دقيقة", loop_min)
            while time.time() < end:
                try:
                    bot.step()
                    save_state(bot.state)
                except Exception as e:
                    log.exception("خطأ في دورة الأخبار")
                    alert_error(bot.state, e)
                time.sleep(CHECK_SECONDS)
        else:
            bot.step(test=test, strict=True)
    except Exception as e:
        log.exception("خطأ في بوت الأخبار")
        alert_error(bot.state, e)
    finally:
        save_state(bot.state)


if __name__ == "__main__":
    main()
