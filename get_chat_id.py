# -*- coding: utf-8 -*-
"""يقرأ آخر الرسائل الواصلة للبوت ويطبع معرّفات الشاتات والقروبات، ويرد بها في نفس الشات."""
import os
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"

# إلغاء أي webhook قد يمنع getUpdates
requests.get(f"{API}/deleteWebhook", timeout=15)

r = requests.get(f"{API}/getUpdates", timeout=15).json()
if not r.get("ok"):
    raise SystemExit(f"خطأ من تيليجرام: {r}")

seen = {}
for u in r.get("result", []):
    for key in ("message", "channel_post", "my_chat_member", "edited_message"):
        m = u.get(key)
        if m and "chat" in m:
            c = m["chat"]
            seen[c["id"]] = (c.get("type"), c.get("title") or c.get("username") or c.get("first_name"))

if not seen:
    print("لا توجد رسائل. أرسل رسالة في القروب (مثل /status) ثم أعد تشغيل الـ workflow.")
else:
    for cid, (typ, name) in seen.items():
        print(f"CHAT_ID = {cid} | النوع: {typ} | الاسم: {name}")
        requests.post(f"{API}/sendMessage",
                      data={"chat_id": cid, "text": f"معرّف هذا الشات: {cid}"}, timeout=15)
