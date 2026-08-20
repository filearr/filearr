"""A benign shadow-delete race must not surface as "Latest Meili failure".

Live report 2026-08-19: the dashboard showed
``indexDeletion index_not_found Index 'items_rebuild_1787200229' not found`` as
the newest Meili failure. That is the shadow-swap rebuild's post-swap delete
racing the stale-shadow reaper over the same throwaway index — the index being
gone is the DESIRED end state, not a failure. These tests pin the filter.
"""

from __future__ import annotations

from filearr.meili_stats import _is_benign_failed_task, _recent_failed_tasks


def test_is_benign_only_for_index_deletion_not_found():
    benign = {"type": "indexDeletion", "error": {"code": "index_not_found"}}
    assert _is_benign_failed_task(benign)
    # A real failure of any other shape is NOT benign.
    assert not _is_benign_failed_task(
        {"type": "documentAdditionOrUpdate", "error": {"code": "vector_embedding_error"}}
    )
    assert not _is_benign_failed_task(
        {"type": "indexDeletion", "error": {"code": "internal"}}
    )
    assert not _is_benign_failed_task({"type": "indexDeletion"})  # no error dict


class _FakeHttp:
    def __init__(self, results):
        self._results = results

    async def get(self, _q):
        class _R:
            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        return _R({"total": len(self._results), "results": self._results})


class _FakeClient:
    def __init__(self, results):
        self._http_requests = _FakeHttp(results)


async def test_recent_failed_tasks_skips_benign_shadow_deletes():
    benign = {
        "uid": 5, "type": "indexDeletion", "finishedAt": "2026-08-19T23:30:29Z",
        "error": {"code": "index_not_found",
                  "message": "Index `items_rebuild_1787200229` not found."},
    }
    real = {
        "uid": 3, "type": "documentAdditionOrUpdate", "finishedAt": "2026-08-18T10:00:00Z",
        "error": {"code": "vector_embedding_error", "message": "missing _vectors"},
    }
    # Newest-first: the benign delete is newer, but the surfaced failure is the
    # real one, and the count discounts the benign entry.
    c = _FakeClient([benign, real])
    recent, total, newest = await _recent_failed_tasks(c, "items")
    assert newest is not None and newest["type"] == "documentAdditionOrUpdate"
    assert newest["code"] == "vector_embedding_error"
    assert total == 1 and recent == 1  # the benign one is discounted


async def test_recent_failed_tasks_all_benign_surfaces_nothing():
    benign = {
        "uid": 9, "type": "indexDeletion", "finishedAt": "2026-08-19T23:30:29Z",
        "error": {"code": "index_not_found", "message": "gone"},
    }
    c = _FakeClient([benign, {**benign, "uid": 8}])
    recent, total, newest = await _recent_failed_tasks(c, "items")
    assert newest is None and total == 0 and recent == 0
