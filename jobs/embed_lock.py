"""One lock so a catalog embed and match classification do not share the cache.

``Embedder._save`` rewrites the npz and meta files wholesale. Classification
must not read them while that rewrite is in progress, and two embeds must not
save at once. Hold the lock across the whole ``asyncio.to_thread`` call.
Do not hold it across Telegram sends.
"""

from __future__ import annotations

import asyncio

catalog_embed_lock = asyncio.Lock()
