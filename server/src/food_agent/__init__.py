"""MMMacros food agent v1.

One agent per turn: the model returns a typed :class:`contracts.AgentTurn`;
code validates it, runs typed tools, keeps a revisioned draft, shows a preview
bound to ``(draft_id, revision, content_hash)`` and commits only on a
code-detected user affirmation. See ``docs/agent/ADR-001.md``.
"""

from __future__ import annotations
