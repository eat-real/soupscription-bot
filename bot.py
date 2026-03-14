#!/usr/bin/env python3
"""
SoupScription Telegram Bot
Доставка домашней еды · Пн, Ср, Пт
"""
import os, json, logging, requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(
    format="%(asctime)s · %(name)s · %(levelname)s · %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════
#  НАСТРОЙКИ (берутся из переменных окружения)
# ══════════════════════════════════════════
BOT_TOKEN      = os.environ["BOT_TOKEN"]
ADMIN_ID       = int(os.environ.get("ADMIN_CHAT_ID", "0"))
SHEETS_ID      = os.environ["SHEETS_ID"]
SHEETS_API_KEY = os.environ["SHEETS_API_KEY"]
DELIVERY_FEE   = 5
FREE_THRESHOLD = 50

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

DAY_NAMES  = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DELIV_DOWS = {0, 2, 4}   # Mon=0, Wed=2, Fri=4

SLOTS = [
    ("breakfast", "☀️ Завтрак"),
    ("lunch",     "🌤 Обед"),
    ("dinner",    "🌙 Ужин"),
]

# ══════════════════════════════════════════
#  СОСТОЯНИЯ ДИАЛОГА
# ══════════════════════════════════════════
WEEK_VIEW, DAY_VIEW, DISH_VIEW, CONFIRM, ADDRESS = range(5)
AUTO_GOAL, AUTO_DAYS, AUTO_CONFIRM = range(5, 8)

# ══════════════════════════════════════════
#  GOOGLE SHEETS (через API ключ)
# ══════════════════════════════════════════
SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

def sheets_get(range_name: str) -> list:
    url = f"{SHEETS_BASE}/{SHEETS_ID}/values/{range_name}"
    r = requests.get(url, params={"key": SHEETS_API_KEY})
    r.raise_for_status()
    return r.json().get("values", [])

def sheets_append(values: list):
    url = f"{SHEETS_BASE}/{SHEETS_ID}/values/orders!A:I:append"
    params = {"key": SHEETS_API_KEY, "valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}
    requests.post(url, params=params, json={"values": [values]})

def sheets_update_cell(row: int, col: int, value):
    col_letter = chr(64 + col)
    url = f"{SHEETS_BASE}/{SHEETS_ID}/values/menu!{col_letter}{row}"
    params = {"key": SHEETS_API_KEY, "valueInputOption": "USER_ENTERED"}
    requests.put(url, params=params, json={"values": [[value]]})

def fetch_menu() -> list:
    rows = sheets_get("menu!A:P")
    if not rows or len(rows) < 7:
        return []
    # Заголовки в строке 6 (индекс 5)
    headers = [h.strip().lower().replace(" ", "_").replace("€","").strip("_") for h in rows[5]]
    result = []
    for row in rows[6:]:
        if not row or not row[0]:
            continue
        if len(row) < len(headers):
            row += [""] * (len(headers) - len(row))
        d = dict(zip(headers, row))
        # Поддержка разных названий колонок
        name  = str(d.get("name_ru") or d.get("name") or "")
        cat   = str(d.get("category") or d.get("cat") or "")
        price = float(str(d.get("price") or d.get("price_") or 0).replace(",",".") or 0)
        portions = int(str(d.get("portions") or 0).replace(",",".").split(".")[0] or 0)
        active = str(d.get("active", "1")).upper()
        if active in ("0", "FALSE", "НЕТ") or portions <= 0:
            continue
        result.append({
            "id":    str(d.get("id", "")),
            "name":  name,
            "cat":   cat,
            "kcal":  int(str(d.get("kcal", 0) or 0).split(".")[0] or 0),
            "p":     int(str(d.get("protein_g") or d.get("p", 0) or 0).split(".")[0] or 0),
            "f":     int(str(d.get("fat_g") or d.get("f", 0) or 0).split(".")[0] or 0),
            "c":     int(str(d.get("carbs_g") or d.get("c", 0) or 0).split(".")[0] or 0),
            "price": price,
            "fresh": str(d.get("fresh", "0")) in ("1", "TRUE", "ДА"),
        })
    return result

def fetch_last_order(user_id: int) -> dict | None:
    try:
        rows = sheets_get("orders!A:I")
        if len(rows) < 2: return None
        headers = [h.strip().lower() for h in rows[0]]
        uid_col  = headers.index("user_id")
        plan_col = headers.index("plan_json")
        user_rows = [r for r in rows[1:]
                     if len(r) > uid_col and str(r[uid_col]) == str(user_id)
                     and len(r) > plan_col and r[plan_col]]
        if not user_rows: return None
        return json.loads(user_rows[-1][plan_col])
    except Exception as e:
        logger.error(f"fetch_last_order: {e}")
        return None

def decrement_inventory(dish_id_counts: dict):
    rows = sheets_get("menu!A:P")
    if not rows or len(rows) < 7: return
    headers  = [h.strip().lower().replace(" ","_").replace("€","").strip("_") for h in rows[5]]
    try:
        port_col = next(i+1 for i,h in enumerate(headers) if "portion" in h)
        id_col   = next(i+1 for i,h in enumerate(headers) if h == "id")
    except StopIteration:
        return
    for i, row in enumerate(rows[6:], start=7):
        if not row or len(row) < id_col: continue
        did = str(row[id_col-1])
        if did in dish_id_counts:
            current = int(str(row[port_col-1] or 0).split(".")[0] or 0)
            sheets_update_cell(i, port_col, max(0, current - dish_id_counts[did]))

def save_order(user, summary_text: str, total: float, address: str, plan: dict = None):
    sheets_append([
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        str(user.id), user.username or "—", user.first_name or "—",
        summary_text, f"{total:.2f}€", address, "новый",
        json.dumps(plan or {}, ensure_ascii=False),
    ])

def auto_fill_plan(menu: list, week: list, days: int, kcal_target: int) -> dict:
    """
    Автоматически заполняет план на N дней (Пн–Пт или Пн–Вс).
    Старается попасть в kcal_target ± 200 в день.
    """
    import random
    plan = {}
    breakfasts = [d for d in menu if d["cat"] == "breakfast" and not d.get("fresh")]
    soups      = [d for d in menu if d["cat"] in ("soup", "special") and not d.get("fresh")]
    mains      = [d for d in menu if d["cat"] in ("main", "special") and not d.get("fresh")]

    # Перемешиваем чтобы не повторяться
    random.shuffle(breakfasts)
    random.shuffle(soups)
    random.shuffle(mains)

    count = 0
    for day in week:
        if count >= days:
            break
        dow = day["dow"]
        if dow in (5, 6):   # Сб и Вс пропускаем (нет доставки для обеда/ужина)
            continue
        if day["blocked"] == {"lunch", "dinner"}:
            continue

        day_plan = {}
        bi, si, mi = count % len(breakfasts), count % len(soups), count % len(mains)

        # Завтрак (если не заблокирован)
        if "breakfast" not in day["blocked"] and breakfasts:
            day_plan["breakfast"] = _dish_entry(breakfasts[bi])

        # Обед — суп
        if soups:
            day_plan["lunch"] = _dish_entry(soups[si])

        # Ужин — горячее
        if mains:
            day_plan["dinner"] = _dish_entry(mains[mi])

        plan[day["date"]] = day_plan
        count += 1

    return plan

def _dish_entry(d: dict) -> dict:
    return {
        "id":    str(d["id"]),
        "name":  str(d["name"]),
        "price": float(d["price"]),
        "kcal":  int(d.get("kcal", 0)),
        "p":     int(d.get("p", 0)),
        "f":     int(d.get("f", 0)),
        "c":     int(d.get("c", 0)),
    }

# ══════════════════════════════════════════
#  ПОСТРОЕНИЕ НЕДЕЛИ
# ══════════════════════════════════════════
def build_week() -> list:
    today = datetime.now()
    days_ahead = (0 - today.weekday()) % 7 or 7   # следующий понедельник
    monday = today + timedelta(days=days_ahead)
    week = []
    for i in range(7):
        d = monday + timedelta(days=i)
        dow = d.weekday()
        blocked = set()
        if dow == 0: blocked.add("breakfast")          # Пн завтрак заблокирован
        if dow == 6: blocked |= {"lunch", "dinner"}    # Вс обед+ужин заблокированы
        week.append({
            "date":        d.strftime("%Y-%m-%d"),
            "display":     d.strftime("%d.%m"),
            "dow":         dow,
            "name":        DAY_NAMES[dow],
            "is_delivery": dow in DELIV_DOWS,
            "fresh_ok":    dow in DELIV_DOWS,
            "blocked":     blocked,
        })
    return week

# ══════════════════════════════════════════
#  РАСЧЁТ ДОСТАВКИ ПО БЛОКАМ
# ══════════════════════════════════════════
def slot_block(dow: int, slot_key: str) -> int:
    """Определяет блок доставки для конкретного слота"""
    if slot_key == "breakfast":
        if dow == 2: return 0   # Ср завтрак → Пн блок
        if dow == 4: return 1   # Пт завтрак → Ср блок
        if dow == 6: return 2   # Вс завтрак → Пт блок
    if dow in (0, 1): return 0  # Пн-Вт → Пн блок
    if dow in (2, 3): return 1  # Ср-Чт → Ср блок
    return 2                    # Пт-Сб → Пт блок

def calc_delivery(plan: dict, week: list) -> tuple:
    """Возвращает (итого доставка, {блок: сумма еды})"""
    dow_map = {d["date"]: d["dow"] for d in week}
    block_totals = {0: 0.0, 1: 0.0, 2: 0.0}
    for date, slots in plan.items():
        dow = dow_map.get(date, 0)
        for sk, dish in slots.items():
            if dish:
                bi = slot_block(dow, sk)
                block_totals[bi] += float(dish["price"])
    fee = sum(DELIVERY_FEE for bt in block_totals.values()
              if 0 < bt < FREE_THRESHOLD)
    return fee, block_totals

# ══════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ СВОДКИ
# ══════════════════════════════════════════
def format_summary(plan: dict, week: list, user_name: str) -> tuple:
    """Возвращает (текст, итого с доставкой)"""
    food_total = 0.0
    day_blocks = []

    for day in week:
        day_plan = plan.get(day["date"], {})
        filled = {sk: d for sk, d in day_plan.items() if d}
        if not filled:
            continue
        kcal = sum(d["kcal"]  for d in filled.values())
        p    = sum(d["p"]     for d in filled.values())
        f    = sum(d["f"]     for d in filled.values())
        c    = sum(d["c"]     for d in filled.values())
        price= sum(float(d["price"]) for d in filled.values())
        food_total += price

        icon = "🚚" if day["is_delivery"] else "📅"
        lines = [f"{icon} *{day['name']} {day['display']}*"]
        for slot_key, slot_label in SLOTS:
            if slot_key in filled:
                d = filled[slot_key]
                lines.append(f"  {slot_label}: {d['name']} — {d['price']}€")
        lines.append(f"  ┄ {kcal} ккал · Б{p}г Ж{f}г У{c}г · {price:.2f}€")
        day_blocks.append("\n".join(lines))

    if not day_blocks:
        return "Корзина пуста 🤷", 0.0

    fee, block_totals = calc_delivery(plan, week)
    block_names = ["Пн доставка", "Ср доставка", "Пт доставка"]

    text = f"🥣 *SoupScription*\n👤 {user_name}\n\n"
    text += "\n\n".join(day_blocks)
    text += f"\n\n🍽 Еда итого: {food_total:.2f}€"

    for bi, bt in block_totals.items():
        if bt > 0:
            if bt >= FREE_THRESHOLD:
                text += f"\n🚚 {block_names[bi]}: бесплатно ✅"
            else:
                need = FREE_THRESHOLD - bt
                text += f"\n🚚 {block_names[bi]}: +{DELIVERY_FEE}€ (до бесплатной ещё {need:.0f}€)"

    text += f"\n\n💰 *Итого: {food_total + fee:.2f}€*"
    return text, food_total + fee

# ══════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════
def week_keyboard(week: list, plan: dict) -> InlineKeyboardMarkup:
    rows = []
    for day in week:
        day_plan = plan.get(day["date"], {})
        avail    = [sk for sk, _ in SLOTS if sk not in day["blocked"]]
        filled   = [sk for sk in avail if day_plan.get(sk)]
        tick = f" ✓{len(filled)}/{len(avail)}" if filled else ""
        deliv = "🚚 " if day["is_delivery"] else ""
        label = f"{deliv}{day['name']} {day['display']}{tick}"
        rows.append([InlineKeyboardButton(label, callback_data=f"day_{day['date']}")])
    rows.append([InlineKeyboardButton("✅ Готово → Сводка", callback_data="done")])
    rows.append([InlineKeyboardButton("❌ Отмена",           callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

def day_keyboard(day: dict, plan: dict) -> InlineKeyboardMarkup:
    rows = []
    day_plan = plan.get(day["date"], {})
    for slot_key, slot_label in SLOTS:
        if slot_key in day["blocked"]:
            continue
        dish = day_plan.get(slot_key)
        if dish:
            label = f"{slot_label}: {dish['name'][:22]} ✓"
        else:
            label = f"{slot_label}: выбрать блюдо…"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"slot_{day['date']}_{slot_key}"
        )])
    rows.append([InlineKeyboardButton("← Назад к неделе", callback_data="back_week")])
    return InlineKeyboardMarkup(rows)

def dishes_keyboard(dishes: list, date: str, slot_key: str,
                    current_id=None) -> InlineKeyboardMarkup:
    rows = []
    for d in dishes:
        fresh = "⚡ " if d.get("fresh") else ""
        check = "✓ " if str(d["id"]) == str(current_id) else ""
        label = f"{check}{fresh}{d['name']} | {d['kcal']} ккал | {d['price']}€"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"dish_{date}_{slot_key}_{d['id']}"
        )])
    rows.append([InlineKeyboardButton("⬜ Пропустить слот", callback_data=f"skip_{date}_{slot_key}")])
    rows.append([InlineKeyboardButton("← Назад к дню",     callback_data=f"day_{date}")])
    return InlineKeyboardMarkup(rows)

def get_dishes_for_slot(menu: list, slot_key: str, fresh_ok: bool) -> list:
    cats = ["breakfast", "special"] if slot_key == "breakfast" else ["soup", "main", "special"]
    return [
        d for d in menu
        if d.get("cat") in cats and not (d.get("fresh") and not fresh_ok)
    ]

# ══════════════════════════════════════════
#  ОБРАБОТЧИКИ
# ══════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот *SoupScription* 🥣\n\n"
        "Домашняя еда с доставкой в Пн, Ср, Пт.\n\n"
        "*/order* — составить заказ вручную\n"
        "*/auto* — ⚡ авто-заказ на 5 дней\n"
        "*/repeat* — 🔄 повторить прошлый заказ\n"
        "*/menu* — меню этой недели",
        parse_mode="Markdown"
    )

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        menu = fetch_menu()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не удалось загрузить меню: {e}")
        return

    cats = {
        "breakfast": "☀️ Завтраки",
        "soup":      "🍲 Супы",
        "main":      "🍖 Горячее",
        "special":   "⭐ Спешиалы недели",
    }
    text = "📋 *Меню недели*\n\n"
    for cat_key, cat_name in cats.items():
        items = [d for d in menu if d.get("cat") == cat_key]
        if not items:
            continue
        text += f"*{cat_name}*\n"
        for d in items:
            fresh = " ⚡свежее" if d.get("fresh") else ""
            text += (f"• {d['name']}{fresh} — {d['price']}€\n"
                     f"  {d['kcal']} ккал · Б{d['p']}г Ж{d['f']}г У{d['c']}г\n")
        text += "\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def order_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Загружаю меню…")
    try:
        menu = fetch_menu()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка загрузки меню: {e}")
        return ConversationHandler.END

    week = build_week()
    ctx.user_data.update({"week": week, "plan": {}, "menu": menu})

    await update.message.reply_text(
        "📅 *Заказ на следующую неделю*\n\n"
        "Нажми на день, выбери завтрак/обед/ужин.\n"
        "Можно заполнить любые дни — не обязательно все.\n\n"
        "🚚 = день доставки · ✓ = слот заполнен",
        parse_mode="Markdown",
        reply_markup=week_keyboard(week, {})
    )
    return WEEK_VIEW

# ── WEEK VIEW ────────────────────────────────────────────────────────────
async def week_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    week = ctx.user_data["week"]
    plan = ctx.user_data["plan"]

    if data == "cancel":
        await q.edit_message_text("Заказ отменён. /order — начать заново.")
        return ConversationHandler.END

    if data == "done":
        filled = any(d for dp in plan.values() for d in dp.values() if d)
        if not filled:
            await q.answer("Добавь хотя бы одно блюдо!", show_alert=True)
            return WEEK_VIEW
        user_name = q.from_user.first_name or "Клиент"
        summary, total = format_summary(plan, week, user_name)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить",  callback_data="confirm")],
            [InlineKeyboardButton("✏️ Изменить",     callback_data="edit")],
            [InlineKeyboardButton("❌ Отмена",        callback_data="cancel")],
        ])
        await q.edit_message_text(summary, parse_mode="Markdown", reply_markup=kb)
        return CONFIRM

    if data.startswith("day_"):
        date = data[4:]
        day = next((d for d in week if d["date"] == date), None)
        if not day:
            return WEEK_VIEW
        deliv_text = "🚚 *День доставки!* Свежие блюда доступны.\n\n" if day["is_delivery"] else ""
        await q.edit_message_text(
            f"{deliv_text}*{day['name']} {day['display']}* — выбери приём пищи:",
            parse_mode="Markdown",
            reply_markup=day_keyboard(day, plan)
        )
        return DAY_VIEW

    return WEEK_VIEW

# ── DAY VIEW ─────────────────────────────────────────────────────────────
async def day_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    week = ctx.user_data["week"]
    plan = ctx.user_data["plan"]
    menu = ctx.user_data["menu"]

    if data == "back_week":
        await q.edit_message_text(
            "📅 *Выбери день:*",
            parse_mode="Markdown",
            reply_markup=week_keyboard(week, plan)
        )
        return WEEK_VIEW

    if data.startswith("day_"):
        date = data[4:]
        day = next((d for d in week if d["date"] == date), None)
        if not day: return DAY_VIEW
        deliv_text = "🚚 *День доставки!*\n\n" if day["is_delivery"] else ""
        await q.edit_message_text(
            f"{deliv_text}*{day['name']} {day['display']}* — выбери приём пищи:",
            parse_mode="Markdown",
            reply_markup=day_keyboard(day, plan)
        )
        return DAY_VIEW

    if data.startswith("slot_"):
        # slot_{YYYY-MM-DD}_{slot_key}
        _, rest = data.split("slot_", 1)
        date, slot_key = rest[:10], rest[11:]
        day  = next((d for d in week if d["date"] == date), None)
        if not day: return DAY_VIEW

        dishes = get_dishes_for_slot(menu, slot_key, day["fresh_ok"])
        if not dishes:
            await q.answer("Нет доступных блюд для этого слота", show_alert=True)
            return DAY_VIEW

        current = plan.get(date, {}).get(slot_key)
        current_id = current["id"] if current else None
        slot_label = next((sl for sk, sl in SLOTS if sk == slot_key), slot_key)

        await q.edit_message_text(
            f"*{day['name']} {day['display']} · {slot_label}*\n\nВыбери блюдо:",
            parse_mode="Markdown",
            reply_markup=dishes_keyboard(dishes, date, slot_key, current_id)
        )
        return DISH_VIEW

    return DAY_VIEW

# ── DISH VIEW ─────────────────────────────────────────────────────────────
async def dish_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    week = ctx.user_data["week"]
    plan = ctx.user_data["plan"]
    menu = ctx.user_data["menu"]

    if data.startswith("day_"):
        date = data[4:]
        day  = next((d for d in week if d["date"] == date), None)
        if not day: return DISH_VIEW
        deliv_text = "🚚 *День доставки!*\n\n" if day["is_delivery"] else ""
        await q.edit_message_text(
            f"{deliv_text}*{day['name']} {day['display']}* — выбери приём пищи:",
            parse_mode="Markdown",
            reply_markup=day_keyboard(day, plan)
        )
        return DAY_VIEW

    if data.startswith("skip_"):
        # skip_{YYYY-MM-DD}_{slot_key}
        rest     = data[5:]
        date     = rest[:10]
        slot_key = rest[11:]
        if date in plan and slot_key in plan[date]:
            del plan[date][slot_key]
        day = next((d for d in week if d["date"] == date), None)
        if not day: return DISH_VIEW
        await q.edit_message_text(
            f"*{day['name']} {day['display']}* — слот пропущен:",
            parse_mode="Markdown",
            reply_markup=day_keyboard(day, plan)
        )
        return DAY_VIEW

    if data.startswith("dish_"):
        # dish_{YYYY-MM-DD}_{slot_key}_{dish_id}
        rest     = data[5:]
        date     = rest[:10]
        after    = rest[11:]              # slot_key_dishid
        slot_key, dish_id = after.rsplit("_", 1)

        dish = next((d for d in menu if str(d["id"]) == dish_id), None)
        if not dish:
            await q.answer("Блюдо недоступно", show_alert=True)
            return DISH_VIEW

        if date not in plan:
            plan[date] = {}
        plan[date][slot_key] = {
            "id":    str(dish["id"]),
            "name":  str(dish["name"]),
            "price": float(dish["price"]),
            "kcal":  int(dish.get("kcal", 0)),
            "p":     int(dish.get("p", 0)),
            "f":     int(dish.get("f", 0)),
            "c":     int(dish.get("c", 0)),
        }
        ctx.user_data["plan"] = plan

        await q.answer(f"✅ {dish['name']} добавлено!")
        day = next((d for d in week if d["date"] == date), None)
        if not day: return DAY_VIEW
        deliv_text = "🚚 *День доставки!*\n\n" if day["is_delivery"] else ""
        await q.edit_message_text(
            f"{deliv_text}*{day['name']} {day['display']}* — выбери приём пищи:",
            parse_mode="Markdown",
            reply_markup=day_keyboard(day, plan)
        )
        return DAY_VIEW

    return DISH_VIEW

# ── CONFIRM VIEW ──────────────────────────────────────────────────────────
async def confirm_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    week = ctx.user_data["week"]
    plan = ctx.user_data["plan"]

    if data == "cancel":
        await q.edit_message_text("Заказ отменён. /order — начать заново.")
        return ConversationHandler.END

    if data == "edit":
        await q.edit_message_text(
            "📅 *Изменяем заказ — выбери день:*",
            parse_mode="Markdown",
            reply_markup=week_keyboard(week, plan)
        )
        return WEEK_VIEW

    if data == "confirm":
        await q.edit_message_text(
            "📍 *Напиши адрес доставки:*\n\n"
            "Например: _Rua da Liberdade 45, 2 esq, Lisboa_",
            parse_mode="Markdown"
        )
        return ADDRESS

    return CONFIRM

# ── ADDRESS ───────────────────────────────────────────────────────────────
async def get_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    address = update.message.text
    week    = ctx.user_data["week"]
    plan    = ctx.user_data["plan"]
    user    = update.effective_user
    name    = user.first_name or "Клиент"

    summary, total = format_summary(plan, week, name)

    # Сохраняем в таблицу
    try:
        save_order(user, summary, total, address, plan)
        # Списываем порции
        counts: dict = {}
        for dp in plan.values():
            for dish in dp.values():
                if dish:
                    counts[dish["id"]] = counts.get(dish["id"], 0) + 1
        decrement_inventory(counts)
    except Exception as e:
        logger.error(f"Sheets error: {e}")

    # Уведомление повару
    if ADMIN_ID:
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"🔔 *Новый заказ!*\n\n{summary}\n\n📍 {address}\n\n"
                f"👤 @{user.username or name}  |  id: {user.id}",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Admin notify error: {e}")

    await update.message.reply_text(
        f"✅ *Заказ принят, {name}!*\n\n"
        f"Уже начинаем готовить 🥣\n\n"
        f"{summary}\n\n📍 {address}",
        parse_mode="Markdown"
    )
    ctx.user_data.clear()
    return ConversationHandler.END

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Заказ отменён. /order — начать заново.")
    return ConversationHandler.END

# ══════════════════════════════════════════
#  /auto — АВТО-ЗАКАЗ
# ══════════════════════════════════════════
async def cmd_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👩 Похудение (~1400 ккал/день)",  callback_data="goal_1400")],
        [InlineKeyboardButton("🧘 Поддержание (~2000 ккал/день)", callback_data="goal_2000")],
        [InlineKeyboardButton("💪 Набор массы (~2500 ккал/день)", callback_data="goal_2500")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])
    await update.message.reply_text(
        "⚡ *Авто-заказ*\n\nПодберу завтрак + обед + ужин на каждый день.\n\n"
        "Какая цель по калориям?",
        parse_mode="Markdown",
        reply_markup=kb
    )
    return AUTO_GOAL

async def auto_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Отменено. /auto — попробовать снова.")
        return ConversationHandler.END

    kcal = int(q.data.split("_")[1])
    ctx.user_data["auto_kcal"] = kcal

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("5 дней (Пн–Пт)",  callback_data="days_5")],
        [InlineKeyboardButton("7 дней (Пн–Вс)",  callback_data="days_7")],
        [InlineKeyboardButton("❌ Отмена",         callback_data="cancel")],
    ])
    await q.edit_message_text(
        f"Цель: *{kcal} ккал/день* ✅\n\nНа сколько дней?",
        parse_mode="Markdown",
        reply_markup=kb
    )
    return AUTO_DAYS

async def auto_days(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Отменено.")
        return ConversationHandler.END

    days_count = int(q.data.split("_")[1])
    kcal       = ctx.user_data.get("auto_kcal", 2000)

    await q.edit_message_text("⏳ Подбираю блюда…")

    try:
        menu = fetch_menu()
        week = build_week()
    except Exception as e:
        await q.edit_message_text(f"⚠️ Ошибка загрузки меню: {e}")
        return ConversationHandler.END

    plan = auto_fill_plan(menu, week, days_count, kcal)
    if not plan:
        await q.edit_message_text("⚠️ Не хватает блюд в меню этой недели.")
        return ConversationHandler.END

    ctx.user_data.update({"plan": plan, "week": week, "menu": menu})

    name = q.from_user.first_name or "Клиент"
    summary, total = format_summary(plan, week, name)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Оформить заказ",      callback_data="confirm")],
        [InlineKeyboardButton("✏️ Изменить вручную",    callback_data="edit")],
        [InlineKeyboardButton("🔀 Подобрать заново",    callback_data="reshuffle")],
        [InlineKeyboardButton("❌ Отмена",               callback_data="cancel")],
    ])
    await q.edit_message_text(
        f"⚡ *Авто-подборка готова!*\n\n{summary}",
        parse_mode="Markdown",
        reply_markup=kb
    )
    return AUTO_CONFIRM

async def auto_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    week = ctx.user_data["week"]
    plan = ctx.user_data["plan"]
    menu = ctx.user_data["menu"]

    if data == "cancel":
        await q.edit_message_text("Отменено. /auto — попробовать снова.")
        return ConversationHandler.END

    if data == "reshuffle":
        kcal  = ctx.user_data.get("auto_kcal", 2000)
        days  = len(plan)
        plan  = auto_fill_plan(menu, week, days, kcal)
        ctx.user_data["plan"] = plan
        name  = q.from_user.first_name or "Клиент"
        summary, total = format_summary(plan, week, name)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Оформить заказ",   callback_data="confirm")],
            [InlineKeyboardButton("✏️ Изменить вручную", callback_data="edit")],
            [InlineKeyboardButton("🔀 Подобрать заново", callback_data="reshuffle")],
            [InlineKeyboardButton("❌ Отмена",            callback_data="cancel")],
        ])
        await q.edit_message_text(
            f"🔀 *Новая подборка:*\n\n{summary}",
            parse_mode="Markdown", reply_markup=kb
        )
        return AUTO_CONFIRM

    if data == "edit":
        await q.edit_message_text(
            "✏️ *Редактируем — выбери день:*",
            parse_mode="Markdown",
            reply_markup=week_keyboard(week, plan)
        )
        return WEEK_VIEW

    if data == "confirm":
        await q.edit_message_text(
            "📍 *Напиши адрес доставки:*\n\n"
            "_Например: Rua da Liberdade 45, 2 esq, Lisboa_",
            parse_mode="Markdown"
        )
        return ADDRESS

    return AUTO_CONFIRM


# ══════════════════════════════════════════
#  /repeat — ПОВТОР ПРОШЛОГО ЗАКАЗА
# ══════════════════════════════════════════
async def cmd_repeat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Ищу твой последний заказ…")

    user_id = update.effective_user.id
    old_plan = fetch_last_order(user_id)

    if not old_plan:
        await update.message.reply_text(
            "😕 Предыдущих заказов не найдено.\n\n"
            "Попробуй */order* или */auto*",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Переносим план на следующую неделю
    try:
        menu = fetch_menu()
        week = build_week()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка загрузки меню: {e}")
        return ConversationHandler.END

    # Сдвигаем даты: берём слоты, ставим на те же дни новой недели
    new_plan = {}
    old_dates_sorted = sorted(old_plan.keys())
    new_dates_sorted = sorted(d["date"] for d in week)

    for i, old_date in enumerate(old_dates_sorted):
        if i >= len(new_dates_sorted):
            break
        new_date = new_dates_sorted[i]
        new_day  = next((d for d in week if d["date"] == new_date), None)
        if not new_day:
            continue

        new_day_plan = {}
        for slot_key, dish in old_plan[old_date].items():
            if not dish or slot_key in new_day["blocked"]:
                continue
            # Проверяем что блюдо ещё есть в меню
            dish_in_menu = next((m for m in menu if str(m["id"]) == str(dish["id"])), None)
            if dish_in_menu:
                new_day_plan[slot_key] = _dish_entry(dish_in_menu)
        if new_day_plan:
            new_plan[new_date] = new_day_plan

    if not new_plan:
        await update.message.reply_text(
            "😕 Блюда из прошлого заказа недоступны на этой неделе.\n\n"
            "Попробуй */auto* — подберу новые!",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    ctx.user_data.update({"plan": new_plan, "week": week, "menu": menu})
    name    = update.effective_user.first_name or "Клиент"
    summary, total = format_summary(new_plan, week, name)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Оформить заказ",      callback_data="confirm")],
        [InlineKeyboardButton("✏️ Изменить что-то",     callback_data="edit")],
        [InlineKeyboardButton("❌ Отмена",               callback_data="cancel")],
    ])
    await update.message.reply_text(
        f"🔄 *Повтор прошлого заказа:*\n\n{summary}\n\n"
        f"_Блюда перенесены на следующую неделю_",
        parse_mode="Markdown",
        reply_markup=kb
    )
    return CONFIRM



def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # ── Ручной заказ ──────────────────────────────────────────────────
    manual_conv = ConversationHandler(
        entry_points=[CommandHandler("order", order_start)],
        states={
            WEEK_VIEW: [CallbackQueryHandler(week_view)],
            DAY_VIEW:  [CallbackQueryHandler(day_view)],
            DISH_VIEW: [CallbackQueryHandler(dish_view)],
            CONFIRM:   [CallbackQueryHandler(confirm_view)],
            ADDRESS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_address)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    # ── Авто-заказ ────────────────────────────────────────────────────
    auto_conv = ConversationHandler(
        entry_points=[CommandHandler("auto", cmd_auto)],
        states={
            AUTO_GOAL:    [CallbackQueryHandler(auto_goal)],
            AUTO_DAYS:    [CallbackQueryHandler(auto_days)],
            AUTO_CONFIRM: [CallbackQueryHandler(auto_confirm)],
            # После "edit" переходит в ручной режим
            WEEK_VIEW:    [CallbackQueryHandler(week_view)],
            DAY_VIEW:     [CallbackQueryHandler(day_view)],
            DISH_VIEW:    [CallbackQueryHandler(dish_view)],
            CONFIRM:      [CallbackQueryHandler(confirm_view)],
            ADDRESS:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_address)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    # ── Повтор заказа ─────────────────────────────────────────────────
    repeat_conv = ConversationHandler(
        entry_points=[CommandHandler("repeat", cmd_repeat)],
        states={
            CONFIRM:  [CallbackQueryHandler(confirm_view)],
            WEEK_VIEW:[CallbackQueryHandler(week_view)],
            DAY_VIEW: [CallbackQueryHandler(day_view)],
            DISH_VIEW:[CallbackQueryHandler(dish_view)],
            ADDRESS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, get_address)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("menu",   cmd_menu))
    app.add_handler(manual_conv)
    app.add_handler(auto_conv)
    app.add_handler(repeat_conv)

    logger.info("SoupScription bot started ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
