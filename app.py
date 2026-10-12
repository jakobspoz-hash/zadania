"""
Dom Zadania – synchronizacja list Google Tasks między dwoma kontami
+ proste API dla wyświetlacza ESP32.

Zmienne środowiskowe (Render → Environment):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET  – z pliku JSON klienta OAuth
  REFRESH_TOKEN_A, REFRESH_TOKEN_B        – z get_tokens.py (A = konto główne, z niego czyta ESP32)
  API_KEY                                 – dowolne długie hasło, to samo wpiszesz w ESP32
  LIST_NAMES   (opcjonalnie) – domyślnie "Krótkoterminowe,Długoterminowe,Ćwiczenia"
  SYNC_INTERVAL (opcjonalnie) – sekundy między synchronizacjami, domyślnie 60
  CONTACT_EMAIL (opcjonalnie) – e-mail na stronie polityki prywatności
"""
import html
import logging
import os
import re
import threading
import time

import requests
from flask import Flask, abort, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dom")

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REFRESH_A = os.environ.get("REFRESH_TOKEN_A", "")
REFRESH_B = os.environ.get("REFRESH_TOKEN_B", "")
API_KEY = os.environ.get("API_KEY", "")
LIST_NAMES = [n.strip() for n in os.environ.get(
    "LIST_NAMES", "Krótkoterminowe,Długoterminowe,Ćwiczenia").split(",") if n.strip()]
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "60"))
CONTACT = os.environ.get("CONTACT_EMAIL", "")
# Przenoszenie z domyślnej listy (tam Gemini dodaje zadania):
#   ROUTE_MODE=all    – przenoś wszystko, co nowego pojawi się na liście domyślnej (domyślnie)
#   ROUTE_MODE=prefix – przenoś tylko zadania ze słowem "dom" (reszta zostaje prywatna)
#   ROUTE_MODE=off    – wyłączone
ROUTE_MODE = os.environ.get("ROUTE_MODE", "all").strip().lower()
START_TIME = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 300))
# Klasyfikacja AI (darmowy klucz z aistudio.google.com). Bez klucza: tylko słowa kluczowe.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-flash-lite-latest,gemini-flash-latest").split(",") if m.strip()]
AI_RETRY_MINUTES = int(os.environ.get("AI_RETRY_MINUTES", "10"))

API = "https://tasks.googleapis.com/tasks/v1"
MARK_RE = re.compile(r"\s*\[sync:([A-Za-z0-9_\-]+)\]\s*$")


# ---------------------------------------------------------------- Google Tasks

class Account:
    """Minimalny klient Google Tasks API dla jednego konta."""

    def __init__(self, name, refresh_token):
        self.name = name
        self.refresh_token = refresh_token
        self._access = None
        self._exp = 0.0
        self._s = requests.Session()

    def _token(self):
        if self._access and time.time() < self._exp - 60:
            return self._access
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        }, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"[{self.name}] odświeżenie tokenu nieudane: {r.status_code} {r.text[:200]}")
        j = r.json()
        self._access = j["access_token"]
        self._exp = time.time() + j.get("expires_in", 3600)
        return self._access

    def _req(self, method, path, ok404=False, **kw):
        for attempt in range(2):
            r = self._s.request(method, API + path, timeout=20,
                                headers={"Authorization": f"Bearer {self._token()}"}, **kw)
            if r.status_code == 401 and attempt == 0:
                self._access = None
                continue
            break
        if ok404 and r.status_code == 404:
            return {}
        if r.status_code >= 400:
            raise RuntimeError(f"[{self.name}] {method} {path}: {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}

    def _paged(self, path, params=None):
        params = dict(params or {})
        out = []
        while True:
            j = self._req("GET", path, params=params)
            out.extend(j.get("items", []))
            if not j.get("nextPageToken"):
                return out
            params["pageToken"] = j["nextPageToken"]

    def lists(self):
        return self._paged("/users/@me/lists", {"maxResults": 100})

    def tasks(self, list_id):
        return self._paged(f"/lists/{list_id}/tasks", {
            "maxResults": 100, "showCompleted": "true", "showHidden": "true"})

    def insert(self, list_id, body):
        return self._req("POST", f"/lists/{list_id}/tasks", json=body)

    def patch(self, list_id, task_id, body):
        return self._req("PATCH", f"/lists/{list_id}/tasks/{task_id}", json=body)

    def delete(self, list_id, task_id):
        self._req("DELETE", f"/lists/{list_id}/tasks/{task_id}", ok404=True)


# ---------------------------------------------------------------- logika sync

def split_notes(notes):
    notes = notes or ""
    m = MARK_RE.search(notes)
    if m:
        return notes[:m.start()].rstrip(), m.group(1)
    return notes.rstrip(), None


def join_notes(text, mark):
    text = (text or "").rstrip()
    return (text + "\n" if text else "") + f"[sync:{mark}]"


def fields(t):
    text, _ = split_notes(t.get("notes"))
    return {
        "title": (t.get("title") or "").strip(),
        "notes": text.strip(),
        "due": (t.get("due") or "")[:10],
        "status": t.get("status") or "needsAction",
    }


def body_from(f, mark):
    b = {
        "title": f["title"],
        "notes": join_notes(f["notes"], mark),
        "due": f"{f['due']}T00:00:00.000Z" if f["due"] else None,
        "status": f["status"],
    }
    if f["status"] == "needsAction":
        b["completed"] = None
    return b


_pending_mass_delete = {}


def sync_list(A, la, B, lb):
    """Synchronizuje listę `la` konta A z listą `lb` konta B.

    Para zadań: każde ma w notatce znacznik [sync:<id partnera>].
    Zadanie bez znacznika = nowe → kopiujemy i oznaczamy oba.
    Zadanie ze znacznikiem, którego partner zniknął = partner usunięty → usuwamy.
    Różnica pól → wygrywa nowsza wersja (pole `updated`).
    """
    sides = {
        "A": (A, la, {t["id"]: t for t in A.tasks(la)}),
        "B": (B, lb, {t["id"]: t for t in B.tasks(lb)}),
    }
    other = {"A": "B", "B": "A"}
    to_delete = []   # (konto, lista, id)
    to_create = []   # (strona, zadanie)
    to_update = []   # (konto, lista, id, body)

    for s in ("A", "B"):
        acc, lid, tasks = sides[s]
        oacc, olid, otasks = sides[other[s]]
        for t in tasks.values():
            _, mark = split_notes(t.get("notes"))
            if mark is None:
                if fields(t)["title"]:          # pomijamy puste (w trakcie wpisywania)
                    to_create.append((s, t))
                continue
            partner = otasks.get(mark)
            if partner is None or split_notes(partner.get("notes"))[1] != t["id"]:
                to_delete.append((acc, lid, t["id"]))
                continue
            if s == "A":                        # porównujemy każdą parę tylko raz
                fa, fb = fields(t), fields(partner)
                if fa != fb:
                    if (t.get("updated") or "") >= (partner.get("updated") or ""):
                        to_update.append((oacc, olid, partner["id"], body_from(fa, t["id"])))
                    else:
                        to_update.append((acc, lid, t["id"], body_from(fb, partner["id"])))

    # Bezpiecznik: masowe usuwanie wykonujemy dopiero, gdy powtórzy się w 2 kolejnych cyklach
    total = len(sides["A"][2]) + len(sides["B"][2])
    key = (la, lb)
    if len(to_delete) > max(3, total // 2):
        sig = frozenset(d[2] for d in to_delete)
        if _pending_mass_delete.get(key) != sig:
            _pending_mass_delete[key] = sig
            log.warning("Wstrzymano usunięcie %d zadań (%s) – potwierdzę w kolejnym cyklu", len(to_delete), la)
            to_delete = []
    else:
        _pending_mass_delete.pop(key, None)

    for acc, lid, tid in to_delete:
        log.info("[%s] usuwam %s", acc.name, tid)
        acc.delete(lid, tid)

    for acc, lid, tid, body in to_update:
        log.info("[%s] aktualizuję %s → %r", acc.name, tid, body["title"])
        acc.patch(lid, tid, body)

    for s, t in to_create:
        acc, lid, _ = sides[s]
        oacc, olid, _ = sides[other[s]]
        f = fields(t)
        copy = oacc.insert(olid, body_from(f, t["id"]))
        acc.patch(lid, t["id"], {"notes": join_notes(f["notes"], copy["id"])})
        log.info("[%s→%s] skopiowano %r", acc.name, oacc.name, f["title"])


# Słowa kluczowe → indeks listy w LIST_NAMES (0 = krótko, 1 = długo, 2 = ćwiczenia).
# Bez słowa kluczowego zadanie trafia na pierwszą listę.
KEYWORDS = [
    (2, ["ćwiczenia", "ćwiczenie", "ćwiczeń", "cwiczenia", "cwiczenie", "trening"]),
    (1, ["długoterminowe", "długoterminowa", "długoterminowy", "długoterminowych",
         "dlugoterminowe", "długo", "dlugo", "kiedyś", "kiedys"]),
    (0, ["krótkoterminowe", "krótkoterminowa", "krotkoterminowe", "krótko", "krotko"]),
]
LIST_WORD_RE = r"(?:(?:do|na)\s+)?(?:list[ayęie]\s+)?"
# Słowo "dom" liczy się tylko na początku zadania i tylko w trybie ROUTE_MODE=prefix
DOM_RE = re.compile(r"^\s*(?:do\s+|na\s+)?(?:list[ayęie]\s+)?dom(?:ow[aąey]*)?(?!\w)\s*[:,\-–]?", re.I)


AI_PROMPT = """Przypisz zadanie domowe do jednej z list. Odpowiedz WYŁĄCZNIE cyfrą.
0 = krótkoterminowe: sprawy na najbliższe dni – zakupy, sprzątanie, telefony, załatwienia, drobne naprawy
1 = długoterminowe: większe projekty i plany na tygodnie/miesiące – remonty, malowanie, wyjazdy, duże zakupy, nauka
2 = ćwiczenia: aktywność fizyczna – pompki, przysiady, bieganie, rozciąganie, trening, joga, rower, plank
Zadanie: {title}
Cyfra:"""


def ai_classify(title):
    """Zwraca indeks listy od AI albo None, gdy AI niedostępne."""
    if not GEMINI_API_KEY:
        return None
    base = {"contents": [{"parts": [{"text": AI_PROMPT.format(title=title)}]}]}
    configs = [
        {"temperature": 0, "maxOutputTokens": 16, "thinkingConfig": {"thinkingBudget": 0}},
        {"temperature": 0, "maxOutputTokens": 1024},
    ]
    for model in GEMINI_MODELS:        # przy przeciążeniu próbujemy kolejnego modelu
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for cfg in configs:
            try:
                r = requests.post(url, json=dict(base, generationConfig=cfg),
                                  headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=12)
            except Exception as e:  # noqa: BLE001
                log.warning("AI %s: %s", model, str(e)[:120])
                break
            if r.status_code == 400:
                continue               # model nie obsługuje tej konfiguracji – prostsza
            if r.status_code != 200:
                log.warning("AI %s: HTTP %s", model, r.status_code)
                break
            try:
                parts = r.json()["candidates"][0].get("content", {}).get("parts", [])
            except Exception:  # noqa: BLE001
                break
            m = re.search(r"[012]", "".join(p.get("text", "") for p in parts))
            if m:
                return int(m.group())
    return None


def classify(title):
    """Zwraca (indeks listy albo None gdy brak słowa kluczowego, oczyszczony tytuł, czy było 'dom')."""
    t = title.strip()
    had_dom = bool(DOM_RE.search(t))
    if ROUTE_MODE == "prefix":
        t = DOM_RE.sub(" ", t, count=1)
    idx = None
    for i, words in KEYWORDS:
        pat = re.compile(LIST_WORD_RE + r"(?<!\w)(?:" + "|".join(map(re.escape, words)) + r")(?!\w)", re.I)
        if pat.search(t):
            idx = i
            t = pat.sub(" ", t, count=1)
            break
    t = re.sub(r"\s+", " ", t).strip(" :,-–.")
    if t:
        t = t[0].upper() + t[1:]
    return idx, t, had_dom


def route_default(acc, lists):
    """Przenosi nowe zadania z listy domyślnej konta do właściwej listy domowej."""
    if ROUTE_MODE == "off":
        return
    targets = [lists.get(n) for n in LIST_NAMES]
    default_id = acc._req("GET", "/users/@me/lists/@default").get("id")
    if not default_id or default_id in targets:
        return
    for t in acc.tasks(default_id):
        title = (t.get("title") or "").strip()
        if not title or t.get("status") == "completed" or t.get("parent"):
            continue
        if (t.get("updated") or "") < START_TIME:   # nie ruszamy starszych, prywatnych zadań
            continue
        idx, clean, had_dom = classify(title)
        if ROUTE_MODE == "prefix" and not had_dom:
            continue
        how = "słowo kluczowe"
        if idx is None:
            idx = ai_classify(clean)
            how = "AI"
        if idx is None:
            retry_until = time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.gmtime(time.time() - AI_RETRY_MINUTES * 60))
            if GEMINI_API_KEY and (t.get("updated") or "") > retry_until:
                log.info("[%s] AI nie odpowiada – %r poczeka do kolejnej próby", acc.name, clean)
                continue               # zostaje na liście domyślnej, spróbujemy za minutę
            idx, how = 0, "domyślnie"
        target = targets[idx] if idx < len(targets) else None
        if not target or not clean:
            continue
        body = {"title": clean, "notes": t.get("notes") or None,
                "due": t.get("due") or None, "status": "needsAction"}
        acc.insert(target, {k: v for k, v in body.items() if v is not None})
        acc.delete(default_id, t["id"])
        log.info("[%s] przeniesiono %r → %s (%s)", acc.name, clean, LIST_NAMES[idx], how)


def find_lists(acc):
    by_name = {l["title"].strip().casefold(): l["id"] for l in acc.lists()}
    return {n: by_name.get(n.casefold()) for n in LIST_NAMES}


# ---------------------------------------------------------------- stan + pętla

app = Flask(__name__)
lock = threading.Lock()
wake = threading.Event()
state = {"snapshot": {"lists": [], "updated": 0}, "last_sync": 0, "last_error": None}
ACC_A = Account("A", REFRESH_A) if REFRESH_A else None
ACC_B = Account("B", REFRESH_B) if REFRESH_B else None


def build_snapshot(acc, lists_a):
    out = []
    for name in LIST_NAMES:
        lid = lists_a.get(name)
        items = []
        if lid:
            for t in acc.tasks(lid):
                if t.get("status") == "completed" or not (t.get("title") or "").strip():
                    continue
                items.append({"id": t["id"], "title": t["title"].strip(),
                              "due": (t.get("due") or "")[:10], "_pos": t.get("position", "")})
            items.sort(key=lambda x: x.pop("_pos"))
        out.append({"id": lid, "title": name, "tasks": items})
    return {"lists": out, "updated": int(time.time())}


def sync_once():
    with lock:
        la = find_lists(ACC_A)
        lb = find_lists(ACC_B) if ACC_B else {}
        route_default(ACC_A, la)
        if ACC_B:
            route_default(ACC_B, lb)
        for name in LIST_NAMES:
            if not la.get(name):
                log.warning("Konto A nie ma listy %r", name)
            elif ACC_B and not lb.get(name):
                log.warning("Konto B nie ma listy %r", name)
            elif ACC_B:
                sync_list(ACC_A, la[name], ACC_B, lb[name])
        state["snapshot"] = build_snapshot(ACC_A, la)
        state["last_sync"] = int(time.time())
        state["last_error"] = None


def loop():
    if GEMINI_API_KEY:
        log.info("Test AI (%s): 'Zrobić 50 pompek' → %s", ",".join(GEMINI_MODELS),
                 ai_classify("Zrobić 50 pompek"))
    else:
        log.info("Brak GEMINI_API_KEY – listę wybierają tylko słowa kluczowe")
    while True:
        try:
            sync_once()
        except Exception as e:  # noqa: BLE001
            log.exception("Błąd synchronizacji")
            state["last_error"] = str(e)[:300]
        wake.wait(SYNC_INTERVAL)
        wake.clear()


if ACC_A and CLIENT_ID:
    threading.Thread(target=loop, daemon=True).start()
else:
    log.warning("Brak tokenów/klienta – synchronizacja wyłączona (strony www działają)")


# ---------------------------------------------------------------- HTTP

def require_key():
    if not API_KEY or request.headers.get("X-Api-Key") != API_KEY:
        abort(401)


PAGE = """<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{t}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.6}}</style>
</head><body>{b}</body></html>"""


@app.get("/")
def home():
    return PAGE.format(t="Dom Zadania", b=(
        "<h1>Dom Zadania</h1><p>Prywatna aplikacja domowa: synchronizuje wspólne listy "
        "zadań w Google Tasks między domownikami i wyświetla je na domowym ekranie.</p>"
        '<p><a href="/privacy">Polityka prywatności</a></p>'))


@app.get("/privacy")
def privacy():
    contact = f"<p>Kontakt: {html.escape(CONTACT)}</p>" if CONTACT else ""
    return PAGE.format(t="Polityka prywatności – Dom Zadania", b=(
        "<h1>Polityka prywatności – Dom Zadania</h1>"
        "<p>Dom Zadania to prywatna aplikacja do synchronizacji domowych list zadań w Google Tasks "
        "między członkami jednego gospodarstwa domowego oraz wyświetlania ich na domowym ekranie.</p>"
        "<p>Aplikacja korzysta wyłącznie z Google Tasks i przetwarza tylko wybrane, wspólne listy domowe. "
        "Dane nie są sprzedawane, udostępniane osobom trzecim ani wykorzystywane w innym celu niż "
        "synchronizacja tych list. Tokeny dostępu są przechowywane na serwerze aplikacji wyłącznie "
        "w celu działania synchronizacji.</p>"
        "<p>Dostęp można w każdej chwili cofnąć na stronie "
        '<a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>.</p>'
        + contact))


@app.get("/health")
def health():
    return "ok"


@app.get("/api/tasks")
def api_tasks():
    require_key()
    return jsonify(state["snapshot"])


@app.get("/api/status")
def api_status():
    require_key()
    return jsonify({"last_sync": state["last_sync"], "last_error": state["last_error"],
                    "lists": LIST_NAMES, "sync_enabled": bool(ACC_A and CLIENT_ID),
                    "two_accounts": bool(ACC_B)})


def _clean(s):
    return " ".join((s or "").replace("\t", " ").split())


@app.get("/api/tasks.txt")
def api_tasks_txt():
    """Prosty format dla ESP32 (bez JSON-a). Linie rozdzielone \\n, pola tabulatorem:
       L <id listy> <nazwa>      – początek listy
       T <id zadania> <tytuł> <termin RRRR-MM-DD lub pusty>
       END                        – koniec (brak = niepełna odpowiedź)"""
    require_key()
    snap = state["snapshot"]
    out = [f"S\t{snap.get('updated', 0)}\t{state['last_error'] or ''}"]
    for l in snap["lists"]:
        out.append(f"L\t{l['id'] or ''}\t{_clean(l['title'])}")
        for t in l["tasks"]:
            out.append(f"T\t{t['id']}\t{_clean(t['title'])}\t{t.get('due', '')}")
    out.append("END")
    return "\n".join(out) + "\n", 200, {"Content-Type": "text/plain; charset=utf-8"}


def _set_status(status):
    require_key()
    if not ACC_A:
        abort(503)
    j = request.get_json(force=True, silent=True) or {}
    lid = j.get("list") or request.values.get("list")
    tid = j.get("task") or request.values.get("task")
    if not lid or not tid:
        abort(400)
    body = {"status": status}
    if status == "needsAction":
        body["completed"] = None
    with lock:
        ACC_A.patch(lid, tid, body)
        if status == "completed":
            for l in state["snapshot"]["lists"]:
                if l["id"] == lid:
                    l["tasks"] = [t for t in l["tasks"] if t["id"] != tid]
    log.info("[ekran] %s → %s", tid, status)
    wake.set()  # przebuduj migawkę i zsynchronizuj z kontem B
    return jsonify({"ok": True})


@app.post("/api/complete")
def api_complete():
    """{"list": "<id listy>", "task": "<id zadania>"} (JSON lub formularz) – odhacza."""
    return _set_status("completed")


@app.post("/api/uncomplete")
def api_uncomplete():
    """Cofa odhaczenie (gdy ktoś stuknął przez pomyłkę)."""
    return _set_status("needsAction")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
