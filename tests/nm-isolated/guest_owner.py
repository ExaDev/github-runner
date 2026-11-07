"""Guarded composition of a guest's private fixture lifecycle."""

from contextlib import ExitStack, contextmanager

import networkmanager
import guest_factories
import adapter_factory as scenario_factory


@contextmanager
def guest_owner(*, timeout, adapter_factory=scenario_factory.adapter_factory, profile_factory=guest_factories.profile_factory, bus_factory=guest_factories.bus_factory, authority_factory=guest_factories.authority_factory, daemon_factory=guest_factories.daemon_factory, guard=networkmanager.verified_isolation):
    """Own bus, authority, daemon, test profile and adapter in teardown order.

    Factories are injected context managers. Production isolation verification currently always refuses, and no concrete profile factory is supplied.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    guard()
    with ExitStack() as stack:
        bus = stack.enter_context(bus_factory(timeout=timeout))
        authority = stack.enter_context(authority_factory(bus=bus, timeout=timeout))
        daemon = stack.enter_context(daemon_factory(bus=bus, timeout=timeout))
        profile = stack.enter_context(profile_factory(bus=bus, daemon=daemon, timeout=timeout))
        adapter = stack.enter_context(adapter_factory(bus=bus, authority=authority, daemon=daemon, profile=profile, timeout=timeout))
        yield adapter
