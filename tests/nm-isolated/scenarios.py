"""Assertions for delayed Update2 scenarios over an injected fixture adapter.

Adapter operations must return only after the described action is confirmed. This module does not open a bus, start services or establish isolation.
"""


def no_fence(adapter, metric):
    """Confirm an unfenced held update takes effect after its client disconnects."""
    before = adapter.profile()
    if before.metric == metric:
        raise ValueError("test metric must differ from initial metric")
    request = adapter.submit_held(metric=metric, fenced=False)
    try:
        adapter.disconnect(request)
        adapter.release(request)
        adapter.await_completion(request)
        after = adapter.profile()
        if after.metric != metric or after.version <= before.version:
            raise AssertionError("unfenced delayed update did not change metric and version")
        return before, after
    except BaseException:
        adapter.retire()
        raise


def fenced(adapter, metric):
    """Confirm an identical separately authorised update fences a held update."""
    before = adapter.profile()
    if before.metric == metric:
        raise ValueError("test metric must differ from initial metric")
    request = adapter.submit_held(metric=metric, fenced=True)
    try:
        adapter.disconnect(request)
        try:
            adapter.allow_later()
            adapter.update_separately(metric=before.metric, fenced=True)
        finally:
            adapter.hold_later()
        fence = adapter.profile()
        if fence.metric != before.metric or fence.version <= before.version:
            raise AssertionError("separate identical update did not advance version")
        adapter.release(request)
        adapter.await_rejection(request)
        after = adapter.profile()
        if after.metric != before.metric or after.version != fence.version:
            raise AssertionError("delayed fenced update changed profile")
        return before, fence, after
    except BaseException:
        adapter.retire()
        raise


def restart(adapter):
    """Confirm daemon identity changes and inspect the resulting profile version."""
    owner_before = adapter.owner()
    profile_before = adapter.profile()
    adapter.restart()
    owner_after = adapter.owner()
    profile_after = adapter.profile()
    if not owner_before or not owner_after or owner_before == owner_after:
        raise AssertionError("daemon unique owner did not change")
    if profile_before.version <= 0 or profile_after.version <= 0:
        raise AssertionError("profile version must be non-zero")
    return owner_before, profile_before, owner_after, profile_after
