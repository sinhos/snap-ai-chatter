# Snapchat AI — reference prototype

Historical Python prototype for a chatting agency: read Snapchat Web conversations,
generate short replies with Gemini, and track invitations to a Telegram community.
Telegram is only a destination link; this project contains no Telegram bot or agency dashboard.

**Status: prototype, not production-ready.** Preserved for reference. Offline tests
cover local logic; Snapchat selectors, authentication, delivery and live Gemini
responses have not been verified. This public repository contains an anonymized
reference snapshot with maintenance fixes. Personal prompt details, private contact
information and original commit metadata are excluded. API credentials and the
Telegram destination are configured locally; the example configuration leaves them empty.

## What is included

- `snap_bot.py`: Playwright browser loop, contact filters, conversation memory,
  invitation tracking, daily limits and active hours.
- `gemini_client.py`: Gemini REST client using `requests`.
- `persona.txt` and `funnel.txt`: anonymized prompt examples, not a finished agency configuration.
- `tests/`: offline regression tests. No real accounts, messages or API calls.

## Setup

Python 3.10 or newer is required. From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

Set `GEMINI_API_KEY`, `GEMINI_MODEL`, `WHITELIST` and optionally `TELEGRAM_LINK`.
Choose a model currently available to your account in [Google AI Studio](https://aistudio.google.com/).
There is deliberately no model default: model lifetimes, pricing and quotas change.
See Google's [model lifecycle documentation](https://ai.google.dev/gemini-api/docs/deprecations).
Review the historical persona and funnel prompts before any real use.

## Commands

```sh
# Inspect Snapchat's accessibility tree and selector counts; manual login required.
python snap_bot.py --inspect

# Default: preview replies without sending or changing conversation state/counters.
python snap_bot.py --once
python snap_bot.py --dry-run

# Explicitly enable real sending to the configured whitelist.
python snap_bot.py --send --once

# Optional live Gemini-only demo: consumes API quota.
python gemini_client.py

# Offline verification; browser installation and API keys are not needed.
python -m unittest discover -s tests -v
```

Dry-run still opens chats (which may mark them as read), sends their text to Gemini,
consumes API quota, and writes local logs. It does **not** send Snapchat messages,
record drafts as sent, increment invitation counts, or consume the local daily send
allowance. Duplicate previews are suppressed in memory for the current process only.

## Configuration

| Variable | Meaning |
| --- | --- |
| `GEMINI_API_KEY` | Required API key, kept in ignored `.env` |
| `GEMINI_MODEL` | Required model ID supported by your account |
| `TELEGRAM_LINK` | Optional invitation destination |
| `WHITELIST` | Comma-separated exact display names, case-insensitive; empty processes nobody |
| `BLACKLIST` | Exact names to exclude, overriding the whitelist |
| `POLL_INTERVAL_SECONDS` | Positive interval between cycles |
| `MAX_REPLIES_PER_CYCLE` | Positive cap on replies/previews per cycle |
| `REPLY_DELAY_MIN/MAX` | Nonnegative delay bounds between real sends |
| `MAX_MESSAGES_PER_DAY` | Local daily send cap; `0` disables it |
| `ACTIVE_HOURS_START/END` | Local-machine hours; `0`/`24` means all day; overnight windows supported |
| `WAVE_SIZE` / `WAVE_MINUTES` | Rotating whitelist subset; size `0` disables rotation; minutes must be positive |
| `BROWSER_CHANNEL` | Empty for Playwright Chromium; `chrome` for installed Chrome |

See `.env.example` for sample values. Daily limits and active hours apply to previews
too, but previews do not increase the persisted send count.

## Local data

Git ignores `.env`, `.chrome-profile/` (authenticated browser session), `friends.json`
(conversation memory), `runtime.json` (send count), `conversations.log`, and
`inspect_dump.*` (page content). These can contain private messages and account data.
Never upload them. Inspection now writes `inspect_dump.yaml` through
[Playwright's ARIA snapshot API](https://playwright.dev/python/docs/api/class-locator#locator-aria-snapshot).

State files are replaced atomically. Invalid state stops processing instead of
silently forgetting history or resetting limits. Back up and repair invalid files
before restarting. Run only one bot process per directory; there is no file locking.

## Known limitations

- Snapchat selectors are heuristic placeholders and must be checked with `--inspect`.
  Display names are not stable account IDs; duplicate names can collide, and the
  conversation list can reorder during a cycle.
- The parser does not reliably distinguish incoming and outgoing bubbles. Duplicate
  detection uses the final visible line, so repeated identical messages can be skipped.
- Pressing Enter is treated as a send attempt, not a server-confirmed delivery.
  A crash between sending and persisting state can lead to duplicate replies.
- Invitation count is tracked locally; prompt instructions are not an enforced maximum.
- No image/snap handling, queue, operator handoff, multi-account support or Telegram integration.
- Gemini errors and incomplete replies are skipped. The original short output-token
  budget may need tuning for the chosen model, especially models using thinking tokens.
- Browser automation may conflict with Snapchat's rules or fail account checks;
  this repository does not promise compatibility or account safety. Use only with
  authorized accounts and conversations, and review current platform requirements.

## Maintenance fixes

Dry-run state isolation, explicit sending, exact whitelist matching, atomic state
writes, configuration validation, updated inspection, Unicode input, and Gemini
error handling that avoids logging API keys or raw responses. The dependencies
remain Python-only; no application framework or additional runtime package was added.
