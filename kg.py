"""Telegram бот „Briefley Градини“: нови заявки за демо от страницата за детски градини + напомняне преди срещата.

Чете GET /kindergarten-bookings (Briefley Analytics API). Праща през отделен бот (TELEGRAM_KG_BOT_TOKEN) в личния чат
(TELEGRAM_KG_CHAT_ID, ако липсва – TELEGRAM_CHAT_ID).
Състояние (kg_state.json): само числа – last_id и кои id вече са напомнени. Без имена/телефони.
Логовете (хранилището е публично) НИКОГА не съдържат лични данни – само id-та и броеве.
  --dry-run   показва само id-тата, без да праща и без да записва
"""
import datetime as dt, json, os, sys, time
from zoneinfo import ZoneInfo
import requests

STATE = os.environ.get("KG_STATE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "kg_state.json"))
SOFIA = ZoneInfo("Europe/Sofia")
DRY = "--dry-run" in sys.argv
REMIND_MIN = 15

E = dict(os.environ)
cfg = os.path.expanduser("~/.kidsapp/keys/config.env")
if os.path.exists(cfg):
    for line in open(cfg):
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); E.setdefault(k.strip(), v.strip())

API = E.get("BRIEFLEY_ANALYTICS_URL", "https://www.briefley.com/analytics/v1").rstrip("/")
H = {"Authorization": f"Bearer {E.get('BRIEFLEY_ANALYTICS_KEY', '').strip()}"}
TOKEN = E.get("TELEGRAM_KG_BOT_TOKEN", "").strip()
CHAT = (E.get("TELEGRAM_KG_CHAT_ID") or E.get("TELEGRAM_CHAT_ID") or "").strip()
MONTHS = ["", "януари", "февруари", "март", "април", "май", "юни", "юли", "август", "септември", "октомври", "ноември", "декември"]
DAYS = ["понеделник", "вторник", "сряда", "четвъртък", "петък", "събота", "неделя"]


def get(**p):
    for i in range(4):
        r = requests.get(f"{API}/kindergarten-bookings", headers=H, params=p, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "10"))); continue
        if r.status_code >= 500:
            time.sleep(5 * (i + 1)); continue
        r.raise_for_status(); return r.json()
    r.raise_for_status()


def all_rows(**p):
    out, cur = [], None
    while True:
        q = dict(p, limit=1000)
        if cur: q["cursor"] = cur
        j = get(**q); out += j.get("data", []); cur = j.get("next_cursor")
        if not cur: return out, j.get("server_time")


def ts(s): return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def when(b):
    try:
        d = dt.date.fromisoformat(b["meeting_date"])
        return f"{DAYS[d.weekday()]}, {d.day} {MONTHS[d.month]} в {b['meeting_time']} ч."
    except Exception:
        return f"{b.get('meeting_date') or ''} {b.get('meeting_time') or ''}".strip()


def who(b):
    s = b.get("name") or "—"
    if b.get("role_label"): s += f" ({b['role_label']})"
    return s


def tel(raw):
    """Телефонът във вид, който Telegram прави натискаем: +359 888 123 456."""
    d = "".join(ch for ch in str(raw) if ch.isdigit() or ch == "+")
    if d.startswith("00"): d = "+" + d[2:]
    if d.startswith("0") and len(d) == 10: d = "+359" + d[1:]
    elif d.startswith("359"): d = "+" + d
    if d.startswith("+359") and len(d) == 13: return f"+359 {d[4:7]} {d[7:10]} {d[10:]}"
    return d or str(raw)


def contact(b):
    parts = []
    if b.get("phone"): parts.append(f"📞 {tel(b['phone'])}")
    if b.get("email"): parts.append(b["email"])
    return ", ".join(parts)


def msg_new(b):
    lines = [f"🆕 Нова заявка за демо: {b.get('kindergarten') or '—'}", who(b)]
    if contact(b): lines.append(contact(b))
    lines.append(f"📅 Среща: {when(b)}")
    if b.get("groups"): lines.append(f"Групи: {b['groups']}")
    if b.get("source"): lines.append(f"Източник: {b['source']}")
    return "\n".join(lines)


def msg_remind(b):
    lines = [f"⏰ След {REMIND_MIN} мин – демо с {b.get('kindergarten') or '—'}", who(b)]
    if b.get("phone"): lines.append(f"📞 {tel(b['phone'])}")
    lines.append(f"Час: {b.get('meeting_time')} ч.")
    return "\n".join(lines)


def send(text, tag):
    print("→ Telegram:", tag)                       # само етикет с id, без лични данни
    if DRY: return
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": CHAT, "text": text}, timeout=30)
    r.raise_for_status()


def save(st):
    if DRY: return
    tmp = STATE + ".tmp"; json.dump(st, open(tmp, "w")); os.replace(tmp, STATE)


def main():
    if not TOKEN or not CHAT:
        print("kg: няма TELEGRAM_KG_BOT_TOKEN/чат – пропускам"); return
    st = json.load(open(STATE)) if os.path.exists(STATE) else None
    if st is None:                                   # първо пускане: само запомня последното id, нищо не праща
        rows, _ = all_rows()
        st = {"last_id": max([b["id"] for b in rows] or [0]), "reminded": []}
        save(st); print(f"kg: първо пускане, {len(rows)} стари заявки, last_id={st['last_id']}, нищо не е пратено"); return
    new, server = all_rows(after_id=st["last_id"])
    for b in sorted(new, key=lambda b: b["id"]):
        try:
            send(msg_new(b), f"нова заявка id={b['id']}")
        except Exception as ex:
            print(f"kg: Telegram грешка id={b['id']}:", type(ex).__name__); break   # без last_id напред → пак следващия път
        st["last_id"] = b["id"]; save(st)
    # напомняния: предстоящи (pending), между сега и +REMIND_MIN мин
    now = ts(server) if server else dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        upcoming, _ = all_rows(since=since)
    except Exception as ex:
        print("kg: напомняния – грешка:", type(ex).__name__); upcoming = []
    rem = set(st.get("reminded", []))
    for b in upcoming:
        m = ts(b.get("meeting_at"))
        if b.get("status") != "pending" or not m or b["id"] in rem: continue
        if dt.timedelta(0) <= m - now <= dt.timedelta(minutes=REMIND_MIN):
            try:
                send(msg_remind(b), f"напомняне id={b['id']}"); rem.add(b["id"])
            except Exception as ex:
                print(f"kg: Telegram грешка (напомняне) id={b['id']}:", type(ex).__name__)
    st["reminded"] = sorted(rem)[-200:]
    save(st)
    print(f"kg: нови {len(new)}, проверени предстоящи {len(upcoming)}")


if __name__ == "__main__":
    if "--test" in sys.argv:                         # пробно съобщение, без данни от API
        send("✅ Ботът „Briefley Градини“ е свързан. Тук ще идват новите заявки за демо от детски градини и напомняне 15 мин преди всяка среща.", "тест")
    else:
        main()
