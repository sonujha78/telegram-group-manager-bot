# telegram-group-manager-bot

Free, always-on Telegram group management bot: welcome/rules, ban/mute/warn,
anti-spam, anti-link, filters & notes. Python + python-telegram-bot, runs 24/7
on a free cloud VM.

## Setup
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env   # put your BotFather token in .env
    python bot.py
