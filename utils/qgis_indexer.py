# -*- coding: utf-8 -*-
"""
QGIS Processing toolbox vector indexer — semantic search for QGIS algorithms.

Builds a vector index of all available QGIS processing algorithms and
provides cosine-similarity search.  Falls back to a local bag-of-words
embedding when no external embedding API is available.
"""
import os
import json
import numpy as np
from qgis.core import QgsApplication, QgsProcessingAlgorithm

from ..core.logger import get_logger

logger = get_logger("utils.qgis_indexer")

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class QgisToolVectorIndexer:
    """Singleton indexer for QGIS processing algorithms with semantic search."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, cache_dir: str = None, api_key: str = "", base_url: str = ""):
        if self._initialized:
            return
        self.cache_dir = cache_dir or os.path.dirname(os.path.dirname(__file__))
        self.index_file = os.path.join(self.cache_dir, "qgis_tools_index.json")
        self.vectors_file = os.path.join(self.cache_dir, "qgis_tools_vectors.npy")

        self.tools_metadata: list = []
        self.vectors: np.ndarray = None
        self.api_key = api_key
        self.base_url = base_url
        self._load_or_build_index()
        self._initialized = True

    # -- Embedding ----------------------------------------------------------

    def _get_embedding(self, texts: list) -> np.ndarray:
        """Get text embeddings via API, falling back to local bag-of-words."""
        if self.api_key and OpenAI:
            try:
                client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url or "https://api.deepseek.com",
                )
                resp = client.embeddings.create(
                    input=texts, model="text-embedding-3-small"
                )
                embeddings = [item.embedding for item in resp.data]
                return np.array(embeddings, dtype=np.float32)
            except Exception as e:
                logger.warning("Remote embedding failed, using local fallback: %s", e)

        return self._local_bag_of_words_embedding(texts)

    @staticmethod
    def _local_bag_of_words_embedding(texts: list) -> np.ndarray:
        """Lightweight character-level n-gram embedding for offline use."""
        dim = 256
        vectors = []
        for text in texts:
            vec = np.zeros(dim, dtype=np.float32)
            for ch in text:
                idx = hash(ch) % dim
                vec[idx] += 1.0
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec /= norm
            vectors.append(vec)
        return np.array(vectors, dtype=np.float32)

    # -- Index management ---------------------------------------------------

    def _load_or_build_index(self):
        """Load cached index or rebuild from scratch."""
        if os.path.exists(self.index_file) and os.path.exists(self.vectors_file):
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    self.tools_metadata = json.load(f)
                self.vectors = np.load(self.vectors_file)
                logger.info("Loaded cached index: %d algorithms", len(self.tools_metadata))
                return
            except Exception as e:
                logger.warning("Cache read failed, rebuilding: %s", e)

        self.rebuild_index()

    def rebuild_index(self):
        """Scan the QGIS processing registry and build the vector index."""
        registry = QgsApplication.processingRegistry()
        algs = registry.algorithms()

        self.tools_metadata = []
        doc_texts = []

        for alg in algs:
            if alg.flags() & QgsProcessingAlgorithm.FlagHideFromToolbox:
                continue

            alg_id = alg.id()
            name = alg.displayName()
            group = alg.group()
            desc = alg.shortDescription() or alg.displayName()

            doc_str = f"算法ID: {alg_id} | 算法名称: {name} | 分组: {group} | 描述: {desc}"

            self.tools_metadata.append({
                "id": alg_id,
                "name": name,
                "group": group,
                "description": desc,
            })
            doc_texts.append(doc_str)

        if not doc_texts:
            return

        logger.info("Generating embeddings for %d algorithms...", len(doc_texts))
        batch_size = 100
        all_vecs = []
        for i in range(0, len(doc_texts), batch_size):
            batch = doc_texts[i : i + batch_size]
            vecs = self._get_embedding(batch)
            all_vecs.append(vecs)

        self.vectors = np.vstack(all_vecs)
        norms = np.linalg.norm(self.vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1e-10
        self.vectors = self.vectors / norms

        try:
            with open(self.index_file, "w", encoding="utf-8") as f:
                json.dump(self.tools_metadata, f, ensure_ascii=False, indent=2)
            np.save(self.vectors_file, self.vectors)
            logger.info("Index built and cached successfully")
        except Exception as e:
            logger.warning("Cache write failed: %s", e)

    def search(self, query: str, top_k: int = 5) -> list:
        """Return the top-k most similar QGIS algorithms for *query*."""
        if self.vectors is None or len(self.tools_metadata) == 0:
            return []

        q_vec = self._get_embedding([query])[0]
        norm = np.linalg.norm(q_vec)
        if norm > 0:
            q_vec /= norm

        scores = np.dot(self.vectors, q_vec)
        top_indices = np.argsort(scores)[::-1][:top_k]

        results = []
        for idx in top_indices:
            item = dict(self.tools_metadata[idx])
            item["score"] = float(scores[idx])
            results.append(item)
        return results
