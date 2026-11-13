"""Telegram известия за нови пробни периоди, покупки, преминали от пробен в платен, подновявания и възстановени суми.

Сравнява абонаментите от Briefley Analytics API със снимката от предната проверка (state.json).
Първото пускане само прави снимката и не праща нищо. Ключовете идват от env (GitHub) или от config.env.
  --dry-run   показва съобщенията, без да праща и без да записва снимката
  --loop N    повтаря проверката всяка минута N минути
"""
import datetime as dt, json, os, sys, time
from zoneinfo import ZoneInfo
import requests

STATE = os.environ.get("NOTIFY_STATE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
SOFIA = ZoneInfo("Europe/Sofia")
DRY = "--dry-run" in sys.argv

E = dict(os.environ)
cfg = os.path.expanduser("~/.kidsapp/keys/config.env")
if not E.get("TELEGRAM_BOT_TOKEN") and os.path.exists(cfg):
    for line in open(cfg):
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); E.setdefault(k.strip(), v.strip())

API = E.get("BRIEFLEY_ANALYTICS_URL", "https://www.briefley.com/analytics/v1")
H = {"Authorization": f"Bearer {E['BRIEFLEY_ANALYTICS_KEY']}"}
STORE = {"apple": "iOS", "google": "Android", "stripe": "сайт"}
PLAN = {"yearly": "годишен", "annual": "годишен", "monthly": "месечен"}

def get(path, **p):
    for i in range(5):
        r = requests.get(f"{API}/{path}", headers=H, params=p, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(5 * (i + 1)); continue
        r.raise_for_status(); return r.json()
    r.raise_for_status()

def subs(**p):
    out, cur = [], None
    while True:
        q = dict(p, limit=1000)
        if cur: q["cursor"] = cur
        j = get("subscriptions", **q); out += j["data"]; cur = j.get("next_cursor")
        if not cur: return out, j.get("server_time")

def ts(s): return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None

def snap(s, old=None):
    # след пробния, ако картата не мине (grace / billing_retry), още НЕ е платено: пазим го като пробен, докато плащането мине
    fresh = s.get("offer") == "free_trial" and ts(s["started_at"]) and dt.datetime.now(dt.timezone.utc) - ts(s["started_at"]) < dt.timedelta(days=40)
    unpaid = s["status"] in ("grace", "billing_retry") and (s["is_trial"] or (old or {}).get("t") or fresh)
    return {"t": bool(s["is_trial"] or unpaid), "st": s["status"], "r": bool(s["refunded_at"])}

def money(s):
    if s.get("price") is None: return ""
    cur = {"EUR": "€", "GBP": "£", "USD": "$", "BGN": "лв."}.get(s.get("currency"), s.get("currency") or "")
    return f" · {s['price']:.2f}".replace(".", ",") + f" {cur}".rstrip()

def gmoney(s):
    """Google пази цената от първото плащане (напр. 1,19 € за промо месеца на годишния): такава цена не е реалната сума."""
    if s.get("store") == "google" and (s.get("price") or 0) < 3: return " · сумата: виж Play Console"
    return money(s)

def plat(s): return f"({STORE.get(s['store'], s['store'])})"

def label(s):
    offer = f" ({s['offer']})" if s.get("offer") and "trial" not in s["offer"] else ""
    return f"{PLAN.get(s['plan'], s['plan'] or '')}{offer}"

def events(s, old, now):
    """Връща (вид, текст) за промяната на един абонамент."""
    started = ts(s["started_at"])
    if old is None:
        if not started or now - started > dt.timedelta(days=2):  # стар запис, дошъл със закъснение
            return None
        if s["is_trial"] or s["status"] == "trial":
            return "trial", f"🔵 Нов стартиран триал {plat(s)} · {label(s)}"
        if s["status"] == "active":
            return "paid", f"🟢 Нова покупка {plat(s)} · {label(s)}{money(s)}"
        return None
    if s["refunded_at"] and not old["r"]:
        return "refund", f"🔴 Възстановена сума {plat(s)} · {label(s)}{gmoney(s)}"
    if old["t"] and not s["is_trial"] and s["status"] == "active":
        return "conv", f"🟢 Триалът стана платен {plat(s)} · {label(s)}{money(s)}"
    return None

def renewals(st, now):
    """Подновявания от /events (събитие renewed = реалното плащане в магазина). Първото пускане само запомня докъде е стигнало."""
    since = st.get("ev_since")
    if not since:
        st["ev_since"] = now.strftime("%Y-%m-%dT%H:%M:%SZ"); st["ev_sent"] = []; return []
    q, cur, rows = {"since": (ts(since) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")}, None, []
    while True:
        p = dict(q, limit=1000)
        if cur: p["cursor"] = cur
        j = get("events", **p); rows += j["data"]; cur = j.get("next_cursor")
        if not cur: break
    sent, out = set(st.get("ev_sent", [])), []
    for e in sorted(rows, key=lambda e: e["occurred_at"] or ""):
        if e["event"] != "renewed" or e.get("is_deleted") or e.get("backfilled") or e["event_id"] in sent: continue   # възстановените стари плащания не се пращат
        if now - ts(e["occurred_at"]) > dt.timedelta(days=3): continue          # стар запис, дошъл със закъснение
        sent.add(e["event_id"])
        amt = e.get("amount")
        cur_ = {"EUR": "€", "GBP": "£", "USD": "$", "BGN": "лв."}.get(e.get("currency"), e.get("currency") or "")
        m = (f" · {amt:.2f}".replace(".", ",") + f" {cur_}".rstrip()) if amt else ""
        if e.get("store") == "google" and amt and amt < 3: m = ""          # промо цената от първия месец, не реалното плащане
        out.append((e, f"🔁 Подновен {PLAN.get(e.get('plan'), e.get('plan') or '')} абонамент {plat(e)}{m}"))
    st["ev_sent"] = list(sent)[-3000:]; st["ev_since"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return out

def day_counts(now):
    """„Общо за деня“ от реалните събития в API-то (днешна дата по София), а не от натрупан брояч.
    Така повторно пускане / стара снимка не може да надуе числата. None при грешка."""
    try:
        mid = now.astimezone(SOFIA).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
        q, cur, rows = {"since": (mid - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}, None, []
        while True:
            p = dict(q, limit=1000)
            if cur: p["cursor"] = cur
            j = get("events", **p); rows += j["data"]; cur = j.get("next_cursor")
            if not cur: break
        c, seen = {}, set()
        for e in rows:
            if e.get("is_deleted") or e.get("backfilled") or e["event_id"] in seen or not e.get("occurred_at"): continue
            if ts(e["occurred_at"]) < mid: continue
            seen.add(e["event_id"])
            k = {"trial_started": "trial", "subscribed": "paid", "converted": "conv", "renewed": "renew"}.get(e["event"])
            if not k: continue
            u = (k, e.get("order_id") if k == "renew" else e.get("subscription_id")) if e.get("subscription_id") else (k, e["event_id"])
            if u in seen: continue                                   # уникални: един trial / покупка на абонамент
            seen.add(u); c[k] = c.get(k, 0) + 1
        return c
    except Exception as ex:
        print("брояч от API:", ex); return None

def total(c): return f"\n\nОбщо за деня: {c.get('paid', 0) + c.get('conv', 0)} покупки · {c.get('renew', 0)} подновявания · {c.get('trial', 0)} триала"

def save(st):
    """Записва снимката веднага (атомарно), за да не се прати същото пак, ако следващо пращане гръмне."""
    if DRY: return
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w")); os.replace(tmp, STATE)

def src_text(src):
    """Откъде е дошъл човекът (users.source от бекенда), когато е известно."""
    if not src: return ""
    t = src.get("type")
    if t == "meta":
        ad, camp = src.get("ad_name"), src.get("campaign_name")
        if ad or camp: return f"\n📣 Реклама: {ad or '—'}" + (f"\n   кампания: {camp}" if camp else "")
        return "\n📣 От реклама в Meta"
    if t in ("email", "utm"): return f"\n✉️ От имейл: {src.get('src') or src.get('utm_campaign') or src.get('utm_source') or ''}".rstrip(": ")
    if t == "promo_code": return f"\n🎟️ Промо код: {src.get('code', '')}"
    if t == "survey": return f"\n🗣️ Сам каза: {src.get('answer', '')}"
    if t == "organic": return "\n🌱 Органично (без реклама)"
    return f"\n📍 Източник: {t}"

def sources(since):
    """user_id → source за наскоро променените потребители."""
    out, cur = {}, None
    try:
        while True:
            q = {"updated_since": since, "limit": 1000}
            if cur: q["cursor"] = cur
            j = get("users", **q)
            for u in j["data"]: out[str(u["user_id"])] = u.get("source")
            cur = j.get("next_cursor")
            if not cur: return out
    except Exception as e:
        print("source:", e); return out

def send(text):
    print("→ Telegram:", text.splitlines()[0])
    if DRY: print("—", text); return
    r = requests.post(f"https://api.telegram.org/bot{E['TELEGRAM_BOT_TOKEN']}/sendMessage",
                      json={"chat_id": E["TELEGRAM_CHAT_ID"], "text": text}, timeout=30)
    r.raise_for_status()

def main():
    st = json.load(open(STATE)) if os.path.exists(STATE) else None
    if st is None:
        rows, server = subs()
        st = {"since": server, "subs": {s["subscription_id"]: snap(s) for s in rows}, "day": None, "count": {}}
        if not DRY: json.dump(st, open(STATE, "w"))
        print(f"Първа снимка: {len(rows)} абонамента, нищо не е пратено."); return
    since = (ts(st["since"]) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")  # застъпване за закъснели записи
    rows, server = subs(updated_since=since)
    now = ts(server) or dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(SOFIA).date().isoformat()
    if st.get("day") != today: st["day"], st["count"] = today, {}
    src, api_c = None, None
    nows = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if "sent" not in st:
        # първо пускане с дедупликацията: засяваме всичко, започнало в последните 2 дни, за да не тръгне вълна от повторни известия
        recent, _ = subs(updated_since=(now - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        st["sent"] = {}
        for s in recent:
            if ts(s["started_at"]) and now - ts(s["started_at"]) < dt.timedelta(days=2):
                for k in ("trial", "paid"): st["sent"][f"{k}:{s['subscription_id']}"] = nows
            if s["status"] == "active" and not s["is_trial"]: st["sent"][f"conv:{s['subscription_id']}"] = nows
            if s["refunded_at"]: st["sent"][f"refund:{s['subscription_id']}"] = nows
        print(f"Засети {len(st['sent'])} пратени ключа (без съобщения).")
    sent = st["sent"]                         # „вид:subscription_id“ → кога е пратено; никога два пъти, пази се 7 дни
    for k in [k for k, v in sent.items() if now - ts(v) > dt.timedelta(days=7)]: del sent[k]
    fails = st.setdefault("fails", {}); retry = False
    for s in sorted(rows, key=lambda s: s["updated_at"] or ""):
        ev = events(s, st["subs"].get(s["subscription_id"]), now)
        if not ev:
            st["subs"][s["subscription_id"]] = snap(s, st["subs"].get(s["subscription_id"])); continue
        kind, text = ev
        key = f"{kind}:{s['subscription_id']}"
        if key in sent:
            print("вече пратено:", key)
            st["subs"][s["subscription_id"]] = snap(s, st["subs"].get(s["subscription_id"])); continue
        if src is None:
            src = sources((now - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        text += src_text((src or {}).get(str(s.get("user_id"))))
        if api_c is None: api_c = day_counts(now)
        c = dict(api_c) if api_c is not None else dict(st["count"], **{kind: st["count"].get(kind, 0) + 1})
        try:
            send(text + total(c))
        except Exception as ex:
            fails[key] = fails.get(key, 0) + 1
            print(f"Telegram грешка ({fails[key]}/3) {key}:", ex)
            if fails[key] < 3:                # снимката на този абонамент и since не се местят → следващата минута пак, най-много 3 пъти
                retry = True; save(st); continue
        fails.pop(key, None)
        st["subs"][s["subscription_id"]] = snap(s, st["subs"].get(s["subscription_id"]))
        st["count"][kind] = st["count"].get(kind, 0) + 1
        sent[key] = nows
        save(st)
    try:
        for e, text in renewals(st, now):
            st["count"]["renew"] = st["count"].get("renew", 0) + 1
            if api_c is None: api_c = day_counts(now)
            try: send(text + total(api_c if api_c is not None else st["count"]))
            except Exception as ex: print("Telegram грешка (подновяване):", ex)
            save(st)
    except Exception as e: print("подновявания:", e)
    if not retry: st["since"] = server
    save(st)
    print(f"Проверени {len(rows)} променени абонамента.")

if __name__ == "__main__":
    # --loop N: проверява всяка минута в продължение на N минути (GitHub пуска графика неравномерно)
    mins = int(sys.argv[sys.argv.index("--loop") + 1]) if "--loop" in sys.argv else 0
    end = time.time() + mins * 60
    while True:
        try: main()
        except Exception as e: print("грешка:", e)
        try:
            import kg; kg.main()              # бот „Briefley Градини“: заявки за демо + напомняния
        except Exception as e: print("kg грешка:", type(e).__name__)
        if time.time() + 60 > end: break
        time.sleep(60)
