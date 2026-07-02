import os
import re
import asyncio
import gspread
import requests
from datetime import datetime
from collections import Counter
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command

# ── КОНФИГ ──
BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
# Список разрешённых Telegram ID (через запятую в ALLOWED_USERS,
# либо отдельными переменными ALLOWED_USER и ALLOWED_USER_2)
ALLOWED_USERS = set()
for _key in ("ALLOWED_USERS", "ALLOWED_USER", "ALLOWED_USER_2"):
    _val = os.getenv(_key, "")
    for _part in _val.split(","):
        _part = _part.strip()
        if _part.isdigit():
            ALLOWED_USERS.add(int(_part))
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL")  # cloudinary://api_key:api_secret@cloud_name

# ── GOOGLE SHEETS ──
def get_sheet():
    creds_data = {
        "type": "service_account",
        "project_id": os.getenv("GS_PROJECT_ID"),
        "private_key_id": os.getenv("GS_PRIVATE_KEY_ID"),
        "private_key": os.getenv("GS_PRIVATE_KEY").replace("\\n", "\n"),
        "client_email": os.getenv("GS_CLIENT_EMAIL"),
        "client_id": os.getenv("GS_CLIENT_ID"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": os.getenv("GS_CLIENT_CERT_URL"),
        "universe_domain": "googleapis.com"
    }
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)

def get_catalog():
    sh = get_sheet()
    return sh.worksheet("catalog")

def get_orders():
    sh = get_sheet()
    return sh.worksheet("orders")

# ── CLOUDINARY UPLOAD ──
def upload_to_cloudinary(file_bytes: bytes, public_id: str) -> str:
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)
    result = cloudinary.uploader.upload(
        file_bytes,
        public_id=f"zozulia/{public_id}",
        overwrite=True
    )
    return result["secure_url"]

# ── БОТ ──
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def is_allowed(message: Message) -> bool:
    return message.from_user.id in ALLOWED_USERS

HELP_TEXT = (
    "👟 Бот управления каталогом by Zozulia\n\n"
    "Команды:\n"
    "• <b>добавить Моника 1850 36 37 38</b> + фото\n"
    "• <b>продано Моника 37</b> — продажа по цене каталога\n"
    "• <b>продано Моника 37 1500</b> — продажа по своей цене (скидка)\n"
    "• <b>вернули Моника 37</b> — возврат (вернёт пару + сторно)\n"
    "• <b>размер Моника убрать 38</b> — скрыть без продажи\n"
    "• <b>размер Моника добавить 38</b>\n"
    "• <b>цена Моника 2000</b>\n"
    "• <b>себестоимость Моника 900</b>\n"
    "• <b>удалить Моника</b>\n"
    "• <b>остатки</b> — показать все размеры в наличии"
)

# Ожидание фото после команды добавить
pending_add = {}

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message):
        return
    await message.answer(HELP_TEXT, parse_mode="HTML")

# ── ОСТАТКИ ──
@dp.message(F.text.regexp(r"(?i)^\s*остатки\s*$"))
async def cmd_stock(message: Message):
    if not is_allowed(message): return

    ws = get_catalog()
    data = ws.get_all_records()

    models = {}
    for row in data:
        if str(row.get("active", "")).upper() == "TRUE":
            model = row["model"]
            size = str(row["article"]).split("-")[-1]
            if model not in models:
                models[model] = {"sizes": [], "price": row["price"]}
            models[model]["sizes"].append(size)

    if not models:
        await message.answer("📦 Каталог пуст")
        return

    text = "📦 <b>Остатки в наличии:</b>\n\n"
    for model, info in models.items():
        counts = Counter(info["sizes"])
        sizes_str = " ".join(
            f"{s}×{c}" if c > 1 else s
            for s, c in sorted(counts.items())
        )
        text += f"<b>{model}</b> — {info['price']} грн\n"
        text += f"Размеры: {sizes_str}\n\n"

    await message.answer(text, parse_mode="HTML")

# ── ДОБАВИТЬ модель ──
@dp.message(F.text.regexp(r"(?i)^добавить\s+(\S+)\s+(\d+)\s+([\d\s]+)$"))
async def cmd_add(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^добавить\s+(\S+)\s+(\d+)\s+([\d\s]+)$", message.text)
    model = match.group(1).capitalize()
    price = match.group(2)
    sizes = match.group(3).split()

    pending_add[message.from_user.id] = {
        "model": model, "price": price, "sizes": sizes
    }
    await message.answer(
        f"📸 Теперь отправь фото для модели <b>{model}</b>",
        parse_mode="HTML"
    )

# ── ФОТО — загрузка и добавление в таблицу ──
@dp.message(F.photo)
async def handle_photo(message: Message):
    if not is_allowed(message): return
    if message.from_user.id not in pending_add:
        await message.answer("Сначала введи команду <b>добавить</b>", parse_mode="HTML")
        return

    data = pending_add.pop(message.from_user.id)
    model = data["model"]
    price = data["price"]
    sizes = data["sizes"]

    await message.answer("⏳ Загружаю фото...")

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    file_bytes = requests.get(file_url).content

    photo_url = upload_to_cloudinary(file_bytes, model.lower())

    ws = get_catalog()
    rows = []
    for size in sizes:
        article = f"{model}-{size}"
        rows.append([model, article, price, photo_url, "TRUE", ""])

    ws.append_rows(rows)

    await message.answer(
        f"✅ Добавлено: <b>{model}</b>\n"
        f"Размеры: {', '.join(sizes)}\n"
        f"Цена: {price} грн",
        parse_mode="HTML"
    )

# ── Вспомогательное: цена и себестоимость модели ──
def get_price_cost(catalog_data, model):
    price = ""
    cost = ""
    for row in catalog_data:
        if row["model"] == model:
            price = row.get("price", "")
            cost = row.get("cost", "")
            break
    return price, cost

# ── Вспомогательное: следующий order_id ──
def next_order_id(orders_data):
    max_id = 0
    for row in orders_data:
        try:
            val = int(str(row.get("order_id", "")).strip())
            if val > max_id:
                max_id = val
        except (ValueError, TypeError):
            continue
    return max_id + 1

# ── ПРОДАНО (списать пару + записать продажу в orders) ──
# Цена — необязательная. Если указана после размера, берём её (скидка/распродажа),
# иначе берём цену из каталога.
@dp.message(F.text.regexp(r"(?i)^продано\s+(\S+)\s+(\d+)(?:\s+(\d+))?$"))
async def cmd_sold(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^продано\s+(\S+)\s+(\d+)(?:\s+(\d+))?$", message.text)
    model = match.group(1).capitalize()
    size = match.group(2)
    custom_price = match.group(3)  # None, если цена не указана
    article = f"{model}-{size}"

    cat = get_catalog()
    cat_data = cat.get_all_records()

    # 1. Списываем ОДНУ доступную пару (первую строку с active=TRUE)
    sold = False
    for i, row in enumerate(cat_data, start=2):
        if str(row["article"]) == article and str(row.get("active", "")).upper() == "TRUE":
            cat.update_cell(i, 5, "FALSE")
            sold = True
            break

    if not sold:
        await message.answer(
            f"❌ Размер <b>{article}</b> отсутствует в наличии — продать нечего",
            parse_mode="HTML"
        )
        return

    # 2. Себестоимость всегда из каталога; цена — из команды или из каталога
    catalog_price, cost = get_price_cost(cat_data, model)
    if custom_price:
        price = custom_price
        price_note = f"Цена: {price} грн (вручную) · "
    else:
        price = catalog_price
        price_note = f"Цена: {price} грн · "

    # 3. Запись продажи в orders
    orders = get_orders()
    orders_data = orders.get_all_records()
    oid = next_order_id(orders_data)
    today = datetime.now().strftime("%Y-%m-%d")
    # order_id, date, article, model, size, client_name, client_card, amount, status, cost
    orders.append_rows([[oid, today, article, model, size, "", "", price, "Продано", cost]])

    # 4. Остаток
    remaining = sum(
        1 for r in cat_data
        if str(r["article"]) == article
        and str(r.get("active", "")).upper() == "TRUE"
    ) - 1

    tail = (
        f"Осталось в наличии: <b>{remaining}</b> пар"
        if remaining > 0 else
        "Это была последняя пара — размер убран с сайта"
    )
    await message.answer(
        f"💰 Продажа записана: <b>{article}</b>\n"
        f"{price_note}order #{oid}\n"
        f"{tail}",
        parse_mode="HTML"
    )

# ── ВЕРНУЛИ (вернуть пару + сторно в orders) ──
@dp.message(F.text.regexp(r"(?i)^вернули\s+(\S+)\s+(\d+)$"))
async def cmd_return(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^вернули\s+(\S+)\s+(\d+)$", message.text)
    model = match.group(1).capitalize()
    size = match.group(2)
    article = f"{model}-{size}"

    cat = get_catalog()
    cat_data = cat.get_all_records()

    # 1. Возвращаем пару: оживляем строку, которая FALSE
    returned = False
    for i, row in enumerate(cat_data, start=2):
        if str(row["article"]) == article and str(row.get("active", "")).upper() != "TRUE":
            cat.update_cell(i, 5, "TRUE")
            returned = True
            break

    # Если FALSE-строки нет — создаём новую (товар вернулся физически)
    if not returned:
        price_new, _ = get_price_cost(cat_data, model)
        photo = ""
        for row in cat_data:
            if row["model"] == model:
                photo = row.get("photo", "")
                break
        cat.append_rows([[model, article, price_new, photo, "TRUE", ""]])

    # 2. Сторно-строка в orders (минус — гасит продажу в аналитике)
    price, cost = get_price_cost(cat_data, model)
    neg_amount = f"-{price}" if str(price).strip() else ""
    neg_cost = f"-{cost}" if str(cost).strip() else ""

    orders = get_orders()
    orders_data = orders.get_all_records()
    oid = next_order_id(orders_data)
    today = datetime.now().strftime("%Y-%m-%d")
    orders.append_rows([[oid, today, article, model, size, "", "", neg_amount, "Возврат", neg_cost]])

    await message.answer(
        f"↩️ Возврат оформлен: <b>{article}</b>\n"
        f"Пара снова в наличии · сторно записано (order #{oid})",
        parse_mode="HTML"
    )

# ── УБРАТЬ размер (скрыть без продажи — служебное) ──
@dp.message(F.text.regexp(r"(?i)^размер\s+(\S+)\s+убрать\s+(\d+)$"))
async def cmd_size_remove(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^размер\s+(\S+)\s+убрать\s+(\d+)$", message.text)
    model = match.group(1).capitalize()
    size = match.group(2)
    article = f"{model}-{size}"

    ws = get_catalog()
    data = ws.get_all_records()

    for i, row in enumerate(data, start=2):
        if str(row["article"]) == article and str(row.get("active", "")).upper() == "TRUE":
            ws.update_cell(i, 5, "FALSE")
            remaining = sum(
                1 for r in data
                if str(r["article"]) == article
                and str(r.get("active", "")).upper() == "TRUE"
            ) - 1
            if remaining > 0:
                await message.answer(
                    f"✅ Скрыта 1 пара <b>{article}</b> (без учёта продажи).\n"
                    f"Осталось: <b>{remaining}</b>",
                    parse_mode="HTML"
                )
            else:
                await message.answer(
                    f"✅ <b>{article}</b> скрыт с сайта (без учёта продажи)",
                    parse_mode="HTML"
                )
            return

    await message.answer(
        f"❌ Размер <b>{article}</b> уже отсутствует в наличии",
        parse_mode="HTML"
    )

# ── ДОБАВИТЬ размер обратно (служебное, +1 пара) ──
@dp.message(F.text.regexp(r"(?i)^размер\s+(\S+)\s+добавить\s+(\d+)$"))
async def cmd_size_add(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^размер\s+(\S+)\s+добавить\s+(\d+)$", message.text)
    model = match.group(1).capitalize()
    size = match.group(2)
    article = f"{model}-{size}"

    ws = get_catalog()
    data = ws.get_all_records()

    for i, row in enumerate(data, start=2):
        if str(row["article"]) == article and str(row.get("active", "")).upper() != "TRUE":
            ws.update_cell(i, 5, "TRUE")
            await message.answer(f"✅ Размер <b>{article}</b> снова в наличии (+1 пара)", parse_mode="HTML")
            return

    price = ""
    photo = ""
    for row in data:
        if row["model"] == model:
            price = row["price"]
            photo = row["photo"]
            break

    ws.append_rows([[model, article, price, photo, "TRUE", ""]])
    await message.answer(f"✅ Размер <b>{article}</b> добавлен в каталог (+1 пара)", parse_mode="HTML")

# ── ЦЕНА ──
@dp.message(F.text.regexp(r"(?i)^цена\s+(\S+)\s+(\d+)$"))
async def cmd_price(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^цена\s+(\S+)\s+(\d+)$", message.text)
    model = match.group(1).capitalize()
    new_price = match.group(2)

    ws = get_catalog()
    data = ws.get_all_records()
    updated = 0

    for i, row in enumerate(data, start=2):
        if row["model"] == model:
            ws.update_cell(i, 3, new_price)
            updated += 1

    if updated:
        await message.answer(
            f"✅ Цена <b>{model}</b> обновлена: {new_price} грн ({updated} размеров)",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ Модель <b>{model}</b> не найдена", parse_mode="HTML")

# ── СЕБЕСТОИМОСТЬ ──
@dp.message(F.text.regexp(r"(?i)^себестоимость\s+(\S+)\s+(\d+)$"))
async def cmd_cost(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^себестоимость\s+(\S+)\s+(\d+)$", message.text)
    model = match.group(1).capitalize()
    cost = match.group(2)

    ws = get_catalog()
    data = ws.get_all_records()
    updated = 0

    for i, row in enumerate(data, start=2):
        if row["model"] == model:
            ws.update_cell(i, 6, cost)
            updated += 1

    if updated:
        await message.answer(
            f"✅ Себестоимость <b>{model}</b>: {cost} грн ({updated} размеров)",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ Модель <b>{model}</b> не найдена", parse_mode="HTML")

# ── УДАЛИТЬ модель ──
@dp.message(F.text.regexp(r"(?i)^удалить\s+(\S+)$"))
async def cmd_delete(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^удалить\s+(\S+)$", message.text)
    model = match.group(1).capitalize()

    ws = get_catalog()
    data = ws.get_all_records()
    rows_to_delete = []

    for i, row in enumerate(data, start=2):
        if row["model"] == model:
            rows_to_delete.append(i)

    for row_idx in reversed(rows_to_delete):
        ws.delete_rows(row_idx)

    if rows_to_delete:
        await message.answer(
            f"🗑 Модель <b>{model}</b> удалена ({len(rows_to_delete)} размеров)",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ Модель <b>{model}</b> не найдена", parse_mode="HTML")

# ── НЕИЗВЕСТНАЯ КОМАНДА (должен быть последним) ──
@dp.message(F.text)
async def cmd_unknown(message: Message):
    if not is_allowed(message): return
    await message.answer(
        "🤔 Не понял команду.\n\n" + HELP_TEXT,
        parse_mode="HTML"
    )

# ── ЗАПУСК ──
async def main():
    print("🤖 Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
import os
import re
import asyncio
import gspread
import requests
from datetime import datetime
from collections import Counter
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command

# ── КОНФИГ ──
BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
# Список разрешённых Telegram ID (через запятую в ALLOWED_USERS,
# либо отдельными переменными ALLOWED_USER и ALLOWED_USER_2)
ALLOWED_USERS = set()
for _key in ("ALLOWED_USERS", "ALLOWED_USER", "ALLOWED_USER_2"):
    _val = os.getenv(_key, "")
    for _part in _val.split(","):
        _part = _part.strip()
        if _part.isdigit():
            ALLOWED_USERS.add(int(_part))
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL")  # cloudinary://api_key:api_secret@cloud_name

# ── GOOGLE SHEETS ──
def get_sheet():
    creds_data = {
        "type": "service_account",
        "project_id": os.getenv("GS_PROJECT_ID"),
        "private_key_id": os.getenv("GS_PRIVATE_KEY_ID"),
        "private_key": os.getenv("GS_PRIVATE_KEY").replace("\\n", "\n"),
        "client_email": os.getenv("GS_CLIENT_EMAIL"),
        "client_id": os.getenv("GS_CLIENT_ID"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": os.getenv("GS_CLIENT_CERT_URL"),
        "universe_domain": "googleapis.com"
    }
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)

def get_catalog():
    sh = get_sheet()
    return sh.worksheet("catalog")

def get_orders():
    sh = get_sheet()
    return sh.worksheet("orders")

# ── CLOUDINARY UPLOAD ──
def upload_to_cloudinary(file_bytes: bytes, public_id: str) -> str:
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(cloudinary_url=CLOUDINARY_URL)
    result = cloudinary.uploader.upload(
        file_bytes,
        public_id=f"zozulia/{public_id}",
        overwrite=True
    )
    return result["secure_url"]

# ── БОТ ──
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def is_allowed(message: Message) -> bool:
    return message.from_user.id in ALLOWED_USERS

HELP_TEXT = (
    "👟 Бот управления каталогом by Zozulia\n\n"
    "Команды:\n"
    "• <b>добавить Моника 1850 36 37 38</b> + фото\n"
    "• <b>продано Моника 37</b> — продажа по цене каталога\n"
    "• <b>продано Моника 37 1500</b> — продажа по своей цене (скидка)\n"
    "• <b>вернули Моника 37</b> — возврат (вернёт пару + сторно)\n"
    "• <b>размер Моника убрать 38</b> — скрыть без продажи\n"
    "• <b>размер Моника добавить 38</b>\n"
    "• <b>цена Моника 2000</b>\n"
    "• <b>себестоимость Моника 900</b>\n"
    "• <b>удалить Моника</b>\n"
    "• <b>остатки</b> — показать все размеры в наличии"
)

# Ожидание фото после команды добавить
pending_add = {}

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message):
        return
    await message.answer(HELP_TEXT, parse_mode="HTML")

# ── ОСТАТКИ ──
@dp.message(F.text.regexp(r"(?i)^\s*остатки\s*$"))
async def cmd_stock(message: Message):
    if not is_allowed(message): return

    ws = get_catalog()
    data = ws.get_all_records()

    models = {}
    for row in data:
        if str(row.get("active", "")).upper() == "TRUE":
            model = row["model"]
            size = str(row["article"]).split("-")[-1]
            if model not in models:
                models[model] = {"sizes": [], "price": row["price"]}
            models[model]["sizes"].append(size)

    if not models:
        await message.answer("📦 Каталог пуст")
        return

    text = "📦 <b>Остатки в наличии:</b>\n\n"
    for model, info in models.items():
        counts = Counter(info["sizes"])
        sizes_str = " ".join(
            f"{s}×{c}" if c > 1 else s
            for s, c in sorted(counts.items())
        )
        text += f"<b>{model}</b> — {info['price']} грн\n"
        text += f"Размеры: {sizes_str}\n\n"

    await message.answer(text, parse_mode="HTML")

# ── ДОБАВИТЬ модель ──
@dp.message(F.text.regexp(r"(?i)^добавить\s+(\S+)\s+(\d+)\s+([\d\s]+)$"))
async def cmd_add(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^добавить\s+(\S+)\s+(\d+)\s+([\d\s]+)$", message.text)
    model = match.group(1).capitalize()
    price = match.group(2)
    sizes = match.group(3).split()

    pending_add[message.from_user.id] = {
        "model": model, "price": price, "sizes": sizes
    }
    await message.answer(
        f"📸 Теперь отправь фото для модели <b>{model}</b>",
        parse_mode="HTML"
    )

# ── ФОТО — загрузка и добавление в таблицу ──
@dp.message(F.photo)
async def handle_photo(message: Message):
    if not is_allowed(message): return
    if message.from_user.id not in pending_add:
        await message.answer("Сначала введи команду <b>добавить</b>", parse_mode="HTML")
        return

    data = pending_add.pop(message.from_user.id)
    model = data["model"]
    price = data["price"]
    sizes = data["sizes"]

    await message.answer("⏳ Загружаю фото...")

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    file_bytes = requests.get(file_url).content

    photo_url = upload_to_cloudinary(file_bytes, model.lower())

    ws = get_catalog()
    rows = []
    for size in sizes:
        article = f"{model}-{size}"
        rows.append([model, article, price, photo_url, "TRUE", ""])

    ws.append_rows(rows)

    await message.answer(
        f"✅ Добавлено: <b>{model}</b>\n"
        f"Размеры: {', '.join(sizes)}\n"
        f"Цена: {price} грн",
        parse_mode="HTML"
    )

# ── Вспомогательное: цена и себестоимость модели ──
def get_price_cost(catalog_data, model):
    price = ""
    cost = ""
    for row in catalog_data:
        if row["model"] == model:
            price = row.get("price", "")
            cost = row.get("cost", "")
            break
    return price, cost

# ── Вспомогательное: следующий order_id ──
def next_order_id(orders_data):
    max_id = 0
    for row in orders_data:
        try:
            val = int(str(row.get("order_id", "")).strip())
            if val > max_id:
                max_id = val
        except (ValueError, TypeError):
            continue
    return max_id + 1

# ── ПРОДАНО (списать пару + записать продажу в orders) ──
# Цена — необязательная. Если указана после размера, берём её (скидка/распродажа),
# иначе берём цену из каталога.
@dp.message(F.text.regexp(r"(?i)^продано\s+(\S+)\s+(\d+)(?:\s+(\d+))?$"))
async def cmd_sold(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^продано\s+(\S+)\s+(\d+)(?:\s+(\d+))?$", message.text)
    model = match.group(1).capitalize()
    size = match.group(2)
    custom_price = match.group(3)  # None, если цена не указана
    article = f"{model}-{size}"

    cat = get_catalog()
    cat_data = cat.get_all_records()

    # 1. Списываем ОДНУ доступную пару (первую строку с active=TRUE)
    sold = False
    for i, row in enumerate(cat_data, start=2):
        if str(row["article"]) == article and str(row.get("active", "")).upper() == "TRUE":
            cat.update_cell(i, 5, "FALSE")
            sold = True
            break

    if not sold:
        await message.answer(
            f"❌ Размер <b>{article}</b> отсутствует в наличии — продать нечего",
            parse_mode="HTML"
        )
        return

    # 2. Себестоимость всегда из каталога; цена — из команды или из каталога
    catalog_price, cost = get_price_cost(cat_data, model)
    if custom_price:
        price = custom_price
        price_note = f"Цена: {price} грн (вручную) · "
    else:
        price = catalog_price
        price_note = f"Цена: {price} грн · "

    # 3. Запись продажи в orders
    orders = get_orders()
    orders_data = orders.get_all_records()
    oid = next_order_id(orders_data)
    today = datetime.now().strftime("%Y-%m-%d")
    # order_id, date, article, model, size, client_name, client_card, amount, status, cost
    orders.append_rows([[oid, today, article, model, size, "", "", price, "Продано", cost]])

    # 4. Остаток
    remaining = sum(
        1 for r in cat_data
        if str(r["article"]) == article
        and str(r.get("active", "")).upper() == "TRUE"
    ) - 1

    tail = (
        f"Осталось в наличии: <b>{remaining}</b> пар"
        if remaining > 0 else
        "Это была последняя пара — размер убран с сайта"
    )
    await message.answer(
        f"💰 Продажа записана: <b>{article}</b>\n"
        f"{price_note}order #{oid}\n"
        f"{tail}",
        parse_mode="HTML"
    )

# ── ВЕРНУЛИ (вернуть пару + сторно в orders) ──
@dp.message(F.text.regexp(r"(?i)^вернули\s+(\S+)\s+(\d+)$"))
async def cmd_return(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^вернули\s+(\S+)\s+(\d+)$", message.text)
    model = match.group(1).capitalize()
    size = match.group(2)
    article = f"{model}-{size}"

    cat = get_catalog()
    cat_data = cat.get_all_records()

    # 1. Возвращаем пару: оживляем строку, которая FALSE
    returned = False
    for i, row in enumerate(cat_data, start=2):
        if str(row["article"]) == article and str(row.get("active", "")).upper() != "TRUE":
            cat.update_cell(i, 5, "TRUE")
            returned = True
            break

    # Если FALSE-строки нет — создаём новую (товар вернулся физически)
    if not returned:
        price_new, _ = get_price_cost(cat_data, model)
        photo = ""
        for row in cat_data:
            if row["model"] == model:
                photo = row.get("photo", "")
                break
        cat.append_rows([[model, article, price_new, photo, "TRUE", ""]])

    # 2. Сторно-строка в orders (минус — гасит продажу в аналитике)
    price, cost = get_price_cost(cat_data, model)
    neg_amount = f"-{price}" if str(price).strip() else ""
    neg_cost = f"-{cost}" if str(cost).strip() else ""

    orders = get_orders()
    orders_data = orders.get_all_records()
    oid = next_order_id(orders_data)
    today = datetime.now().strftime("%Y-%m-%d")
    orders.append_rows([[oid, today, article, model, size, "", "", neg_amount, "Возврат", neg_cost]])

    await message.answer(
        f"↩️ Возврат оформлен: <b>{article}</b>\n"
        f"Пара снова в наличии · сторно записано (order #{oid})",
        parse_mode="HTML"
    )

# ── УБРАТЬ размер (скрыть без продажи — служебное) ──
@dp.message(F.text.regexp(r"(?i)^размер\s+(\S+)\s+убрать\s+(\d+)$"))
async def cmd_size_remove(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^размер\s+(\S+)\s+убрать\s+(\d+)$", message.text)
    model = match.group(1).capitalize()
    size = match.group(2)
    article = f"{model}-{size}"

    ws = get_catalog()
    data = ws.get_all_records()

    for i, row in enumerate(data, start=2):
        if str(row["article"]) == article and str(row.get("active", "")).upper() == "TRUE":
            ws.update_cell(i, 5, "FALSE")
            remaining = sum(
                1 for r in data
                if str(r["article"]) == article
                and str(r.get("active", "")).upper() == "TRUE"
            ) - 1
            if remaining > 0:
                await message.answer(
                    f"✅ Скрыта 1 пара <b>{article}</b> (без учёта продажи).\n"
                    f"Осталось: <b>{remaining}</b>",
                    parse_mode="HTML"
                )
            else:
                await message.answer(
                    f"✅ <b>{article}</b> скрыт с сайта (без учёта продажи)",
                    parse_mode="HTML"
                )
            return

    await message.answer(
        f"❌ Размер <b>{article}</b> уже отсутствует в наличии",
        parse_mode="HTML"
    )

# ── ДОБАВИТЬ размер обратно (служебное, +1 пара) ──
@dp.message(F.text.regexp(r"(?i)^размер\s+(\S+)\s+добавить\s+(\d+)$"))
async def cmd_size_add(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^размер\s+(\S+)\s+добавить\s+(\d+)$", message.text)
    model = match.group(1).capitalize()
    size = match.group(2)
    article = f"{model}-{size}"

    ws = get_catalog()
    data = ws.get_all_records()

    for i, row in enumerate(data, start=2):
        if str(row["article"]) == article and str(row.get("active", "")).upper() != "TRUE":
            ws.update_cell(i, 5, "TRUE")
            await message.answer(f"✅ Размер <b>{article}</b> снова в наличии (+1 пара)", parse_mode="HTML")
            return

    price = ""
    photo = ""
    for row in data:
        if row["model"] == model:
            price = row["price"]
            photo = row["photo"]
            break

    ws.append_rows([[model, article, price, photo, "TRUE", ""]])
    await message.answer(f"✅ Размер <b>{article}</b> добавлен в каталог (+1 пара)", parse_mode="HTML")

# ── ЦЕНА ──
@dp.message(F.text.regexp(r"(?i)^цена\s+(\S+)\s+(\d+)$"))
async def cmd_price(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^цена\s+(\S+)\s+(\d+)$", message.text)
    model = match.group(1).capitalize()
    new_price = match.group(2)

    ws = get_catalog()
    data = ws.get_all_records()
    updated = 0

    for i, row in enumerate(data, start=2):
        if row["model"] == model:
            ws.update_cell(i, 3, new_price)
            updated += 1

    if updated:
        await message.answer(
            f"✅ Цена <b>{model}</b> обновлена: {new_price} грн ({updated} размеров)",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ Модель <b>{model}</b> не найдена", parse_mode="HTML")

# ── СЕБЕСТОИМОСТЬ ──
@dp.message(F.text.regexp(r"(?i)^себестоимость\s+(\S+)\s+(\d+)$"))
async def cmd_cost(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^себестоимость\s+(\S+)\s+(\d+)$", message.text)
    model = match.group(1).capitalize()
    cost = match.group(2)

    ws = get_catalog()
    data = ws.get_all_records()
    updated = 0

    for i, row in enumerate(data, start=2):
        if row["model"] == model:
            ws.update_cell(i, 6, cost)
            updated += 1

    if updated:
        await message.answer(
            f"✅ Себестоимость <b>{model}</b>: {cost} грн ({updated} размеров)",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ Модель <b>{model}</b> не найдена", parse_mode="HTML")

# ── УДАЛИТЬ модель ──
@dp.message(F.text.regexp(r"(?i)^удалить\s+(\S+)$"))
async def cmd_delete(message: Message):
    if not is_allowed(message): return
    match = re.match(r"(?i)^удалить\s+(\S+)$", message.text)
    model = match.group(1).capitalize()

    ws = get_catalog()
    data = ws.get_all_records()
    rows_to_delete = []

    for i, row in enumerate(data, start=2):
        if row["model"] == model:
            rows_to_delete.append(i)

    for row_idx in reversed(rows_to_delete):
        ws.delete_rows(row_idx)

    if rows_to_delete:
        await message.answer(
            f"🗑 Модель <b>{model}</b> удалена ({len(rows_to_delete)} размеров)",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ Модель <b>{model}</b> не найдена", parse_mode="HTML")

# ── НЕИЗВЕСТНАЯ КОМАНДА (должен быть последним) ──
@dp.message(F.text)
async def cmd_unknown(message: Message):
    if not is_allowed(message): return
    await message.answer(
        "🤔 Не понял команду.\n\n" + HELP_TEXT,
        parse_mode="HTML"
    )

# ── ЗАПУСК ──
async def main():
    print("🤖 Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
