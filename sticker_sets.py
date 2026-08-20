"""
sticker_sets.py

Aiogram-facing helpers for creating and extending Telegram sticker sets
(regular animated packs and custom_emoji packs). Shared by:

  - bot.py's manual /newpack flow (adds one sticker per incoming message);
  - shop.py's admin catalog import (adds a whole source pack at once, to
    build the two browsable "template" packs);
  - shop.py's order delivery (adds a buyer's personalised purchase at
    once, to build that buyer's own pack).

create_set_with_retry() is the same logic bot.py used to have inline for
/newpack (moved here unchanged, plus flood-control handling -- see below);
build_or_extend_set() is the new batch version the shop needs for
"create-or-append N stickers at once" instead of one message at a time.

Telegram throttles createNewStickerSet/addStickerToSet much harder than
regular messages -- importing even a moderately-sized pack with no pacing
between calls reliably trips flood control (TelegramRetryAfter), and the
wait Telegram asks for escalates the harder you hit it (real observed
example: 243 seconds after a burst of ~15-20 unpaced addStickerToSet
calls). Both functions below (a) pace themselves with a small delay
between calls so this is unlikely to happen in normal-sized batches, and
(b) transparently wait out TelegramRetryAfter and retry when it happens
anyway, rather than letting it crash the whole import/order -- Telegram's
own guidance is to respect the exact retry_after it reports, not to guess
or back off on your own schedule.

Since Bot API 7.2, createNewStickerSet itself accepts 1-50 *initial*
stickers in a single call (not just one) -- build_or_extend_set below
uses this to create a set's first batch in ONE call instead of
createNewStickerSet(1) + one addStickerToSet per remaining sticker, which
is where nearly all of a big import's wall-clock time used to come from.

That batch defaults to 12, not Telegram's max of 50 -- a real production
run on 2026-08-20 showed a single 37-sticker batch tripping flood control
on its very first attempt (a TelegramRetryAfter of several minutes),
suggesting Telegram weighs a createNewStickerSet call's flood-control
cost by how many stickers are inside it, not just by "one request". A
smaller batch both has a better chance of clearing at all and wastes less
time before falling back if it doesn't. If the batch call gets rejected
for a bad file (Telegram doesn't say which one), it falls back to the
one-by-one path, so nothing is lost, just slower. If it gets rejected by
flood control instead, it does NOT fall back or retry -- see
create_set_with_retry's docstring for why that specific combination (new
name + wait, repeated) is what turned one blocked call into 5 blocked
calls in a row (48 minutes of sleeping) before crashing the whole update
handler that day. Anything beyond the initial batch (a pack growing past
it) still goes through addStickerToSet one at a time -- there's no batch
"add to existing set" call, only a batch "create" call.

Kept out of pack_builder.py on purpose -- that module stays aiogram-free,
see its own docstring -- and out of bot.py so shop.py doesn't have to
import the whole bot module (pulling in bot.py's own router/handlers)
just to build a sticker set.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InputSticker

from pack_builder import generate_set_name, MAX_TITLE_LEN

log = logging.getLogger("tgs_logo_bot.sticker_sets")

OnWait = Callable[[float], Awaitable[None]]
OnProgress = Callable[[str, int], Awaitable[None]]

# Proactive pause between sticker-set API calls -- Telegram's exact limits
# for these methods aren't published. 1.5s turned out not to be
# conservative enough in practice (still saw repeated multi-minute
# TelegramRetryAfter waits even with that pacing), so the default here is
# more cautious; override with TGS_BOT_STICKER_API_DELAY (seconds) if
# needed without a code change. Deliberately slow-but-reliable over fast:
# this only runs during an admin import or right after a purchase, never
# in a hot path a user is actively waiting on message-by-message.
STICKER_API_DELAY = float(os.getenv("TGS_BOT_STICKER_API_DELAY", "3.0"))

# How many times to wait-and-retry a single call after TelegramRetryAfter
# before giving up on that one sticker and moving on (counted as skipped,
# not a crash -- see build_or_extend_set's docstring).
MAX_RETRY_AFTER_ATTEMPTS = 3


async def _call_with_flood_retry(
    call: Callable[[], Awaitable[None]],
    *,
    on_wait: OnWait | None = None,
    max_attempts: int = MAX_RETRY_AFTER_ATTEMPTS,
) -> None:
    """Invoke `call()` (a zero-arg async callable so it can be retried),
    transparently waiting out TelegramRetryAfter up to max_attempts times
    -- sleeping exactly as long as Telegram says each time, plus a small
    safety margin. `on_wait`, if given, is awaited with the wait duration
    (in seconds) so a caller can tell the person something's happening
    instead of the bot going quiet for minutes. Any other exception, or a
    TelegramRetryAfter on the final attempt, propagates to the caller."""
    for attempt in range(max_attempts):
        try:
            await call()
            return
        except TelegramRetryAfter as e:
            if attempt == max_attempts - 1:
                raise
            log.warning(
                "Telegram flood control: waiting %ss (attempt %s/%s)",
                e.retry_after, attempt + 1, max_attempts,
            )
            if on_wait:
                try:
                    await on_wait(e.retry_after)
                except Exception:
                    log.warning("on_wait callback failed", exc_info=True)
            await asyncio.sleep(e.retry_after + 1)

# Telegram's own per-set caps (see createNewStickerSet in the Bot API docs,
# and the general Telegram limits page): an *animated* regular sticker set
# tops out at 50 stickers; a custom_emoji set at 200. These are hard
# rejections from Telegram, not something worth trying past, so the batch
# helper below stops adding before it would hit them.
MAX_REGULAR_ANIMATED = 50
MAX_CUSTOM_EMOJI = 200


def max_for(sticker_type: str) -> int:
    return MAX_CUSTOM_EMOJI if sticker_type == "custom_emoji" else MAX_REGULAR_ANIMATED


_bot_username_cache: str | None = None


async def get_bot_username(bot: Bot) -> str:
    """get_me() is one API round-trip; cache it module-wide since a bot's
    own username can't change while it's running."""
    global _bot_username_cache
    if _bot_username_cache is None:
        me = await bot.get_me()
        _bot_username_cache = me.username
    return _bot_username_cache


async def create_set_with_retry(
    bot: Bot,
    user_id: int,
    title: str,
    stickers: list[InputSticker],
    sticker_type: str,
    bot_username: str | None = None,
    attempts: int = 5,
    on_wait: OnWait | None = None,
) -> str:
    """Calls createNewStickerSet with 1-50 initial `stickers` in one go,
    regenerating the short name and retrying on a name conflict (a
    generated name colliding with an existing set somewhere on Telegram --
    short names are global, not per-bot, so this is expected to happen
    occasionally for common titles). Flood control (TelegramRetryAfter) is
    waited out via _call_with_flood_retry; if it's *still* not resolved
    after that (Telegram remaining in a flood-control state through its
    own reported retry_after), this propagates immediately rather than
    burning through the remaining naming attempts -- a new name cannot
    fix a rate limit, and retrying it anyway is what turned one blocked
    call into 5 blocked calls in a row on 2026-08-20. Any other error
    (bad title, a bad file somewhere in `stickers`, etc.) is also not
    retried and propagates immediately, since neither a new name nor a
    wait would help -- build_or_extend_set is what decides whether to
    retry with a smaller batch in that case, not this function."""
    bot_username = bot_username or await get_bot_username(bot)
    last_err: Exception | None = None
    for attempt in range(attempts):
        name = generate_set_name(title, user_id, bot_username, attempt=attempt)
        try:
            await _call_with_flood_retry(
                lambda n=name: bot.create_new_sticker_set(
                    user_id=user_id,
                    name=n,
                    title=title[:MAX_TITLE_LEN],
                    stickers=stickers,
                    sticker_type=sticker_type,
                ),
                on_wait=on_wait,
            )
            return name
        except TelegramBadRequest as e:
            last_err = e
            text = (getattr(e, "message", None) or str(e)).lower()
            if any(k in text for k in ("occupied", "already", "short_name", "shortname")):
                continue
            raise
    raise last_err


async def build_or_extend_set(
    bot: Bot,
    user_id: int,
    title: str,
    stickers: list[InputSticker],
    sticker_type: str,
    existing_name: str | None = None,
    current_count: int = 0,
    bot_username: str | None = None,
    on_wait: OnWait | None = None,
    on_progress: OnProgress | None = None,
    keys: list[Any] | None = None,
) -> tuple[str | None, int, int, list[tuple[Any, str]]]:
    """Create a new set (or keep adding to `existing_name`, if given) from
    a batch of InputSticker items in one call. `current_count` is however
    many stickers the caller already knows are in the set (tracked in our
    own settings table for the two template packs -- see shop.py -- rather
    than re-fetched from Telegram every time).

    Paces itself (STICKER_API_DELAY) between calls and waits out flood
    control when it happens anyway (see _call_with_flood_retry) -- so
    unlike a naive loop, this reliably *finishes* a batch of a few dozen
    stickers instead of crashing partway through with TelegramRetryAfter.
    `on_wait`, if given, is awaited with each wait's duration so the
    caller can let a human know what's happening during a longer pause.

    `on_progress`, if given, is awaited with (name, current_count) after
    every successful create/add call -- not just once at the end. That's
    one call for the initial batch (see the batched fast path below, size
    controlled by TGS_BOT_STICKER_BATCH_SIZE), then one per sticker beyond
    that. A big batch with several flood-control waits can take many
    minutes, and the caller (see shop.py's _rebuild_template_packs) uses
    this to persist
    sticker_pack_name/sticker_pack_count to the settings table right away
    each time, rather than only after this whole function returns. That's
    what makes /stats show real progress mid-batch, and -- more
    importantly -- what lets a later /rebuildpacks genuinely resume from
    wherever an interrupted run actually got to, instead of re-attempting
    the same items from current_count=0 (and risking a second, orphaned
    pack) just because the process didn't survive to the end.

    `keys`, if given, must be the same length as `stickers` -- one opaque
    caller-defined value (e.g. a catalog item id) per candidate. Whichever
    keys actually made it in are returned in `added_file_ids` as
    (key, file_id) pairs, in the order they were added, so a caller can
    later target that exact sticker with deleteStickerFromSet without
    re-deriving its position. This costs exactly one extra getStickerSet
    call at the end of the whole batch (not per-sticker), since
    createNewStickerSet/addStickerToSet don't hand back the file_id of
    what they just added. Pass nothing if the caller doesn't need this
    (e.g. a one-off buyer pack that's never individually edited later).

    Returns (name, added, skipped, added_file_ids):
      - name is None only if nothing could be added at all (set already
        full, Telegram rejected every candidate, or flood control never
        cleared -- see below).
      - skipped counts stickers dropped for any of three reasons: the
        set's Telegram size cap (see max_for()) was already reached,
        Telegram rejected that specific file (logged, doesn't abort the
        rest -- one bad/unlucky sticker shouldn't cost the others), or
        flood control never cleared within MAX_RETRY_AFTER_ATTEMPTS waits
        on some call -- unlike a bad file, this DOES abort everything not
        yet attempted (counted into skipped in bulk) rather than trying
        the next candidate, since a still-active flood block will almost
        certainly hit the next call too, and finding that out costs
        another multi-minute wait per attempt (see the module docstring's
        2026-08-20 incident).
      - added_file_ids is [] whenever `keys` wasn't passed, nothing was
        added, or the closing getStickerSet call itself failed (logged,
        not raised -- the pack build already succeeded at that point, so
        losing this bookkeeping shouldn't fail the whole batch).
    """
    if keys is not None and len(keys) != len(stickers):
        raise ValueError("keys must be the same length as stickers")

    cap = max_for(sticker_type)
    name = existing_name
    added = 0
    skipped = 0
    initial_count = current_count
    bot_username = bot_username or await get_bot_username(bot)

    async def _report_progress():
        if on_progress:
            try:
                await on_progress(name, current_count)
            except Exception:
                log.warning("on_progress callback failed", exc_info=True)

    remaining: list[tuple[Any, InputSticker]] = list(
        zip(keys, stickers) if keys is not None else [(None, s) for s in stickers]
    )
    succeeded_keys: list[Any] = []

    # Cap on the *initial* createNewStickerSet batch -- Telegram's own hard
    # limit on that call is 50, but a real production run on 2026-08-20
    # showed a single 37-sticker batch tripping flood control on its very
    # first attempt (a multi-minute TelegramRetryAfter), suggesting
    # Telegram weighs this call's flood-control cost by how many stickers
    # are inside it, not just by "one request" -- so a smaller batch has a
    # better chance of clearing at all, and a failed one wastes less time
    # before falling back. Override with TGS_BOT_STICKER_BATCH_SIZE if you
    # want to experiment; keep at or below 50.
    batch_cap = min(50, int(os.getenv("TGS_BOT_STICKER_BATCH_SIZE", "12")))

    if name is None:
        if current_count >= cap:
            return None, 0, len(remaining), []

        # Fast path: Telegram accepts multiple *initial* stickers in one
        # createNewStickerSet call (up to 50), so try to open the set with
        # a batch instead of one sticker + N individually-paced
        # addStickerToSet calls -- far fewer round trips when it works.
        #
        # If Telegram is currently flood-limiting this bot's sticker-set
        # calls, it stays limited regardless of which name or how small a
        # follow-up request is -- see create_set_with_retry's docstring on
        # 2026-08-20's incident, where retrying with a new name 5 times in
        # a row just repeated the same multi-minute block 5 times before
        # crashing the whole update handler. So: a flood-control failure
        # here does NOT fall through to the one-by-one path below (that
        # would almost certainly hit the exact same wall immediately) --
        # it gives up cleanly for this run instead, and the caller (see
        # shop.py's _rebuild_template_packs) tells the admin to just try
        # /rebuildpacks again later. A rejected *file* (TelegramBadRequest)
        # is a different, per-content problem, so that one still falls
        # back to one-by-one as before.
        flood_blocked = False
        batch_size = min(batch_cap, cap - current_count, len(remaining))
        if batch_size > 1:
            batch = remaining[:batch_size]
            batch_keys = [k for k, _ in batch]
            batch_stickers = [s for _, s in batch]
            try:
                name = await create_set_with_retry(
                    bot, user_id, title, batch_stickers, sticker_type, bot_username, on_wait=on_wait,
                )
                added += len(batch)
                current_count += len(batch)
                succeeded_keys.extend(batch_keys)
                remaining = remaining[batch_size:]
                await _report_progress()
                await asyncio.sleep(STICKER_API_DELAY)
            except TelegramBadRequest as e:
                # Telegram doesn't say *which* file in the batch was bad --
                # fall back to the slower one-by-one path below, which
                # already knows how to skip a single bad file without
                # losing the rest. `remaining` is untouched above, so
                # nothing here has been lost.
                log.warning("batched createNewStickerSet rejected (falling back to one-by-one): %s", e)
            except TelegramRetryAfter as e:
                log.warning(
                    "batched createNewStickerSet still flood-limited after Telegram's own "
                    "retry_after (last: %ss) -- giving up on this run rather than retrying "
                    "(see create_set_with_retry docstring)", e.retry_after,
                )
                flood_blocked = True

        if name is None and not flood_blocked:
            # Either batching didn't apply (batch_size <= 1) or the batch
            # attempt above failed on a bad file. Find the first sticker
            # Telegram will actually accept to create the set -- if the
            # very first candidate is rejected for being bad, try the next
            # one rather than aborting the whole batch. But a flood-control
            # hit here, same as above, ends the search immediately instead
            # of grinding through every remaining candidate at multi-minute
            # cost each.
            while remaining:
                key, candidate = remaining.pop(0)
                try:
                    name = await create_set_with_retry(
                        bot, user_id, title, [candidate], sticker_type, bot_username, on_wait=on_wait,
                    )
                    added += 1
                    current_count += 1
                    succeeded_keys.append(key)
                    await _report_progress()
                    await asyncio.sleep(STICKER_API_DELAY)
                    break
                except TelegramBadRequest as e:
                    log.warning("createNewStickerSet rejected a sticker: %s", e)
                    skipped += 1
                except TelegramRetryAfter as e:
                    log.warning(
                        "still flood-limited on a single sticker after Telegram's own "
                        "retry_after (last: %ss) -- giving up on this run", e.retry_after,
                    )
                    flood_blocked = True
                    break

        if name is None:
            skipped += len(remaining)
            return None, added, skipped, []

    for i, (key, sticker) in enumerate(remaining):
        if current_count >= cap:
            skipped += len(remaining) - i
            break
        try:
            await _call_with_flood_retry(
                lambda s=sticker: bot.add_sticker_to_set(user_id=user_id, name=name, sticker=s),
                on_wait=on_wait,
            )
            added += 1
            current_count += 1
            succeeded_keys.append(key)
            await _report_progress()
        except TelegramBadRequest as e:
            log.warning("addStickerToSet rejected a sticker: %s", e)
            skipped += 1
        except TelegramRetryAfter as e:
            # Same reasoning as the create phase above: _call_with_flood_retry
            # already waited out Telegram's own reported retry_after and it's
            # STILL blocked, so the next item would almost certainly hit the
            # identical wall immediately. Stop here (counting everything not
            # yet tried as skipped) instead of repeating a multi-minute wait
            # once per remaining sticker.
            log.error(
                "giving up on the rest of this batch after %s flood-control waits on "
                "one sticker (last wait: %ss) -- %s items left untried",
                MAX_RETRY_AFTER_ATTEMPTS, e.retry_after, len(remaining) - i - 1,
            )
            skipped += len(remaining) - i
            break
        await asyncio.sleep(STICKER_API_DELAY)

    added_file_ids: list[tuple[Any, str]] = []
    if name and succeeded_keys and any(k is not None for k in succeeded_keys):
        try:
            fresh = await bot.get_sticker_set(name=name)
            tail = fresh.stickers[initial_count: initial_count + len(succeeded_keys)]
            added_file_ids = [
                (key, st.file_id) for key, st in zip(succeeded_keys, tail) if key is not None
            ]
        except Exception:
            log.warning("could not re-fetch %r to record new file_ids", name, exc_info=True)

    return name, added, skipped, added_file_ids
