"""
High-Performance, Zero-Dependency Thread-Safe In-Memory TTL Cache.
Provides Redis-like in-memory caching to eliminate redundant SQLite queries
and Deribit API calls, keeping RAM consumption under 5 MB.
"""
import functools
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("cache")


class TTLCache:
    """
    Lightweight, thread-safe in-memory cache with Time-To-Live (TTL) expiration
    and bounded maximum size to prevent memory leaks.
    """

    def __init__(self, maxsize: int = 150, default_ttl: float = 30.0):
        self.maxsize = maxsize
        self.default_ttl = default_ttl
        self._lock = threading.Lock()
        # Storage format: key -> (value, expire_timestamp, last_accessed)
        self._data: Dict[str, Tuple[Any, float, float]] = {}

    def get(self, key: str) -> Optional[Any]:
        """Retrieves a cached value if exists and not expired."""
        now = time.monotonic()
        with self._lock:
            if key not in self._data:
                return None
            val, expire_at, _ = self._data[key]
            if now >= expire_at:
                del self._data[key]
                return None
            # Update last accessed time for LRU tracking
            self._data[key] = (val, expire_at, now)
            return val

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Stores a value in cache with specified TTL in seconds."""
        now = time.monotonic()
        expire_ttl = ttl if ttl is not None else self.default_ttl
        expire_at = now + expire_ttl

        with self._lock:
            # Enforce maxsize limit by evicting expired or oldest items
            if len(self._data) >= self.maxsize and key not in self._data:
                self._evict_stale_or_oldest(now)

            self._data[key] = (value, expire_at, now)

    def delete(self, key: str) -> None:
        """Removes a specific key from cache."""
        with self._lock:
            self._data.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> int:
        """Removes all keys starting with the given prefix."""
        with self._lock:
            keys_to_del = [k for k in self._data if k.startswith(prefix)]
            for k in keys_to_del:
                del self._data[k]
            return len(keys_to_del)

    def clear(self) -> None:
        """Clears all cached data."""
        with self._lock:
            self._data.clear()

    def stats(self) -> Dict[str, Any]:
        """Returns cache stats."""
        now = time.monotonic()
        with self._lock:
            active_count = sum(1 for _, exp, _ in self._data.values() if exp > now)
            return {
                "total_entries": len(self._data),
                "active_entries": active_count,
                "maxsize": self.maxsize,
                "default_ttl_sec": self.default_ttl,
            }

    def _evict_stale_or_oldest(self, now: float) -> None:
        """Internal helper to prune expired entries or evict least recently used."""
        # 1. Prune expired entries first
        expired = [k for k, (_, exp, _) in self._data.items() if now >= exp]
        for k in expired:
            del self._data[k]

        # 2. If still at maxsize, evict LRU item
        if len(self._data) >= self.maxsize:
            oldest_key = min(self._data.keys(), key=lambda k: self._data[k][2])
            del self._data[oldest_key]


# Global shared cache instances
memory_cache = TTLCache(maxsize=150, default_ttl=30.0)


def cached(ttl: float = 30.0, key_prefix: str = ""):
    """
    Decorator for caching function return values in memory.
    Usage:
        @cached(ttl=30.0, key_prefix="deribit_book")
        def fetch_data(): ...
    """
    def decorator(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Generate cache key
            arg_str = "_".join(str(a) for a in args)
            kw_str = "_".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
            cache_key = f"{key_prefix or fn.__name__}:{arg_str}:{kw_str}"

            cached_val = memory_cache.get(cache_key)
            if cached_val is not None:
                return cached_val

            result = fn(*args, **kwargs)
            memory_cache.set(cache_key, result, ttl=ttl)
            return result

        return wrapper

    return decorator
