"""The hosted-mode client (SPEC §9): transport, remote resolution, and the cache.

Standard library only. The published interface is :func:`lattice.remote.cache.catch_up`
and :func:`lattice.remote.cache.read_lock`, plus the stream consumer
:class:`lattice.remote.follower.Follower` (with :func:`~lattice.remote.follower.live_follower`
for readers, SPEC §9.5); everything else is private to the client.
"""
