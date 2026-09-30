import aiosqlite
import json
import time
import os
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "../data/cache.db")


async def init_db():
    """Initialize the SQLite cache database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=30000;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS lookup_cache (
                email TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                cached_at INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS verify_cache (
                email TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                cached_at INTEGER NOT NULL
            )
        """)
        await db.commit()


async def get_lookup_cache(email: str, ttl_hours: Optional[int] = None) -> Optional[dict]:
    """Retrieve a cached lookup result. Permanent by default unless ttl_hours is specified."""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA busy_timeout=30000;")
            async with db.execute(
                "SELECT result, cached_at FROM lookup_cache WHERE email = ?",
                (email.lower().strip(),)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    result, cached_at = row
                    if ttl_hours is not None:
                        age_hours = (time.time() - cached_at) / 3600
                        if age_hours >= ttl_hours:
                            return None
                    return json.loads(result)
    except Exception:
        pass
    return None


async def set_lookup_cache(email: str, result: dict):
    """Store a lookup result in the cache permanently."""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA busy_timeout=30000;")
            await db.execute(
                "INSERT OR REPLACE INTO lookup_cache (email, result, cached_at) VALUES (?, ?, ?)",
                (email.lower().strip(), json.dumps(result), int(time.time()))
            )
            await db.commit()
    except Exception:
        pass


async def delete_lookup_cache(email: str) -> bool:
    """Delete a cached lookup result for the specified email."""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA busy_timeout=30000;")
            await db.execute("DELETE FROM lookup_cache WHERE email = ?", (email.lower().strip(),))
            await db.commit()
            return True
    except Exception:
        return False


async def get_verify_cache(email: str, ttl_hours: Optional[int] = None) -> Optional[dict]:
    """Retrieve a cached verification result. Permanent by default unless ttl_hours is specified."""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA busy_timeout=30000;")
            async with db.execute(
                "SELECT result, cached_at FROM verify_cache WHERE email = ?",
                (email.lower().strip(),)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    result, cached_at = row
                    if ttl_hours is not None:
                        age_hours = (time.time() - cached_at) / 3600
                        if age_hours >= ttl_hours:
                            return None
                    return json.loads(result)
    except Exception:
        pass
    return None


async def set_verify_cache(email: str, result: dict):
    """Store a verification result in the cache."""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=30.0) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA busy_timeout=30000;")
            await db.execute(
                "INSERT OR REPLACE INTO verify_cache (email, result, cached_at) VALUES (?, ?, ?)",
                (email.lower().strip(), json.dumps(result), int(time.time()))
            )
            await db.commit()
    except Exception:
        pass

