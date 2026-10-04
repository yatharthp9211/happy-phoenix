import os
import json
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import warnings
from telemetry import publish_event

import time

MEMORY_FILE = "memory.json"
EMBEDDINGS_FILE = "memory_embeddings.npy"

class SemanticMemory:
    def __init__(self, model_name='all-MiniLM-L6-v2'):
        self.model = SentenceTransformer(model_name, device='cpu')
        self.memories = self._load_data()
        self.embeddings = self._load_embeddings()
        
        # Sync embeddings if data was modified externally
        self._sync()

    def _load_data(self):
        default_data = {"memories": []}
        if not os.path.exists(MEMORY_FILE):
            return default_data["memories"]
        try:
            with open(MEMORY_FILE, "r") as f:
                data = json.load(f)
                
                # Migrate legacy schema to new schema
                if "memories" not in data:
                    new_memories = []
                    for k, v in data.items():
                        if isinstance(v, list):
                            for item in v:
                                if isinstance(item, str):
                                    new_memories.append({
                                        "text": item,
                                        "type": k,
                                        "topic": "general",
                                        "importance": 0.5,
                                        "frequency": 1,
                                        "created": time.time(),
                                        "last_seen": time.time()
                                    })
                    return new_memories
                return data.get("memories", [])
        except:
            return default_data["memories"]

    def _save_data(self):
        with open(MEMORY_FILE, "w") as f:
            json.dump({"memories": self.memories}, f, indent=2)

    def _load_embeddings(self):
        if os.path.exists(EMBEDDINGS_FILE):
            try:
                return np.load(EMBEDDINGS_FILE).tolist()
            except:
                return []
        return []

    def _save_embeddings(self):
        np.save(EMBEDDINGS_FILE, np.array(self.embeddings))

    def _sync(self):
        if len(self.memories) != len(self.embeddings):
            print(f"Syncing memory embeddings... ({len(self.memories)} facts)")
            if len(self.memories) == 0:
                self.embeddings = []
            else:
                strings = [m["text"] for m in self.memories]
                self.embeddings = self.model.encode(strings).tolist()
            self._save_embeddings()

    def add_memory(self, text, memory_type="fact", topic="general", importance=0.5, speaker="user", evidence="", confidence=0.8):
        new_embedding = self.model.encode([text])[0]
        
        # Deduplication check
        if self.embeddings is not None and len(self.embeddings) > 0:
            similarities = cosine_similarity([new_embedding], self.embeddings)[0]
            max_sim_idx = np.argmax(similarities)
            if similarities[max_sim_idx] > 0.85:
                # Update existing memory — confidence decay if contradicting
                mem = self.memories[max_sim_idx]
                mem["frequency"] = mem.get("frequency", 1) + 1
                mem["last_seen"] = time.time()
                mem["importance"] = max(mem.get("importance", 0.5), importance)
                
                # If the new text is very similar but not identical, it might be 
                # a refinement. Keep the newer version if confidence is higher.
                if similarities[max_sim_idx] < 0.95 and confidence > mem.get("confidence", 0.5):
                    # Decay old confidence, adopt new text
                    mem["confidence"] = mem.get("confidence", 0.5) * 0.5
                    mem["text"] = text
                    mem["evidence"] = evidence
                    mem["confidence"] = confidence
                else:
                    # Pure duplicate — boost confidence slightly
                    mem["confidence"] = min(1.0, mem.get("confidence", 0.5) + 0.05)
                
                self._save_data()
                print(f"[SEMANTIC MEMORY UPDATED DUP]: {mem['text']}")
                publish_event("memory", "memory_duplicate_merged", "INFO", {"text": mem["text"], "frequency": mem["frequency"]})
                return
                
        # Completely new memory with full provenance
        import uuid
        memory_obj = {
            "id": str(uuid.uuid4()),
            "text": text,
            "type": memory_type,
            "topic": topic,
            "importance": importance,
            "confidence": confidence,
            "speaker": speaker,
            "evidence": evidence,
            "created": time.time(),
            "last_seen": time.time(),
            "frequency": 1
        }
        
        self.memories.append(memory_obj)
        if self.embeddings is None:
            self.embeddings = [new_embedding]
        else:
            self.embeddings = np.vstack((self.embeddings, new_embedding))
            
        self._save_data()
        self._save_embeddings()
        print(f"[SEMANTIC MEMORY ADDED]: {text}")
        publish_event("memory", "memory_added", "INFO", {"text": text, "type": memory_type, "topic": topic, "evidence": evidence})

    def retrieve_relevant(self, query, active_context=None, top_k=5):
        if not self.memories:
            return ""
            
        query_emb = self.model.encode([query])[0]
        similarities = cosine_similarity([query_emb], self.embeddings)[0]
        
        current_time = time.time()
        scored_memories = []
        
        active_topic = active_context.get("topic", "general").lower() if active_context else ""
        
        for idx, mem in enumerate(self.memories):
            sim = similarities[idx]
            
            # Normalize frequency (cap at 10)
            freq_score = min(mem.get("frequency", 1) / 10.0, 1.0)
            
            # Recency factor (decays over 30 days)
            age_days = (current_time - mem.get("last_seen", current_time)) / (86400)
            recency_score = max(0.0, 1.0 - (age_days / 30.0))
            
            # Context match
            context_match = 0.0
            if active_topic and active_topic != "general":
                mem_topic = mem.get("topic", "").lower()
                if active_topic in mem_topic or mem_topic in active_topic:
                    context_match = 1.0
                elif mem.get("type") in ["preference", "identity", "goal"]:
                    context_match = 0.5 # Global traits are partially matched always
            else:
                context_match = 0.5 # Neutral fallback if no strong context
                
            imp = mem.get("importance", 0.5)
            
            # 0.40 similarity + 0.25 context_match + 0.15 importance + 0.10 frequency + 0.10 recency
            final_score = (0.40 * sim) + (0.25 * context_match) + (0.15 * imp) + (0.10 * freq_score) + (0.10 * recency_score)
            
            if final_score >= 0.65:
                scored_memories.append((final_score, mem))
                
        # Sort by score descending
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        top_mems = scored_memories[:top_k]
        
        if not top_mems:
            return ""
            
        # Format the output grouped by type
        formatted = []
        for _, m in top_mems:
            formatted.append(f"[{m.get('type', 'fact').upper()}] {m['text']}")
            
        return "\n".join(formatted)
