"""
db.py

Persistent storage for the public shop feature (catalog, cart, orders,
settings), using SQLite via aiosqlite. Kept separate from bot.py the same
way tgs_editor.py/pack_builder.py already are: a thin, framework-agnostic
layer that shop.py calls into, so storage and Telegram-facing logic can
change independently.

Nothing here existed before the shop feature -- the original bot kept all
state in aiogram's in-memory FSM storage (see bot.py's PackStates), which
is fine for "edit this one file" but can't survive a restart with real
paid orders in flight, so the shop gets its own on-disk store.

Every catalog item stores BOTH the untouched original sticker bytes
(orig_tgs) and a demo copy with the placeholder text already baked in
(demo_tgs -- used for catalog browsing/preview and for the two "template"
Telegram packs built on import). A buyer's order is personalised by
re-running replace_text() on orig_tgs with their own text at delivery
time (see shop.py's _generate_personalized), not by editing demo_tgs
again -- that keeps every purchase generated from the real source
geometry found at import time (target_index), rather than from glyphs
the bot already drew once for the demo.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

DB_PATH = Path(os.getenv("TGS_BOT_DB_PATH", str(Path(__file__).resolve().parent / "bot.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_pack_name  TEXT,
    file_unique_id    TEXT UNIQUE,
    emoji             TEXT NOT NULL DEFAULT '⭐',
    title             TEXT,
    orig_tgs          BLOB NOT NULL,
    demo_tgs          BLOB NOT NULL,
    preview_file_id   TEXT,
    target_index      INTEGER,
    price_stars       INTEGER NOT NULL DEFAULT 0,
    position          INTEGER NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        REAL NOT NULL,
    -- file_id of THIS item's own sticker inside the two showcase Telegram
    -- sets (sticker_pack_name / emoji_pack_name settings) -- set once the
    -- item is actually added there (see sticker_sets.build_or_extend_set's
    -- `keys` param and shop.py's _rebuild_template_packs). NULL means
    -- either "never made it into that pack" (e.g. beyond Telegram's
    -- 50/200 cap) or "added before this column existed" -- see
    -- shop.py's _backfill_showcase_file_ids for the latter. Needed to
    -- call deleteStickerFromSet for a specific item on /catalog delete.
    showcase_sticker_file_id TEXT,
    showcase_emoji_file_id   TEXT
);

CREATE TABLE IF NOT EXISTS cart_items (
    user_id    INTEGER NOT NULL,
    item_id    INTEGER NOT NULL REFERENCES catalog_items(id) ON DELETE CASCADE,
    added_at   REAL NOT NULL,
    PRIMARY KEY (user_id, item_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    item_ids           TEXT NOT NULL,
    custom_text        TEXT NOT NULL,
    delivery_format    TEXT NOT NULL DEFAULT 'both',
    total_stars        INTEGER NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    charge_id          TEXT,
    sticker_pack_name  TEXT,
    emoji_pack_name    TEXT,
    note               TEXT,
    is_free            INTEGER NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Users who unlocked free checkout via the secret /promo command (see
-- shop.py cmd_promo / has_free_access). Admins bypass payment too, but
-- that's just is_admin() on ADMIN_IDS -- this table is only for regular
-- users let in through the promo code.
CREATE TABLE IF NOT EXISTS free_access (
    user_id     INTEGER PRIMARY KEY,
    granted_at  REAL NOT NULL,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_catalog_active_position ON catalog_items(active, position);
CREATE INDEX IF NOT EXISTS idx_cart_user ON cart_items(user_id);
"""


@asynccontextmanager
async def _connect():
    """Async context manager wrapping a single aiosqlite connection: opens
    it, configures it, yields it, and always closes it again -- exactly
    once per call.

    Do NOT write `async with await _connect() as conn:` -- that was the
    bug here before: aiosqlite's own Connection object is *itself* both
    awaitable and an async context manager (that's what lets people write
    `async with aiosqlite.connect(...) as conn:` directly), so awaiting it
    once and then also entering it via `async with` tries to start its
    background thread a second time and crashes with "RuntimeError:
    threads can only be started once". This wrapper does the one-time
    connect+setup itself, so every call site below just writes
    `async with _connect() as conn:` (no `await` before `_connect()`) and
    can't hit that trap."""
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        await conn.close()


async def init() -> None:
    """Create tables/indexes if missing and turn on WAL so the (rare)
    concurrent writer doesn't block readers browsing the catalog. Safe to
    call every startup -- everything is CREATE ... IF NOT EXISTS."""
    async with _connect() as conn:
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.executescript(_SCHEMA)
        await _migrate(conn)
        await conn.commit()


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Idempotent ALTER TABLE migrations for columns added after the
    orders table already shipped -- CREATE TABLE IF NOT EXISTS above only
    creates missing *tables*, it won't add a column to one that already
    exists on disk (e.g. an existing bot.db from before the free/promo
    feature). Safe to call every startup: checks first, only alters when
    the column is actually missing."""
    cur = await conn.execute("PRAGMA table_info(orders)")
    cols = {row["name"] for row in await cur.fetchall()}
    if "is_free" not in cols:
        await conn.execute("ALTER TABLE orders ADD COLUMN is_free INTEGER NOT NULL DEFAULT 0")

    cur = await conn.execute("PRAGMA table_info(catalog_items)")
    cols = {row["name"] for row in await cur.fetchall()}
    if "showcase_sticker_file_id" not in cols:
        await conn.execute("ALTER TABLE catalog_items ADD COLUMN showcase_sticker_file_id TEXT")
    if "showcase_emoji_file_id" not in cols:
        await conn.execute("ALTER TABLE catalog_items ADD COLUMN showcase_emoji_file_id TEXT")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

_LIGHT_COLS = (
    "id, source_pack_name, emoji, title, target_index, price_stars, "
    "position, active, preview_file_id, showcase_sticker_file_id, showcase_emoji_file_id"
)

# Maps the logical "which showcase pack" key used by callers (shop.py,
# sticker_sets.py) to the actual column -- keeps set_showcase_file_id's
# SQL safe from string-building with anything other than these two
# hardcoded literals.
_SHOWCASE_COLS = {
    "sticker": "showcase_sticker_file_id",
    "emoji": "showcase_emoji_file_id",
}


async def next_position() -> int:
    async with _connect() as conn:
        cur = await conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM catalog_items")
        (pos,) = await cur.fetchone()
        return pos


async def item_exists(file_unique_id: str) -> bool:
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM catalog_items WHERE file_unique_id = ?", (file_unique_id,)
        )
        return await cur.fetchone() is not None


async def add_catalog_item(
    *,
    source_pack_name: str | None,
    file_unique_id: str,
    emoji: str,
    title: str | None,
    orig_tgs: bytes,
    demo_tgs: bytes,
    target_index: int | None,
    price_stars: int = 0,
) -> int:
    pos = await next_position()
    async with _connect() as conn:
        cur = await conn.execute(
            "INSERT INTO catalog_items "
            "(source_pack_name, file_unique_id, emoji, title, orig_tgs, demo_tgs, "
            " target_index, price_stars, position, active, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
            (
                source_pack_name, file_unique_id, emoji, title, orig_tgs, demo_tgs,
                target_index, price_stars, pos, time.time(),
            ),
        )
        await conn.commit()
        return cur.lastrowid


async def list_catalog_light(active_only: bool = True, priced_only: bool = False) -> list[dict[str, Any]]:
    """Ordered catalog listing WITHOUT the (potentially large) blob
    columns -- use this for browsing/pagination/admin listing, and only
    fetch the full row (get_catalog_item) for the one item actually being
    rendered or personalised."""
    q = f"SELECT {_LIGHT_COLS} FROM catalog_items"
    conds = []
    if active_only:
        conds.append("active = 1")
    if priced_only:
        conds.append("price_stars > 0")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY position ASC"
    async with _connect() as conn:
        cur = await conn.execute(q)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def count_catalog_items(active_only: bool = True, priced_only: bool = False) -> int:
    q = "SELECT COUNT(*) FROM catalog_items"
    conds = []
    if active_only:
        conds.append("active = 1")
    if priced_only:
        conds.append("price_stars > 0")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    async with _connect() as conn:
        cur = await conn.execute(q)
        (n,) = await cur.fetchone()
        return n


async def get_catalog_item(item_id: int) -> dict[str, Any] | None:
    """Full row, including orig_tgs/demo_tgs blobs -- use only for the
    single item currently being displayed or personalised, not for lists."""
    async with _connect() as conn:
        cur = await conn.execute("SELECT * FROM catalog_items WHERE id = ?", (item_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_catalog_item_light(item_id: int) -> dict[str, Any] | None:
    async with _connect() as conn:
        cur = await conn.execute(f"SELECT {_LIGHT_COLS} FROM catalog_items WHERE id = ?", (item_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_price(item_id: int, stars: int) -> bool:
    async with _connect() as conn:
        cur = await conn.execute("UPDATE catalog_items SET price_stars = ? WHERE id = ?", (stars, item_id))
        await conn.commit()
        return cur.rowcount > 0


async def set_all_prices(stars: int) -> int:
    async with _connect() as conn:
        cur = await conn.execute("UPDATE catalog_items SET price_stars = ?", (stars,))
        await conn.commit()
        return cur.rowcount


async def set_price_where_unset(stars: int) -> int:
    async with _connect() as conn:
        cur = await conn.execute(
            "UPDATE catalog_items SET price_stars = ? WHERE price_stars <= 0", (stars,)
        )
        await conn.commit()
        return cur.rowcount


async def set_active(item_id: int, active: bool) -> bool:
    async with _connect() as conn:
        cur = await conn.execute(
            "UPDATE catalog_items SET active = ? WHERE id = ?", (1 if active else 0, item_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def set_preview_file_id(item_id: int, file_id: str) -> None:
    async with _connect() as conn:
        await conn.execute(
            "UPDATE catalog_items SET preview_file_id = ? WHERE id = ?", (file_id, item_id)
        )
        await conn.commit()


async def set_showcase_file_id(item_id: int, pack_kind: str, file_id: str) -> None:
    """Records that `item_id`'s sticker now lives in the showcase pack
    identified by `pack_kind` ("sticker" or "emoji") under this file_id --
    see the catalog_items schema comment above. Called right after a
    successful build_or_extend_set (fresh items) or by
    shop.py's _backfill_showcase_file_ids (older items)."""
    col = _SHOWCASE_COLS[pack_kind]
    async with _connect() as conn:
        await conn.execute(f"UPDATE catalog_items SET {col} = ? WHERE id = ?", (file_id, item_id))
        await conn.commit()


async def delete_catalog_item(item_id: int) -> bool:
    """Permanently removes a catalog item (unlike set_active(False), which
    only hides it). Callers are responsible for also removing it from any
    live showcase Telegram packs first -- see shop.py's admin delete flow
    -- this only touches the database. cart_items referencing this item
    are cleared automatically (ON DELETE CASCADE, see schema)."""
    async with _connect() as conn:
        cur = await conn.execute("DELETE FROM catalog_items WHERE id = ?", (item_id,))
        await conn.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Settings (key-value)
# ---------------------------------------------------------------------------

async def get_setting(key: str, default: str | None = None) -> str | None:
    async with _connect() as conn:
        cur = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Cart (one user's in-progress "build your own pack" selection)
# ---------------------------------------------------------------------------

async def add_to_cart(user_id: int, item_id: int) -> None:
    async with _connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO cart_items (user_id, item_id, added_at) VALUES (?,?,?)",
            (user_id, item_id, time.time()),
        )
        await conn.commit()


async def remove_from_cart(user_id: int, item_id: int) -> None:
    async with _connect() as conn:
        await conn.execute(
            "DELETE FROM cart_items WHERE user_id = ? AND item_id = ?", (user_id, item_id)
        )
        await conn.commit()


async def clear_cart(user_id: int) -> None:
    async with _connect() as conn:
        await conn.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))
        await conn.commit()


async def in_cart(user_id: int, item_id: int) -> bool:
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM cart_items WHERE user_id = ? AND item_id = ?", (user_id, item_id)
        )
        return await cur.fetchone() is not None


async def cart_count(user_id: int) -> int:
    async with _connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM cart_items WHERE user_id = ?", (user_id,))
        (n,) = await cur.fetchone()
        return n


async def get_cart_light(user_id: int) -> list[dict[str, Any]]:
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT c.id, c.emoji, c.title, c.price_stars "
            "FROM cart_items ci JOIN catalog_items c ON c.id = ci.item_id "
            "WHERE ci.user_id = ? AND c.active = 1 "
            "ORDER BY ci.added_at ASC",
            (user_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def create_order(
    user_id: int, item_ids: list[int], custom_text: str, total_stars: int,
    delivery_format: str = "both", is_free: bool = False,
) -> int:
    now = time.time()
    async with _connect() as conn:
        cur = await conn.execute(
            "INSERT INTO orders (user_id, item_ids, custom_text, delivery_format, total_stars, "
            "status, is_free, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, json.dumps(item_ids), custom_text, delivery_format, total_stars,
             "pending", int(is_free), now, now),
        )
        await conn.commit()
        return cur.lastrowid


async def get_order(order_id: int) -> dict[str, Any] | None:
    async with _connect() as conn:
        cur = await conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_order_status(order_id: int, status: str, **fields: Any) -> None:
    """Update an order's status plus any other columns passed as kwargs
    (charge_id=..., sticker_pack_name=..., emoji_pack_name=..., note=...).
    Column names come only from call sites in this codebase, never from
    user input, so building the SET clause from fields.keys() is safe."""
    cols = ["status = ?", "updated_at = ?"]
    vals: list[Any] = [status, time.time()]
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        vals.append(v)
    vals.append(order_id)
    async with _connect() as conn:
        await conn.execute(f"UPDATE orders SET {', '.join(cols)} WHERE id = ?", vals)
        await conn.commit()


async def stats() -> dict[str, Any]:
    async with _connect() as conn:
        # is_free = 0 everywhere below -- free/promo orders never charged
        # real Stars, so they must not inflate paid/revenue counters.
        cur = await conn.execute(
            "SELECT COUNT(*) FROM orders WHERE (status = 'paid' OR status = 'delivered') AND is_free = 0"
        )
        (paid_orders,) = await cur.fetchone()
        cur = await conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'delivered' AND is_free = 0"
        )
        (delivered_orders,) = await cur.fetchone()
        cur = await conn.execute(
            "SELECT COALESCE(SUM(total_stars), 0) FROM orders WHERE status = 'delivered' AND is_free = 0"
        )
        (revenue_stars,) = await cur.fetchone()
        cur = await conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'delivered' AND is_free = 1"
        )
        (free_delivered,) = await cur.fetchone()
        cur = await conn.execute("SELECT COUNT(*) FROM catalog_items WHERE active = 1")
        (catalog_items,) = await cur.fetchone()
    return {
        "paid_orders": paid_orders,
        "delivered_orders": delivered_orders,
        "revenue_stars": revenue_stars,
        "catalog_items": catalog_items,
        "free_delivered": free_delivered,
    }


# ---------------------------------------------------------------------------
# Free access (secret /promo code -- see shop.py cmd_promo)
# ---------------------------------------------------------------------------

async def grant_free_access(user_id: int, note: str = "") -> None:
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO free_access (user_id, granted_at, note) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id, time.time(), note),
        )
        await conn.commit()


async def has_free_access(user_id: int) -> bool:
    async with _connect() as conn:
        cur = await conn.execute("SELECT 1 FROM free_access WHERE user_id = ?", (user_id,))
        return await cur.fetchone() is not None


async def revoke_free_access(user_id: int) -> bool:
    async with _connect() as conn:
        cur = await conn.execute("DELETE FROM free_access WHERE user_id = ?", (user_id,))
        await conn.commit()
        return cur.rowcount > 0


async def list_free_access() -> list[dict[str, Any]]:
    async with _connect() as conn:
        cur = await conn.execute(
            "SELECT user_id, granted_at, note FROM free_access ORDER BY granted_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
