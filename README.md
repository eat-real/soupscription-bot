# SoupScription Bot — Инструкция по запуску

## Что нужно сделать один раз (30–40 минут)

---

### ШАГ 1 — Создай бота в Telegram (5 минут)

1. Открой Telegram, найди @BotFather
2. Напиши `/newbot`
3. Придумай имя: `SoupScription`
4. Придумай username: `soupscription_bot` (должен быть свободным)
5. BotFather пришлёт **токен** — скопируй его, он нужен дальше

---

### ШАГ 2 — Узнай свой Telegram ID (2 минуты)

1. Найди бота @userinfobot
2. Напиши ему `/start`
3. Он пришлёт твой **ID** (число типа 123456789) — это ADMIN_CHAT_ID

---

### ШАГ 3 — Настрой Google Sheets (10 минут)

#### 3а. Создай таблицу
1. Открой `tasty_full_sheets.xlsx`, загрузи в Google Drive
2. Открой как Google Sheets
3. Скопируй ID таблицы из адресной строки:
   `https://docs.google.com/spreadsheets/d/`**ВОТ_ЭТО_ID**`/edit`

#### 3б. Структура листа "menu" (уже есть в шаблоне):
| id | name | cat | kcal | p | f | c | price | fresh | portions | active |
|----|------|-----|------|---|---|---|-------|-------|----------|--------|
| 1  | Овсянка | breakfast | 320 | 8 | 6 | 58 | 5 | 0 | 18 | 1 |

- `cat` = breakfast / soup / main / special
- `fresh` = 1 если только в день доставки, иначе 0
- `portions` = сколько порций на неделю (ты меняешь каждый понедельник)
- `active` = 1 показывать, 0 скрыть

#### 3в. Получи ключ сервисного аккаунта
1. Открой [console.cloud.google.com](https://console.cloud.google.com)
2. Создай новый проект (любое название)
3. Включи **Google Sheets API**: APIs & Services → Enable APIs → Google Sheets API
4. Включи **Google Drive API**: аналогично
5. Создай сервисный аккаунт: IAM & Admin → Service Accounts → Create
6. Роль: Editor
7. Ключи: Actions → Manage keys → Add key → JSON → скачай файл
8. **Открой скачанный JSON файл** — скопируй всё содержимое целиком
9. В Google Sheets: Настройки доступа → добавь email сервисного аккаунта (из JSON, поле `client_email`) как редактора

---

### ШАГ 4 — Задеплой на Railway (10 минут)

1. Зайди на [railway.app](https://railway.app) → Sign Up with Google
2. New Project → Deploy from GitHub repo
   - Если нет GitHub: New Project → Deploy from local directory → перетащи папку `soupscription-bot`
3. После загрузки → Variables (переменные окружения), добавь:

| Имя переменной | Значение |
|---------------|----------|
| `BOT_TOKEN` | токен от BotFather |
| `ADMIN_CHAT_ID` | твой Telegram ID |
| `SHEETS_ID` | ID таблицы Google |
| `GOOGLE_CREDS_JSON` | содержимое JSON файла целиком (всё в одну строку) |

4. Deploy → через 1–2 минуты бот живой!

---

### ШАГ 5 — Проверь (2 минуты)

Открой своего бота в Telegram:
- `/start` — приветствие
- `/menu` — список блюд из таблицы
- `/order` — составить заказ

---

## Как пользоваться каждую неделю

Каждый понедельник утром:
1. Открой Google Sheets → лист **menu**
2. Обнови колонку `portions` для каждого блюда (сколько порций приготовил)
3. Блюда с `portions = 0` автоматически пропадут из бота
4. Новые спешиалы добавляй снизу с `cat = special`

Заказы падают в лист **orders** и приходят тебе в Telegram.

---

## Если что-то не работает

- Бот не отвечает → проверь BOT_TOKEN в Railway
- Ошибка таблицы → проверь что email сервисного аккаунта добавлен в доступ к таблице
- Напиши мне в чат, разберёмся!
