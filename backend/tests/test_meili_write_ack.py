"""Document-write acknowledgement (2026-08-18): upsert/replace/delete wait a
bounded time for their Meili task and raise MeiliWriteFailed on a definitive
'failed' -- the class of silent loss that hid the _vectors incident for weeks."""

from __future__ import annotations

import contextlib

import pytest

from filearr import search as search_mod
from filearr.config import get_settings


class _Info:
    def __init__(self, uid):
        self.task_uid = uid


class _Res:
    def __init__(self, status, error=None):
        self.status = status
        self.error = error


class _Idx:
    def __init__(self, uid=7):
        self.uid = uid
        self.calls = []

    async def update_documents(self, docs, primary_key=None):
        self.calls.append(("upsert", len(docs)))
        return _Info(self.uid)

    async def add_documents(self, docs, primary_key=None):
        self.calls.append(("replace", len(docs)))
        return _Info(self.uid)

    async def delete_documents(self, ids):
        self.calls.append(("delete", len(ids)))
        return _Info(self.uid)


class _Client:
    def __init__(self, idx, result=None, wait_exc=None, waits=None):
        self.idx = idx
        self.result = result
        self.wait_exc = wait_exc
        self.waits = waits if waits is not None else []

    def index(self, name):
        return self.idx

    async def wait_for_task(self, uid, *, timeout_in_ms=None, **kw):
        self.waits.append((uid, timeout_in_ms))
        if self.wait_exc:
            raise self.wait_exc
        return self.result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def ack(monkeypatch):
    monkeypatch.setattr(get_settings(), "meili_write_ack_seconds", 12.0)

    def _use(client):
        monkeypatch.setattr(search_mod, "client", lambda: client)
        return client

    return _use


async def test_failed_task_raises_with_meili_error(ack):
    idx = _Idx()
    err = {"code": "vector_embedding_error", "message": "no vectors"}
    c = ack(_Client(idx, _Res("failed", err)))
    with pytest.raises(search_mod.MeiliWriteFailed) as ei:
        await search_mod.upsert_docs([{"id": "a"}])
    assert ei.value.code == "vector_embedding_error" and "no vectors" in str(ei.value)
    assert c.waits == [(7, 12_000)]


async def test_succeeded_and_still_processing_are_fine(ack):
    idx = _Idx()
    ack(_Client(idx, _Res("succeeded")))
    await search_mod.replace_docs([{"id": "a"}])
    ack(_Client(idx, _Res("processing")))  # budget ran out while queued: trusted
    await search_mod.delete_docs(["a"])
    ack(_Client(idx, wait_exc=TimeoutError("slow")))  # SDK timeout: trusted
    await search_mod.upsert_docs([{"id": "a"}])
    assert [k for k, _ in idx.calls] == ["replace", "delete", "upsert"]


async def test_ack_disabled_never_waits(ack, monkeypatch):
    monkeypatch.setattr(get_settings(), "meili_write_ack_seconds", 0)
    c = ack(_Client(_Idx(), _Res("failed", {"code": "x", "message": "y"})))
    await search_mod.upsert_docs([{"id": "a"}])  # fire-and-forget: no raise
    assert c.waits == []


async def test_fake_client_without_wait_is_tolerated(ack):
    class _Bare:
        def index(self, name):
            return _Idx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    ack(_Bare())
    with contextlib.nullcontext():
        await search_mod.upsert_docs([{"id": "a"}])
