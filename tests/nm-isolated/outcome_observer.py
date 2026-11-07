"""Infer a disconnected request's outcome from controlled, observed state."""

import time


class OutcomeObserver:
    """Compare profile snapshots after an independently confirmed completion barrier.

    The barrier must establish that the daemon finished processing the specific authorisation, not merely that the authority sent a reply. The caller must exclude other writers. An unchanged profile cannot identify a rejection reason.
    """

    def __init__(self, reader, barrier, *, clock=time.monotonic):
        self.reader = reader
        self.barrier = barrier
        self.clock = clock
        self.requests = {}

    def register(self, request, before, expected_metric):
        if request in self.requests or before.metric == expected_metric:
            raise ValueError("request must be unique and change the metric")
        self.requests[request] = (before, expected_metric)

    def wait(self, request, *, timeout):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        before, expected_metric = self.requests[request]
        deadline = self.clock() + timeout
        if not self.barrier.finished(request, timeout=timeout):
            return "inconclusive"
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("outcome observation deadline expired")
        after = self.reader.profile(timeout=remaining)
        if after.metric == expected_metric and after.version > before.version:
            return "applied"
        if after == before:
            return "not-applied"
        return "inconclusive"
