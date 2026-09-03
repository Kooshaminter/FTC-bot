# Smilepass Telegram Bot

This repository is the Telegram/server version of the existing `smilepass_app.py` desktop workflow.

## Required environment variables

Set these in Deployka (or your server's secret/environment settings):

- `TELEGRAM_BOT_TOKEN` — token from @BotFather
- `SMILEPASS_TOKEN` — your existing Smilepass API token

Do not commit either token to GitHub.

## Workflow

1. Choose one of the six clinics.
2. Enter patient name.
3. Confirm the patient or choose from multiple matches.
4. If there are multiple insurance policies, choose the correct one.
5. Paste the `DATA = {...}` block.
6. The bot edits one Telegram progress message as it works:
   - Checking token
   - Finding patient
   - Getting insurance policy
   - Creating breakdown
   - Updating plan limits
   - Updating procedures
   - Final success/failure
7. The implemented workflow stops after procedures are saved. It does NOT automatically call Smilepass `refresh_policy_coverage` or `mark_verified`.

## Clinics

- 81 — Smile Centre Mapleridge
- 87 — Smile Well Dental - Langley
- 11 — Parkwoods Dental
- 88 — Clarence Street Dental
- 89 — Root Cause Dental Group
- 83 — Rethink Dentistry

## Run locally

```bash
pip install -r requirements.txt
set TELEGRAM_BOT_TOKEN=YOUR_BOTFATHER_TOKEN
set SMILEPASS_TOKEN=YOUR_SMILEPASS_TOKEN
python main.py
```

On Linux/macOS use `export` instead of `set`.

## Important

The original desktop app contains Windows/Tkinter UI and local history functionality. Those are intentionally not required by this server bot. The Smilepass API payloads and endpoints used for the selected workflow are carried over from the uploaded source code.
