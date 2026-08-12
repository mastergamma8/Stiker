"""
pack_builder.py

Pure, Telegram-API-independent helpers for turning a user-supplied pack
title into a valid sticker-set "short name", and for picking search emoji
for a sticker being added to a pack. No aiogram import here on purpose --
same reasoning as tgs_editor.py being kept separate from bot.py: the parts
that don't need a live bot connection stay easy to test on their own.

Telegram's rules for a sticker-set short name (see createNewStickerSet,
https://core.telegram.org/bots/api#createnewstickerset):
  - 1-64 characters
  - only English letters, digits and underscores
  - must begin with a letter
  - can't contain consecutive underscores
  - must end in "_by_<bot_username>" (case-insensitive)
  - must be globally unique across all of Telegram, not just per-user/bot

A human-typed title is very often Cyrillic, contains spaces/punctuation, and
is not unique -- three separate problems a short name generator has to
solve at once. generate_set_name() below transliterates, strips anything
not allowed, and appends the owner's user id plus (from the second attempt
on) a random suffix, so repeated collisions on a popular title like
"Мой лого" can be retried without asking the user for a different name.
"""
import random
import re
import unicodedata

MAX_NAME_LEN = 64
MAX_TITLE_LEN = 64

_TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    'і': 'i', 'ї': 'yi', 'є': 'ye', 'ґ': 'g',  # Ukrainian extras, harmless extra coverage
}


def slugify(text: str, max_len: int = 24) -> str:
    """Transliterate/strip `text` down to only what a sticker-set short
    name may contain (English letters + digits; underscores are added by
    the caller, not here), lowercase, truncated to `max_len`. Falls back to
    a generic non-empty stem if nothing usable survives (e.g. an all-emoji
    or all-punctuation title), since the result must be able to start a
    name that begins with a letter."""
    out = []
    for ch in text.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
        # anything else (spaces, punctuation, emoji, other scripts) is
        # simply dropped rather than mapped to '_', to avoid runs of
        # consecutive underscores once the caller joins name parts
    slug = ''.join(out)[:max_len]
    if not slug or not slug[0].isalpha():
        slug = 'pack' + slug
    return slug


def generate_set_name(title: str, user_id: int, bot_username: str, attempt: int = 0) -> str:
    """One candidate short name for createNewStickerSet, in
    '<slug>_<uid>[_<rand>]_by_<bot_username>' form. `attempt` selects a
    different candidate on retry after a SHORTNAME/occupied conflict --
    call again with attempt=1, 2, ... until create_new_sticker_set
    succeeds. bot_username is used as-is (Telegram compares it
    case-insensitively, so no need to lowercase it here)."""
    bot_username = bot_username.lstrip('@')
    suffix = str(user_id % 1_000_000)
    if attempt > 0:
        suffix += f"{random.randint(100, 999)}"
    tail = f"_{suffix}_by_{bot_username}"
    base = slugify(title, max_len=max(1, MAX_NAME_LEN - len(tail)))
    name = f"{base}{tail}"
    return name[:MAX_NAME_LEN]


_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0001F000-\U0001F0FF"
    "\U0000FE0F"
    "]",
    flags=re.UNICODE,
)

DEFAULT_EMOJI = "⭐"


def extract_emoji(text: str | None, limit: int = 20) -> list[str]:
    """Pull up to `limit` distinct emoji characters out of free text (e.g.
    a message caption), preserving order of first appearance. Used as a
    fallback source for a sticker's emoji_list when there's no better
    signal (see _pick_emoji in bot.py, which tries the source sticker's own
    .emoji field first -- this is only for raw document uploads or when
    someone explicitly types emoji alongside the file)."""
    if not text:
        return []
    seen = []
    for ch in text:
        if _EMOJI_RE.match(ch) and ch not in seen:
            seen.append(ch)
        if len(seen) >= limit:
            break
    return seen


def is_valid_short_name(name: str) -> bool:
    """Local sanity check mirroring Telegram's syntactic rules (not
    uniqueness, which only the server can know). Useful for tests and for
    catching a broken bot_username early instead of via an API error."""
    if not (1 <= len(name) <= MAX_NAME_LEN):
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        return False
    if "__" in name:
        return False
    return True
