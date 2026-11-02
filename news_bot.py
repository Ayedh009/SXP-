# -*- coding: utf-8 -*-
"""
بوت الأخبار الاقتصادية الأمريكية لتيليجرام (مجاني وبدون مفتاح)
المصدر: تقويم Forex Factory المجاني (أخبار أمريكا USD فقط، الأهمية العالية والمتوسطة)

- ملخص الأسبوع: كل اثنين 12:00 فجراً بتوقيت نيويورك
- ملخص اليوم: كل يوم عمل 8:00 صباحاً بتوقيت نيويورك
- تنبيه قبل كل خبر بـ 15 دقيقة
- رسالة "صدر الآن" عند موعد صدور الخبر (التوقع والسابق؛ القيمة الفعلية
  غير متوفرة مجاناً في هذا المصدر)

يعمل كتشغيلة قصيرة كل 5 دقائق عبر GitHub Actions (news.yml).
المتغيرات: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (عدة معرّفات مفصولة بفاصلة)
اختياري: NEWS_TEST=1 لإرسال ملخص الأسبوع واليوم فوراً للتجربة
المتطلبات: pip install requests
"""

import os
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

REMIND_MIN = 15            # التنبيه قبل الخبر بـ 15 دقيقة
RELEASE_WINDOW_MIN = 90    # أقصى تأخر مسموح لإرسال رسالة "صدر الآن"
ERROR_GAP = 3 * 3600       # أقل فاصل بين تنبيهي عطل

DAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
IMPACT = {"High": ("🔴", "عالية"), "Medium": ("🟠", "متوسطة")}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("newsbot")


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
    s.setdefault("err_t", 0)
    return s


def save_state(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False)
    except Exception:
        log.exception("تعذر حفظ الحالة")


# ───────────────────────── جلب الأخبار ─────────────────────────
def fetch_events():
    last = None
    for attempt in range(3):
        try:
            r = requests.get(FEED, timeout=20,
                             headers={"User-Agent": "Mozilla/5.0 (news-bot)"})
            if r.status_code == 429:
                last = RuntimeError("المصدر طلب الانتظار (429)")
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            rows = r.json()
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
                })
            out.sort(key=lambda x: x["t"])
            return out
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
    return f"{emo} {when(e)} — {e['title']}"


def weekly_text(events):
    if not events:
        return "📅 ملخص الأسبوع\nلا توجد أخبار أمريكية مهمة هذا الأسبوع."
    L = ["📅 ملخص أخبار الأسبوع الأمريكية المؤثرة على SPX",
         "(🔴 أهمية عالية | 🟠 متوسطة)", "━━━━━━━━━━━━━━"]
    day = None
    for e in events:
        d = e["t"].date()
        if d != day:
            day = d
            L += ["", f"📌 {DAYS_AR[e['t'].weekday()]} {e['t'].strftime('%d/%m')}"]
        L.append(line(e))
    L += ["", "⚠️ التذبذب يزيد عادة حول الأخبار الحمراء؛ انتبه للإشارات قبلها وبعدها."]
    return "\n".join(L)


def daily_text(events, today):
    ev = [e for e in events if e["t"].date() == today]
    if not ev:
        return "📰 أخبار اليوم\nلا توجد أخبار أمريكية مهمة اليوم ✅"
    L = [f"📰 تنبيه: توجد أخبار أمريكية اليوم ({DAYS_AR[today.weekday()]} "
         f"{today.strftime('%d/%m')})", "━━━━━━━━━━━━━━"]
    L += [line(e) for e in ev]
    L += ["", "سيصلك تنبيه قبل كل خبر بـ 15 دقيقة، ورسالة عند صدوره."]
    return "\n".join(L)


def remind_text(e, mins):
    emo, name = IMPACT[e["impact"]]
    mins = max(int(round(mins)), 1)
    L = [f"⏰ خبر بعد {mins} دقيقة {emo}",
         f"📌 {e['title']}",
         f"🕒 {when(e)}",
         f"الأهمية: {name}"]
    if e["forecast"] or e["previous"]:
        L.append(f"التوقع: {e['forecast'] or '—'} | السابق: {e['previous'] or '—'}")
    L.append("⚠️ تجنّب الدخول قبل الخبر؛ التذبذب قد يكون حاداً.")
    return "\n".join(L)


def release_text(e):
    emo, name = IMPACT[e["impact"]]
    return "\n".join([
        f"📢 صدر الآن {emo}",
        f"📌 {e['title']}",
        f"🕒 {when(e)}",
        f"التوقع: {e['forecast'] or '—'} | السابق: {e['previous'] or '—'}",
        "القيمة الفعلية غير متوفرة في المصدر المجاني؛ راجع التقويم أو الشارت.",
        "⚠️ توقّع تذبذباً حاداً في الدقائق القادمة.",
    ])


# ───────────────────────── التشغيل ─────────────────────────
def run(state):
    now = datetime.now(NY)
    test = os.getenv("NEWS_TEST") == "1"
    events = fetch_events()
    msgs = []        # (نص، دالة تسجّل أنه أُرسل)

    # ملخص الأسبوع: الاثنين من 12:00 فجراً (يُرسل مرة واحدة)
    wk = now.strftime("%G-W%V")
    if test or (now.weekday() == 0 and now.hour < 20 and state["weekly"] != wk):
        msgs.append((weekly_text(events), lambda: state.update(weekly=wk)))

    # ملخص اليوم: أيام العمل من 8:00 صباحاً (يُرسل مرة واحدة)
    today = now.date()
    if test or (now.weekday() < 5 and now.hour >= 8 and now.hour < 16
                and state["daily"] != str(today)):
        msgs.append((daily_text(events, today), lambda: state.update(daily=str(today))))

    # تنبيه قبل الخبر + رسالة صدور الخبر
    for e in events:
        mins = (e["t"] - now).total_seconds() / 60.0
        if 0 < mins <= REMIND_MIN and e["id"] not in state["reminded"]:
            msgs.append((remind_text(e, mins),
                         lambda i=e["id"]: state["reminded"].append(i)))
        if -RELEASE_WINDOW_MIN <= mins <= 0 and e["id"] not in state["released"]:
            msgs.append((release_text(e),
                         lambda i=e["id"]: state["released"].append(i)))

    if not msgs:
        log.info("لا جديد الآن.")
        return
    # دمج رسائل نفس التشغيلة في رسالة واحدة
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
    state = load_state()
    try:
        run(state)
    except Exception as e:
        log.exception("خطأ في بوت الأخبار")
        alert_error(state, e)
    finally:
        # تنظيف قوائم قديمة كي لا تكبر
        state["reminded"] = state["reminded"][-300:]
        state["released"] = state["released"][-300:]
        save_state(state)


if __name__ == "__main__":
    main()
