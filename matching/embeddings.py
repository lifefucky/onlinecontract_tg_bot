"""Dense embeddings via the OpenAI-compatible ProxyAPI. Vectors are cached on disk."""

from __future__ import annotations

import json
import logging

import numpy as np
from openai import OpenAI

from matching import config
from matching.attributes import cleanup_embed
from matching.errors import CatalogEmbedRequired, PipelineError

logger = logging.getLogger(__name__)


class Embedder:
    def __init__(self) -> None:
        self.client = None
        read_dir = config.embedding_read_dir()
        self.cache_path = config.CACHE_DIR / "embeddings_bge_m3.npz"
        self.meta_path = config.CACHE_DIR / "embeddings_meta.json"
        self._read_cache_path = read_dir / "embeddings_bge_m3.npz"
        self._read_meta_path = read_dir / "embeddings_meta.json"
        self._texts: list[str] = []
        self._matrix: np.ndarray | None = None
        self._index: dict[str, int] = {}
        self.api_calls = 0
        self.new_texts = 0
        self._load()

    def _client(self) -> OpenAI:
        if not config.OPENAI_API_KEY:
            raise PipelineError("OPENAI_API_KEY is missing in the project .env")
        if self.client is None:
            self.client = OpenAI(
                api_key=config.OPENAI_API_KEY,
                base_url=config.OPENAI_BASE_URL,
                timeout=90.0,
                max_retries=4,
            )
        return self.client

    def _load(self) -> None:
        if not self._read_cache_path.exists() or not self._read_meta_path.exists():
            return
        meta = json.loads(self._read_meta_path.read_text(encoding="utf-8"))
        if meta.get("model") != config.EMBED_MODEL:
            return
        self._texts = list(meta.get("texts") or [])
        matrix = np.load(self._read_cache_path)["vectors"]
        if matrix.ndim != 2 or len(self._texts) != matrix.shape[0]:
            self._texts = []
            return
        self._matrix = matrix.astype(np.float32)
        self._index = {text: i for i, text in enumerate(self._texts)}

    def _save(self) -> None:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if self._matrix is None:
            return
        np.savez_compressed(self.cache_path, vectors=self._matrix)
        self.meta_path.write_text(
            json.dumps(
                {"model": config.EMBED_MODEL, "dim": int(self._matrix.shape[1]), "texts": self._texts},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def missing(self, texts: list[str]) -> list[str]:
        pending: list[str] = []
        seen: set[str] = set()
        for text in texts:
            cleaned = cleanup_embed(text)
            if not cleaned or cleaned in self._index or cleaned in seen:
                continue
            pending.append(cleaned)
            seen.add(cleaned)
        return pending

    def embed(self, texts: list[str]) -> dict[str, np.ndarray]:
        cleaned = [cleanup_embed(text) for text in texts]
        missing = []
        seen: set[str] = set()
        for text in cleaned:
            if text and text not in self._index and text not in seen:
                missing.append(text)
                seen.add(text)
        if missing:
            self._fetch(missing)
        out: dict[str, np.ndarray] = {}
        for text in cleaned:
            if not text:
                continue
            out[text] = self._matrix[self._index[text]]
        return out

    def _fetch(self, missing: list[str]) -> None:
        client = self._client()
        for start in range(0, len(missing), config.EMBED_BATCH):
            batch = missing[start : start + config.EMBED_BATCH]
            try:
                response = client.embeddings.create(model=config.EMBED_MODEL, input=batch)
            except Exception as exc:
                message = str(exc).replace(config.OPENAI_API_KEY, "***")
                raise PipelineError(f"embedding request failed: {message[:400]}") from None
            self.api_calls += 1
            ordered = sorted(response.data, key=lambda item: item.index)
            if len(ordered) != len(batch):
                raise PipelineError("embedding response length does not match the batch")
            fresh: list[np.ndarray] = []
            for item in ordered:
                vector = np.asarray(item.embedding, dtype=np.float32)
                norm = float(np.linalg.norm(vector))
                if norm == 0.0:
                    raise PipelineError("embedding provider returned a zero vector")
                fresh.append(vector / norm)
            block = np.vstack(fresh)
            if self._matrix is None:
                self._matrix = block
            else:
                self._matrix = np.vstack([self._matrix, block])
            offset = len(self._texts)
            self._texts.extend(batch)
            for i, text in enumerate(batch):
                self._index[text] = offset + i
            self.new_texts += len(batch)
            self._save()
            logger.info("embedded %s/%s", min(start + config.EMBED_BATCH, len(missing)), len(missing))


def embed_catalog(embedder: Embedder, names: list[str], *, allow_large: bool | None = None) -> None:
    """Cache every price position. A full uncached catalog is not sent unless allowed.

    Re-embeds only names that are absent from the cache. The same file therefore
    does not call the model again. ``MATCH_ALLOW_CATALOG_EMBED=1`` (or an
    explicit ``allow_large``) is required when more than one batch is missing.
    """
    missing = embedder.missing(names)
    if not missing:
        return
    if len(missing) > config.EMBED_BATCH and not config.catalog_embed_allowed(allow_large):
        raise CatalogEmbedRequired(
            f"price embeddings are not cached ({len(missing)} positions); "
            "the model was not called"
        )
    embedder.embed(missing)
