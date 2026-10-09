#!/usr/bin/env python3
"""
Bot Telegram per Arcadia Cinema (pensato per GitHub Actions).

Cosa fa
  - La prima volta che parte invia subito l'intera programmazione dei cinema scelti:
    un messaggio per film, con locandina e orari. Lo stesso accade quando aggiungi
    un cinema a CINEMAS o un nuovo ID a TELEGRAM_CHAT_ID: chi è nuovo la riceve una volta.
  - Poi, a ogni giro, avvisa quando compare un film nuovo o viene aggiunto un orario
    a un film già in programmazione.

Come si avvia
  python bot.py   fa un solo giro e termina. Il workflow di GitHub lo lancia ogni 15 minuti.

Variabili d'ambiente
  TELEGRAM_TOKEN    token del bot (da @BotFather)
  TELEGRAM_CHAT_ID  chat che ricevono i messaggi, separate da virgola
                    (la tua chat, quella di un amico, un gruppo)
  CINEMAS           cinema da seguire, separati da virgola
                    (melzo, bellinzago, erbusco, stezzano). Default: melzo
  WATCH             (opzionale) parole chiave separate da virgola: se presente, i NUOVI
                    ORARI vengono notificati solo per i film il cui titolo le contiene.
                    I film nuovi vengono sempre notificati.
  STATE_FILE        file dove salvare lo stato. Default: state.json
  DRY_RUN           se "1" stampa i messaggi invece di inviarli
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
CINEMAS = [
    c
    for c in dict.fromkeys(x.strip().lower() for x in os.environ.get("CINEMAS", "melzo").split(","))
    if c in CINEMA_NAMES
]
WATCH = [w.strip().lower() for w in os.environ.get("WATCH", "").split(",") if w.strip()]
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_IDS = {c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
DRY_RUN = os.environ.get("DRY_RUN") == "1" or not (TOKEN and CHAT_IDS)
if DRY_RUN and not CHAT_IDS:
    CHAT_IDS = {"anteprima"}

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


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(text))


def send_photo(chat: str, photo: str, caption: str) -> bool:
    ok = tg("sendPhoto", {"chat_id": chat, "photo": photo, "caption": caption, "parse_mode": "HTML"})
    if ok is None:
        print("Locandina non inviata, uso il solo testo", file=sys.stderr)
        return False
    time.sleep(1)
    return True


def send_text(chat: str, text: str) -> bool:
    ok = True
    for chunk in split_message(text):
        res = tg(
            "sendMessage",
            {"chat_id": chat, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True},
        )
        ok = ok and res is not None
        time.sleep(1)
    return ok


def send(chat: str, header: str, body: str = "", photo: str = "") -> bool:
    """Invia una notifica. Ritorna False se Telegram non l'ha accettata.

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
        return True
    if photo:
        if len(full) <= 1000:
            if send_photo(chat, photo, full):
                return True
        elif send_photo(chat, photo, header):
            return send_text(chat, body) if body else True
    return send_text(chat, full)


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
# Stato: programmazione già vista e chi ha già ricevuto la lista completa
# ----------------------------------------------------------------------------
def load_state() -> dict:
    raw = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if "cinemas" not in raw:  # formato più vecchio: {cinema: {film...}}
        raw = {"cinemas": {k: v for k, v in raw.items() if k in CINEMA_NAMES}}
    welcomed = raw.get("welcomed", {})
    if isinstance(welcomed, list):  # versione precedente: elenco di chat già salutate
        welcomed = {chat: list(raw["cinemas"]) for chat in welcomed}
    return {
        "cinemas": raw["cinemas"],
        "welcomed": welcomed,  # {chat: [cinema di cui ha già ricevuto la lista completa]}
        "commands_cleared": bool(raw.get("commands_cleared")),
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True))


# ----------------------------------------------------------------------------
# Controllo e notifiche
# ----------------------------------------------------------------------------
def watched(title: str) -> bool:
    return not WATCH or any(w in title.lower() for w in WATCH)


def send_programming(chat: str, cinema: str, films: dict) -> bool:
    """Invia a una chat l'intera programmazione di un cinema: un messaggio per film."""
    name = CINEMA_NAMES[cinema]
    if not send(chat, f"🎞 <b>Programmazione Arcadia {name}</b>\n{len(films)} film"):
        return False
    for film in films.values():
        send(
            chat,
            f"🎬 <b>{esc(film['title'])}</b>\n📍 {name}",
            f"{format_shows(film['shows'])}\n\n{scheda_link(film)}",
            film.get("poster", ""),
        )
    return True


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


def check_all(state: dict) -> int:
    """Controlla i cinema scelti e invia i messaggi. Ritorna il numero di cinema non letti."""
    errors = 0
    chats = sorted(CHAT_IDS)
    for cinema in CINEMAS:
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
            print(f"[{cinema}] primo avvio: {len(new)} film in programmazione")
        else:
            # le novità vanno a chi ha già ricevuto la lista completa di questo cinema
            ready = [c for c in chats if cinema in state["welcomed"].get(c, [])]
            diff_and_notify(cinema, old, new, ready)
        state["cinemas"][cinema] = new

        # chi non l'ha ancora ricevuta (primo avvio, nuovo ID, nuovo cinema) la riceve ora
        for chat in chats:
            seen = state["welcomed"].setdefault(chat, [])
            if cinema in seen:
                continue
            if not seen:
                send(chat, "✅ <b>Bot attivo!</b>\nEcco la programmazione attuale dei cinema che stai seguendo.")
            if send_programming(chat, cinema, new):
                seen.append(cinema)
                save_state(state)

    # un cinema tolto da CINEMAS va dimenticato: se verrà riaggiunto, la lista ripartirà intera
    for cinema in list(state["cinemas"]):
        if cinema not in CINEMAS:
            del state["cinemas"][cinema]
            for seen in state["welcomed"].values():
                if cinema in seen:
                    seen.remove(cinema)
    return errors


def main() -> int:
    if not CINEMAS:
        print("Nessun cinema valido in CINEMAS: scegli tra " + ", ".join(CINEMA_NAMES), file=sys.stderr)
        return 1
    state = load_state()
    try:
        if not DRY_RUN and not state["commands_cleared"]:
            # le versioni precedenti avevano i comandi /list e /track: via dal menu di Telegram
            if tg("deleteMyCommands") is not None:
                state["commands_cleared"] = True
        errors = check_all(state)
    finally:
        save_state(state)
    return 1 if errors == len(CINEMAS) else 0


if __name__ == "__main__":
    sys.exit(main())
