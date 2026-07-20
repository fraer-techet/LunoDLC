import asyncio
import html
import os
from datetime import datetime, timedelta, timezone

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6049379160"))
SUB_BASE_URL = os.environ["SUB_BASE_URL"].rstrip("/")
PREMIUM_DAYS = int(os.environ.get("PREMIUM_DAYS", "30"))
PREMIUM_STARS = int(os.environ.get("PREMIUM_STARS", "150"))
PREMIUM_PRICE_LABEL = os.environ.get("PREMIUM_PRICE_LABEL", f"Premium {PREMIUM_DAYS} days")
PORT = int(os.environ.get("PORT", "10000"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
pool: asyncpg.Pool | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_active(status: str, expires) -> bool:
    if status not in ("trial", "premium"):
        return False
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > utcnow()


def days_left(expires) -> int:
    if expires is None:
        return 0
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    seconds = (expires - utcnow()).total_seconds()
    if seconds <= 0:
        return 0
    return max(1, int((seconds + 86399) // 86400))


def sub_url(token: str) -> str:
    return f"{SUB_BASE_URL}/sub/{token}"


def main_keyboard(user_row) -> InlineKeyboardMarkup:
    buttons = []
    active = is_active(user_row["status"], user_row["subscription_expires"])
    if not user_row["trial_used"] and not active:
        buttons.append([InlineKeyboardButton(text="Активировать триал 7 дней", callback_data="trial")])
    buttons.append([InlineKeyboardButton(text="Купить Premium", callback_data="buy")])
    if active:
        buttons.append([InlineKeyboardButton(text="Моя подписка", callback_data="mysub")])
    buttons.append([InlineKeyboardButton(text="Личный кабинет", url=sub_url(user_row["sub_token"]))])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Список серверов", callback_data="admin_servers")],
            [InlineKeyboardButton(text="Пользовательское меню", callback_data="user_menu")],
        ]
    )


async def ensure_user(telegram_id: int):
    assert pool is not None
    row = await pool.fetchrow("select * from users where telegram_id = $1", telegram_id)
    if row:
        return row
    row = await pool.fetchrow(
        """
        insert into users (telegram_id, status, trial_used)
        values ($1, 'free', false)
        returning *
        """,
        telegram_id,
    )
    return row


async def refresh_user(telegram_id: int):
    assert pool is not None
    return await pool.fetchrow("select * from users where telegram_id = $1", telegram_id)


def status_text(user_row) -> str:
    active = is_active(user_row["status"], user_row["subscription_expires"])
    link = sub_url(user_row["sub_token"])
    if active:
        left = days_left(user_row["subscription_expires"])
        kind = "Trial" if user_row["status"] == "trial" else "Premium"
        return (
            f"Статус: <b>{kind}</b>\n"
            f"Осталось дней: <b>{left}</b>\n"
            f"Ссылка подписки:\n<code>{html.escape(link)}</code>\n\n"
            f"Вставь ссылку в Hiddify / v2rayNG / Streisand как subscription URL."
        )
    trial_line = "Триал ещё доступен." if not user_row["trial_used"] else "Триал уже использован."
    return (
        f"Статус: <b>нет активной подписки</b>\n"
        f"{trial_line}\n"
        f"Ссылка кабинета:\n<code>{html.escape(link)}</code>"
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = await ensure_user(message.from_user.id)
    text = "Добро пожаловать в премиальный VPN.\n\n" + status_text(user)
    kb = main_keyboard(user)
    if message.from_user.id == ADMIN_ID:
        text += "\n\nРежим администратора активен."
        kb = InlineKeyboardMarkup(
            inline_keyboard=main_keyboard(user).inline_keyboard + admin_keyboard().inline_keyboard
        )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "user_menu")
async def cb_user_menu(callback: CallbackQuery):
    user = await ensure_user(callback.from_user.id)
    await callback.message.edit_text(
        status_text(user),
        reply_markup=main_keyboard(user),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "mysub")
async def cb_mysub(callback: CallbackQuery):
    user = await ensure_user(callback.from_user.id)
    await callback.message.edit_text(
        status_text(user),
        reply_markup=main_keyboard(user),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "trial")
async def cb_trial(callback: CallbackQuery):
    assert pool is not None
    user = await ensure_user(callback.from_user.id)
    if is_active(user["status"], user["subscription_expires"]):
        await callback.answer("Подписка уже активна", show_alert=True)
        return
    if user["trial_used"]:
        await callback.answer("Триал уже был использован", show_alert=True)
        return
    expires = utcnow() + timedelta(days=7)
    user = await pool.fetchrow(
        """
        update users
        set status = 'trial',
            trial_used = true,
            subscription_expires = $2
        where telegram_id = $1
        returning *
        """,
        callback.from_user.id,
        expires,
    )
    await callback.message.edit_text(
        "Триал на 7 дней активирован.\n\n" + status_text(user),
        reply_markup=main_keyboard(user),
        parse_mode="HTML",
    )
    await callback.answer("Триал активирован")


@dp.callback_query(F.data == "buy")
async def cb_buy(callback: CallbackQuery):
    await ensure_user(callback.from_user.id)
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=PREMIUM_PRICE_LABEL,
        description=f"Доступ Premium на {PREMIUM_DAYS} дней",
        payload=f"premium:{PREMIUM_DAYS}",
        currency="XTR",
        prices=[LabeledPrice(label=PREMIUM_PRICE_LABEL, amount=PREMIUM_STARS)],
        provider_token="",
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    assert pool is not None
    payload = message.successful_payment.invoice_payload or ""
    days = PREMIUM_DAYS
    if payload.startswith("premium:"):
        try:
            days = int(payload.split(":", 1)[1])
        except ValueError:
            days = PREMIUM_DAYS
    user = await ensure_user(message.from_user.id)
    now = utcnow()
    base = now
    if is_active(user["status"], user["subscription_expires"]):
        exp = user["subscription_expires"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now:
            base = exp
    expires = base + timedelta(days=days)
    user = await pool.fetchrow(
        """
        update users
        set status = 'premium',
            subscription_expires = $2
        where telegram_id = $1
        returning *
        """,
        message.from_user.id,
        expires,
    )
    await message.answer(
        "Оплата прошла успешно. Premium активирован.\n\n" + status_text(user),
        reply_markup=main_keyboard(user),
        parse_mode="HTML",
    )


@dp.message(Command("add_server"))
async def cmd_add_server(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    assert pool is not None
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Формат:\n<code>/add_server ИМЯ|||конфиг</code>\n"
            "Пример:\n<code>/add_server 🇩🇪 Germany|||vless://uuid@host:443?...#old</code>",
            parse_mode="HTML",
        )
        return
    if "|||" not in raw:
        await message.answer("Нужен разделитель ||| между именем и конфигом.")
        return
    name, config = raw.split("|||", 1)
    name = name.strip()
    config = config.strip()
    if not name or not config:
        await message.answer("Имя и конфиг не должны быть пустыми.")
        return
    row = await pool.fetchrow(
        """
        insert into server_pool (raw_config, custom_name)
        values ($1, $2)
        returning id, custom_name
        """,
        config,
        name,
    )
    await message.answer(f"Сервер добавлен: #{row['id']} — {html.escape(row['custom_name'])}", parse_mode="HTML")


@dp.message(Command("delete_server"))
async def cmd_delete_server(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    assert pool is not None
    args = (command.args or "").strip()
    if not args:
        rows = await pool.fetch("select id, custom_name from server_pool order by id")
        if not rows:
            await message.answer("Пул серверов пуст.")
            return
        lines = [f"#{r['id']} — {r['custom_name']}" for r in rows]
        await message.answer(
            "Укажи ID:\n<code>/delete_server ID</code>\n\n" + html.escape("\n".join(lines)),
            parse_mode="HTML",
        )
        return
    if not args.isdigit():
        await message.answer("ID должен быть числом.")
        return
    row = await pool.fetchrow("delete from server_pool where id = $1 returning id, custom_name", int(args))
    if not row:
        await message.answer("Сервер не найден.")
        return
    await message.answer(f"Удалён #{row['id']} — {html.escape(row['custom_name'])}", parse_mode="HTML")


@dp.message(Command("edit_name"))
async def cmd_edit_name(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    assert pool is not None
    raw = (command.args or "").strip()
    if not raw or "|||" not in raw:
        await message.answer(
            "Формат:\n<code>/edit_name ID|||Новое имя</code>",
            parse_mode="HTML",
        )
        return
    left, name = raw.split("|||", 1)
    left = left.strip()
    name = name.strip()
    if not left.isdigit() or not name:
        await message.answer("Некорректные данные.")
        return
    row = await pool.fetchrow(
        """
        update server_pool
        set custom_name = $2
        where id = $1
        returning id, custom_name
        """,
        int(left),
        name,
    )
    if not row:
        await message.answer("Сервер не найден.")
        return
    await message.answer(f"Обновлён #{row['id']} — {html.escape(row['custom_name'])}", parse_mode="HTML")


@dp.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    assert pool is not None
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await message.answer("Формат: /grant TELEGRAM_ID DAYS")
        return
    tg_id = int(parts[0])
    days = int(parts[1])
    user = await ensure_user(tg_id)
    now = utcnow()
    base = now
    if is_active(user["status"], user["subscription_expires"]):
        exp = user["subscription_expires"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now:
            base = exp
    expires = base + timedelta(days=days)
    user = await pool.fetchrow(
        """
        update users
        set status = 'premium',
            subscription_expires = $2
        where telegram_id = $1
        returning *
        """,
        tg_id,
        expires,
    )
    await message.answer(
        f"Выдан Premium пользователю {tg_id} до {expires.isoformat()}\n"
        f"Ссылка: <code>{html.escape(sub_url(user['sub_token']))}</code>",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "admin_servers")
async def cb_admin_servers(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    assert pool is not None
    rows = await pool.fetch("select id, custom_name from server_pool order by id")
    if not rows:
        text = "Пул серверов пуст.\n/add_server ИМЯ|||конфиг"
    else:
        text = "Серверы:\n" + "\n".join(f"#{r['id']} — {html.escape(r['custom_name'])}" for r in rows)
        text += (
            "\n\n/add_server ИМЯ|||конфиг"
            "\n/delete_server ID"
            "\n/edit_name ID|||Новое имя"
            "\n/grant TELEGRAM_ID DAYS"
        )
    await callback.message.edit_text(text, reply_markup=admin_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Админ-панель", reply_markup=admin_keyboard())


async def health(_request):
    return web.Response(text="ok")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def main():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=60)
    await start_health_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
