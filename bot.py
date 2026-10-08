#!/usr/bin/env python3
"""
Bot Telegram per Arcadia Cinema.

Cosa fa
  - Notifica quando compare un film nuovo o viene aggiunto un orario a un film
    già in programmazione, per tutti i cinema Arcadia che hai scelto di tracciare.
  - /track  -> lista di cinema attivabili/disattivabili con un tap (✅ attivo, ❌ no)
  - /list   -> invia l'intera programmazione dei cinema tracciati (locandine, orari)

Come si avvia
  python bot.py          fa un solo giro e termina (modalità GitHub Actions): legge i
                         comandi arrivati dall'ultimo giro, controlla i cinema e invia
                         le notifiche. Il workflow lo lancia ogni pochi minuti, quindi
                         i comandi ricevono risposta al giro successivo.
  python bot.py --loop   (facoltativo) resta sempre acceso su un PC/server: risponde
                         subito ai comandi e controlla i cinema ogni CHECK_MINUTES

Variabili d'ambiente
  TELEGRAM_TOKEN    token del bot (da @BotFather)
  TELEGRAM_CHAT_ID  chat autorizzate, separate da virgola (la tua chat e/o un gruppo).
                    Le altre chat vengono ignorate.
  CINEMAS           cinema attivi di default per chi non ha ancora usato /track
                    (melzo, bellinzago, erbusco, stezzano). Default: melzo
  CHECK_MINUTES     ogni quanti minuti controllare il sito (solo con --loop). Default: 15
  WATCH             (opzionale) parole chiave separate da virgola: se presente, i NUOVI
                    ORARI vengono notificati solo per i film il cui titolo le contiene.
                    I film nuovi vengono sempre notificati.
  STATE_FILE        file dove salvare lo stato. Default: state.json
  DRY_RUN           se "1" stampa i messaggi invece di inviarli

Quando un cinema viene tracciato per la prima volta, la sua programmazione attuale
viene solo memorizzata: si riceve notifica solo di ciò che cambia da quel momento.
"""
import html
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.arcadiacinema.com"
CINEMA_NAMES = {
    "melzo": "Melzo",
    "bellinzago": "Bellinzago Lombardo",
    "erbusco": "Erbusco",
    "stezzano": "Stezzano",
}
DEFAULT_CINEMAS = [
    c.strip() for c in os.environ.get("CINEMAS", "melzo").split(",") if c.strip() in CINEMA_NAMES
]
WATCH = [w.strip().lower() for w in os.environ.get("WATCH", "").split(",") if w.strip()]
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED = {c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
CHECK_EVERY = int(os.environ.get("CHECK_MINUTES", "15")) * 60
DRY_RUN = os.environ.get("DRY_RUN") == "1" or not (TOKEN and ALLOWED)
if DRY_RUN and not ALLOWED:
    ALLOWED = {"anteprima"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; arcadia-notifier/1.0; uso personale)",
    "Accept-Language": "it-IT,it;q=0.9",
}

# /acquista/<slug>/<id cinema>/<id formato>/<id proiezione>/
BUY_RE = re.compile(r"/acquista/([^/]+)/(\d+)/(\d+)/(\d+)")
# attributo title del link: "TITOLO FILM:  15/12 - 00:01"
TITLE_RE = re.compile(r"^(?P<title>.+?):\s+(?P<date>\d{2}/\d{2})\s+-\s+(?P<time>\d{2}:\d{2})\s*$")
# locandine dei film (non i banner della home, che stanno in /cdn/home/)
POSTER_RE = re.compile(r"/cdn/movies/|img\.cine-vu\.it/locandineSchede")
# /scheda/<slug>/<id>/...
SCHEDA_RE = re.compile(r"/scheda/([^/]+)/\d+")


# ----------------------------------------------------------------------------
# Lettura del sito
# ----------------------------------------------------------------------------
def fetch(url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Impossibile scaricare {url}: {last}")


def following_text(a) -> str:
    """Testo subito dopo il link dell'orario (es. 'INFINITY VISION'), se c'è."""
    sib = a.next_sibling
    while sib is not None and isinstance(sib, str) and not sib.strip():
        sib = sib.next_sibling
    if sib is None:
        return ""
    if isinstance(sib, str):
        return sib.strip()
    if getattr(sib, "name", None) == "a":
        return ""
    return sib.get_text(" ", strip=True)


def poster_for(a) -> str:
    """Locandina del film: l'immagine di locandina che precede il link dell'orario."""
    img = a.find_previous("img", src=POSTER_RE)
    if img is None:
        return ""
    # alcuni indirizzi contengono caratteri come [ ] che Telegram non accetta
    return quote(urljoin(BASE, img["src"]), safe=":/%?=&")


def scrape(cinema: str) -> dict:
    """Ritorna {slug: {title, url, poster, shows: {id_proiezione: {when, fmt}}}}."""
    page = fetch(f"{BASE}/{cinema}")
    soup = BeautifulSoup(page, "html.parser")

    scheda_urls = {}
    for a in soup.find_all("a", href=SCHEDA_RE):
        m = SCHEDA_RE.search(a["href"])
        scheda_urls.setdefault(m.group(1), a["href"])

    films: dict = {}
    for a in soup.find_all("a", href=BUY_RE):
        m = BUY_RE.search(a["href"])
        slug, show_id = m.group(1), m.group(4)
        t = TITLE_RE.match(a.get("title", ""))
        if not t:
            continue
        film = films.setdefault(
            slug,
            {
                "title": t.group("title").strip(),
                "url": scheda_urls.get(slug, f"{BASE}/{cinema}"),
                "poster": poster_for(a),
                "shows": {},
            },
        )
        film["shows"][show_id] = {
            "when": f"{t.group('date')} {t.group('time')}",
            "fmt": following_text(a),
        }
    return films


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def tg(method, payload=None, timeout=30):
    """Chiama l'API di Telegram. Ritorna il risultato, oppure None se fallisce."""
    for _ in range(2):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload or {}, timeout=timeout
            )
        except requests.RequestException as e:
            print(f"Errore Telegram ({method}): {e}", file=sys.stderr)
            return None
        if r.status_code == 429:  # troppi messaggi: Telegram dice quanto aspettare
            try:
                wait = int(r.json()["parameters"]["retry_after"])
            except Exception:  # noqa: BLE001
                wait = 5
            time.sleep(wait + 1)
            continue
        if not r.ok:
            print(f"Errore Telegram {method} {r.status_code}: {r.text}", file=sys.stderr)
            return None
        try:
            return r.json().get("result", True)
        except ValueError:
            return True
    return None


def split_message(text: str, limit: int = 4000):
    lines, buf = text.split("\n"), ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


def send_photo(chat: str, photo: str, caption: str) -> bool:
    ok = tg("sendPhoto", {"chat_id": chat, "photo": photo, "caption": caption, "parse_mode": "HTML"})
    if ok is None:
        print("Locandina non inviata, uso il solo testo", file=sys.stderr)
        return False
    time.sleep(1)
    return True


def send_text(chat: str, text: str) -> None:
    for chunk in split_message(text):
        tg(
            "sendMessage",
            {"chat_id": chat, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True},
        )
        time.sleep(1)


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(text))


def send(chat: str, header: str, body: str = "", photo: str = "") -> None:
    """Invia una notifica.

    - con locandina: se intestazione + orari stanno nella didascalia (max 1024
      caratteri) parte un solo messaggio; altrimenti la locandina porta solo
      l'intestazione e gli orari seguono in un secondo messaggio.
    - senza locandina (o se Telegram la rifiuta): un messaggio di testo completo.
    """
    full = f"{header}\n\n{body}" if body else header
    if DRY_RUN:
        print(f"--- MESSAGGIO per {chat} (dry run) ---")
        if photo:
            print(f"[locandina: {photo}]")
        if photo and len(full) > 1000:
            print(plain(header))
            print("--- secondo messaggio ---")
            print(plain(body))
        else:
            print(plain(full))
        return
    if photo:
        if len(full) <= 1000:
            if send_photo(chat, photo, full):
                return
        elif send_photo(chat, photo, header):
            if body:
                send_text(chat, body)
            return
    send_text(chat, full)


# ----------------------------------------------------------------------------
# Formattazione
# ----------------------------------------------------------------------------
GIORNI = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


def to_date(day: str) -> date:
    """'19/12' -> data completa. L'anno non è sul sito: si sceglie quello che
    porta la data più vicina a oggi (da 120 giorni fa a circa 8 mesi avanti)."""
    d, m = map(int, day.split("/"))
    today = date.today()
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            cand = date(year, m, d)
        except ValueError:
            continue
        if -120 <= (cand - today).days <= 245:
            return cand
    return date(today.year, m, d)


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def format_shows(shows: dict) -> str:
    """Un blocco per giorno, un orario per riga:

    📅 Sabato 19/12
    00:30 (INFINITY VISION)
    01:00 (ENERGIA INFINITY VISION)
    """
    by_day: dict = {}
    for s in shows.values():
        day, hour = s["when"].split(" ")
        by_day.setdefault(day, []).append((hour, s["fmt"]))
    blocks = []
    for day in sorted(by_day, key=to_date):
        items = sorted(by_day[day])
        lines = [f"{h} ({esc(f)})" if f else h for h, f in items]
        label = f"{GIORNI[to_date(day).weekday()]} {day}"
        blocks.append(f"📅 <b>{label}</b>\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def scheda_link(film: dict) -> str:
    return f'<a href="{html.escape(film["url"])}">SCHEDA FILM</a>'


# ----------------------------------------------------------------------------
# Stato (cinema memorizzati, scelte di ogni chat, posizione negli aggiornamenti)
# ----------------------------------------------------------------------------
def load_state() -> dict:
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if "cinemas" not in state:  # vecchio formato: {cinema: {film...}}
        state = {"cinemas": {k: v for k, v in state.items() if k in CINEMA_NAMES}}
    state.setdefault("chats", {})
    state.setdefault("offset", 0)
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True))


def tracked(state: dict, chat: str) -> list:
    """Cinema tracciati da una chat (se non ha mai usato /track: quelli di default)."""
    chosen = state["chats"].get(chat, DEFAULT_CINEMAS)
    return [c for c in CINEMA_NAMES if c in chosen]


# ----------------------------------------------------------------------------
# Controllo periodico e notifiche
# ----------------------------------------------------------------------------
def watched(title: str) -> bool:
    return not WATCH or any(w in title.lower() for w in WATCH)


def diff_and_notify(cinema: str, old: dict, new: dict, chats: list) -> None:
    name = CINEMA_NAMES[cinema]
    for slug, film in new.items():
        title = esc(film["title"])
        link = scheda_link(film)
        if slug not in old:
            header = f"🎬 <b>Nuovo film a {name}</b>\n<b>{title}</b>"
            body = f"{format_shows(film['shows'])}\n\n{link}"
        else:
            added = {i: s for i, s in film["shows"].items() if i not in old[slug]["shows"]}
            if not added or not watched(film["title"]):
                continue
            header = f"🕒 <b>Nuovi orari a {name}</b>\n<b>{title}</b>"
            body = f"{format_shows(added)}\n\n{link}"
        for chat in chats:
            send(chat, header, body, film.get("poster", ""))


def check_all(state: dict) -> tuple:
    """Controlla i cinema tracciati da almeno una chat. Ritorna (errori, cinema controllati)."""
    wanted = {c for chat in ALLOWED for c in tracked(state, chat)}
    errors = 0
    for cinema in sorted(wanted):
        chats = [c for c in sorted(ALLOWED) if cinema in tracked(state, c)]
        try:
            new = scrape(cinema)
        except Exception as e:  # noqa: BLE001
            print(f"[{cinema}] errore: {e}", file=sys.stderr)
            errors += 1
            continue
        if not new:
            # pagina vuota o struttura cambiata: non toccare lo stato
            print(f"[{cinema}] nessun film trovato, stato non aggiornato", file=sys.stderr)
            errors += 1
            continue

        old = state["cinemas"].get(cinema)
        if old is None:
            print(f"[{cinema}] primo avvio: salvati {len(new)} film, nessuna notifica")
            for chat in chats:
                send(chat, f"✅ Tracciamento attivo per Arcadia {CINEMA_NAMES[cinema]}: "
                           f"{len(new)} film in programmazione.")
        else:
            diff_and_notify(cinema, old, new, chats)
        state["cinemas"][cinema] = new

    # un cinema che nessuno traccia più non va tenuto: se verrà riattivato
    # si ripartirà da zero, senza una valanga di "nuovi film"
    for cinema in list(state["cinemas"]):
        if cinema not in wanted:
            del state["cinemas"][cinema]
    return errors, len(wanted)


# ----------------------------------------------------------------------------
# Comandi
# ----------------------------------------------------------------------------
HELP = (
    "👋 <b>Bot Arcadia Cinema</b>\n\n"
    "/list - tutta la programmazione dei cinema che tracci\n"
    "/track - scegli quali cinema tracciare\n\n"
    "Ti avviso quando esce un film nuovo o viene aggiunto un orario."
)


def track_keyboard(state: dict, chat: str) -> dict:
    on = tracked(state, chat)
    rows = [
        [{"text": f"{'✅' if c in on else '❌'} {name}", "callback_data": f"s:{c}:{0 if c in on else 1}"}]
        for c, name in CINEMA_NAMES.items()
    ]
    return {"inline_keyboard": rows}


def cmd_track(state: dict, chat: str) -> None:
    tg(
        "sendMessage",
        {
            "chat_id": chat,
            "text": "🎟 <b>Cinema da tracciare</b>\nTocca un cinema per attivarlo (✅) o disattivarlo (❌).",
            "parse_mode": "HTML",
            "reply_markup": track_keyboard(state, chat),
        },
    )


def cmd_list(state: dict, chat: str) -> None:
    cinemas = tracked(state, chat)
    if not cinemas:
        send_text(chat, "Non stai tracciando nessun cinema. Usa /track per sceglierli.")
        return
    for cinema in cinemas:
        name = CINEMA_NAMES[cinema]
        try:
            films = scrape(cinema)
        except Exception as e:  # noqa: BLE001
            print(f"[{cinema}] errore: {e}", file=sys.stderr)
            films = None
        if not films:
            send_text(chat, f"⚠️ Non riesco a leggere la programmazione di {name} in questo momento.")
            continue
        send_text(chat, f"🎞 <b>Programmazione Arcadia {name}</b>\n{len(films)} film")
        for film in films.values():
            send(
                chat,
                f"🎬 <b>{esc(film['title'])}</b>\n📍 {name}",
                f"{format_shows(film['shows'])}\n\n{scheda_link(film)}",
                film.get("poster", ""),
            )


def on_set(state: dict, query: dict, chat: str, cinema: str, want: bool) -> None:
    """Imposta un cinema su attivo/disattivo. Il tasto indica lo stato voluto (non
    'inverti'), così un doppio tap o una risposta in ritardo non lo fanno rimbalzare."""
    if cinema not in CINEMA_NAMES:
        tg("answerCallbackQuery", {"callback_query_id": query["id"]})
        return
    on = tracked(state, chat)
    if want and cinema not in on:
        on.append(cinema)
    elif not want and cinema in on:
        on.remove(cinema)
    note = f"{CINEMA_NAMES[cinema]} {'attivato' if want else 'disattivato'}"
    state["chats"][chat] = [c for c in CINEMA_NAMES if c in on]
    save_state(state)

    tg("answerCallbackQuery", {"callback_query_id": query["id"], "text": note})
    tg(
        "editMessageReplyMarkup",
        {
            "chat_id": chat,
            "message_id": query["message"]["message_id"],
            "reply_markup": track_keyboard(state, chat),
        },
    )

    # cinema appena attivato: si memorizza subito la programmazione attuale,
    # così da ricevere solo le novità da questo momento
    if cinema in on and cinema not in state["cinemas"]:
        try:
            films = scrape(cinema)
            if films:
                state["cinemas"][cinema] = films
                save_state(state)
        except Exception as e:  # noqa: BLE001
            print(f"[{cinema}] errore: {e}", file=sys.stderr)  # lo farà il prossimo controllo


def handle_update(state: dict, update: dict) -> None:
    if "message" in update:
        msg = update["message"]
        chat = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        if chat not in ALLOWED:
            print(f"Messaggio ignorato da chat non autorizzata {chat}", file=sys.stderr)
            return
        if not text.startswith("/"):
            return
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/list":
            cmd_list(state, chat)
        elif cmd == "/track":
            cmd_track(state, chat)
        elif cmd in ("/start", "/help"):
            send_text(chat, HELP)
    elif "callback_query" in update:
        query = update["callback_query"]
        message = query.get("message")
        if not message:
            return
        chat = str(message["chat"]["id"])
        data = query.get("data", "")
        parts = data.split(":")
        if chat in ALLOWED and len(parts) == 3 and parts[0] == "s":
            on_set(state, query, chat, parts[1], parts[2] == "1")


def process_updates(state: dict, timeout: int = 0) -> bool:
    """Gestisce i comandi arrivati. Con timeout > 0 resta in attesa (long polling).
    Ritorna False se Telegram non è raggiungibile."""
    updates = tg(
        "getUpdates",
        {"offset": state["offset"], "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
        timeout=timeout + 15,
    )
    if updates is None:
        return False
    for update in updates:
        state["offset"] = update["update_id"] + 1
        save_state(state)  # prima di gestirlo, così un errore non lo fa ripetere
        try:
            handle_update(state, update)
        except Exception as e:  # noqa: BLE001
            print(f"Errore nel gestire l'aggiornamento: {e}", file=sys.stderr)
    return True


def set_commands() -> None:
    tg(
        "setMyCommands",
        {
            "commands": [
                {"command": "list", "description": "Tutta la programmazione dei cinema tracciati"},
                {"command": "track", "description": "Scegli i cinema da tracciare"},
            ]
        },
    )


# ----------------------------------------------------------------------------
# Avvio
# ----------------------------------------------------------------------------
def run_once() -> int:
    state = load_state()
    if not DRY_RUN:
        set_commands()
        process_updates(state, 0)
    errors, total = check_all(state)
    save_state(state)
    return 1 if total and errors == total else 0


def run_loop() -> int:
    if DRY_RUN:
        print("Con --loop servono TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.", file=sys.stderr)
        return 1
    state = load_state()
    set_commands()
    print("Bot avviato. Premi Ctrl+C per fermarlo.")
    last_check = 0.0
    while True:
        try:
            if not process_updates(state, 30):
                time.sleep(10)
            if time.time() - last_check >= CHECK_EVERY:
                check_all(state)
                save_state(state)
                last_check = time.time()
        except KeyboardInterrupt:
            print("Bot fermato.")
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"Errore inatteso: {e}", file=sys.stderr)
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(run_loop() if "--loop" in sys.argv else run_once())
