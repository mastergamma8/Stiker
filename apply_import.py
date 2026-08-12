"""
apply_import.py

Run this ONCE, on the same device/folder as your live bot.py, to merge
the stickers from stickers_import.db into your real catalog:

    python apply_import.py

What it does:
  - Finds your real bot.db the same way db.py does (TGS_BOT_DB_PATH env
    var if set, otherwise bot.db next to this script).
  - Creates it (with the right schema) if it doesn't exist yet -- safe to
    run even before the bot's ever been started.
  - Copies every row from stickers_import.db's catalog_items into it,
    skipping anything whose content already exists there (matched by a
    content hash, not filename, so running this twice is harmless).
  - Leaves price_stars at 0 for everything it adds -- set a price
    afterwards from inside Telegram: /setprice all <stars>.
  - Does NOT touch the two "template" Telegram packs -- that still needs
    a live Telegram connection, so after this finishes, start the bot and
    send /rebuildpacks (as admin) to actually add these to the browsable
    sticker/emoji packs. That step paces itself and can take a few
    minutes for 37 stickers; see the bot's own messages while it runs.

Pure stdlib sqlite3 -- no aiogram/aiosqlite needed, so this runs standalone
even before your dependencies are installed.
"""
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STAGING_PATH = os.path.join(HERE, "stickers_import.db")
TARGET_PATH = os.environ.get("TGS_BOT_DB_PATH") or os.path.join(HERE, "bot.db")

# Must match db.py's catalog_items table exactly, so a freshly-bootstrapped
# bot.db (if this runs before the bot ever has) is fully compatible.
SCHEMA = """
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
    created_at        REAL NOT NULL
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
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def main():
    if not os.path.exists(STAGING_PATH):
        sys.exit(f"Не нашёл {STAGING_PATH} -- держите этот скрипт в той же папке, что и stickers_import.db.")

    print(f"Целевая база: {TARGET_PATH}")
    is_new = not os.path.exists(TARGET_PATH)
    target = sqlite3.connect(TARGET_PATH)
    target.executescript(SCHEMA)
    target.commit()
    if is_new:
        print("(создал новый файл -- бот ещё ни разу не запускался в этой папке, это нормально)")

    staging = sqlite3.connect(STAGING_PATH)
    staging.row_factory = sqlite3.Row

    (next_pos,) = target.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM catalog_items"
    ).fetchone()

    added = 0
    skipped = 0
    for row in staging.execute("SELECT * FROM catalog_items ORDER BY position ASC"):
        exists = target.execute(
            "SELECT 1 FROM catalog_items WHERE file_unique_id = ?",
            (row["file_unique_id"],),
        ).fetchone()
        if exists:
            skipped += 1
            continue
        target.execute(
            "INSERT INTO catalog_items "
            "(source_pack_name, file_unique_id, emoji, title, orig_tgs, demo_tgs, "
            " target_index, price_stars, position, active, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
            (
                row["source_pack_name"], row["file_unique_id"], row["emoji"], row["title"],
                row["orig_tgs"], row["demo_tgs"], row["target_index"], 0,
                next_pos, time.time(),
            ),
        )
        next_pos += 1
        added += 1
    target.commit()

    (total,) = target.execute("SELECT COUNT(*) FROM catalog_items WHERE active = 1").fetchone()
    (priced,) = target.execute(
        "SELECT COUNT(*) FROM catalog_items WHERE active = 1 AND price_stars > 0"
    ).fetchone()

    print(f"\nДобавлено: {added}")
    if skipped:
        print(f"Пропущено (уже были): {skipped}")
    print(f"\nВсего в каталоге: {total}, из них с ценой: {priced}, без цены: {total - priced}.")
    print(
        "\nДальше в Telegram, как админ:\n"
        "  1. /setprice unpriced <звёзды>   -- назначить цену только новым "
        "позициям (не тронет то, что уже было оценено)\n"
        "  2. /rebuildpacks                 -- собрать/дособрать витринные "
        "паки (это не быстро, бот сам подождёт, если Telegram попросит паузу)"
    )


if __name__ == "__main__":
    main()
