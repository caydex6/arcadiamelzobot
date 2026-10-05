#!/usr/bin/env python3
"""
Bot Telegram per Arcadia Cinema.

Controlla la programmazione di uno o più cinema Arcadia e invia una notifica
su Telegram quando:
  - compare un film nuovo
  - viene aggiunto un orario a un film già in programmazione

Configurazione tramite variabili d'ambiente:
  TELEGRAM_TOKEN    token del bot (da @BotFather)
  TELEGRAM_CHAT_ID  id della chat/gruppo/canale che riceve i messaggi
  CINEMAS           cinema da controllare, separati da virgola
                    (melzo, bellinzago, erbusco, stezzano). Default: melzo
  WATCH             (opzionale) parole chiave separate da virgola: se presente,
                    i NUOVI ORARI vengono notificati solo per i film il cui
                    titolo contiene una di queste parole. I film nuovi vengono
                    sempre notificati.
  STATE_FILE        file dove salvare lo stato. Default: state.json
  DRY_RUN           se "1" stampa i messaggi invece di inviarli

Alla prima esecuzione per un cinema lo stato viene solo salvato, senza
notifiche per tutto ciò che è già in programmazione.
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
CINEMAS = [c.strip() for c in os.environ.get("CINEMAS", "melzo").split(",") if c.strip()]
WATCH = [w.strip().lower() for w in os.environ.get("WATCH", "").split(",") if w.strip()]
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
DRY_RUN = os.environ.get("DRY_RUN") == "1" or not (TOKEN and CHAT_ID)

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


def send_photo(photo: str, caption: str) -> bool:
    """Invia la locandina con una didascalia. False se Telegram la rifiuta."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            json={"chat_id": CHAT_ID, "photo": photo, "caption": caption, "parse_mode": "HTML"},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"Errore invio locandina: {e}", file=sys.stderr)
        return False
    if not r.ok:
        print(f"Locandina rifiutata da Telegram ({r.status_code}): {r.text}", file=sys.stderr)
        return False
    time.sleep(1)
    return True


def send_text(text: str) -> None:
    for chunk in split_message(text):
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if not r.ok:
            print(f"Errore Telegram {r.status_code}: {r.text}", file=sys.stderr)
        time.sleep(1)


def send(header: str, body: str = "", photo: str = "") -> None:
    """Invia una notifica.

    - con locandina: se intestazione + orari stanno nella didascalia (max 1024
      caratteri) parte un solo messaggio; altrimenti la locandina porta solo
      l'intestazione e gli orari seguono in un secondo messaggio (senza
      ripetere l'intestazione).
    - senza locandina (o se Telegram la rifiuta): un messaggio di testo completo.
    """
    full = f"{header}\n\n{body}" if body else header
    if DRY_RUN:
        print("--- MESSAGGIO (dry run) ---")
        if photo:
            print(f"[locandina: {photo}]")
        if photo and len(full) > 1000:
            print(re.sub(r"<[^>]+>", "", html.unescape(header)))
            print("--- secondo messaggio ---")
            print(re.sub(r"<[^>]+>", "", html.unescape(body)))
        else:
            print(re.sub(r"<[^>]+>", "", html.unescape(full)))
        return
    if photo:
        if len(full) <= 1000:
            if send_photo(photo, full):
                return
        elif send_photo(photo, header):
            if body:
                send_text(body)
            return
    send_text(full)


def split_message(text: str, limit: int = 4000):
    lines, buf = text.split("\n"), ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


GIORNI = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


def to_date(day: str) -> date:
    """'19/12' -> data completa. L'anno non è sul sito: si sceglie quello che
    porta la data più vicina a oggi (da 30 giorni fa a circa 11 mesi avanti)."""
    d, m = map(int, day.split("/"))
    today = date.today()
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            cand = date(year, m, d)
        except ValueError:
            continue
        if -30 <= (cand - today).days <= 335:
            return cand
    return date(today.year, m, d)


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


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def watched(title: str) -> bool:
    return not WATCH or any(w in title.lower() for w in WATCH)


def diff_and_notify(cinema: str, old: dict, new: dict) -> None:
    name = cinema.capitalize()
    for slug, film in new.items():
        link = f'<a href="{html.escape(film["url"])}">SCHEDA FILM</a>'
        title = esc(film["title"])
        if slug not in old:
            send(
                f"🎬 <b>Nuovo film a {name}</b>\n<b>{title}</b>",
                f"{format_shows(film['shows'])}\n\n{link}",
                film.get("poster", ""),
            )
            continue
        added = {i: s for i, s in film["shows"].items() if i not in old[slug]["shows"]}
        if added and watched(film["title"]):
            send(
                f"🕒 <b>Nuovi orari a {name}</b>\n<b>{title}</b>",
                f"{format_shows(added)}\n\n{link}",
                film.get("poster", ""),
            )


def main() -> int:
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    errors = 0

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

        if cinema not in state:
            print(f"[{cinema}] primo avvio: salvati {len(new)} film, nessuna notifica")
            send(f"✅ Bot attivo per Arcadia {cinema.capitalize()}: {len(new)} film in programmazione.")
        else:
            diff_and_notify(cinema, state[cinema], new)

        state[cinema] = new

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True))
    return 1 if errors == len(CINEMAS) else 0


if __name__ == "__main__":
    sys.exit(main())
