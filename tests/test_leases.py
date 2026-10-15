from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage
from test_agent import FakeRegistry

from codepilot.agent import CodePilot
from codepilot.leases import LeaseLostError, ThreadBusyError, ThreadLeaseManager


def test_two_instances_cannot_write_same_thread_concurrently(tmp_path):
    db = tmp_path / "state.db"
    started = threading.Event()
    release = threading.Event()
    first_registry = FakeRegistry()

    def delayed_reply(messages):
        started.set()
        assert release.wait(5)
        return AIMessage(content="first reply")

    first_registry.models["deepseek"].invoke = delayed_reply
    first = CodePilot(tmp_path, db, first_registry)
    session_id = first.new_session()
    second = CodePilot(tmp_path, db, FakeRegistry())
    second.resume(session_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first.send, "first message")
        assert started.wait(5)
        with pytest.raises(ThreadBusyError):
            second.send("second message")
        release.set()
        future.result(timeout=5)

    assert [str(message.content) for message in second.history()] == [
        "first message", "first reply",
    ]
    first.close()
    second.close()


def test_expired_lease_can_be_reclaimed(tmp_path):
    db = tmp_path / "state.db"
    first = ThreadLeaseManager(db, ttl_seconds=1, heartbeat_seconds=0.25)
    second = ThreadLeaseManager(db, ttl_seconds=1, heartbeat_seconds=0.25)
    first.acquire("thread-1")
    with pytest.raises(ThreadBusyError):
        second.acquire("thread-1")

    time.sleep(1.05)
    second.acquire("thread-1")
    assert second.status("thread-1")["owned_by_this_process"] is True
    second.release("thread-1")
    first.close()
    second.close()


def test_fencing_token_blocks_a_stale_owner(tmp_path):
    db = tmp_path / "state.db"
    first = ThreadLeaseManager(db, ttl_seconds=1, heartbeat_seconds=0.25)
    second = ThreadLeaseManager(db, ttl_seconds=1, heartbeat_seconds=0.25)
    first_token = first.acquire("thread-1")

    time.sleep(1.05)
    second_token = second.acquire("thread-1")

    assert second_token > first_token
    assert second.assert_current("thread-1") == second_token
    with pytest.raises(LeaseLostError):
        first.assert_current("thread-1", first_token)
    first.release("thread-1")
    assert second.status("thread-1")["fence_token"] == second_token
    first.close()
    second.close()


def test_completed_approval_cannot_be_resumed_twice(tmp_path):
    db = tmp_path / "state.db"
    call = {
        "name": "write_file",
        "args": {"path": "once.txt", "content": "created once"},
        "id": "write-once",
    }
    first = CodePilot(tmp_path, db, FakeRegistry(call))
    session_id = first.new_session()
    assert "__interrupt__" in first.send("create the file")
    second = CodePilot(tmp_path, db, FakeRegistry(call))
    second.resume(session_id)

    first.approve({"write-once": True})
    with pytest.raises(ValueError, match="No pending tool approval"):
        second.approve({"write-once": True})

    assert (tmp_path / "once.txt").read_text(encoding="utf-8") == "created once"
    first.close()
    second.close()
