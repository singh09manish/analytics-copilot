"""A bounded checkpointer for the agent graph's process-global conversation memory.

LangGraph's `InMemorySaver` never evicts anything. It writes one checkpoint per
super-step -- about nine per turn -- and each one carries a full copy of the channel
values, including `rows` (up to 1000) and the retrieved `context`. Measured on this
branch with a 1000-row result: ~90 KB retained per turn, growing linearly with both
the number of turns in a conversation and the number of conversations, with nothing
to evict it. An authenticated user looping distinct conversation_ids with wide
queries could grow RSS without limit.

The governance angle matters more than the RSS: an admin's *unmasked* result rows sat
in process memory for the lifetime of the process, which undercuts the masked/unmasked
story the whole demo rests on. Bounding the store puts a hard ceiling on how long any
row survives the request that produced it.

Two caps, because the growth has two dimensions:

- `max_threads`  -- an LRU over conversation threads; the least recently written
  thread is dropped whole (storage + writes + blobs) via `delete_thread`.
- `max_checkpoints_per_thread` -- inside a thread, only the most recent N checkpoints
  are kept. LangGraph resumes from the latest checkpoint, so older ones only serve
  time travel, which this app does not use. The cap is comfortably above one turn's
  super-step count so a turn in progress is never pruned out from under itself.

Evicting a thread costs that conversation its history: the next turn starts fresh
rather than failing. That is the right trade for a demo whose alternative is unbounded
growth.
"""
import threading
from collections import OrderedDict

from langgraph.checkpoint.memory import InMemorySaver

MAX_THREADS = 64
MAX_CHECKPOINTS_PER_THREAD = 24


class BoundedInMemorySaver(InMemorySaver):
    """InMemorySaver with an LRU cap on threads and a cap on checkpoints per thread."""

    def __init__(self, *, max_threads: int = MAX_THREADS,
                 max_checkpoints_per_thread: int = MAX_CHECKPOINTS_PER_THREAD, **kwargs):
        super().__init__(**kwargs)
        self._max_threads = max_threads
        self._max_checkpoints_per_thread = max_checkpoints_per_thread
        # Recency order only; the values are unused. Guarded by `_bound_lock` because
        # graph invocations run on FastAPI's threadpool, several at a time.
        self._recent_threads: OrderedDict[str, None] = OrderedDict()
        self._bound_lock = threading.Lock()

    def put(self, config, checkpoint, metadata, new_versions):
        result = super().put(config, checkpoint, metadata, new_versions)
        configurable = config.get("configurable") or {}
        thread_id = configurable.get("thread_id")
        if thread_id is not None:
            self._prune_thread(thread_id, configurable.get("checkpoint_ns", ""))
            self._evict_old_threads(thread_id)
        return result

    def _evict_old_threads(self, thread_id: str) -> None:
        with self._bound_lock:
            self._recent_threads.pop(thread_id, None)
            self._recent_threads[thread_id] = None
            evicted = []
            while len(self._recent_threads) > self._max_threads:
                evicted.append(self._recent_threads.popitem(last=False)[0])
        for victim in evicted:
            self.delete_thread(victim)

    def _prune_thread(self, thread_id: str, checkpoint_ns: str) -> None:
        """Keep only the newest checkpoints of one thread, plus the blobs they use.

        Checkpoint ids are UUIDv6, which sort lexicographically in creation order, so
        "newest" is just the tail of a sorted key list. Blobs are keyed by
        (thread, ns, channel, version) and are where the actual row data lives, so
        dropping checkpoints without dropping their now-unreferenced blobs would
        reclaim almost nothing -- the referenced set is recomputed from the survivors.
        """
        ns_store = self.storage.get(thread_id, {}).get(checkpoint_ns)
        if not ns_store or len(ns_store) <= self._max_checkpoints_per_thread:
            return
        with self._bound_lock:
            keep_ids = sorted(ns_store)[-self._max_checkpoints_per_thread:]
            keep = set(keep_ids)
            for checkpoint_id in [c for c in ns_store if c not in keep]:
                del ns_store[checkpoint_id]
                self.writes.pop((thread_id, checkpoint_ns, checkpoint_id), None)
            live_versions = set()
            for checkpoint_id in keep_ids:
                saved = self.serde.loads_typed(ns_store[checkpoint_id][0])
                for channel, version in (saved.get("channel_versions") or {}).items():
                    live_versions.add((channel, version))
            for key in list(self.blobs.keys()):
                if (key[0], key[1]) == (thread_id, checkpoint_ns) \
                        and (key[2], key[3]) not in live_versions:
                    del self.blobs[key]
