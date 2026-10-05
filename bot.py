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
from pathlib import Path

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


def scrape(cinema: str) -> dict:
    """Ritorna {slug: {title, url, shows: {id_proiezione: {when, fmt}}}}."""
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
            {"title": t.group("title").strip(), "url": scheda_urls.get(slug, f"{BASE}/{cinema}"), "shows": {}},
        )
        film["shows"][show_id] = {
            "when": f"{t.group('date')} {t.group('time')}",
            "fmt": following_text(a),
        }
    return films


def send(text: str) -> None:
    if DRY_RUN:
        print("--- MESSAGGIO (dry run) ---")
        print(re.sub(r"<[^>]+>", "", html.unescape(text)))
        return
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


def split_message(text: str, limit: int = 4000):
    lines, buf = text.split("\n"), ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


def format_shows(shows: dict) -> str:
    """Raggruppa per giorno: '• 05/10: 17:10, 21:15 (2D)'."""
    by_day: dict = {}
    for s in shows.values():
        day, hour = s["when"].split(" ")
        by_day.setdefault(day, []).append((hour, s["fmt"]))
    out = []
    for day, items in by_day.items():
        items.sort()
        parts = [f"{h} ({f})" if f else h for h, f in items]
        out.append(f"• {day}: {', '.join(parts)}")
    return "\n".join(out)


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def watched(title: str) -> bool:
    return not WATCH or any(w in title.lower() for w in WATCH)


def diff_and_notify(cinema: str, old: dict, new: dict) -> None:
    name = cinema.capitalize()
    for slug, film in new.items():
        link = f'<a href="{html.escape(film["url"])}">Scheda film</a>'
        if slug not in old:
            send(
                f"🎬 <b>Nuovo film a {name}</b>\n<b>{esc(film['title'])}</b>\n\n"
                f"{esc(format_shows(film['shows']))}\n\n{link}"
            )
            continue
        added = {i: s for i, s in film["shows"].items() if i not in old[slug]["shows"]}
        if added and watched(film["title"]):
            send(
                f"🕒 <b>Nuovi orari a {name}</b>\n<b>{esc(film['title'])}</b>\n\n"
                f"{esc(format_shows(added))}\n\n{link}"
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
