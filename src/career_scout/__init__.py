"""Career Scout — a local-first job and scholarship agent.

Everything about a user lives on that user's own machine, under ``CAREER_SCOUT_HOME``
(see :mod:`career_scout.store.paths`). There is no server, no shared database, and no
account on anyone else's infrastructure. A hundred users is a hundred
independent installs that never meet.

Two things may leave the machine, and both pass through audited chokepoints:

* outbound HTTP to enumerated opportunity sources — :mod:`career_scout.net`
* messages sent from the user's own Gmail — :mod:`career_scout.send`

Nothing else opens a socket. ``test_egress_chokepoint_runtime`` asserts it by
patching ``socket.socket.connect`` and running a full pipeline pass.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
