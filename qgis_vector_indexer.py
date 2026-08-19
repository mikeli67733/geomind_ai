# -*- coding: utf-8 -*-
"""
qgis_vector_indexer.py
QGIS Processing 工具箱算法向量化与语义检索索引器
"""
import os
import json
import numpy as np
from qgis.core import QgsApplication, QgsProcessingAlgorithm

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class QgisToolVectorIndexer:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(QgisToolVectorIndexer, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, cache_dir: str = None, api_key: str = "", base_url: str = ""):
        if self._initialized:
            return
        self.cache_dir = cache_dir or os.path.dirname(__file__)
        self.index_file = os.path.join(self.cache_dir, "qgis_tools_index.json")
        self.vectors_file = os.path.join(self.cache_dir, "qgis_tools_vectors.npy")

        self.tools_metadata = []  # 存储算子元数据
        self.vectors = None  # 存储向量 numpy 矩阵
        self.api_key = api_key
        self.base_url = base_url
        self._load_or_build_index()
        self._initialized = True

    def _get_embedding(self, texts: list) -> np.ndarray:
        """获取文本 Embedding 向量（优先使用配置的 API，否则使用简易本地词频向量兜底）"""
        if self.api_key and OpenAI:
            try:
                client = OpenAI(api_key=self.api_key, base_url=self.base_url or "https://api.deepseek.com")
                # 兼容通用的 embedding 端点
                resp = client.embeddings.create(
                    input=texts,
                    model="text-embedding-3-small"
                )
                embeddings = [item.embedding for item in resp.data]
                return np.array(embeddings, dtype=np.float32)
            except Exception as e:
                print(f"[QgisIndexer] 远程 Embedding 失败，降级本地轻量词频匹配: {e}")

        # 本地 TF 简易轻量向量化兜底（零外部依赖）
        return self._local_bag_of_words_embedding(texts)

    def _local_bag_of_words_embedding(self, texts: list) -> np.ndarray:
        """极简的本地 n-gram / 字符特征向量，用于无网络/无 API 时的向量相似度匹配"""
        dim = 256
        vectors = []
        for text in texts:
            vec = np.zeros(dim, dtype=np.float32)
            for i, ch in enumerate(text):
                idx = hash(ch) % dim
                vec[idx] += 1.0
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec /= norm
            vectors.append(vec)
        return np.array(vectors, dtype=np.float32)

    def _load_or_build_index(self):
        """加载已有索引或初次全量构建索引"""
        if os.path.exists(self.index_file) and os.path.exists(self.vectors_file):
            try:
                with open(self.index_file, 'r', encoding='utf-8') as f:
                    self.tools_metadata = json.load(f)
                self.vectors = np.load(self.vectors_file)
                print(f"[QgisIndexer] 成功加载本地缓存索引: {len(self.tools_metadata)} 个 QGIS 算子")
                return
            except Exception as e:
                print(f"[QgisIndexer] 读取缓存失败，将重新构建: {e}")

        self.rebuild_index()

    def rebuild_index(self):
        """扫描 QGIS 工具箱构建索引"""
        registry = QgsApplication.processingRegistry()
        algs = registry.algorithms()

        self.tools_metadata = []
        doc_texts = []

        for alg in algs:
            # 过滤内部无用算子
            if alg.flags() & QgsProcessingAlgorithm.FlagHideFromToolbox:
                continue

            alg_id = alg.id()  # e.g. "native:buffer"
            name = alg.displayName()  # e.g. "缓冲区"
            group = alg.group()  # e.g. "矢量几何图形"
            desc = alg.shortDescription() or alg.displayName()

            # 拼接用于检索的富文本
            doc_str = f"算法ID: {alg_id} | 算法名称: {name} | 分组: {group} | 描述: {desc}"

            self.tools_metadata.append({
                "id": alg_id,
                "name": name,
                "group": group,
                "description": desc
            })
            doc_texts.append(doc_str)

        if not doc_texts:
            return

        print(f"[QgisIndexer] 正在为 {len(doc_texts)} 个 QGIS 算子生成语义向量...")
        # 分批计算 Embedding
        batch_size = 100
        all_vecs = []
        for i in range(0, len(doc_texts), batch_size):
            batch = doc_texts[i:i + batch_size]
            vecs = self._get_embedding(batch)
            all_vecs.append(vecs)

        self.vectors = np.vstack(all_vecs)

        # 归一化便于余弦相似度计算 (Dot Product)
        norms = np.linalg.norm(self.vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1e-10
        self.vectors = self.vectors / norms

        # 保存到本地文件缓存
        try:
            with open(self.index_file, 'w', encoding='utf-8') as f:
                json.dump(self.tools_metadata, f, ensure_ascii=False, indent=2)
            np.save(self.vectors_file, self.vectors)
            print("[QgisIndexer] 索引构建完成并已缓存。")
        except Exception as e:
            print(f"[QgisIndexer] 缓存写入失败: {e}")

    def search(self, query: str, top_k: int = 5) -> list:
        """语义检索最相关的 QGIS 算法"""
        if self.vectors is None or len(self.tools_metadata) == 0:
            return []

        q_vec = self._get_embedding([query])[0]
        norm = np.linalg.norm(q_vec)
        if norm > 0:
            q_vec /= norm

        # 计算余弦相似度
        scores = np.dot(self.vectors, q_vec)
        top_indices = np.argsort(scores)[::-1][:top_k]

        results = []
        for idx in top_indices:
            item = dict(self.tools_metadata[idx])
            item["score"] = float(scores[idx])
            results.append(item)
        return results