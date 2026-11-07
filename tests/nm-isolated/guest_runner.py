"""Dispatch fixture scenarios through a supplied guarded adapter context."""

from dataclasses import asdict

import scenarios
import guest_owner


def make_runner(adapter_factory):
    """Create a runner; the factory owns setup, isolation and teardown."""
    def run(name, metric, timeout):
        with adapter_factory(timeout=timeout) as adapter:
            if name == "no-fence":
                before, after = scenarios.no_fence(adapter, metric)
                return {"before": asdict(before), "after": asdict(after)}
            if name == "fenced":
                before, fence, after = scenarios.fenced(adapter, metric)
                return {"before": asdict(before), "fence": asdict(fence), "after": asdict(after)}
            raise ValueError("unknown scenario")
    return run


def guarded_adapter_factory(*, timeout):
    """Compose the guest owner, whose production guard still refuses startup."""
    return guest_owner.guest_owner(timeout=timeout)
