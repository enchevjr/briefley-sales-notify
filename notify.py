"""Telegram известия за нови пробни периоди, покупки, преминали от пробен в платен и възстановени суми.

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

def snap(s): return {"t": s["is_trial"], "st": s["status"], "r": bool(s["refunded_at"])}

def money(s):
    if s.get("price") is None: return ""
    cur = {"EUR": "€", "GBP": "£", "USD": "$", "BGN": "лв."}.get(s.get("currency"), s.get("currency") or "")
    return f" · {s['price']:.2f}".replace(".", ",") + f" {cur}".rstrip()

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
        if s["status"] in ("active", "grace"):
            return "paid", f"🟢 Нова покупка {plat(s)} · {label(s)}{money(s)}"
        return None
    if s["refunded_at"] and not old["r"]:
        return "refund", f"🔴 Възстановена сума {plat(s)} · {label(s)}{money(s)}"
    if old["t"] and not s["is_trial"] and s["status"] in ("active", "grace"):
        return "conv", f"🟢 Триалът стана платен {plat(s)} · {label(s)}{money(s)}"
    return None

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
    src = None
    for s in sorted(rows, key=lambda s: s["updated_at"] or ""):
        ev = events(s, st["subs"].get(s["subscription_id"]), now)
        if ev and src is None:
            src = sources((now - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        st["subs"][s["subscription_id"]] = snap(s)
        if ev:
            kind, text = ev
            text += src_text((src or {}).get(str(s.get("user_id"))))
            st["count"][kind] = st["count"].get(kind, 0) + 1
            c = st["count"]
            send(f"{text}\n\nОбщо за деня: {c.get('paid', 0) + c.get('conv', 0)} покупки · {c.get('trial', 0)} триала")
    st["since"] = server
    if not DRY: json.dump(st, open(STATE, "w"))
    print(f"Проверени {len(rows)} променени абонамента.")

if __name__ == "__main__":
    # --loop N: проверява всяка минута в продължение на N минути (GitHub пуска графика неравномерно)
    mins = int(sys.argv[sys.argv.index("--loop") + 1]) if "--loop" in sys.argv else 0
    end = time.time() + mins * 60
    while True:
        try: main()
        except Exception as e: print("грешка:", e)
        if time.time() + 60 > end: break
        time.sleep(60)
