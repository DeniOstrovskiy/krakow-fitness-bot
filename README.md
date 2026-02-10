# Krakow Schedule Bot (read-only)

Telegram bot that reads the public schedule page and returns this week's slots for a given training name.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
python -m playwright install
```

2. Create a `.env` file (copy from `.env.example`) and set:
- `BOT_TOKEN`
- `SCHEDULE_URLS` (one or many Krakow club schedule links)
- `CLUB_NAMES` (one or many names, same count as URLs or a single name)

Single club option:
- `SCHEDULE_URL` and `CLUB_NAME` also work if you prefer a single URL.

Optional:
- `EVENT_SELECTOR` if the parser does not detect events
- `MAX_RESULTS`
- `USE_PLAYWRIGHT=1` (recommended for these pages)
- `PLAYWRIGHT_SEEK_WEEK=1` (auto-switch to current week)
- `PLAYWRIGHT_MAX_STEPS=12` (max week navigation clicks)

3. Run the bot:

```bash
python bot.py
```

## How it works

- You send a training name like `yoga`.
- The bot fetches the schedule page and parses time/date/title.
- It returns slots for the current week (Monday to Sunday).
- For trainer search, use `trainer: Name Surname` or `/trainer Name Surname`.

## Troubleshooting

If no slots are found but the page is public:
1. Open the schedule page in a browser.
2. Inspect a single event element and copy its CSS selector.
3. Put it into `.env` as `EVENT_SELECTOR=...` or `EVENT_SELECTORS=...` for multiple clubs.

If the page loads the schedule via JavaScript, keep `USE_PLAYWRIGHT=1`.

You can also try a different Krakow club schedule URL if the first one is not correct.
