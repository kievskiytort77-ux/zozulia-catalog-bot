import os
import re
import asyncio
import gspread
import requests
from datetime import datetime
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command

# ── КОНФИГ ──
BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_ID = os.getenv("SHEET_ID")
ALLOWED_USER = int(os.getenv("ALLOWED_USER", "0"))  # твой Telegram ID
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
    return message.from_user.id == ALLOWED_USER

# Ожидание фото после команды добавить
pending_add = {}

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "👟 Бот управления каталогом by Zozulia\n\n"
        "Команды:\n"
        "• <b>добавить Моника 1850 36 37 38</b> + фото\n"
        "• <b>размер Моника убрать 38</b>\n"
        "• <b>размер Моника добавить 38</b>\n"
        "• <b>цена Моника 2000</b>\n"
        "• <b>себестоимость Моника 900</b>\n"
        "• <b>удалить Моника</b>\n"
        "• <b>остатки</b> — показать все размеры в наличии",
        parse_mode="HTML"
    )

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

    # Скачиваем фото
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    file_bytes = requests.get(file_url).content

    # Загружаем в Cloudinary
    photo_url = upload_to_cloudinary(file_bytes, model.lower())

    # Записываем в таблицу
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

# ── УБРАТЬ размер ──
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
        if row["article"] == article:
            ws.update_cell(i, 5, "FALSE")  # колонка E = active
            await message.answer(f"✅ Размер <b>{article}</b> убран с сайта", parse_mode="HTML")
            return

    await message.answer(f"❌ Артикул <b>{article}</b> не найден", parse_mode="HTML")

# ── ДОБАВИТЬ размер обратно ──
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
        if row["article"] == article:
            ws.update_cell(i, 5, "TRUE")
            await message.answer(f"✅ Размер <b>{article}</b> снова в наличии", parse_mode="HTML")
            return

    # Если строки нет — создаём новую
    # Ищем цену и фото существующей модели
    price = ""
    photo = ""
    for row in data:
        if row["model"] == model:
            price = row["price"]
            photo = row["photo"]
            break

    ws.append_rows([[model, article, price, photo, "TRUE", ""]])
    await message.answer(f"✅ Размер <b>{article}</b> добавлен в каталог", parse_mode="HTML")

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
            ws.update_cell(i, 3, new_price)  # колонка C = price
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
            ws.update_cell(i, 6, cost)  # колонка F = cost
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

    # Удаляем снизу вверх чтобы индексы не сдвигались
    for row_idx in reversed(rows_to_delete):
        ws.delete_rows(row_idx)

    if rows_to_delete:
        await message.answer(
            f"🗑 Модель <b>{model}</b> удалена ({len(rows_to_delete)} размеров)",
            parse_mode="HTML"
        )
    else:
        await message.answer(f"❌ Модель <b>{model}</b> не найдена", parse_mode="HTML")

# ── ОСТАТКИ ──
@dp.message(F.text.lower() == "остатки")
async def cmd_stock(message: Message):
    if not is_allowed(message): return

    ws = get_catalog()
    data = ws.get_all_records()

    # Группируем по моделям
    models = {}
    for row in data:
        if str(row["active"]).upper() == "TRUE":
            model = row["model"]
            size = row["article"].split("-")[-1]
            if model not in models:
                models[model] = {"sizes": [], "price": row["price"]}
            models[model]["sizes"].append(size)

    if not models:
        await message.answer("📦 Каталог пуст")
        return

    text = "📦 <b>Остатки в наличии:</b>\n\n"
    for model, info in models.items():
        sizes_str = " ".join(info["sizes"])
        text += f"<b>{model}</b> — {info['price']} грн\n"
        text += f"Размеры: {sizes_str}\n\n"

    await message.answer(text, parse_mode="HTML")

# ── ЗАПУСК ──
async def main():
    print("🤖 Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
