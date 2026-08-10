"""Final review, Important finding I4: the process-global checkpointer retained every
super-step's channel values -- including up to 1000 result rows per turn -- forever.
Measured on the pre-fix branch: ~90 KB per turn, 45 checkpoints after 5 turns, growing
with every new conversation_id and never pruned. These tests pin the ceiling.
"""
from copilot.agent import graph as agent_graph
from copilot.agent.checkpointer import BoundedInMemorySaver
from copilot.agent.pipeline import answer_question
from tests.conftest import FakeSnowflake
from tests.test_graph import PlanningFakeProvider


class WideResultSnowflake(FakeSnowflake):
    """Returns a wide result so retained rows dominate the saver's footprint."""

    def __init__(self, rows: int = 1000):
        super().__init__()
        self.result = (["MODEL", "SERIAL"], [(f"model-{i}", f"sn-{i:08d}") for i in range(rows)])


def _saver_size(saver) -> int:
    """Rough retained bytes: blobs hold the serialized channel values."""
    return sum(len(payload) for _kind, payload in saver.blobs.values())


def test_threads_are_capped_by_lru():
    saver = BoundedInMemorySaver(max_threads=5)
    for i in range(50):
        _write_turn(saver, f"conv-{i}")

    assert len(saver.storage) <= 5
    assert set(saver.storage) == {f"conv-{i}" for i in range(45, 50)}
    # Eviction must reclaim the payload, not just the index.
    assert all(key[0] in saver.storage for key in saver.blobs)
    assert all(key[0] in saver.storage for key in saver.writes)


def test_checkpoints_within_one_thread_are_capped():
    saver = BoundedInMemorySaver(max_checkpoints_per_thread=4)
    for _ in range(30):
        _write_turn(saver, "one-conversation")
    assert len(saver.storage["one-conversation"][""]) <= 4
    # Blobs for pruned checkpoints must go too, or the cap reclaims nothing.
    live = set()
    for stored in saver.storage["one-conversation"][""].values():
        saved = saver.serde.loads_typed(stored[0])
        live |= {(c, v) for c, v in (saved.get("channel_versions") or {}).items()}
    assert all((k[2], k[3]) in live for k in saver.blobs)


def _write_turn(saver, thread_id: str) -> None:
    """One put() with a chunky payload, mimicking a super-step's channel values."""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    import uuid

    from langgraph.checkpoint.base import empty_checkpoint

    checkpoint = empty_checkpoint()
    checkpoint["id"] = str(uuid.uuid1())
    checkpoint["channel_values"] = {"rows": [["x" * 64] * 8 for _ in range(64)]}
    checkpoint["channel_versions"] = {"rows": f"{len(saver.storage.get(thread_id, {})):032d}.0"}
    saver.put(config, checkpoint, {"source": "loop", "step": 1}, checkpoint["channel_versions"])


def test_many_conversations_do_not_grow_memory_without_bound():
    """End-to-end through answer_question: the retained size after many wide-result
    conversations must stay near the steady state, not keep climbing."""
    saver = agent_graph._CHECKPOINTER
    provider = PlanningFakeProvider()
    sf = WideResultSnowflake()

    for i in range(saver._max_threads):
        answer_question("Which models?", provider, sf, conversation_id=f"warm-{i}")
    baseline_threads = len(saver.storage)
    baseline_bytes = _saver_size(saver)

    for i in range(saver._max_threads * 3):
        answer_question("Which models?", provider, sf, conversation_id=f"flood-{i}")

    assert len(saver.storage) <= saver._max_threads
    assert baseline_threads <= saver._max_threads
    # 4x the conversations must not mean 4x the memory.
    assert _saver_size(saver) < baseline_bytes * 2, (
        f"retained bytes grew from {baseline_bytes} to {_saver_size(saver)}")


def test_long_conversation_does_not_grow_memory_without_bound():
    """The other axis: many turns on ONE conversation_id."""
    saver = agent_graph._CHECKPOINTER
    provider = PlanningFakeProvider()
    sf = WideResultSnowflake()
    thread = "single-long-conversation"

    for _ in range(5):
        answer_question("Which models?", provider, sf, conversation_id=thread)
    after_5_turns = _saver_size(saver)

    for _ in range(40):
        answer_question("Which models?", provider, sf, conversation_id=thread)

    assert len(saver.storage[thread][""]) <= saver._max_checkpoints_per_thread
    assert _saver_size(saver) < after_5_turns * 3, (
        f"retained bytes grew from {after_5_turns} to {_saver_size(saver)} over 45 turns")


def test_graph_uses_the_bounded_saver():
    assert isinstance(agent_graph._CHECKPOINTER, BoundedInMemorySaver)
