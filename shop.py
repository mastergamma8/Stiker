"""
shop.py

The public-facing shop: /start opens a catalog of stickers/premium emoji
(built from a source pack the admin imports once), anyone can browse it,
add designs to their own custom pack or buy one on the spot, pay with
Telegram Stars, and receive their own sticker pack + emoji pack with
their text baked into every design.

How a catalog item is born (admin only, see cmd_importpack /
handle_import_sticker below): the admin sends any sticker that belongs to
an existing Telegram sticker pack. The bot fetches that whole pack via
getStickerSet, and for each animated (.tgs) sticker in it:
  1. downloads the original bytes (kept forever as `orig_tgs`);
  2. runs the *existing* text-replacement engine (tgs_editor.replace_text)
     with the placeholder text PLACEHOLDER_TEXT ("TEXT" by default) to
     produce a demo copy (`demo_tgs`) -- this is what the catalog shows
     while browsing;
  3. remembers *which* shape group replace_text found (`target_index`),
     so a paying customer's own text can be dropped into the exact same
     spot later without re-running detection on an already-edited file.
All of this reuses tgs_editor.py/text_to_lottie.py exactly as the manual
"send me a sticker, I'll change its text" flow in bot.py already does --
the shop is a new front end on the same engine, not a new engine.

Routing note: `router` here is included into the Dispatcher *before*
bot.py's own router (see bot.py's main()), so anything this module claims
-- specific FSM states, callback_query data, pre_checkout/successful
payment -- is handled here first, and only messages this router doesn't
match at all fall through to bot.py's manual-mode handlers (including its
catch-all at the very end). This mirrors how bot.py already orders its
own handlers (state-specific before generic).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InputSticker,
    LabeledPrice,
    Message,
    MessageEntity,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import sticker_sets
from pack_builder import DEFAULT_EMOJI, MAX_TITLE_LEN
from tgs_editor import (
    MAX_TGS_BYTES,
    TextGroupNotFoundError,
    load_tgs_bytes,
    replace_text,
    save_tgs_bytes,
)

log = logging.getLogger("tgs_logo_bot.shop")
router = Router(name="shop")

# The literal placeholder baked into every catalog preview/template pack --
# see the module docstring and the user-facing spec this implements: the
# demo pack (and its title) both read exactly "TEXT" by default. Override
# with TGS_BOT_PLACEHOLDER_TEXT if you'd rather use something else.
PLACEHOLDER_TEXT = os.getenv("TGS_BOT_PLACEHOLDER_TEXT", "TEXT")

# Comma-separated Telegram numeric user IDs allowed to import/price/manage
# the catalog. TGS_BOT_ADMIN_IDS overrides this when set (see .env.example);
# the literal default below exists so admin access works out of the box
# without an .env -- same reasoning as BOT_TOKEN's fallback in bot.py. Add
# more admins by setting TGS_BOT_ADMIN_IDS to a comma-separated list, or by
# editing this default directly.
ADMIN_IDS = {
    int(x) for x in os.getenv("TGS_BOT_ADMIN_IDS", "8220195553,7755576459").replace(" ", "").split(",") if x.strip()
}

# Secret code for free checkout (see cmd_promo below) -- anyone who sends
# "/promo <this code>" gets every future order for free, same as an admin.
# Deliberately not mentioned anywhere in /help or README: the whole point
# is that regular users don't know this command exists. Override via
# TGS_BOT_PROMO_CODE in .env -- do this before sharing the code with
# anyone, since the default below ships in the repo and isn't a secret
# from anyone who reads the source (same caveat as BOT_TOKEN/ADMIN_IDS).
PROMO_CODE = os.getenv("TGS_BOT_PROMO_CODE", "FREEDEWID")

MAX_CUSTOM_TEXT_LEN = 40

# Fixed price per catalog item/design, in Telegram Stars. /importpack now
# applies this automatically to any newly-imported item instead of asking
# (see handle_import_sticker) -- /setprice is still there for a one-off
# manual exception, it just isn't needed for the normal flow anymore.
# Override via TGS_BOT_PRICE_STARS.
PRICE_STARS = int(os.getenv("TGS_BOT_PRICE_STARS", "3"))

# Custom (Telegram Premium) emoji shown in place of a plain "⭐" wherever
# the bot mentions Stars in message text -- see _star_entities/send_stars/
# answer_stars below. Override via TGS_BOT_PREMIUM_STAR_EMOJI_ID.
#
# IMPORTANT: per Telegram's Bot API, a bot may only use custom emoji in
# messages it sends (https://core.telegram.org/bots/api#messageentity) if
# the bot's OWNER account has an active Telegram Premium subscription (or
# the bot purchased an additional username on Fragment) -- this is a
# different requirement from "recipients need Premium to see it", and is
# NOT about the person chatting with the bot. If that isn't the case,
# Telegram rejects the request outright; send_stars/answer_stars catch
# that and silently resend with a plain "⭐" instead, so the shop keeps
# working either way. Also note Telegram inline keyboard buttons cannot
# show custom emoji inline in their label text at all (a hard Bot API
# limitation) -- the ⭐ inside button labels (e.g. "⚡ Купить — 3⭐") stays
# a plain star regardless; only real message text can be upgraded.
PREMIUM_STAR_EMOJI_ID = os.getenv("TGS_BOT_PREMIUM_STAR_EMOJI_ID", "5897920748101571572")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _utf16_len(s: str) -> int:
    """Length of `s` in UTF-16 code units -- the unit Telegram's
    MessageEntity offset/length use, which is NOT the same as Python's
    len(s) whenever a string contains a character outside the Basic
    Multilingual Plane (most emoji, e.g. 🛍💥🎉, need 2 UTF-16 units each,
    but Python's len() still counts each as 1 character). Every offset in
    _star_entities is computed with this, not len() -- otherwise a "⭐"
    appearing after any such emoji in the same string would get tagged at
    the wrong position and Telegram would either reject the whole message
    or highlight the wrong characters."""
    return len(s.encode("utf-16-le")) // 2


def _star_entities(text: str) -> list[MessageEntity]:
    """MessageEntity list that overlays PREMIUM_STAR_EMOJI_ID on every
    literal "⭐" in `text`. The text itself always keeps the plain star --
    that's Telegram's required fallback glyph (shown wherever a custom
    emoji can't be rendered) -- entities just dress it up on top wherever
    a client can show it, so this never changes what has to be typed/
    stored, only how it can be displayed."""
    out: list[MessageEntity] = []
    start = 0
    while True:
        idx = text.find("⭐", start)
        if idx == -1:
            break
        out.append(MessageEntity(
            type="custom_emoji",
            offset=_utf16_len(text[:idx]),
            length=1,
            custom_emoji_id=PREMIUM_STAR_EMOJI_ID,
        ))
        start = idx + 1
    return out


def _is_premium_emoji_error(e: TelegramBadRequest) -> bool:
    """Best-effort match on Telegram rejecting a custom_emoji entity --
    typically because the bot owner's account doesn't have Telegram
    Premium (see PREMIUM_STAR_EMOJI_ID above). aiogram doesn't give this
    its own exception subclass, so this matches on the error text; anything
    that doesn't look like this specific failure re-raises unchanged so
    send_stars/answer_stars never silently swallow an unrelated error."""
    msg = str(e).lower()
    return ("custom" in msg and "emoji" in msg) or "premium" in msg


async def send_stars(bot: Bot, chat_id: int, text: str, reply_markup=None) -> Message:
    """bot.send_message, but every "⭐" in `text` renders as the premium
    emoji (see PREMIUM_STAR_EMOJI_ID) wherever Telegram allows it. Falls
    back to a plain "⭐" automatically -- and only -- if Telegram rejects
    the custom emoji itself, so a bot owner without Telegram Premium gets
    a normal-looking shop instead of a broken one."""
    ents = _star_entities(text)
    try:
        return await bot.send_message(chat_id, text, entities=ents or None, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if not ents or not _is_premium_emoji_error(e):
            raise
        log.warning("premium star emoji rejected (bot owner likely lacks Telegram Premium/Fragment username) -- retrying plain")
        return await bot.send_message(chat_id, text, reply_markup=reply_markup)


async def answer_stars(message: Message, text: str, reply_markup=None) -> Message:
    """Same as send_stars, for message.answer(...) call sites."""
    ents = _star_entities(text)
    try:
        return await message.answer(text, entities=ents or None, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if not ents or not _is_premium_emoji_error(e):
            raise
        log.warning("premium star emoji rejected (bot owner likely lacks Telegram Premium/Fragment username) -- retrying plain")
        return await message.answer(text, reply_markup=reply_markup)


class CheckoutStates(StatesGroup):
    waiting_for_text = State()
    confirm = State()


class AdminImport(StatesGroup):
    waiting_for_sticker = State()


# ---------------------------------------------------------------------------
# Main menu / /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await _show_main_menu(bot, message.chat.id, message.from_user.id)


async def _show_main_menu(bot: Bot, chat_id: int, user_id: int) -> None:
    items = await db.list_catalog_light(active_only=True, priced_only=True)
    n = len(items)
    admin = is_admin(user_id)

    if n == 0:
        if admin:
            await send_stars(
                bot, chat_id,
                "Каталог пока пуст. Пришлите любой стикер из готового "
                "стикер-пака (форвардните его сюда) — импортирую весь пак: "
                f"сделаю демо-версию с текстом «{PLACEHOLDER_TEXT}» для "
                "каждого стикера, соберу из них стикер-пак и эмодзи-пак с "
                f"названием «{PLACEHOLDER_TEXT}», и открою каталог для всех "
                f"по фиксированной цене {PRICE_STARS}⭐ за штуку.\n\n"
                "Либо команда /importpack [название].",
            )
        else:
            await bot.send_message(
                chat_id,
                "Привет! 👋 Каталог стикеров и премиум-эмодзи скоро "
                "откроется — загляните чуть позже 🙌",
            )
        return

    total = sum(i["price_stars"] for i in items)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🛍 Каталог", callback_data="shop:v:0"))
    kb.row(InlineKeyboardButton(text=f"⚡ Купить всё — {n} шт., {total}⭐", callback_data="shop:buyall"))
    cart_n = await db.cart_count(user_id)
    cart_label = f"🛒 Мой пак ({cart_n})" if cart_n else "🛒 Мой пак"
    kb.row(InlineKeyboardButton(text=cart_label, callback_data="shop:cart"))
    if admin:
        kb.row(InlineKeyboardButton(text="✏️ Свой стикер (ручной режим)", callback_data="shop:manual"))
        kb.row(InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="shop:admin"))

    catalog_title = await db.get_setting("catalog_title", PLACEHOLDER_TEXT)
    await send_stars(
        bot, chat_id,
        f"Привет! 👋 Это каталог «{catalog_title}» — {n} стикеров и "
        "премиум-эмодзи, каждый можно подписать своим текстом.\n\n"
        "Смотрите каталог, добавляйте нужное в свой пак и покупайте по "
        "одному, паком или всё сразу («⚡ Купить всё») — оплата в Telegram "
        "Stars ⭐.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "shop:menu")
async def cb_menu(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    await _show_main_menu(bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "shop:manual")
async def cb_manual(callback: CallbackQuery) -> None:
    # Defense in depth: _show_main_menu no longer renders this button for
    # non-admins, but a chat can still have an old menu message from
    # before this change with the button intact and tappable.
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступно только администратору.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        "Ручной режим — правка текста на вашем собственном стикере:\n"
        "Пришлите .tgs файл, перешлите анимированный стикер, или пришлите "
        "премиум-эмодзи — я найду текст и предложу заменить. Подробнее и "
        "команды: /help."
    )


@router.callback_query(F.data == "shop:noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(
        f"Ваш Telegram ID: {message.from_user.id}\n\n"
        "Чтобы стать администратором каталога, добавьте этот ID в "
        "переменную окружения TGS_BOT_ADMIN_IDS (через запятую, если их "
        "несколько) и перезапустите бота."
    )


@router.message(Command("promo"))
async def cmd_promo(message: Message) -> None:
    """Secret command, not listed anywhere in /help: "/promo <code>"
    unlocks free checkout permanently for whoever sends the right code.
    Deliberately quiet on a wrong code too (just "неверный код", no hint
    about format/length) so guessing gives away as little as possible."""
    user_id = message.from_user.id
    if is_admin(user_id) or await db.has_free_access(user_id):
        await message.answer("У вас и так уже бесплатный доступ ко всему каталогу 🎁")
        return

    parts = message.text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if not code:
        await message.answer("Использование: /promo <код>")
        return

    if code.lower() != PROMO_CODE.lower():
        await message.answer("Неверный код.")
        return

    await db.grant_free_access(user_id, note=message.from_user.username or "")
    await message.answer(
        "✅ Готово! Весь каталог теперь бесплатен для вас — оформляйте заказ "
        "как обычно через /start и вместо счёта в Stars я сразу пришлю паки."
    )


# ---------------------------------------------------------------------------
# Catalog browsing
# ---------------------------------------------------------------------------

def _build_card_keyboard(item: dict, idx: int, total: int, in_cart: bool, cart_n: int) -> InlineKeyboardBuilder:
    label = f"{idx + 1}/{total} · {item['emoji']} · {item['price_stars']}⭐"
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="◀️", callback_data=f"shop:v:{idx - 1}"),
        InlineKeyboardButton(text=label, callback_data="shop:noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"shop:v:{idx + 1}"),
    )
    if in_cart:
        kb.row(InlineKeyboardButton(text="✅ В своём паке — убрать", callback_data=f"shop:rm:{item['id']}:{idx}"))
    else:
        kb.row(InlineKeyboardButton(text="➕ Добавить в свой пак", callback_data=f"shop:add:{item['id']}:{idx}"))
    kb.row(InlineKeyboardButton(
        text=f"⚡ Купить только этот — {item['price_stars']}⭐",
        callback_data=f"shop:buynow:{item['id']}",
    ))
    cart_label = f"🛒 Мой пак ({cart_n})" if cart_n else "🛒 Мой пак"
    kb.row(
        InlineKeyboardButton(text=cart_label, callback_data="shop:cart"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="shop:menu"),
    )
    return kb


async def _send_catalog_sticker(bot: Bot, chat_id: int, item: dict, reply_markup) -> Message:
    """Sends item's demo sticker. Strongly prefers sending by an
    already-known file_id over uploading raw bytes: sendSticker with a
    file_id is a cheap reference to a file Telegram already has, while
    sendSticker with fresh bytes is a real upload -- and Telegram flood-
    limits uploads far more aggressively than it does references, which
    bites hard when paging through a few dozen items back-to-back (see
    /catalog and /start's catalog browsing, both of which go through this
    same function).

    Tries, in order:
      1) showcase_sticker_file_id / showcase_emoji_file_id -- this exact
         item's own sticker as it already exists in a real, Telegram-
         hosted showcase pack (see db.py's schema comment) -- never needs
         uploading, since Telegram already has that exact file.
      2) preview_file_id -- a cached file_id from a previous raw upload of
         these same demo_tgs bytes, for items that never made it into
         either showcase pack (e.g. beyond the 50/200-item cap, or the
         catalog predates /importpack's pack-building).
      3) a fresh upload of demo_tgs, as an absolute last resort -- and
         only then cached as preview_file_id so this item won't need
         re-uploading again either.
    Any of these can go stale (pack rebuilt outside this flow, bot
    re-added, file expired) -- on a rejection, falls through to the next
    one rather than giving up."""
    for file_id in (item["showcase_sticker_file_id"], item["showcase_emoji_file_id"], item["preview_file_id"]):
        if not file_id:
            continue
        try:
            return await bot.send_sticker(chat_id=chat_id, sticker=file_id, reply_markup=reply_markup)
        except TelegramBadRequest:
            log.warning("stale file_id rejected for item %s, trying next fallback", item["id"])

    # Nothing cached (or every cached id was stale) -- upload from the
    # stored bytes and cache the result so this item won't hit this path
    # again.
    fresh = BufferedInputFile(item["demo_tgs"], filename="preview.tgs")
    msg = await bot.send_sticker(chat_id=chat_id, sticker=fresh, reply_markup=reply_markup)
    if msg.sticker:
        await db.set_preview_file_id(item["id"], msg.sticker.file_id)
    return msg


async def _send_item_card(bot: Bot, chat_id: int, user_id: int, item: dict, idx: int, total: int) -> None:
    in_cart = await db.in_cart(user_id, item["id"])
    cart_n = await db.cart_count(user_id)
    kb = _build_card_keyboard(item, idx, total, in_cart, cart_n)
    await _send_catalog_sticker(bot, chat_id, item, kb.as_markup())


@router.callback_query(F.data.startswith("shop:v:"))
async def cb_view_item(callback: CallbackQuery, bot: Bot) -> None:
    items = await db.list_catalog_light(active_only=True, priced_only=True)
    if not items:
        await callback.answer("Каталог пока пуст.", show_alert=True)
        return

    idx = int(callback.data.split(":")[2])
    idx = max(0, min(idx, len(items) - 1))
    full = await db.get_catalog_item(items[idx]["id"])
    if full is None:
        await callback.answer("Эта позиция больше недоступна.", show_alert=True)
        return

    await _send_item_card(bot, callback.message.chat.id, callback.from_user.id, full, idx, len(items))
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("shop:add:") | F.data.startswith("shop:rm:"))
async def cb_toggle_cart(callback: CallbackQuery) -> None:
    action, item_id_s, idx_s = callback.data.split(":")[1:]
    item_id, idx = int(item_id_s), int(idx_s)

    if action == "add":
        await db.add_to_cart(callback.from_user.id, item_id)
    else:
        await db.remove_from_cart(callback.from_user.id, item_id)

    item = await db.get_catalog_item_light(item_id)
    total = await db.count_catalog_items(active_only=True, priced_only=True)
    if item is None or total == 0:
        await callback.answer()
        return
    in_cart = await db.in_cart(callback.from_user.id, item_id)
    cart_n = await db.cart_count(callback.from_user.id)
    kb = _build_card_keyboard(item, idx, total, in_cart, cart_n)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await callback.answer("Добавлено в свой пак ✅" if action == "add" else "Убрано")


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------

async def _show_cart(bot: Bot, chat_id: int, user_id: int) -> None:
    cart = await db.get_cart_light(user_id)
    kb = InlineKeyboardBuilder()

    if not cart:
        kb.row(InlineKeyboardButton(text="🛍 В каталог", callback_data="shop:v:0"))
        await bot.send_message(
            chat_id,
            "Пак пока пуст. Загляните в каталог и добавьте что-нибудь 🛍",
            reply_markup=kb.as_markup(),
        )
        return

    total = sum(c["price_stars"] for c in cart)
    lines = ["🛒 Ваш пак:\n"]
    for c in cart:
        lines.append(f"{c['emoji']} #{c['id']} — {c['price_stars']}⭐")
        kb.row(InlineKeyboardButton(text=f"❌ Убрать {c['emoji']} #{c['id']}", callback_data=f"shop:cartrm:{c['id']}"))
    lines.append(f"\nИтого: {total}⭐")

    kb.row(InlineKeyboardButton(text=f"⚡ Купить пак — {total}⭐", callback_data="shop:checkout"))
    kb.row(
        InlineKeyboardButton(text="🗑 Очистить", callback_data="shop:cartclear"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="shop:menu"),
    )
    await send_stars(bot, chat_id, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data == "shop:cart")
async def cb_cart(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    await _show_cart(bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data.startswith("shop:cartrm:"))
async def cb_cart_remove(callback: CallbackQuery, bot: Bot) -> None:
    item_id = int(callback.data.split(":")[2])
    await db.remove_from_cart(callback.from_user.id, item_id)
    await callback.answer("Убрано")
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await _show_cart(bot, callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "shop:cartclear")
async def cb_cart_clear(callback: CallbackQuery, bot: Bot) -> None:
    await db.clear_cart(callback.from_user.id)
    await callback.answer("Пак очищен")
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await _show_cart(bot, callback.message.chat.id, callback.from_user.id)


# ---------------------------------------------------------------------------
# Checkout: custom text -> preview -> Stars invoice -> delivery
# ---------------------------------------------------------------------------

def _generate_personalized(row: dict, text: str) -> bytes:
    """Returns finished .tgs bytes personalised with `text`, or the
    original bytes unchanged if this catalog item never had a detected
    text group (a purely decorative design in the source pack). Raises
    TextGroupNotFoundError/ValueError/RuntimeError on failure -- callers
    decide how to handle that (ask for shorter text before payment, or
    fall back / refund after payment)."""
    if row["target_index"] is None:
        return row["orig_tgs"]
    data = load_tgs_bytes(row["orig_tgs"])
    modified, _report = replace_text(data, text, target=row["target_index"])
    out = save_tgs_bytes(modified)
    if len(out) > MAX_TGS_BYTES:
        raise ValueError(f"personalized file too large: {len(out)} bytes")
    return out


async def _start_checkout(bot: Bot, chat_id: int, state: FSMContext, item_ids: list[int]) -> None:
    await state.update_data(checkout_item_ids=item_ids)
    await state.set_state(CheckoutStates.waiting_for_text)
    n = len(item_ids)
    what = "этот дизайн" if n == 1 else f"все {n} дизайнов в паке"
    await bot.send_message(
        chat_id,
        f"Каким текстом подписать {what}? Пришлите сообщением — можно "
        f"кириллицей и латиницей, до {MAX_CUSTOM_TEXT_LEN} символов.\n"
        "/cancel — отменить.",
    )


@router.callback_query(F.data.startswith("shop:buynow:"))
async def cb_buy_now(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    item_id = int(callback.data.split(":")[2])
    item = await db.get_catalog_item_light(item_id)
    if not item or not item["active"] or item["price_stars"] <= 0:
        await callback.answer("Этот дизайн сейчас недоступен.", show_alert=True)
        return
    await callback.answer()
    await _start_checkout(bot, callback.message.chat.id, state, [item_id])


@router.callback_query(F.data == "shop:checkout")
async def cb_checkout(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    cart = await db.get_cart_light(callback.from_user.id)
    if not cart:
        await callback.answer("Пак пуст.", show_alert=True)
        return
    await callback.answer()
    await _start_checkout(bot, callback.message.chat.id, state, [c["id"] for c in cart])


@router.callback_query(F.data == "shop:buyall")
async def cb_buy_all(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """'⚡ Купить всё' on the main menu -- same checkout flow as the cart
    (_start_checkout), just pre-loaded with the entire active/priced
    catalog instead of whatever the user happened to add to their own
    pack, so buying everything doesn't require adding every item to the
    cart by hand first."""
    items = await db.list_catalog_light(active_only=True, priced_only=True)
    if not items:
        await callback.answer("Каталог пуст.", show_alert=True)
        return
    await callback.answer()
    await _start_checkout(bot, callback.message.chat.id, state, [i["id"] for i in items])


@router.message(CheckoutStates.waiting_for_text, Command("cancel"))
async def checkout_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил оформление. /start — вернуться в каталог.")


@router.message(CheckoutStates.waiting_for_text, F.text)
async def checkout_got_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text:
        await message.answer("Текст пустой, пришлите хотя бы одну букву.")
        return
    if len(text) > MAX_CUSTOM_TEXT_LEN:
        await message.answer(f"Многовато букв для стикера — попробуйте покороче (до {MAX_CUSTOM_TEXT_LEN} символов).")
        return

    data = await state.get_data()
    item_ids = data.get("checkout_item_ids") or []
    if not item_ids:
        await state.clear()
        await message.answer("Сессия потерялась, начните заново из каталога: /start.")
        return

    rows = []
    total = 0
    for iid in item_ids:
        row = await db.get_catalog_item(iid)
        if row is not None and row["active"] and row["price_stars"] > 0:
            rows.append(row)
            total += row["price_stars"]

    if not rows:
        await state.clear()
        await message.answer("Выбранные позиции больше недоступны, начните заново: /start.")
        return

    # Validate every item can actually take this text *before* charging
    # anything -- generation is deterministic, so this exact result is
    # what delivery will reproduce after payment (see on_successful_payment).
    failed_ids = []
    preview_bytes = None
    for row in rows:
        try:
            out = _generate_personalized(row, text)
        except (TextGroupNotFoundError, ValueError, RuntimeError):
            failed_ids.append(row["id"])
            continue
        except Exception:
            log.exception("personalization failed for item %s", row["id"])
            failed_ids.append(row["id"])
            continue
        if preview_bytes is None:
            preview_bytes = out

    if failed_ids:
        ids = ", ".join(f"#{i}" for i in failed_ids)
        await message.answer(
            f"Этот текст не поместился в {len(failed_ids)} из {len(rows)} "
            f"дизайнов ({ids}) — попробуйте текст покороче."
        )
        return  # stay in the same state so they can just retry

    await state.update_data(checkout_text=text, checkout_valid_ids=[r["id"] for r in rows], checkout_total=total)
    await state.set_state(CheckoutStates.confirm)

    if preview_bytes:
        try:
            await message.answer_sticker(BufferedInputFile(preview_bytes, filename="preview.tgs"))
        except Exception:
            log.warning("preview send failed", exc_info=True)

    n = len(rows)
    user_id = message.from_user.id
    free = is_admin(user_id) or await db.has_free_access(user_id)

    kb = InlineKeyboardBuilder()
    if free:
        kb.row(InlineKeyboardButton(text="🎁 Получить бесплатно", callback_data="shop:getfree"))
        kb.row(InlineKeyboardButton(text="✖️ Отмена", callback_data="shop:paycancel"))
        await message.answer(
            f"Вот как будет выглядеть первый дизайн с текстом «{text}». Всего в "
            f"заказе: {n} шт. — бесплатно для вас 🎁\n\n"
            "Жмите кнопку ниже — сразу пришлю стикер-пак и эмодзи-пак со "
            "всеми выбранными дизайнами и этим текстом.",
            reply_markup=kb.as_markup(),
        )
        return

    kb.row(InlineKeyboardButton(text=f"💳 Оплатить {total}⭐", callback_data="shop:pay"))
    kb.row(InlineKeyboardButton(text="✖️ Отмена", callback_data="shop:paycancel"))
    await answer_stars(
        message,
        f"Вот как будет выглядеть первый дизайн с текстом «{text}». Всего в "
        f"заказе: {n} шт., итого {total}⭐.\n\n"
        "После оплаты пришлю стикер-пак и эмодзи-пак со всеми выбранными "
        "дизайнами и этим текстом.",
        reply_markup=kb.as_markup(),
    )


@router.message(CheckoutStates.waiting_for_text)
async def checkout_wrong_type(message: Message) -> None:
    await message.answer("Пришлите текст сообщением (можно кириллицей). /cancel — отменить.")


@router.message(CheckoutStates.confirm, Command("cancel"))
async def checkout_confirm_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил оформление. /start — вернуться в каталог.")


@router.message(CheckoutStates.confirm)
async def checkout_confirm_reminder(message: Message) -> None:
    await message.answer("Нажмите «Оплатить» или «Отмена» на сообщении выше — либо /cancel.")


@router.callback_query(CheckoutStates.confirm, F.data == "shop:paycancel")
async def cb_pay_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.answer("Ок, отменил. /start — вернуться в каталог.")


@router.callback_query(CheckoutStates.confirm, F.data == "shop:pay")
async def cb_pay(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    item_ids = data.get("checkout_valid_ids") or []
    text = data.get("checkout_text")
    total = data.get("checkout_total") or 0

    if not item_ids or not text or total <= 0:
        await callback.answer("Сессия истекла, начните заново.", show_alert=True)
        await state.clear()
        return

    order_id = await db.create_order(callback.from_user.id, item_ids, text, total)
    await state.clear()
    n = len(item_ids)

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Свой пак ({n} шт.)"[:32],
        description=(
            f"Текст «{text}», {n} дизайнов. После оплаты пришлю стикер-пак "
            "и эмодзи-пак с вашим текстом."
        )[:255],
        payload=f"order:{order_id}",
        currency="XTR",
        prices=[LabeledPrice(label="Оплата", amount=total)],
    )
    await callback.answer()


@router.callback_query(CheckoutStates.confirm, F.data == "shop:getfree")
async def cb_get_free(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user_id = callback.from_user.id
    # Re-check server-side rather than trusting the button shown earlier --
    # access could have been revoked (see /revokefree) between showing
    # this screen and the tap.
    if not (is_admin(user_id) or await db.has_free_access(user_id)):
        await callback.answer(
            "Бесплатный доступ недоступен — оформите обычную оплату через /start.",
            show_alert=True,
        )
        await state.clear()
        return

    data = await state.get_data()
    item_ids = data.get("checkout_valid_ids") or []
    text = data.get("checkout_text")
    total = data.get("checkout_total") or 0

    if not item_ids or not text:
        await callback.answer("Сессия истекла, начните заново.", show_alert=True)
        await state.clear()
        return

    order_id = await db.create_order(user_id, item_ids, text, total, is_free=True)
    order = await db.get_order(order_id)
    await state.clear()
    await callback.answer("Готовлю бесплатно 🎁")
    await _deliver_order(bot, order, chat_id=callback.message.chat.id)


@router.callback_query(F.data.in_({"shop:pay", "shop:paycancel", "shop:getfree"}))
async def cb_pay_stale(callback: CallbackQuery) -> None:
    # Only reached if the state-scoped handlers above didn't match, i.e.
    # the checkout confirmation this button belonged to isn't active
    # anymore (session cleared, or the bot restarted in between).
    await callback.answer("Эта сессия оформления уже не активна — начните заново: /start.", show_alert=True)


@router.pre_checkout_query()
async def on_pre_checkout(pcq: PreCheckoutQuery) -> None:
    payload = pcq.invoice_payload
    order_id = None
    if payload.startswith("order:"):
        try:
            order_id = int(payload.split(":", 1)[1])
        except ValueError:
            order_id = None

    order = await db.get_order(order_id) if order_id is not None else None
    if (
        order is None
        or order["status"] != "pending"
        or order["user_id"] != pcq.from_user.id
        or order["total_stars"] != pcq.total_amount
        or pcq.currency != "XTR"
    ):
        await pcq.answer(ok=False, error_message="Заказ устарел или недоступен — начните заново через /start.")
        return
    await pcq.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    user_id = message.from_user.id
    charge_id = sp.telegram_payment_charge_id

    order_id = None
    if sp.invoice_payload.startswith("order:"):
        try:
            order_id = int(sp.invoice_payload.split(":", 1)[1])
        except ValueError:
            order_id = None

    order = await db.get_order(order_id) if order_id is not None else None
    if order is None:
        log.error("successful_payment with no matching order, payload=%r", sp.invoice_payload)
        await message.answer(
            "Оплата прошла, но я не нашёл ваш заказ — возвращаю оплату. "
            "Попробуйте оформить заново через /start."
        )
        await _refund(bot, user_id, charge_id, order_id_for_log=None)
        return

    await db.set_order_status(order["id"], "paid", charge_id=charge_id)
    order = await db.get_order(order["id"])  # re-fetch so it carries charge_id/status below
    await _deliver_order(bot, order, chat_id=message.chat.id)


async def _deliver_order(bot: Bot, order: dict[str, Any], *, chat_id: int) -> None:
    """Generate the ordered designs, build/extend the buyer's sticker +
    emoji packs in Telegram, mark the order delivered, and message them
    the result. Shared by the real-Stars path (on_successful_payment,
    above) and the free-access path (cb_get_free) -- by the time this
    runs, the order is already charged (real Stars) or explicitly marked
    free (order['is_free']), so the only job left is turning it into
    actual packs. On failure: real orders get refunded (there's a real
    charge to reverse); free orders just get marked failed (there isn't)."""
    user_id = order["user_id"]
    is_free = bool(order["is_free"])
    item_ids = json.loads(order["item_ids"])
    text = order["custom_text"]

    sticker_inputs: list[InputSticker] = []
    emoji_inputs: list[InputSticker] = []
    failed_ids: list[int] = []
    for iid in item_ids:
        row = await db.get_catalog_item(iid)
        if row is None:
            failed_ids.append(iid)
            continue
        try:
            out = _generate_personalized(row, text)
        except Exception:
            log.exception("delivery-time personalization failed for item %s (order %s)", iid, order["id"])
            failed_ids.append(iid)
            continue
        emoji_list = [row["emoji"] or DEFAULT_EMOJI]
        sticker_inputs.append(InputSticker(
            sticker=BufferedInputFile(out, filename="s.tgs"), format="animated", emoji_list=emoji_list,
        ))
        emoji_inputs.append(InputSticker(
            sticker=BufferedInputFile(out, filename="e.tgs"), format="animated", emoji_list=emoji_list,
        ))

    if not sticker_inputs:
        log.error("all items failed to personalize for order %s", order["id"])
        await db.set_order_status(order["id"], "failed", note="generation failed for all items")
        if is_free:
            await bot.send_message(
                chat_id,
                "Не получилось подготовить ни один из выбранных дизайнов, извините 🙏"
            )
        else:
            await bot.send_message(
                chat_id,
                "Оплата прошла, но не получилось подготовить ни один из "
                "выбранных дизайнов — возвращаю оплату, извините 🙏"
            )
            await _refund(bot, user_id, order["charge_id"], order_id_for_log=order["id"])
        return

    bot_username = await sticker_sets.get_bot_username(bot)
    title = text[:MAX_TITLE_LEN]
    on_wait = _make_wait_notifier(bot, chat_id)

    sticker_pack_name = None
    emoji_pack_name = None
    try:
        sticker_pack_name, _added, _skipped, _fids = await sticker_sets.build_or_extend_set(
            bot, user_id, title, sticker_inputs, "regular", bot_username=bot_username, on_wait=on_wait,
        )
    except Exception:
        log.exception("sticker pack build failed for order %s", order["id"])

    try:
        emoji_pack_name, _added, _skipped, _fids = await sticker_sets.build_or_extend_set(
            bot, user_id, title, emoji_inputs, "custom_emoji", bot_username=bot_username, on_wait=on_wait,
        )
    except Exception:
        log.exception("emoji pack build failed for order %s", order["id"])

    if not sticker_pack_name and not emoji_pack_name:
        await db.set_order_status(order["id"], "failed", note="both pack builds failed")
        if is_free:
            await bot.send_message(
                chat_id,
                "Не получилось создать паки в Telegram, извините 🙏 Попробуйте ещё раз чуть позже."
            )
        else:
            await bot.send_message(
                chat_id,
                "Оплата прошла, но не получилось создать паки в Telegram — "
                "возвращаю оплату, извините 🙏 Попробуйте ещё раз чуть позже."
            )
            await _refund(bot, user_id, order["charge_id"], order_id_for_log=order["id"])
        return

    await db.set_order_status(
        order["id"], "delivered",
        sticker_pack_name=sticker_pack_name or "", emoji_pack_name=emoji_pack_name or "",
    )
    await db.clear_cart(user_id)

    lines = [f"Готово! Текст «{text}», {len(sticker_inputs)} шт. 🎉\n"]
    if sticker_pack_name:
        lines.append(f"Стикеры: https://t.me/addstickers/{sticker_pack_name}")
    if emoji_pack_name:
        lines.append(f"Эмодзи: https://t.me/addemoji/{emoji_pack_name}")
    if failed_ids:
        lines.append(
            f"\n⚠️ {len(failed_ids)} шт. не вошли в пак (что-то изменилось с "
            "этими позициями между заказом и оплатой) — напишите об этом "
            "владельцу бота, если хотите их отдельно."
        )
    await bot.send_message(chat_id, "\n".join(lines))


async def _refund(bot: Bot, user_id: int, charge_id: str, *, order_id_for_log: int | None) -> None:
    try:
        await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
        if order_id_for_log is not None:
            await db.set_order_status(order_id_for_log, "refunded")
    except Exception:
        log.exception("refund failed for order %s, charge_id=%s", order_id_for_log, charge_id)


# ---------------------------------------------------------------------------
# Admin: import a source pack into the catalog
# ---------------------------------------------------------------------------

@router.message(Command("importpack"))
async def cmd_importpack(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда только для администратора каталога. Ваш ID: /myid")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        await db.set_setting("catalog_title", parts[1].strip()[:MAX_TITLE_LEN])
    await state.set_state(AdminImport.waiting_for_sticker)
    await message.answer(
        "Пришлите любой стикер из готового стикер-пака (форвардните его "
        "сюда, или откройте пак в Telegram и отправьте один стикер из "
        "него) — импортирую весь пак. Не-анимированные стикеры (PNG/WEBM) "
        "будут пропущены — движок работает только с .tgs.\n/cancel — отменить."
    )


@router.message(AdminImport.waiting_for_sticker, Command("cancel"))
async def cmd_import_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Импорт отменён.")


@router.message(AdminImport.waiting_for_sticker, F.sticker)
async def handle_import_sticker(message: Message, state: FSMContext, bot: Bot) -> None:
    st = message.sticker
    if not st.set_name:
        await message.answer(
            "У этого стикера нет исходного пака — пришлите стикер, который "
            "уже состоит в каком-то стикер-паке (например, форварднутый из "
            "готового набора)."
        )
        return

    await message.answer(f"Загружаю пак «{st.set_name}» от Telegram…")
    try:
        source = await bot.get_sticker_set(name=st.set_name)
    except Exception as e:
        log.exception("get_sticker_set failed")
        await message.answer(f"Не получилось получить пак от Telegram: {e}")
        return

    imported = 0
    skipped_not_animated = 0
    skipped_dupe = 0
    skipped_error = 0

    for s in source.stickers:
        if not s.is_animated:
            skipped_not_animated += 1
            continue
        if await db.item_exists(s.file_unique_id):
            skipped_dupe += 1
            continue
        try:
            tg_file = await bot.get_file(s.file_id)
            buf = await bot.download_file(tg_file.file_path)
            raw = buf.read()
            data = load_tgs_bytes(raw)
        except Exception:
            log.warning("couldn't download/parse sticker during import", exc_info=True)
            skipped_error += 1
            continue

        target_index = None
        demo_bytes = raw
        try:
            modified, report = replace_text(data, PLACEHOLDER_TEXT, target=None)
            candidate_bytes = save_tgs_bytes(modified)
            if len(candidate_bytes) <= MAX_TGS_BYTES:
                demo_bytes = candidate_bytes
                target_index = report["target_index"]
            # else: placeholder text alone already overflows 64KB for this
            # sticker -- keep it importable (demo_bytes stays == raw) but
            # without personalisation; a customer's own text is unlikely to
            # fit either, so this item just won't get a target_index.
        except (TextGroupNotFoundError, ValueError, RuntimeError):
            pass  # no text group / unrenderable placeholder -- import as decorative, as-is
        except Exception:
            log.warning("replace_text failed unexpectedly during import", exc_info=True)

        await db.add_catalog_item(
            source_pack_name=st.set_name,
            file_unique_id=s.file_unique_id,
            emoji=s.emoji or DEFAULT_EMOJI,
            title=None,
            orig_tgs=raw,
            demo_tgs=demo_bytes,
            target_index=target_index,
        )
        imported += 1

    report_lines = [f"Импортировано: {imported}"]
    if skipped_not_animated:
        report_lines.append(f"Пропущено (не анимированные): {skipped_not_animated}")
    if skipped_dupe:
        report_lines.append(f"Пропущено (уже в каталоге): {skipped_dupe}")
    if skipped_error:
        report_lines.append(f"Пропущено (ошибка обработки): {skipped_error}")
    await message.answer("\n".join(report_lines))

    await message.answer("Собираю/дособираю превью-паки в Telegram (это не быстро, Telegram ограничивает частоту запросов)…")
    await _rebuild_template_packs(bot, message.from_user.id, notify_chat_id=message.chat.id)

    links = []
    sp_name = await db.get_setting("sticker_pack_name")
    ep_name = await db.get_setting("emoji_pack_name")
    if sp_name:
        links.append(f"Стикеры (превью): https://t.me/addstickers/{sp_name}")
    if ep_name:
        links.append(f"Эмодзи (превью): https://t.me/addemoji/{ep_name}")
    if links:
        await message.answer("\n".join(links))

    # Price is fixed (see PRICE_STARS) -- apply it straight away instead of
    # asking, to whatever's still unpriced. That's normally just what this
    # message imported, but set_price_where_unset also safely covers any
    # leftover-unpriced items from an earlier, interrupted import (flood
    # control, a dropped connection, whatever) -- re-sending a sticker from
    # the same source pack imports 0 new items (all dupes) but still finds
    # and prices anything an earlier attempt left at 0.
    await state.clear()
    n_priced = await db.set_price_where_unset(PRICE_STARS)
    if n_priced:
        await answer_stars(
            message,
            f"Цена {PRICE_STARS}⭐ применена к {n_priced} позициям без цены. "
            "Каталог теперь открыт всем через /start.\n\n"
            "Изменить цену позже: /setprice <id|all|unpriced> <звёзды>. Список позиций: /catalog.",
        )
    elif imported:
        await message.answer("Все позиции каталога уже с ценой — каталог полностью готов.")
    else:
        await message.answer("Новых позиций не было, но витринные паки и цены уже в порядке.")


@router.message(AdminImport.waiting_for_sticker, F.document)
async def handle_import_wrong_type_doc(message: Message) -> None:
    await message.answer(
        "Пришлите именно стикер (не файл документом) — например, "
        "форвардните стикер из нужного пака, или откройте пак в Telegram и "
        "отправьте сюда один стикер из него."
    )


@router.message(AdminImport.waiting_for_sticker)
async def handle_import_wrong_type(message: Message) -> None:
    await message.answer("Жду стикер из пака, который нужно импортировать. /cancel — отменить.")


def _make_wait_notifier(bot: Bot, chat_id: int | None, *, min_seconds_to_notify: float = 5.0) -> sticker_sets.OnWait | None:
    """Builds an on_wait callback for sticker_sets.build_or_extend_set
    that tells a human what's happening during a flood-control pause,
    instead of the bot just going quiet for however long Telegram asked
    for. Skips trivial (<min_seconds_to_notify) waits, and never sends
    more than one notice every 20s even if several waits happen in a
    row, so a rough patch doesn't turn into a spam of messages."""
    if chat_id is None:
        return None
    state = {"last_notified": 0.0}

    async def on_wait(seconds: float) -> None:
        if seconds < min_seconds_to_notify:
            return
        now = time.monotonic()
        if now - state["last_notified"] < 20:
            return
        state["last_notified"] = now
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        human = f"{mins} мин {secs} сек" if mins else f"{secs} сек"
        try:
            await bot.send_message(
                chat_id,
                f"Telegram попросил подождать ~{human} (ограничение на "
                "частоту создания стикеров) — жду сам, ничего делать не нужно.",
            )
        except Exception:
            log.warning("wait-notify message failed", exc_info=True)

    return on_wait


async def _rebuild_template_packs(bot: Bot, admin_user_id: int, *, notify_chat_id: int | None = None) -> None:
    """(Re)builds the two browsable "template" packs (regular stickers +
    custom emoji, both showing PLACEHOLDER_TEXT) from the full active
    catalog. Only ever adds -- never removes/reorders -- so running this
    again after a later /importpack (or after a previous run got cut off
    by flood control -- see build_or_extend_set) just tops the same two
    packs up with whatever's still missing, up to Telegram's per-type cap
    (see sticker_sets.py). Safe to call with nothing new to add: both
    lists below come out empty and it's a fast no-op.

    "Still missing" is decided per item, from its own
    showcase_sticker_file_id / showcase_emoji_file_id (see db.py's schema
    comment and _backfill_showcase_file_ids) -- NOT by slicing the active
    list against sticker_pack_count/emoji_pack_count. Those two settings
    only ever count "how many stickers this pack has received in total",
    which can end up *larger* than len(active items) once something
    active at add-time later gets hidden with /removeitem (that doesn't
    touch the pack -- the sticker just stays). Slicing by count in that
    case would wrongly treat brand new items as "already covered" and
    silently never add them; checking each item's own file_id instead
    means a later /catalog delete (which clears it again) or a hide/show
    cycle can never throw this off.

    Progress is persisted to the settings table after *every* successful
    sticker (via on_progress below), not just once this whole function
    returns -- a full batch can take a long time if flood control keeps
    interrupting it, and without this, both /stats and a resumed
    /rebuildpacks after an interruption would have no way to know how far
    a still-running or cut-short attempt actually got."""
    await _backfill_showcase_file_ids(bot)
    on_wait = _make_wait_notifier(bot, notify_chat_id)
    items = await db.list_catalog_light(active_only=True)
    title = await db.get_setting("catalog_title", PLACEHOLDER_TEXT)

    sticker_pack_name = await db.get_setting("sticker_pack_name")
    emoji_pack_name = await db.get_setting("emoji_pack_name")
    sticker_have = int(await db.get_setting("sticker_pack_count", "0"))
    emoji_have = int(await db.get_setting("emoji_pack_count", "0"))

    sticker_room = max(0, sticker_sets.MAX_REGULAR_ANIMATED - sticker_have)
    emoji_room = max(0, sticker_sets.MAX_CUSTOM_EMOJI - emoji_have)
    to_add_sticker = [i for i in items if not i["showcase_sticker_file_id"]][:sticker_room]
    to_add_emoji = [i for i in items if not i["showcase_emoji_file_id"]][:emoji_room]

    bot_username = await sticker_sets.get_bot_username(bot)

    async def on_sticker_progress(name: str, count: int) -> None:
        await db.set_setting("sticker_pack_name", name)
        await db.set_setting("sticker_pack_count", str(count))

    async def on_emoji_progress(name: str, count: int) -> None:
        await db.set_setting("emoji_pack_name", name)
        await db.set_setting("emoji_pack_count", str(count))

    if to_add_sticker:
        rows = [await db.get_catalog_item(i["id"]) for i in to_add_sticker]
        rows = [r for r in rows if r is not None]
        inputs = [
            InputSticker(sticker=BufferedInputFile(r["demo_tgs"], filename="s.tgs"),
                         format="animated", emoji_list=[r["emoji"] or DEFAULT_EMOJI])
            for r in rows
        ]
        name, added, _skipped, added_fids = await sticker_sets.build_or_extend_set(
            bot, admin_user_id, title, inputs, "regular",
            existing_name=sticker_pack_name, current_count=sticker_have, bot_username=bot_username,
            on_wait=on_wait, on_progress=on_sticker_progress, keys=[r["id"] for r in rows],
        )
        if name:
            await db.set_setting("sticker_pack_name", name)
            await db.set_setting("sticker_pack_count", str(sticker_have + added))
        for item_id, file_id in added_fids:
            await db.set_showcase_file_id(item_id, "sticker", file_id)

    if to_add_emoji:
        rows = [await db.get_catalog_item(i["id"]) for i in to_add_emoji]
        rows = [r for r in rows if r is not None]
        inputs = [
            InputSticker(sticker=BufferedInputFile(r["demo_tgs"], filename="e.tgs"),
                         format="animated", emoji_list=[r["emoji"] or DEFAULT_EMOJI])
            for r in rows
        ]
        name, added, _skipped, added_fids = await sticker_sets.build_or_extend_set(
            bot, admin_user_id, title, inputs, "custom_emoji",
            existing_name=emoji_pack_name, current_count=emoji_have, bot_username=bot_username,
            on_wait=on_wait, on_progress=on_emoji_progress, keys=[r["id"] for r in rows],
        )
        if name:
            await db.set_setting("emoji_pack_name", name)
            await db.set_setting("emoji_pack_count", str(emoji_have + added))
        for item_id, file_id in added_fids:
            await db.set_showcase_file_id(item_id, "emoji", file_id)


async def _backfill_showcase_file_ids(bot: Bot) -> None:
    """One-time, idempotent recovery for catalog items added to the
    showcase packs before showcase_sticker_file_id/showcase_emoji_file_id
    existed (see db.py's schema comment) -- without this, the /catalog
    delete button would have no file_id to call deleteStickerFromSet with
    for anything imported before this update. Called at the top of
    /catalog, so it's a no-op (one cheap query, no Telegram calls) once
    everything is already filled in.

    Best-effort reconstruction: _rebuild_template_packs only ever
    *appends*, in catalog `position` order, so the first `<pack>_count`
    catalog items by position (across ALL items, active or not -- an item
    can be hidden with /removeitem *after* already being added to a
    pack) should line up 1:1 with the first `<pack>_count` stickers
    currently in that pack. This can only come out wrong if an item was
    hidden and a *later* /importpack ran before this backfill did (which
    changes which items counted as "first N" at build time vs now) --
    an unusual sequence, and one that only affects items imported before
    this update anyway."""
    all_items = await db.list_catalog_light(active_only=False)  # position ASC
    if not all_items:
        return

    for pack_kind, name_key, count_key, col in (
        ("sticker", "sticker_pack_name", "sticker_pack_count", "showcase_sticker_file_id"),
        ("emoji", "emoji_pack_name", "emoji_pack_count", "showcase_emoji_file_id"),
    ):
        name = await db.get_setting(name_key)
        if not name:
            continue
        count = int(await db.get_setting(count_key, "0"))
        if count <= 0:
            continue
        head = all_items[:count]
        if not any(not i[col] for i in head):
            continue  # already fully backfilled for this pack
        try:
            fresh = await bot.get_sticker_set(name=name)
        except Exception:
            log.warning("backfill: get_sticker_set(%s) failed", name, exc_info=True)
            continue
        for item, sticker in zip(head, fresh.stickers):
            if not item[col]:
                await db.set_showcase_file_id(item["id"], pack_kind, sticker.file_id)


# ---------------------------------------------------------------------------
# Admin: catalog management
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "shop:admin")
async def cb_admin(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        "Админ-команды:\n"
        "/importpack [название] — импортировать стикер-пак в каталог.\n"
        "/catalog — пролистать каталог стикерами (◀️▶️), с кнопкой 🗑 "
        "Удалить — уберёт позицию из каталога и из витринных паков в Telegram.\n"
        f"/setprice <id|all|unpriced> <звёзды> — задать цену (по умолчанию "
        f"/importpack сам ставит {PRICE_STARS} Stars на всё новое).\n"
        "/removeitem <id> / /restoreitem <id> — скрыть/вернуть позицию "
        "(без удаления из витринных паков).\n"
        "/rebuildpacks — дособрать витринные паки, если импорт прервался "
        "(например, из-за ограничения Telegram на частоту запросов).\n"
        "/clearcatalog — удалить ВЕСЬ каталог и оба витринных пака целиком, "
        "чтобы загрузить всё заново через /importpack.\n"
        "/freeusers — кто получил бесплатный доступ по промокоду.\n"
        "/revokefree <id> — забрать бесплатный доступ у пользователя.\n"
        "/stats — статистика заказов."
    )


@router.message(Command("rebuildpacks"))
async def cmd_rebuildpacks(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "Проверяю витринные паки против каталога и дособираю недостающее "
        "(если всё уже в порядке — просто подтвержу)…"
    )
    await _rebuild_template_packs(bot, message.from_user.id, notify_chat_id=message.chat.id)

    links = []
    sp_name = await db.get_setting("sticker_pack_name")
    ep_name = await db.get_setting("emoji_pack_name")
    if sp_name:
        links.append(f"Стикеры (превью): https://t.me/addstickers/{sp_name}")
    if ep_name:
        links.append(f"Эмодзи (превью): https://t.me/addemoji/{ep_name}")
    if links:
        await message.answer("Готово.\n" + "\n".join(links))
    else:
        await message.answer("Готово — витринных паков пока нет (каталог пуст).")


@router.message(Command("setprice"))
async def cmd_setprice(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer(
            "Формат: /setprice <id, all или unpriced> <звёзды>\n"
            "all — цена для всех позиций (перезапишет и уже заданные).\n"
            "unpriced — только для позиций без цены (например, после "
            "ручного добавления в каталог в обход /importpack).\n\n"
            f"Обычно не нужна: /importpack сам ставит фиксированную цену "
            f"{PRICE_STARS} Stars на всё новое. Эта команда — для разовых "
            "исключений."
        )
        return
    target, price_s = parts[1], parts[2]
    try:
        price = int(price_s)
    except ValueError:
        await message.answer("Цена должна быть целым числом.")
        return
    if price < 1:
        await answer_stars(message, "Цена должна быть не меньше 1⭐.")
        return

    if target.lower() == "all":
        n = await db.set_all_prices(price)
        await answer_stars(message, f"Цена {price}⭐ выставлена для {n} позиций (включая те, что уже были с ценой).")
        return

    if target.lower() == "unpriced":
        n = await db.set_price_where_unset(price)
        await answer_stars(message, f"Цена {price}⭐ выставлена для {n} позиций без цены (остальные не тронуты).")
        return

    try:
        item_id = int(target)
    except ValueError:
        await message.answer("id должен быть числом (или 'all').")
        return
    ok = await db.set_price(item_id, price)
    await answer_stars(message, f"Готово: #{item_id} → {price}⭐." if ok else f"Не нашёл позицию #{item_id}.")


def _build_admin_card_keyboard(item: dict, idx: int, total: int) -> InlineKeyboardBuilder:
    status = "" if item["active"] else " · 🙈 скрыт"
    label = f"{idx + 1}/{total} · #{item['id']} · {item['emoji']} · {item['price_stars']}⭐{status}"
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="◀️", callback_data=f"shop:ac:{idx - 1}"),
        InlineKeyboardButton(text=label, callback_data="shop:noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"shop:ac:{idx + 1}"),
    )
    kb.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"shop:adel:{item['id']}:{idx}"))
    kb.row(InlineKeyboardButton(text="🏠 Меню", callback_data="shop:menu"))
    return kb


async def _send_admin_item_card(bot: Bot, chat_id: int, item: dict, idx: int, total: int) -> None:
    kb = _build_admin_card_keyboard(item, idx, total)
    await _send_catalog_sticker(bot, chat_id, item, kb.as_markup())


@router.message(Command("catalog"))
async def cmd_catalog(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return
    await _backfill_showcase_file_ids(bot)
    items = await db.list_catalog_light(active_only=False)
    if not items:
        await message.answer("Каталог пуст. /importpack — загрузить пак.")
        return
    full = await db.get_catalog_item(items[0]["id"])
    if full is None:
        await message.answer("Каталог пуст. /importpack — загрузить пак.")
        return
    await _send_admin_item_card(bot, message.chat.id, full, 0, len(items))


@router.callback_query(F.data.startswith("shop:ac:"))
async def cb_admin_view_item(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора.", show_alert=True)
        return
    items = await db.list_catalog_light(active_only=False)
    if not items:
        await callback.answer("Каталог пуст.", show_alert=True)
        return
    idx = int(callback.data.split(":")[2])
    idx = max(0, min(idx, len(items) - 1))
    full = await db.get_catalog_item(items[idx]["id"])
    if full is None:
        await callback.answer("Эта позиция больше недоступна.", show_alert=True)
        return

    await _send_admin_item_card(bot, callback.message.chat.id, full, idx, len(items))
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("shop:adel:"))
async def cb_admin_delete_prompt(callback: CallbackQuery) -> None:
    """First tap on 🗑 Удалить -- swaps the card's keyboard for a yes/no
    confirmation instead of deleting right away, since this is
    irreversible (removes the sticker from the live showcase pack(s),
    which anyone who already added that pack would also see disappear)."""
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора.", show_alert=True)
        return
    item_id_s, idx_s = callback.data.split(":")[2:]
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"shop:adelyes:{item_id_s}:{idx_s}"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data=f"shop:adelno:{item_id_s}:{idx_s}"),
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass
    await callback.answer("Уберёт позицию из каталога и из витринных паков в Telegram", show_alert=True)


@router.callback_query(F.data.startswith("shop:adelno:"))
async def cb_admin_delete_cancel(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора.", show_alert=True)
        return
    item_id_s, idx_s = callback.data.split(":")[2:]
    item_id, idx = int(item_id_s), int(idx_s)
    await callback.answer("Отменено")

    item = await db.get_catalog_item_light(item_id)
    total = await db.count_catalog_items(active_only=False)
    if item is None or total == 0:
        return
    kb = _build_admin_card_keyboard(item, idx, total)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("shop:adelyes:"))
async def cb_admin_delete_confirm(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора.", show_alert=True)
        return
    item_id_s, idx_s = callback.data.split(":")[2:]
    item_id, idx = int(item_id_s), int(idx_s)

    item = await db.get_catalog_item(item_id)
    if item is None:
        await callback.answer("Уже удалено.", show_alert=True)
        return

    warnings: list[str] = []
    if item["showcase_sticker_file_id"]:
        try:
            await bot.delete_sticker_from_set(sticker=item["showcase_sticker_file_id"])
            cur = int(await db.get_setting("sticker_pack_count", "0"))
            await db.set_setting("sticker_pack_count", str(max(0, cur - 1)))
        except Exception:
            log.warning("delete_sticker_from_set (sticker pack) failed for item %s", item_id, exc_info=True)
            warnings.append("не получилось убрать из витринного стикер-пака")
    if item["showcase_emoji_file_id"]:
        try:
            await bot.delete_sticker_from_set(sticker=item["showcase_emoji_file_id"])
            cur = int(await db.get_setting("emoji_pack_count", "0"))
            await db.set_setting("emoji_pack_count", str(max(0, cur - 1)))
        except Exception:
            log.warning("delete_sticker_from_set (emoji pack) failed for item %s", item_id, exc_info=True)
            warnings.append("не получилось убрать из витринного эмодзи-пака")

    await db.delete_catalog_item(item_id)
    await callback.answer("Удалено")

    chat_id = callback.message.chat.id
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    note = f"🗑 Удалено: #{item_id} {item['emoji']}"
    if warnings:
        note += "\n⚠️ " + "; ".join(warnings) + " (сам стикер там мог остаться)"
    await bot.send_message(chat_id, note)

    items = await db.list_catalog_light(active_only=False)
    if not items:
        await bot.send_message(chat_id, "Каталог теперь пуст. /importpack — загрузить пак.")
        return
    idx = max(0, min(idx, len(items) - 1))
    full = await db.get_catalog_item(items[idx]["id"])
    if full is not None:
        await _send_admin_item_card(bot, chat_id, full, idx, len(items))


@router.message(Command("clearcatalog"))
async def cmd_clearcatalog(message: Message) -> None:
    """Bulk counterpart to /catalog's one-at-a-time 🗑 Удалить -- wipes the
    ENTIRE catalog plus both live showcase Telegram packs in one go, so
    the admin can start over with a fresh /importpack instead of clicking
    🗑 on every item individually. Same two-step confirm pattern as the
    single-item delete (cb_admin_delete_prompt/-confirm), just scaled up:
    the warning spells out exactly how many items and which two packs are
    about to disappear, since there's no per-item card to review first."""
    if not is_admin(message.from_user.id):
        return
    total = await db.count_catalog_items(active_only=False)
    sp_name = await db.get_setting("sticker_pack_name")
    ep_name = await db.get_setting("emoji_pack_name")
    if not total and not sp_name and not ep_name:
        await message.answer("Каталог и так пуст. /importpack — загрузить пак.")
        return

    lines = [
        f"⚠️ Это удалит ВСЮ витрину: {total} позиций каталога и оба "
        "витринных пака целиком из Telegram. Необратимо — отменить не "
        "получится, в отличие от /removeitem."
    ]
    if sp_name:
        lines.append(f"Стикеры: https://t.me/addstickers/{sp_name}")
    if ep_name:
        lines.append(f"Эмодзи: https://t.me/addemoji/{ep_name}")
    lines.append("После очистки — сразу /importpack, чтобы загрузить каталог заново.")

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Да, удалить всё", callback_data="shop:clearyes"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data="shop:clearno"),
    )
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data == "shop:clearno")
async def cb_clear_cancel(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора.", show_alert=True)
        return
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text("Отменено, каталог не тронут.")
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "shop:clearyes")
async def cb_clear_confirm(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора.", show_alert=True)
        return
    await callback.answer("Удаляю…")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    # Re-read fresh rather than trusting anything captured when the
    # confirmation prompt was first shown -- mirrors why
    # cb_admin_delete_confirm re-fetches the item instead of trusting the
    # callback payload.
    sp_name = await db.get_setting("sticker_pack_name")
    ep_name = await db.get_setting("emoji_pack_name")

    warnings: list[str] = []
    if sp_name:
        try:
            await bot.delete_sticker_set(name=sp_name)
        except Exception:
            log.warning("delete_sticker_set (showcase sticker pack %s) failed", sp_name, exc_info=True)
            warnings.append(f"не получилось удалить витринный стикер-пак «{sp_name}» в Telegram")
    if ep_name:
        try:
            await bot.delete_sticker_set(name=ep_name)
        except Exception:
            log.warning("delete_sticker_set (showcase emoji pack %s) failed", ep_name, exc_info=True)
            warnings.append(f"не получилось удалить витринный эмодзи-пак «{ep_name}» в Telegram")

    deleted_n = await db.clear_catalog()
    for key in ("sticker_pack_name", "emoji_pack_name", "sticker_pack_count", "emoji_pack_count"):
        await db.delete_setting(key)

    note = f"🗑 Каталог очищен: удалено позиций — {deleted_n}, витринные паки удалены."
    if warnings:
        note += ("\n⚠️ " + "; ".join(warnings) +
                 " (сами стикеры там могли остаться, но каталог их больше не знает "
                 "-- при желании уберите пак(и) вручную через @Stickers)")
    note += "\n\n/importpack — загрузить каталог заново."
    await bot.send_message(callback.message.chat.id, note)


async def _toggle_active(message: Message, active: bool) -> None:
    parts = message.text.split()
    if len(parts) != 2:
        cmd = "/removeitem" if not active else "/restoreitem"
        await message.answer(f"Формат: {cmd} <id>")
        return
    try:
        item_id = int(parts[1])
    except ValueError:
        await message.answer("id должен быть числом.")
        return
    ok = await db.set_active(item_id, active)
    await message.answer("Готово." if ok else f"Не нашёл позицию #{item_id}.")


@router.message(Command("removeitem"))
async def cmd_removeitem(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await _toggle_active(message, active=False)


@router.message(Command("restoreitem"))
async def cmd_restoreitem(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await _toggle_active(message, active=True)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    s = await db.stats()
    total_active = await db.count_catalog_items(active_only=True)
    sticker_have = int(await db.get_setting("sticker_pack_count", "0"))
    emoji_have = int(await db.get_setting("emoji_pack_count", "0"))
    sticker_target = min(total_active, sticker_sets.MAX_REGULAR_ANIMATED)
    emoji_target = min(total_active, sticker_sets.MAX_CUSTOM_EMOJI)

    await answer_stars(
        message,
        f"Заказов оплачено: {s['paid_orders']}\n"
        f"Доставлено: {s['delivered_orders']}\n"
        f"Выручка (по доставленным): {s['revenue_stars']}⭐\n"
        f"Бесплатных заказов (промо/админ): {s['free_delivered']}\n"
        f"Позиций в каталоге: {s['catalog_items']}\n\n"
        f"Витринный стикер-пак: {sticker_have}/{sticker_target}\n"
        f"Витринный эмодзи-пак: {emoji_have}/{emoji_target}\n"
        + ("(меньше, чем в каталоге -- /rebuildpacks ещё не закончил или его стоит запустить)"
           if sticker_have < sticker_target or emoji_have < emoji_target else
           "(всё собрано)")
    )


@router.message(Command("freeusers"))
async def cmd_freeusers(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    rows = await db.list_free_access()
    if not rows:
        await message.answer("Пока никто не активировал /promo.")
        return
    lines = [f"#{r['user_id']}" + (f" (@{r['note']})" if r["note"] else "") for r in rows]
    await message.answer(
        f"Бесплатный доступ есть у {len(rows)}:\n" + "\n".join(lines) +
        "\n\n/revokefree <id> — забрать доступ."
    )


@router.message(Command("revokefree"))
async def cmd_revokefree(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: /revokefree <telegram id>")
        return
    target_id = int(parts[1])
    ok = await db.revoke_free_access(target_id)
    await message.answer(
        f"Готово, забрал бесплатный доступ у #{target_id}." if ok
        else f"У #{target_id} и так не было бесплатного доступа."
    )
