"""
Telegram-бот: редактор текста в .tgs стикерах/премиум-эмодзи, и сборщик
собственных паков премиум-эмодзи из готовых стикеров.

Редактирование текста: пользователь присылает .tgs файл, форвардит
анимированный стикер, присылает премиум-эмодзи как отдельное сообщение или
просто печатает его в тексте — бот сам находит в файле векторную группу с
текстом (разбирая геометрию фигур: где несколько контуров сидят на одной
строке — а не по имени слоя, которого чаще всего просто нет), спрашивает
новый текст, перерисовывает его шрифтом (с сохранением позиции/размера/
поворота/анимации исходной надписи) и присылает готовый .tgs обратно.

Премиум-эмодзи технически устроены так же, как обычные анимированные
стикеры: тот же .tgs (gzip + Lottie JSON), тот же холст 512x512, тот же
лимит в 64 КБ — Telegram использует отдельный формат 100x100 только для
статичных/видео кастомных эмодзи, не для анимированных. Поэтому движок
(tgs_editor.py) вообще не различает их — разница только в том, как бот
достаёт файл из сообщения.

Сборка пака (/newpack): пользователь присылает готовые анимированные
стикеры один за другим (например, форвардит их прямо из существующего
стикерпака — можно и результат редактирования текста выше), каждый
становится отдельным кастомным эмодзи в новом наборе, который бот создаёт
и пополняет через Bot API (createNewStickerSet/addStickerToSet,
sticker_type=custom_emoji). /donepack — получить ссылку t.me/addemoji/...
для добавления пака себе.

Запуск:
    export TGS_BOT_TOKEN="ваш_токен_от_BotFather"
    python bot.py
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, BufferedInputFile, InputSticker

from pack_builder import extract_emoji, DEFAULT_EMOJI, MAX_TITLE_LEN
from tgs_editor import (
    load_tgs_bytes,
    save_tgs_bytes,
    replace_text,
    locate_text_group,
    describe_candidate,
    TextGroupNotFoundError,
    MAX_TGS_BYTES,
)

import db
import sticker_sets
from shop import router as shop_router, is_admin, _backfill_showcase_file_ids

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# TGS_BOT_TOKEN takes precedence when set (see .env.example); the literal
# value below only exists so the bot still runs out of the box without any
# setup. If you're reading this in a checkout of the project: that token is
# not a secret anymore (it shipped in the repo) -- regenerate it with
# @BotFather -> /mybots -> your bot -> API Token -> Revoke, and set the new
# one via TGS_BOT_TOKEN instead of editing this file again.
BOT_TOKEN = os.getenv("TGS_BOT_TOKEN", "8436428951:AAGhJmYU2LuRPHM6KJi2a21p6UTyVKKs0mM")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tgs_logo_bot")

router = Router()


class EditStates(StatesGroup):
    waiting_for_text = State()


class PackStates(StatesGroup):
    waiting_for_title = State()
    collecting = State()


HELP_TEXT = (
    "/start — каталог готовых стикеров и премиум-эмодзи: выберите дизайн "
    "(или сразу несколько), подпишите своим текстом и получите готовый "
    "стикер-пак + эмодзи-пак — оплата в Telegram Stars ⭐.\n\n"
    "/myid — узнать свой Telegram ID."
)

HELP_TEXT_ADMIN_EXTRA = (
    "\n\n— инструменты администратора —\n\n"
    "Кроме каталога есть два ручных инструмента для .tgs стикеров и "
    "премиум-эмодзи (один и тот же формат — анимированная векторная "
    "графика Telegram), оба доступны только вам:\n\n"
    "① Менять текст на стикере\n"
    "1. Пришлите .tgs файл как документ, перешлите анимированный стикер, "
    "пришлите премиум-эмодзи отдельным сообщением или просто напечатайте "
    "его в тексте.\n"
    "2. Я сам найду в файле векторную надпись (по геометрии фигур — не "
    "важно, как называется слой) и покажу, что нашёл.\n"
    "3. Пришлите новый текст (можно кириллицей и латиницей).\n"
    "4. Получите обратно готовый .tgs — форма, размер, поворот и вся "
    "остальная анимация стикера не меняются, меняются только сами буквы.\n\n"
    "/target <номер> или /target <имя> — если я нашёл не ту надпись, "
    "выберите нужную из списка (номер показываю при получении файла). Если "
    "файл уже загружен в этом диалоге, применю выбор сразу же — повторно "
    "присылать его не нужно.\n"
    "/target без аргумента — сбросить выбор и снова искать автоматически.\n"
    "/cancel — отменить текущее редактирование.\n\n"
    "② Собрать свой пак премиум-эмодзи из готовых стикеров\n"
    "1. /newpack Название — начать новый пак (без названия просто спрошу).\n"
    "2. Присылайте стикеры один за другим — форвардите их из любого "
    "стикерпака, или используйте то, что отредактировали в ①. Эмодзи для "
    "поиска возьму из самого стикера, если он из готового пака.\n"
    "3. /donepack — закончить и получить ссылку t.me/addemoji/... для "
    "добавления пака себе. Отправлять эти эмодзи в сообщениях смогут "
    "только пользователи с Telegram Premium — так работает Telegram.\n\n"
    "/newpack [название] — начать сборку пака премиум-эмодзи.\n"
    "/donepack — закончить сборку пака и получить ссылку.\n"
    "/cancelpack — отменить сборку пака (удалит уже созданный набор в "
    "Telegram, если он есть).\n"
    "/cancel тоже отменяет сборку пака, если она сейчас идёт."
)


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = HELP_TEXT
    if is_admin(message.from_user.id):
        text += HELP_TEXT_ADMIN_EXTRA
    await message.answer(text)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await _cancel_pack_if_any(message, state):
        return
    await state.clear()
    await message.answer("Окей, отменил. /start — открыть каталог.")


@router.message(Command("target"))
async def cmd_target(message: Message, state: FSMContext):
    """Change which text group to target. If a .tgs is already loaded in
    this dialog (state has tgs_bytes from a prior upload), re-locate and
    apply the new target to it right away and report exactly what was
    picked -- no resend required. This used to only remember the hint for
    the *next* uploaded file, which was confusing: the bot would confirm
    the choice immediately, but nothing actually changed until the user
    resent the file, and if they didn't (or resent the wrong thing), the
    old target silently stayed in effect.

    Admin-only, like the rest of manual mode -- a regular user can never
    have a file loaded in this dialog (handle_incoming_file/handle_custom_
    emoji_message already refuse them), so this would be a no-op for them
    anyway; refusing up front is just a clearer answer than a silent hint
    that never applies to anything."""
    if not is_admin(message.from_user.id):
        await message.answer(
            "Ручной режим (и /target вместе с ним) доступен только "
            "администратору. Хотите стикер или эмодзи со своим текстом — "
            "загляните в каталог: /start"
        )
        return
    parts = message.text.split(maxsplit=1)
    fsm_data = await state.get_data()
    raw = fsm_data.get("tgs_bytes")

    if len(parts) < 2 or not parts[1].strip():
        await state.update_data(target_hint=None)
        if raw is None:
            await message.answer(
                "Сбросил ручной выбор — снова буду искать текст автоматически.\n"
                "Чтобы указать место самому, пришлите номер из списка кандидатов "
                "(показываю его при получении файла) или имя группы, например:\n"
                "/target 2\nили\n/target mylogo"
            )
            return
        data = load_tgs_bytes(raw)
        try:
            candidates, idx = locate_text_group(data, hint=None)
        except TextGroupNotFoundError as e:
            await message.answer(f"Сбросил ручной выбор, но {e}")
            return
        await state.set_state(EditStates.waiting_for_text)
        status = _describe_selection(candidates, idx, None)
        await message.answer(f"Сбросил ручной выбор, {status}\n\nКаким текстом её заменить?")
        return

    value = parts[1].strip()

    if raw is None:
        # No file in this dialog yet -- remember the hint for when one arrives.
        await state.update_data(target_hint=value)
        await message.answer(
            f"Хорошо, при следующей обработке буду ориентироваться на «{value}». "
            "Теперь пришлите .tgs файл, стикер или премиум-эмодзи."
        )
        return

    # A file is already loaded in this dialog -- validate and apply right now.
    data = load_tgs_bytes(raw)
    try:
        candidates, idx = locate_text_group(data, hint=value)
    except TextGroupNotFoundError as e:
        if e.candidates:
            listing = "\n".join(
                describe_candidate(c, i) for i, c in enumerate(e.candidates, start=1)
            )
            await message.answer(
                f"«{value}» не подходит — такой группы в загруженном файле нет.\n"
                f"Вот доступные варианты:\n{listing}\n\n"
                "Пришлите /target с другим номером или именем."
            )
        else:
            await message.answer(f"«{value}» не подходит: {e}")
        return

    await state.update_data(target_hint=value)
    await state.set_state(EditStates.waiting_for_text)
    status = _describe_selection(candidates, idx, value)
    await message.answer(f"Готово: {status}\n\nКаким текстом её заменить?")


async def _cancel_pack_if_any(message: Message, state: FSMContext) -> bool:
    """If a pack-building session (either state under PackStates) is active,
    end it -- best-effort deleting any sticker set already created on
    Telegram so a cancelled pack doesn't linger as an orphaned, empty-ish
    set under the user's name -- and report what happened. Returns False
    (without touching state or replying) when there's no pack session, so
    callers like cmd_cancel can fall back to their own unrelated behaviour."""
    current_state = await state.get_state()
    if current_state not in (PackStates.waiting_for_title.state, PackStates.collecting.state):
        return False
    fsm_data = await state.get_data()
    pack_name = fsm_data.get("pack_name")
    await state.clear()
    if not pack_name:
        await message.answer("Отменил, пак ещё не был создан в Telegram — удалять нечего.")
        return True
    try:
        await message.bot.delete_sticker_set(name=pack_name)
        await message.answer(f"Пак отменён, набор «{pack_name}» удалён из Telegram.")
    except Exception:
        log.warning("delete_sticker_set failed", exc_info=True)
        await message.answer(
            f"Отменил на своей стороне, но не смог удалить уже созданный набор "
            f"«{pack_name}» в Telegram — при необходимости удалите его вручную "
            "через @Stickers (/deletestickerpack на самом наборе)."
        )
    return True


@router.message(Command("newpack"))
async def cmd_newpack(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору бота.")
        return
    parts = message.text.split(maxsplit=1)
    title = parts[1].strip() if len(parts) > 1 else ""
    if title:
        await _start_pack_collecting(message, state, title)
    else:
        await state.set_state(PackStates.waiting_for_title)
        await message.answer(
            "Как назвать пак? Это заголовок, который увидят пользователи — "
            "можно кириллицей, техническое имя для ссылки сделаю сам.\n\n"
            "Или сразу одной командой: /newpack Название пака"
        )


@router.message(PackStates.waiting_for_title, F.text)
async def handle_pack_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        await message.answer("Название пустое, напишите хотя бы одну букву.")
        return
    await _start_pack_collecting(message, state, title)


async def _start_pack_collecting(message: Message, state: FSMContext, title: str) -> None:
    title = title[:MAX_TITLE_LEN]
    await state.update_data(pack_title=title, pack_name=None, pack_count=0)
    await state.set_state(PackStates.collecting)
    await message.answer(
        f"Пак «{title}». Присылайте анимированные стикеры один за другим — "
        "форвардите их из любого стикерпака (подойдут и премиум-эмодзи, и "
        "то, что отредактировали здесь же) — каждый станет отдельным "
        "кастомным эмодзи. Эмодзи для поиска возьму из самого стикера, если "
        "он из готового пака; если нет — пришлите его в подписи к файлу или "
        "просто отдельным сообщением рядом.\n\n"
        "/donepack — закончить и получить ссылку\n"
        "/cancelpack — отменить (и удалить уже созданное в Telegram)"
    )


@router.message(Command("donepack"))
async def cmd_donepack(message: Message, state: FSMContext):
    current_state = await state.get_state()
    fsm_data = await state.get_data()
    pack_name = fsm_data.get("pack_name")
    if current_state != PackStates.collecting.state or not pack_name:
        await message.answer(
            "Сейчас нет пака с добавленными эмодзи — нечего заканчивать. "
            "/newpack, чтобы начать."
        )
        return
    title = fsm_data.get("pack_title", "")
    count = fsm_data.get("pack_count", 0)
    await state.clear()
    await message.answer(
        f"Готово! «{title}», эмодзи в паке: {count}.\n"
        f"Добавить себе: https://t.me/addemoji/{pack_name}\n\n"
        "Откройте ссылку в Telegram и нажмите «Добавить набор». Видеть эти "
        "эмодзи смогут все, а вот отправлять их в сообщениях — только "
        "пользователи с Telegram Premium (так работают кастомные эмодзи в "
        "Telegram, это не ограничение бота)."
    )


@router.message(Command("cancelpack"))
async def cmd_cancelpack(message: Message, state: FSMContext):
    if not await _cancel_pack_if_any(message, state):
        await message.answer("Сейчас нет пака в работе — нечего отменять.")


async def _download_tgs_bytes(message: Message) -> tuple[bytes, str] | None:
    """Extracts raw .tgs bytes from a document or an animated sticker/custom
    emoji message. Returns (bytes, source_filename) or None (already replied
    with an error)."""
    bot = message.bot

    if message.document:
        doc = message.document
        name = doc.file_name or "sticker.tgs"
        is_tgs_name = name.lower().endswith(".tgs")
        is_tgs_mime = (doc.mime_type or "") in ("application/x-tgsticker", "application/gzip")
        if not (is_tgs_name or is_tgs_mime):
            await message.answer(
                "Это не похоже на .tgs файл. Пришлите файл с расширением .tgs "
                "(подходит и обычный анимированный стикер, и премиум-эмодзи — "
                "у них один и тот же формат)."
            )
            return None
        file_id = doc.file_id

    elif message.sticker:
        st = message.sticker
        kind = "премиум-эмодзи" if getattr(st, "type", None) == "custom_emoji" else "стикер"
        if not st.is_animated:
            await message.answer(
                f"Это статичный или видео {kind}. Нужен именно анимированный "
                "(.tgs)."
            )
            return None
        name = "sticker.tgs"
        file_id = st.file_id

    else:
        return None

    tg_file = await bot.get_file(file_id)
    buf = await bot.download_file(tg_file.file_path)
    return buf.read(), name


def _has_custom_emoji(message: Message) -> bool:
    return any(e.type == "custom_emoji" for e in (message.entities or []))


async def _download_custom_emoji_bytes(message: Message) -> tuple[bytes, str, str | None] | None:
    """Resolves inline custom_emoji entities (premium emoji typed straight
    into a text message) to a .tgs file via getCustomEmojiStickers. Returns
    (bytes, source_filename, source_emoji) or None (already replied with an
    error). source_emoji is whatever single emoji Telegram associates with
    that custom emoji already (may be None) -- used by pack mode so a
    re-packed premium emoji keeps a sensible search emoji without asking."""
    ids = [e.custom_emoji_id for e in message.entities if e.type == "custom_emoji" and e.custom_emoji_id]
    if not ids:
        return None

    try:
        stickers = await message.bot.get_custom_emoji_stickers(custom_emoji_ids=ids[:1])
    except Exception:
        log.warning("get_custom_emoji_stickers failed", exc_info=True)
        await message.answer("Не получилось получить файл этого эмодзи от Telegram.")
        return None
    if not stickers:
        await message.answer("Telegram не вернул данные по этому эмодзи.")
        return None

    st = stickers[0]
    if not st.is_animated:
        await message.answer(
            "Это статичный или видео премиум-эмодзи (PNG/WebP/WebM). "
            "Нужен именно анимированный — в формате .tgs."
        )
        return None

    tg_file = await message.bot.get_file(st.file_id)
    buf = await message.bot.download_file(tg_file.file_path)
    extra = f" (и ещё {len(ids) - 1}, обработаю только первый)" if len(ids) > 1 else ""
    if extra:
        await message.answer(f"Нашёл несколько эмодзи в сообщении{extra}.")
    return buf.read(), "custom_emoji.tgs", st.emoji


def _describe_selection(candidates, idx, target_hint) -> str:
    """Human-readable status for which text group is currently selected,
    including alternatives when auto-picked. Shared between the initial
    file upload and a /target correction applied to an already-loaded
    file, so both paths always report the *actual* resolved candidate
    instead of a generic "ok" that isn't backed by the real selection."""
    chosen = candidates[idx]
    x0, y0, x1, y1 = chosen['bbox']
    w, h = x1 - x0, y1 - y0
    stats = f"~{w:.0f}×{h:.0f}, {chosen['n_paths']} контур(ов)"
    if target_hint is None:
        msg = f"нашёл текст автоматически ✅ ({stats})."
        if len(candidates) > 1:
            alts = "\n".join(
                describe_candidate(c, i) for i, c in enumerate(candidates[:4], start=1)
            )
            msg += (
                "\n\nЕсли это не та надпись — вот похожие места в файле, "
                f"выберите /target <номер>:\n{alts}"
            )
    else:
        msg = f"использую указанную группу «{target_hint}» ✅ ({stats})."
    return msg


async def _process_incoming_source(message: Message, state: FSMContext, raw: bytes):
    """Shared logic for any newly-received .tgs source (document, forwarded
    sticker, or resolved custom emoji): unpack, auto-locate the text group,
    remember it, and tell the user what was found."""
    try:
        data = load_tgs_bytes(raw)
    except Exception:
        await message.answer(
            "Не получилось распаковать файл — это не gzip-сжатый Lottie-JSON "
            "(некорректный .tgs)."
        )
        return

    # Remember the bytes as soon as we know they're a valid .tgs -- *before*
    # attempting to resolve any hint. This is what lets /target correct an
    # already-loaded file immediately (see cmd_target): even if the current
    # hint fails to resolve below, the file itself is already saved, so the
    # very next /target attempt can apply without asking for a resend.
    await state.update_data(tgs_bytes=raw)

    fsm_data = await state.get_data()
    target_hint = fsm_data.get("target_hint")

    try:
        candidates, idx = locate_text_group(data, hint=target_hint)
    except TextGroupNotFoundError as e:
        if e.candidates:
            listing = "\n".join(
                describe_candidate(c, i) for i, c in enumerate(e.candidates, start=1)
            )
            await message.answer(
                f"Не нашёл «{target_hint}» в этом файле.\n"
                f"Вот что похоже на текст:\n{listing}\n\n"
                "Укажите номер или имя командой /target, например /target 2."
            )
        else:
            await message.answer(
                "Не нашёл в этом файле ни одной векторной группы, похожей на "
                "текст (возможно, надпись растровая — картинкой, а не "
                "фигурами, — либо в этом стикере/эмодзи вообще нет лого)."
            )
        return

    await state.set_state(EditStates.waiting_for_text)
    status = _describe_selection(candidates, idx, target_hint)
    await message.answer(f"Файл получен, {status}\n\nКаким текстом её заменить?")


def _pick_emoji_for_pack(message: Message) -> list[str]:
    """Best-effort emoji_list for a sticker being added to a pack: prefer
    the emoji Telegram already associates with the *source* sticker (set
    by whoever made the original pack -- exactly right for "forward me a
    sticker from an existing pack"), then any emoji typed in a caption
    alongside the file, then a generic placeholder so the call never fails
    for lack of emoji_list (Telegram requires at least one)."""
    if message.sticker and message.sticker.emoji:
        return [message.sticker.emoji]
    found = extract_emoji(message.caption)
    return found if found else [DEFAULT_EMOJI]


async def _add_sticker_to_pack(message: Message, state: FSMContext, raw: bytes, emoji_list: list[str]) -> None:
    """Shared tail end of both pack-mode content handlers below: validate
    size, create the set on the first sticker or add to it on every one
    after, and report progress. Never raises -- all Telegram/network
    failures are caught and reported so one bad sticker doesn't end the
    whole collecting session."""
    if len(raw) > MAX_TGS_BYTES:
        await message.answer(
            f"Этот файл весит {len(raw)} байт — больше лимита Telegram в "
            f"{MAX_TGS_BYTES} байт для .tgs, эмодзи из него не сделать. "
            "Можно прислать другой, или /donepack чтобы закончить с тем, что уже есть."
        )
        return

    fsm_data = await state.get_data()
    title = fsm_data.get("pack_title") or "Emoji pack"
    pack_name = fsm_data.get("pack_name")
    count = fsm_data.get("pack_count", 0)
    bot = message.bot
    user_id = message.from_user.id

    input_sticker = InputSticker(
        sticker=BufferedInputFile(raw, filename="emoji.tgs"),
        format="animated",
        emoji_list=emoji_list[:20],
    )

    try:
        if pack_name is None:
            bot_username = await sticker_sets.get_bot_username(bot)
            pack_name = await sticker_sets.create_set_with_retry(
                bot, user_id, title, input_sticker, "custom_emoji", bot_username
            )
        else:
            await bot.add_sticker_to_set(user_id=user_id, name=pack_name, sticker=input_sticker)
    except TelegramBadRequest as e:
        log.warning("sticker pack API call failed: %s", e)
        await message.answer(
            f"Telegram отклонил этот эмодзи: {getattr(e, 'message', None) or e}\n"
            "Можно прислать следующий, или /donepack чтобы закончить с тем, что уже есть."
        )
        return
    except Exception:
        log.exception("sticker pack API call failed")
        await message.answer(
            "Не получилось добавить этот эмодзи — техническая ошибка на моей "
            "стороне. Можно прислать следующий, или /donepack чтобы закончить."
        )
        return

    count += 1
    await state.update_data(pack_name=pack_name, pack_count=count)
    await message.answer(
        f"Добавлено ({count}/200) {' '.join(emoji_list[:3])} ✅\n"
        "Ещё стикер — или /donepack, чтобы получить ссылку на пак."
    )


@router.message(PackStates.collecting, F.document | F.sticker)
async def handle_pack_sticker(message: Message, state: FSMContext):
    result = await _download_tgs_bytes(message)
    if result is None:
        return
    raw, _name = result
    await _add_sticker_to_pack(message, state, raw, _pick_emoji_for_pack(message))


@router.message(PackStates.collecting, _has_custom_emoji)
async def handle_pack_custom_emoji(message: Message, state: FSMContext):
    result = await _download_custom_emoji_bytes(message)
    if result is None:
        return
    raw, _name, source_emoji = result
    emoji_list = [source_emoji] if source_emoji else (extract_emoji(message.text) or [DEFAULT_EMOJI])
    await _add_sticker_to_pack(message, state, raw, emoji_list)


@router.message(PackStates.collecting, F.text)
async def handle_pack_text_reminder(message: Message):
    await message.answer(
        "Сейчас собираю пак — присылайте анимированные стикеры/эмодзи файлами, "
        "не текстом. /donepack — закончить, /cancelpack — отменить."
    )


@router.message(F.document | F.sticker)
async def handle_incoming_file(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "Ручная правка текста доступна только администратору бота. "
            "Хотите стикер или эмодзи со своим текстом — загляните в "
            "каталог: /start"
        )
        return
    result = await _download_tgs_bytes(message)
    if result is None:
        return
    raw, _name = result
    await _process_incoming_source(message, state, raw)


@router.message(_has_custom_emoji)
async def handle_custom_emoji_message(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "Ручная правка текста доступна только администратору бота. "
            "Хотите стикер или эмодзи со своим текстом — загляните в "
            "каталог: /start"
        )
        return
    result = await _download_custom_emoji_bytes(message)
    if result is None:
        return
    raw, _name, _emoji = result
    await _process_incoming_source(message, state, raw)


@router.message(EditStates.waiting_for_text, F.text)
async def handle_new_text(message: Message, state: FSMContext):
    new_text = message.text.strip()
    if not new_text:
        await message.answer("Текст пустой, пришлите хотя бы одну букву.")
        return

    fsm_data = await state.get_data()
    raw = fsm_data.get("tgs_bytes")
    target_hint = fsm_data.get("target_hint")
    if raw is None:
        await state.clear()
        await message.answer("Сессия потерялась, пришлите файл заново.")
        return

    data = load_tgs_bytes(raw)
    try:
        modified, report = replace_text(data, new_text, target=target_hint)
    except TextGroupNotFoundError as e:
        await message.answer(f"Не получилось найти группу для замены: {e}")
        await state.clear()
        return
    except Exception as e:
        log.exception("replace_text failed")
        await message.answer(f"Не получилось сгенерировать текст: {e}")
        return

    out_bytes = save_tgs_bytes(modified)
    if len(out_bytes) > MAX_TGS_BYTES:
        await message.answer(
            f"Готовый файл весит {len(out_bytes)} байт — это больше лимита "
            f"Telegram в {MAX_TGS_BYTES} байт для .tgs. Попробуйте текст короче."
        )
        return

    doc_file = BufferedInputFile(out_bytes, filename="edited.tgs")
    await message.answer_document(
        doc_file,
        caption=(
            f"Готово! «{new_text}», {len(out_bytes)} байт "
            f"(лимит Telegram — {MAX_TGS_BYTES})."
        ),
    )

    try:
        sticker_file = BufferedInputFile(out_bytes, filename="edited.tgs")
        await message.answer_sticker(sticker_file)
    except Exception:
        log.warning("Couldn't send live sticker preview", exc_info=True)

    await state.clear()


@router.message(EditStates.waiting_for_text)
async def handle_new_text_wrong_type(message: Message):
    await message.answer("Пришлите новый текст сообщением (просто текстом).")


@router.message()
async def handle_fallback(message: Message):
    if is_admin(message.from_user.id):
        await message.answer(
            "/start — каталог готовых стикеров и эмодзи. Или пришлите .tgs "
            "файл, перешлите анимированный стикер, или пришлите премиум-эмодзи "
            "для ручного режима. /help — подробная инструкция."
        )
    else:
        await message.answer("/start — каталог готовых стикеров и премиум-эмодзи. /help — подробнее.")


async def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан токен бота. Установите переменную окружения "
            "TGS_BOT_TOKEN (или впишите её в файл .env) — "
            "токен выдаёт @BotFather в Telegram."
        )
    await db.init()
    bot = Bot(token=BOT_TOKEN)
    try:
        # Warms up showcase_sticker_file_id/showcase_emoji_file_id for any
        # catalog items that predate those columns, so the very first
        # /start or /catalog after a deploy sends by file_id instead of
        # uploading raw bytes -- see shop.py's _send_catalog_sticker and
        # _backfill_showcase_file_ids. Best-effort: if this fails (e.g. a
        # transient network hiccup on startup), catalog browsing still
        # works either way, just falls back to a real upload the first
        # time each item is shown until a later /catalog re-attempts it.
        await _backfill_showcase_file_ids(bot)
    except Exception:
        log.warning("startup showcase file_id backfill failed, continuing anyway", exc_info=True)
    dp = Dispatcher(storage=MemoryStorage())
    # shop_router first: it owns /start plus every shop-specific state and
    # callback_query, and only falls through to `router` (manual text-edit
    # mode + /newpack, including its own catch-all at the very end) for
    # anything it doesn't claim. See shop.py's module docstring.
    dp.include_router(shop_router)
    dp.include_router(router)
    log.info("Bot starting (polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
