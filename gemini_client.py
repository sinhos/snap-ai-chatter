"""Wrapper minimale sulla REST API di Gemini (free tier).

Usa solo `requests` per non dipendere dagli SDK Google, che cambiano spesso.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL = os.getenv("GEMINI_MODEL", "").strip()
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiError(RuntimeError):
    pass


def validate_config() -> None:
    if not API_KEY or API_KEY == "incolla_qui_la_tua_chiave":
        raise GeminiError("GEMINI_API_KEY mancante: configura .env.")
    if not MODEL:
        raise GeminiError("GEMINI_MODEL mancante: scegli un modello disponibile in Google AI Studio.")


def generate_reply(
    persona: str,
    funnel: str,
    memory_text: str,
    friend_name: str,
    stage: str,
    already_pitched: bool,
    telegram_link: str,
) -> str:
    """Genera il prossimo messaggio nello stile configurato, consapevole dell'imbuto.

    - `persona`  : stile/voce (persona.txt)
    - `funnel`   : strategia per portarli su Telegram (funnel.txt)
    - `memory_text` : storico con quell'amico (la "memoria" di ogni chat)
    - `stage`    : nuovo | rapport | invitato | entrato
    - `already_pitched` : True se il link Telegram è già stato mandato
    """
    validate_config()

    funnel_filled = (
        funnel.replace("{telegram_link}", telegram_link or "(nessun link impostato)")
        .replace("{already_pitched}", "sì" if already_pitched else "no")
    )
    system_prompt = f"{persona}\n\n--- OBIETTIVO E STRATEGIA ---\n{funnel_filled}"

    user_prompt = (
        f"Stai chattando con {friend_name}.\n"
        f"Stato nell'imbuto: {stage}. Link già inviato: "
        f"{'sì' if already_pitched else 'no'}.\n\n"
        f"Memoria della vostra conversazione (io = assistente, loro = {friend_name}):\n"
        f"{memory_text or '(nessuno scambio precedente)'}\n\n"
        "Scrivi SOLO il prossimo messaggio, seguendo la persona configurata. "
        "Resta naturale: invita su Telegram solo se il momento è giusto e mai due "
        "volte di fila."
    )

    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.9, "topP": 0.95, "maxOutputTokens": 120},
    }

    url = ENDPOINT.format(model=MODEL)
    try:
        resp = requests.post(url, headers={"x-goog-api-key": API_KEY}, json=body, timeout=30)
    except requests.RequestException:
        raise GeminiError("Errore di rete verso Gemini; riprova più tardi.") from None

    if resp.status_code != 200:
        raise GeminiError(f"Gemini HTTP {resp.status_code}: controlla modello, chiave e quota.")

    try:
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        raise GeminiError("Gemini ha restituito una risposta JSON non valida.") from None
    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError("Nessuna risposta da Gemini: richiesta bloccata o senza candidati.")

    if candidates[0].get("finishReason") != "STOP":
        raise GeminiError("Risposta Gemini incompleta o bloccata: nessun messaggio da inviare.")

    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise GeminiError("Gemini ha risposto vuoto.")

    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        text = text[1:-1].strip()
    if not text:
        raise GeminiError("Gemini ha risposto vuoto.")
    return text


if __name__ == "__main__":
    base = Path(__file__).parent
    demo = generate_reply(
        persona=(base / "persona.txt").read_text(encoding="utf-8"),
        funnel=(base / "funnel.txt").read_text(encoding="utf-8"),
        memory_text="loro: ueh come va\nio: tutto bene te?\nloro: si dai tranqui",
        friend_name="Contatto demo",
        stage="rapport",
        already_pitched=False,
        telegram_link="",
    )
    print("Risposta di prova:", demo)
