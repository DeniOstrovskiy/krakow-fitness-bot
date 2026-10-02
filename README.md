# Krakow Schedule Bot (read-only)

Telegram bot that reads the public schedule page and returns this week's slots for a given training name.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

If you plan to use Playwright (`USE_PLAYWRIGHT=1`), also run:

```bash
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
- `USE_PLAYWRIGHT=1` (enable JS rendering if needed)
- `PLAYWRIGHT_SEEK_WEEK=1` (auto-switch to current week)
- `PLAYWRIGHT_MAX_STEPS=12` (max week navigation clicks)

3. Run the bot:

```bash
python bot.py
```

## Render (free Web Service)

Render free tier works for Web Services. Use webhook mode:

1. Create a Web Service from this repo.
2. Build Command:
   - `pip install -r requirements.txt`
3. Start Command:
   - `python3 bot.py`
4. Environment variables:
   - `BOT_TOKEN`
   - `SCHEDULE_URLS`
   - `CLUB_NAMES`
   - `TIMEZONE=Europe/Warsaw`
   - `USE_PLAYWRIGHT=0`
   - Optional: `WEBHOOK_PATH=/telegram`
   - Optional: `WEBHOOK_URL=https://your-service.onrender.com` (base URL only)

Notes:
- Render sets `RENDER_EXTERNAL_URL` automatically for Web Services; the bot will use it if `WEBHOOK_URL` is not set.
- Free services sleep on inactivity; first reply can be delayed.

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

---

# Автозапись на занятия (бесплатный деплой)

Вход на zdrofit.pl защищён reCAPTCHA, поэтому бот не вводит пароль: вы входите сами в браузере и присылаете боту cookie командой `/cookie` (раз в несколько недель, когда сессия истечёт).

## Команды бота
- `/watch клуб | занятие | дни | время | тренер` — например `/watch kazimierz | bodypump | пн,ср | 18:00-20:00 | Buczek`
- `/list`, `/remove N`, `/pause`, `/resume`, `/status`
- `/cookie <строка Cookie из браузера>` — сообщение сразу удаляется
- `/myid` — ваш Telegram id

Отмену записи бот не делает: отменяйте на сайте.

## Запуск на Render (бесплатно)
1. **Upstash**: создайте бесплатную Redis-базу и скопируйте REST URL и REST Token.
2. **Render** → Web Service из этого репозитория:
   - Build: `pip install -r requirements.txt`
   - Start: `python3 run.py`
   - Переменные: `BOT_TOKEN`, `TIMEZONE=Europe/Warsaw`, `USE_PLAYWRIGHT=0`, `SCHEDULE_URLS`, `CLUB_NAMES` (как раньше), плюс новые: `ALLOWED_USER_ID`, `TICK_SECRET`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`.
   - Сначала запустите без `ALLOWED_USER_ID`, напишите боту `/myid`, вставьте число в переменную и перезапустите.
3. **cron-job.org**: создайте задание GET `https://<ваш-сервис>.onrender.com/tick?key=<TICK_SECRET>` с интервалом 1 минута (если бесплатный тариф не даёт, то 2–5 минут). Оно не даёт сервису заснуть и запускает проверку расписания.
4. В Telegram: `/cookie ...`, затем `/watch ...` для каждого занятия, затем `/status`.

## Как достать cookie
Войдите на zdrofit.pl в Chrome → F12 → Network → обновите страницу → первый запрос → Request Headers → значение `Cookie` целиком.

## Локальный запуск
`python run.py` без `WEBHOOK_URL`: бот работает через polling и проверяет расписание раз в минуту. Состояние лежит в `data.json`.

## Первый боевой запуск
Добавьте одно занятие, которое сейчас открыто для записи, и проверьте результат на сайте. `/status` покажет ответ сайта на последнюю попытку записи. Если успех определяется неверно, пришлите этот текст.

## Кнопки «Записаться» в поиске
Отправьте боту название занятия (например `bodypump`) или `trainer: Имя Фамилия`. Бот пришлёт каждое занятие из ближайших 7 дней отдельным сообщением с кнопкой «✅ Записаться» (до `MAX_CARDS`, по умолчанию 12; кнопки видит только владелец из `ALLOWED_USER_ID`, остальные получают обычный список). Нажатие работает так:
- запись открыта и есть места: бот записывает сразу;
- запись ещё не открыта или мест нет: занятие ставится на автозапись, бот запишет, как только станет возможно (`/list` покажет очередь, `/remove N` уберёт).

---

# Деплой на Render (Docker, бесплатно)

1. **Upstash**: бесплатная Redis-база, скопируйте REST URL и REST Token.
2. **GitHub**: `git add -A && git commit -m "..." && git push`.
3. **Render**: New → Blueprint → этот репозиторий (читается `render.yaml`) или New → Web Service → Docker. Переменные: `BOT_TOKEN`, `ALLOWED_USER_ID`, `SCHEDULE_URLS`, `CLUB_NAMES`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`, `TICK_SECRET` (любая длинная случайная строка), `TIMEZONE=Europe/Warsaw`, `USE_PLAYWRIGHT=0`. Health Check Path: `/health`.
4. **cron-job.org**: GET `https://<ваш-сервис>.onrender.com/tick?key=<TICK_SECRET>` каждую минуту (не даёт сервису заснуть и запускает проверку расписания).
5. Остановите локального бота и в Telegram заново отправьте `/cookie`, затем `/status`.
