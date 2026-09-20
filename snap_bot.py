"""Snapchat Web auto-chatter (Playwright + Gemini).

⚠️  Automatizzare Snapchat viola i suoi Termini di Servizio e può portare al
ban dell'account. Usalo solo sul TUO account, con consapevolezza del rischio.

Comandi:
    python snap_bot.py --inspect     # mappa la pagina per tarare i SELECTORS
    python snap_bot.py --dry-run     # gira ma NON invia (stampa cosa direbbe)
    python snap_bot.py --send        # modalità reale (invia davvero)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from gemini_client import GeminiError, generate_reply
from gemini_client import validate_config as validate_gemini_config

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

PERSONA = (BASE / "persona.txt").read_text(encoding="utf-8")
FUNNEL = (BASE / "funnel.txt").read_text(encoding="utf-8")
FRIENDS_FILE = BASE / "friends.json"   # memoria per-amico + stato imbuto
LOG_FILE = BASE / "conversations.log"
USER_DATA_DIR = BASE / ".chrome-profile"   # sessione Snapchat persistente

MAX_HISTORY_LINES = 40   # quante righe di storico tenere per amico

# ── Config da .env ───────────────────────────────────────────────────
WHITELIST = [n.strip().lower() for n in os.getenv("WHITELIST", "").split(",") if n.strip()]
BLACKLIST = [n.strip().lower() for n in os.getenv("BLACKLIST", "").split(",") if n.strip()]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MAX_REPLIES = int(os.getenv("MAX_REPLIES_PER_CYCLE", "3"))
DELAY_MIN = float(os.getenv("REPLY_DELAY_MIN", "4"))
DELAY_MAX = float(os.getenv("REPLY_DELAY_MAX", "14"))
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "").strip() or None
TELEGRAM_LINK = os.getenv("TELEGRAM_LINK", "").strip()

# ── Throttle anti-ban / anti-quota (per tanti amici) ─────────────────
MAX_PER_DAY = int(os.getenv("MAX_MESSAGES_PER_DAY", "0"))   # 0 = nessun tetto
ACTIVE_START = int(os.getenv("ACTIVE_HOURS_START", "0"))    # ora 0-23
ACTIVE_END = int(os.getenv("ACTIVE_HOURS_END", "24"))       # ora di fine (esclusa)
WAVE_SIZE = int(os.getenv("WAVE_SIZE", "0"))                # 0 = niente ondate
WAVE_MINUTES = int(os.getenv("WAVE_MINUTES", "30"))         # durata di un'ondata

RUNTIME_FILE = BASE / "runtime.json"   # contatore messaggi del giorno

SNAP_URL = "https://web.snapchat.com"

# ── SELECTORS — DA TARARE con `--inspect` ────────────────────────────
# Le classi CSS di Snapchat sono offuscate (hash casuali), quindi puntiamo a
# ruoli ARIA / attributi stabili. Verificali con la modalità --inspect e
# correggi qui se serve: è il punto che più probabilmente va aggiustato.
SELECTORS = {
    # Elementi della lista conversazioni a sinistra.
    "conversation_items": '[role="listitem"], a[href^="/web/"][role="link"]',
    # Indicatore di "non letto" dentro un item (pallino/badge).
    "unread_marker": '[aria-label*="unread" i], [aria-label*="non let" i]',
    # Bolle dei messaggi nella conversazione aperta.
    "message_bubbles": '[role="log"] [role="listitem"], [data-testid*="message" i]',
    # Casella di testo dove scrivere.
    "message_input": '[contenteditable="true"], [role="textbox"]',
}


# ── Memoria per-amico + stato imbuto ─────────────────────────────────
# friends.json: { "<nome>": {stage, pitched_count, last_handled, history[]} }
def read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} non valido: ripristina il file prima di continuare.") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: atteso un oggetto JSON.")
    return data


def write_state(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_friends() -> dict:
    data = read_state(FRIENDS_FILE)
    for name, rec in data.items():
        if (not isinstance(rec, dict)
                or rec.get("stage") not in {"nuovo", "rapport", "invitato", "entrato"}
                or type(rec.get("pitched_count")) is not int
                or rec["pitched_count"] < 0
                or not isinstance(rec.get("last_handled"), str)
                or not isinstance(rec.get("history"), list)
                or not all(isinstance(line, str) for line in rec["history"])):
            raise ValueError(f"friends.json: record non valido per {name!r}.")
    return data


def save_friends(friends: dict) -> None:
    write_state(FRIENDS_FILE, friends)


def get_friend(friends: dict, name: str) -> dict:
    """Ritorna (creandolo) il record di memoria di un amico."""
    rec = friends.get(name)
    if rec is None:
        rec = {"stage": "nuovo", "pitched_count": 0, "last_handled": "", "history": []}
        friends[name] = rec
    return rec


def log(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{stamp}] {line}"
    print(msg)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def msg_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def allowed(name: str, wave: list) -> bool:
    low = name.strip().lower()
    if low in BLACKLIST:
        return False
    if WHITELIST:
        return low in wave
    return False


# ── Throttle: tetto giornaliero, fascia oraria, ondate ───────────────
def load_runtime() -> dict:
    today = time.strftime("%Y-%m-%d")
    data = read_state(RUNTIME_FILE)
    if data and (not isinstance(data.get("date"), str)
                 or type(data.get("sent")) is not int or data["sent"] < 0):
        raise ValueError("runtime.json: data o contatore non validi.")
    if data.get("date") != today:        # nuovo giorno → azzera il contatore
        data = {"date": today, "sent": 0}
    return data


def save_runtime(rt: dict) -> None:
    write_state(RUNTIME_FILE, rt)


def validate_config(send: bool = False) -> None:
    if POLL_INTERVAL <= 0 or MAX_REPLIES <= 0:
        raise ValueError("POLL_INTERVAL_SECONDS e MAX_REPLIES_PER_CYCLE devono essere positivi.")
    if not all(math.isfinite(v) for v in (DELAY_MIN, DELAY_MAX)) or not 0 <= DELAY_MIN <= DELAY_MAX:
        raise ValueError("REPLY_DELAY_MIN/MAX devono essere finiti e 0 <= min <= max.")
    if MAX_PER_DAY < 0 or WAVE_SIZE < 0 or WAVE_MINUTES <= 0:
        raise ValueError("Limiti non negativi e WAVE_MINUTES > 0 richiesti.")
    if not 0 <= ACTIVE_START <= 23 or not 0 <= ACTIVE_END <= 24 or ACTIVE_START == ACTIVE_END:
        raise ValueError("Fascia oraria non valida (inizio 0-23, fine 0-24, diversi).")
    if send and not WHITELIST:
        raise ValueError("--send richiede una WHITELIST esplicita.")


def daily_cap_reached(rt: dict) -> bool:
    return MAX_PER_DAY > 0 and rt.get("sent", 0) >= MAX_PER_DAY


def within_active_hours() -> bool:
    if ACTIVE_START == 0 and ACTIVE_END >= 24:
        return True
    h = int(time.strftime("%H"))
    if ACTIVE_START <= ACTIVE_END:
        return ACTIVE_START <= h < ACTIVE_END
    return h >= ACTIVE_START or h < ACTIVE_END   # finestra a cavallo di mezzanotte


def current_wave() -> list:
    """Sottoinsieme della whitelist attivo ORA (rotazione a ondate nel tempo)."""
    if WAVE_SIZE <= 0 or not WHITELIST:
        return WHITELIST
    num_waves = (len(WHITELIST) + WAVE_SIZE - 1) // WAVE_SIZE
    idx = int(time.time() // (WAVE_MINUTES * 60)) % num_waves
    start = idx * WAVE_SIZE
    return WHITELIST[start:start + WAVE_SIZE]


# ── Login ─────────────────────────────────────────────────────────────
def ensure_logged_in(page) -> None:
    page.goto(SNAP_URL, wait_until="domcontentloaded")
    log("Apro Snapchat Web. Se chiede il login, fallo a mano nella finestra.")
    # Aspetta che compaia la UI di chat (lista conversazioni) per max ~3 min.
    for _ in range(180):
        if page.locator(SELECTORS["conversation_items"]).count() > 0:
            log("Login ok, UI di chat rilevata.")
            return
        time.sleep(1)
    input("Non rilevo la chat. Fai login nella finestra, poi premi INVIO qui... ")


# ── Lettura conversazioni ─────────────────────────────────────────────
def read_conversation_text(page, max_msgs: int = 12) -> str:
    """Estrae il testo degli ultimi messaggi della chat aperta."""
    bubbles = page.locator(SELECTORS["message_bubbles"])
    n = bubbles.count()
    lines = []
    for i in range(max(0, n - max_msgs), n):
        try:
            txt = bubbles.nth(i).inner_text().strip()
        except PWTimeout:
            continue
        if txt:
            lines.append(txt)
    return "\n".join(lines)


def build_memory_text(rec: dict, on_screen: str) -> str:
    """Unisce lo storico salvato con quello che è ora sullo schermo."""
    parts = []
    if rec["history"]:
        parts.append("\n".join(rec["history"]))
    if on_screen:
        parts.append("[ultimi messaggi visibili ora]\n" + on_screen)
    return "\n".join(parts).strip()


def process_cycle(page, dry_run: bool, friends: dict, rt: dict,
                  previewed: dict | None = None) -> int:
    """Un giro: trova conversazioni non lette, risponde. Ritorna n. risposte."""
    items = page.locator(SELECTORS["conversation_items"])
    total = items.count()
    wave = current_wave()
    if WAVE_SIZE > 0 and WHITELIST:
        log(f"Ondata attiva ({WAVE_SIZE} alla volta): {wave}")
    sent = 0
    if previewed is None:
        previewed = {}

    for i in range(total):
        if sent >= MAX_REPLIES:
            log(f"Raggiunto il limite di {MAX_REPLIES} risposte per ciclo.")
            break
        if daily_cap_reached(rt):
            log(f"Tetto giornaliero di {MAX_PER_DAY} messaggi raggiunto. Basta per oggi.")
            break

        item = items.nth(i)
        try:
            name = item.inner_text().strip().split("\n")[0] or f"chat#{i}"
            has_unread = item.locator(SELECTORS["unread_marker"]).count() > 0
        except PWTimeout:
            continue

        if not has_unread:
            continue
        if not allowed(name, wave):
            continue   # fuori whitelist/ondata o in blacklist: salto in silenzio

        # Apri la conversazione
        try:
            item.click()
            page.wait_for_timeout(1500)
        except PWTimeout:
            continue

        on_screen = read_conversation_text(page)
        if not on_screen:
            log(f"«{name}»: nessun testo leggibile, salto.")
            continue

        last = on_screen.split("\n")[-1]
        h = msg_hash(name + "|" + last)
        rec = friends.get(name) or get_friend({}, name)
        if rec["last_handled"] == h:
            continue  # già risposto a questo messaggio
        if dry_run and previewed.get(name) == h:
            continue

        already_pitched = rec["pitched_count"] > 0
        memory_text = build_memory_text(rec, on_screen)

        # Genera la risposta (consapevole di memoria + imbuto)
        try:
            reply = generate_reply(
                persona=PERSONA,
                funnel=FUNNEL,
                memory_text=memory_text,
                friend_name=name,
                stage=rec["stage"],
                already_pitched=already_pitched,
                telegram_link=TELEGRAM_LINK,
            )
        except GeminiError as exc:
            log(f"«{name}»: Gemini ha fallito: {exc}")
            continue

        log(f"«{name}» [{rec['stage']}] ultimo: {last!r}")
        if dry_run:
            log(f"   [DRY-RUN] invierei → {reply!r}")
            previewed[name] = h
            sent += 1
            continue
        else:
            if not send_message(page, reply):
                log(f"«{name}»: invio fallito, controlla il selettore input.")
                continue
            log(f"   ✓ inviato → {reply!r}")

        # Aggiorna memoria + stato imbuto
        pitched_now = bool(TELEGRAM_LINK) and TELEGRAM_LINK in reply
        if pitched_now:
            rec["pitched_count"] += 1
            rec["stage"] = "invitato"
            log(f"   → link Telegram inviato a «{name}» (totale {rec['pitched_count']}).")
        elif rec["stage"] == "nuovo":
            rec["stage"] = "rapport"

        rec["history"].append(f"loro: {last}")
        rec["history"].append(f"io: {reply}")
        rec["history"] = rec["history"][-MAX_HISTORY_LINES:]
        rec["last_handled"] = h
        friends[name] = rec
        save_friends(friends)
        sent += 1
        rt["sent"] = rt.get("sent", 0) + 1
        save_runtime(rt)

        # Ritardo "umano" tra una chat e l'altra
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    return sent


def send_message(page, text: str) -> bool:
    box = page.locator(SELECTORS["message_input"]).last
    try:
        box.click()
        # fill supporta anche Unicode/emoji e sostituisce eventuali bozze residue.
        box.fill(text)
        page.wait_for_timeout(random.uniform(200, 600))
        box.press("Enter")
        return True
    except PWTimeout:
        return False


# ── Inspect: mappa la pagina per tarare i selettori ───────────────────
def run_inspect(page) -> None:
    page.goto(SNAP_URL, wait_until="domcontentloaded")
    input("Fai login se serve, apri una chat con messaggi, poi premi INVIO... ")
    snapshot = page.locator("body").aria_snapshot()
    out = BASE / "inspect_dump.yaml"
    out.write_text(snapshot, encoding="utf-8")
    log(f"Albero di accessibilità salvato in {out}")
    for key, sel in SELECTORS.items():
        try:
            cnt = page.locator(sel).count()
        except Exception as exc:  # noqa: BLE001
            cnt = f"errore: {exc}"
        log(f"  SELECTOR {key!r} → {cnt} match  ({sel})")
    log("Apri inspect_dump.yaml e i conteggi qui sopra per correggere SELECTORS.")
    input("Premi INVIO per chiudere... ")


# ── Main ──────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="Snapchat Web AI auto-chatter")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="non invia (default)")
    mode.add_argument("--send", action="store_true", help="abilita invio reale alla whitelist")
    ap.add_argument("--inspect", action="store_true", help="mappa la pagina per i selettori")
    ap.add_argument("--once", action="store_true", help="un solo ciclo, poi esci")
    args = ap.parse_args()
    args.dry_run = not args.send
    try:
        validate_config(send=args.send and not args.inspect)
        if not args.inspect:
            validate_gemini_config()
            friends = load_friends()
            load_runtime()
    except (ValueError, GeminiError, OSError) as exc:
        ap.error(str(exc))

    USER_DATA_DIR.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,
            channel=BROWSER_CHANNEL,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if args.inspect:
            run_inspect(page)
            ctx.close()
            return

        ensure_logged_in(page)
        if args.dry_run:
            log("MODALITÀ DRY-RUN: non verrà inviato nulla.")
        if WHITELIST:
            log(f"Whitelist attiva: {WHITELIST}")
        else:
            log("Nessuna whitelist: nessuna conversazione verrà elaborata.")
        if TELEGRAM_LINK:
            log(f"Imbuto attivo verso: {TELEGRAM_LINK}")
        else:
            log("⚠️  TELEGRAM_LINK vuoto: l'AI chatterà ma non avrà un link dove mandarli.")
        if MAX_PER_DAY:
            log(f"Tetto giornaliero: {MAX_PER_DAY} messaggi.")
        if ACTIVE_START != 0 or ACTIVE_END < 24:
            log(f"Fascia oraria attiva: {ACTIVE_START:02d}:00–{ACTIVE_END:02d}:00.")
        if WAVE_SIZE > 0:
            log(f"Ondate: {WAVE_SIZE} amici ogni {WAVE_MINUTES} min.")

        previewed = {}
        try:
            while True:
                if not within_active_hours():
                    log(f"Fuori fascia oraria ({ACTIVE_START:02d}–{ACTIVE_END:02d}). Aspetto.")
                    if args.once:
                        break
                    time.sleep(min(900, POLL_INTERVAL * 5) + random.uniform(0, 60))
                    continue

                rt = load_runtime()
                if daily_cap_reached(rt):
                    log(f"Tetto giornaliero ({MAX_PER_DAY}) già raggiunto. Aspetto domani.")
                    if args.once:
                        break
                    time.sleep(min(1800, POLL_INTERVAL * 10) + random.uniform(0, 120))
                    continue

                n = process_cycle(page, args.dry_run, friends, rt, previewed)
                done = rt.get("sent", 0)
                cap = MAX_PER_DAY or "∞"
                log(f"Ciclo finito ({n} risposte, {done}/{cap} oggi). Riprovo tra {POLL_INTERVAL}s.")
                if args.once:
                    break
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log("Interrotto dall'utente. Chiudo.")
        finally:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
