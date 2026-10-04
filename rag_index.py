"""
RAG Index & Retriever for PHOENIX (mark 3).

- Scans a root folder (project + knowledge/) for text-ish files.
- Chunks documents, embeds with sentence-transformers (all-MiniLM-L6-v2).
- Incremental re-index reduced to changed/new/deleted files (mtime + size).
- retrieve(query) -> trimmed [KNOWLEDGE] block with source-pathed citations.

Store: <root>/rag_index.json + <root>/rag_embeddings.npy
"""

import os
import re
import json
import time
import hashlib
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
SUPPORTED_EXTS = {".md", ".txt", ".py", ".json", ".jsonl", ".csv", ".log", ".toml", ".yaml", ".yml"}
# Transcripts + bot source were being fed back as "knowledge", making the model
# echo its own action logs back into the prompt (and TTS). Keep RAG to the
# user's actual knowledge folder; never index our own live logs or source.
SKIP_DIRS = {
    "__pycache__", ".git", ".venv", "venv", "env", "node_modules",
    ".claude", ".opencode", "Dependencies", "dependencies", "dashboard",
    "qa_harness", "scratch", "transcripts", "transcripts_backup", "logs",
}
NOISE_SUFFIXES = {".py", ".jsonl", ".log"}
INDEX_FILE = "rag_index.json"
EMBEDDINGS_FILE = "rag_embeddings.npy"
MAX_FILE_BYTES = 300_000
CHUNK_SIZE = 512
CHUNK_OVERLAP = 64


class RagIndex:
    def __init__(self, root: str, top_k: int = 5, min_sim: float = 0.30, max_chars: int = 1800):
        self.root = Path(root)
        self.index_path = self.root / INDEX_FILE
        self.embeddings_path = self.root / EMBEDDINGS_FILE
        self.top_k = top_k
        self.min_sim = min_sim
        self.max_chars = max_chars
        try:
            self.model = SentenceTransformer(EMBEDDING_MODEL, device='cpu')
        except Exception as e:
            raise RuntimeError(f"RagIndex needs sentence-transformers: {e}") from e
        self.chunks = []      # list of {id,path,rel,text,mtime,size,added}
        self.embeddings = []  # aligned list-of-list with chunks
        self._scan_roots = []
        for cand in (self.root / "knowledge", self.root):
            self._scan_roots.append(cand)
        self._load()

    # ---------------- persistence ----------------
    def _load(self):
        if self.index_path.exists():
            try:
                self.chunks = json.loads(self.index_path.read_text(encoding="utf-8")).get("chunks", [])
            except Exception:
                self.chunks = []
        if self.embeddings_path.exists():
            try:
                self.embeddings = np.load(self.embeddings_path, allow_pickle=True).tolist()
            except Exception:
                self.embeddings = []
        if not isinstance(self.chunks, list) or not isinstance(self.embeddings, list) \
                or len(self.chunks) != len(self.embeddings):
            self.chunks = []
            self.embeddings = []

    def _save(self):
        self.index_path.write_text(json.dumps({"chunks": self.chunks}, indent=2), encoding="utf-8")
        if self.embeddings:
            np.save(self.embeddings_path, np.array(self.embeddings, dtype=object))

    # ---------------- discovery ----------------
    def _iter_files(self):
        seen = set()
        for base in self._scan_roots:
            if not base.exists():
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                for f in files:
                    p = Path(root) / f
                    if p.name in (INDEX_FILE, EMBEDDINGS_FILE):
                        continue
                    if p.suffix.lower() not in SUPPORTED_EXTS:
                        continue
                    if p.suffix.lower() in NOISE_SUFFIXES:
                        continue
                    # Never merge the bot's own dialogue transcripts as knowledge:
                    # they are chat history, not facts, and leak [User]/[PHOENIX]/[action] lines.
                    parts = set(p.parts)
                    if any(k in parts for k in ("transcripts", "logs", "log")):
                        continue
                    try:
                        if p.stat().st_size > MAX_FILE_BYTES:
                            continue
                    except OSError:
                        continue
                    key = str(p)
                    if key not in seen:
                        seen.add(key)
                        yield p

    _chunk_re = re.compile(r"\n{2,}|(?<=[.!?])\s+(?=[A-Z\"'(])")

    def _read_units(self, path: Path):
        """Yield text units. .jsonl yields one labeled line per entry."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []
        if path.suffix.lower() == ".jsonl":
            units = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                role = str(obj.get("role", "sys"))
                content = str(obj.get("content", "")).strip()
                if content:
                    label = "User" if role == "user" else "PHOENIX" if role in ("assistant", "system") else role
                    units.append(f"[{label}] {content}")
            return units
        return [text]

    def _chunk_text(self, text: str):
        text = text.strip()
        if not text:
            return []
        paragraphs = [p.strip() for p in self._chunk_re.split(text) if p.strip()]
        merged = []
        cur = ""
        carry = ""
        for para in paragraphs:
            if not cur and carry:
                cur = carry
            if len(cur) + len(para) + 1 <= CHUNK_SIZE:
                cur = (cur + "\n" + para).strip() if cur else para
                continue
            if cur:
                merged.append(cur)
            while len(para) > CHUNK_SIZE:
                merged.append(para[:CHUNK_SIZE])
                para = para[CHUNK_SIZE - CHUNK_OVERLAP:]
            cur = para
            carry = ""
        if cur:
            merged.append(cur)
        return merged

    # ---------------- build ----------------
    def build(self, force: bool = False):
        sig_map = {}
        for p in self._iter_files():
            try:
                mtime, size = p.stat().st_mtime, p.stat().st_size
            except OSError:
                continue
            sig_map[str(p)] = (mtime, size)

        files = set(sig_map)
        emb_by_id = {c["id"]: e for c, e in zip(self.chunks, self.embeddings)}

        keep_chunks = []
        needs = set()
        for fp in files:
            hits = [c for c in self.chunks if c["path"] == fp]
            mtime, size = sig_map[fp]
            if force or not hits:
                needs.add(fp)
                continue
            first = hits[0]
            if abs(first["mtime"] - mtime) > 0.5 or first["size"] != size:
                needs.add(fp)
            else:
                keep_chunks.extend(hits)

        # chunks whose file disappeared are dropped by keeping only hits of files present
        keep_chunks = [c for c in keep_chunks if c["path"] in files]

        new_texts, new_meta = [], []
        for fp in needs:
            p = Path(fp)
            mtime, size = sig_map[fp]
            units = self._read_units(p)
            texts = self._chunk_text("\n\n".join(units))
            rel = os.path.relpath(fp, self.root).replace("\\", "/")
            for i, txt in enumerate(texts):
                cid = hashlib.sha256(f"{fp}:{i}".encode()).hexdigest()[:16]
                new_texts.append(txt)
                new_meta.append({
                    "id": cid, "path": fp, "rel": rel, "text": txt,
                    "mtime": mtime, "size": size, "added": time.time(),
                })

        combined = keep_chunks + new_meta
        emb = [emb_by_id.get(c["id"]) for c in combined]
        todo = [(i, c["text"]) for i, c in enumerate(combined) if emb[i] is None]
        if todo:
            vals = self.model.encode([t for _, t in todo], batch_size=16, show_progress_bar=False)
            for (i, _), v in zip(todo, vals):
                emb[i] = v

        self.chunks = combined
        self.embeddings = emb
        self._save()
        return len(new_texts)

    def stats(self):
        return {"chunks": len(self.chunks), "files": len({c["path"] for c in self.chunks})}

    # ---------------- retrieval ----------------
    _STOP_TOKENS = {"the", "a", "an", "i", "you", "what", "are", "is", "of", "for",
                    "to", "on", "in", "and", "or", "do", "does", "my", "your", "our",
                    "it", "its", "this", "that", "with", "as", "at", "be", "can",
                    "we", "me", "us", "please", "tell", "about", "me"}

    def _query_tokens(self, query: str):
        toks = re.findall(r"[a-z0-9_]+", query.lower())
        return [t for t in toks if t not in self._STOP_TOKENS and len(t) > 1]

    def retrieve(self, query: str, top_k: int = None, exclude_rel: list = None) -> str:
        top_k = top_k or self.top_k
        if not self.chunks or not self.embeddings or not (query or "").strip():
            return ""
        q_tokens = self._query_tokens(query)
        try:
            q_emb = self.model.encode([query])[0]
        except Exception:
            return ""
        sims = cosine_similarity([q_emb], np.array(self.embeddings, dtype=float))[0]
        now = time.time()
        exclude_rel = set(exclude_rel or [])
        scored = []
        for idx, c in enumerate(self.chunks):
            if c.get("rel") in exclude_rel:
                continue
            sim = float(sims[idx])
            text_lower = c["text"].lower()
            overlap = 0.0
            if q_tokens:
                hits = sum(1 for t in q_tokens if t in text_lower)
                overlap = float(hits) / len(q_tokens)
            age_days = (now - c.get("mtime", now)) / 86400.0
            recency = max(0.0, 1.0 - age_days / 120.0)
            final = 0.45 * sim + 0.40 * overlap + 0.15 * recency
            if final < self.min_sim:
                continue
            scored.append((final, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        lines, used = [], 0
        for _, c in scored[:top_k]:
            line = f"[{c.get('rel') or c.get('path')}] {c['text']}"
            if used + len(line) + 1 > self.max_chars:
                remain = self.max_chars - used
                if remain > 120:
                    lines.append(line[:remain].rstrip() + "…")
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return ""
        return "\n".join(lines)


def build_index_if_stale(root: str) -> RagIndex:
    idx = RagIndex(root)
    added = idx.build()
    if added:
        print(f"[RAG] Indexed {added} new chunks | {idx.stats()}")
    return idx