"""Named root profiles for the Roots tab.

A profile is a saved snapshot of a root set: a list of writable host paths and
a list of read-only host paths. Profiles are persisted as JSON in one file
(`roots_profiles.json` by default) next to `roots.txt` / `readonly_roots.txt`.

Design:

- The store is a thin, dependency-free wrapper over a JSON file. It knows
  nothing about Docker or validation; it only stores and retrieves snapshots.
- Path validation stays in `sandbox.build_config`, so a profile is validated by
  the exact same rules that a manual save uses. A profile is never a bypass.
- Applying a profile is the caller's job: read the profile, then call
  `RootManager.apply(...)`. This module has no side effects on the sandbox.

The file format is deliberately forgiving on read: unknown keys are ignored and
missing keys default, so an older or hand-edited file still loads.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RootProfile:
    name: str
    writable: tuple[str, ...] = ()
    readonly: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "writable": list(self.writable),
            "readonly": list(self.readonly),
        }

    @classmethod
    def from_json(cls, payload: dict) -> "RootProfile":
        name = str(payload.get("name", "")).strip()
        writable = tuple(str(x) for x in payload.get("writable", []) if str(x).strip())
        readonly = tuple(str(x) for x in payload.get("readonly", []) if str(x).strip())
        return cls(name=name, writable=writable, readonly=readonly)

    def key(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return (self.writable, self.readonly)


class ProfileError(ValueError):
    """A profile operation failed. Message is user-facing."""


@dataclass
class _State:
    profiles: dict[str, RootProfile] = field(default_factory=dict)


class ProfileStore:
    """A JSON-backed, thread-safe collection of named root profiles.

    Profiles are keyed by name (case-sensitive). Saving under an existing name
    overwrites that profile's paths. The file is written atomically (temp +
    rename) so a crash mid-write cannot leave an unreadable store.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = self._load()

    # -- persistence -----------------------------------------------------
    def _load(self) -> _State:
        if not self.path.exists():
            return _State()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt store must not brick the app; start empty and let the
            # next write overwrite it.
            return _State()
        entries = raw.get("profiles", []) if isinstance(raw, dict) else raw
        profiles: dict[str, RootProfile] = {}
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            profile = RootProfile.from_json(entry)
            if profile.name:
                profiles[profile.name] = profile
        return _State(profiles=profiles)

    def _save(self) -> None:
        payload = {
            "version": 1,
            "profiles": [p.to_json() for p in self._state.profiles.values()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # -- queries ---------------------------------------------------------
    def list(self) -> list[RootProfile]:
        with self._lock:
            return sorted(self._state.profiles.values(), key=lambda p: p.name.lower())

    def get(self, name: str) -> RootProfile | None:
        with self._lock:
            return self._state.profiles.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._state.profiles, key=str.lower)

    # -- mutation --------------------------------------------------------
    def save(
        self,
        name: str,
        writable: list[str],
        readonly: list[str],
    ) -> RootProfile:
        name = (name or "").strip()
        if not name:
            raise ProfileError("Profile name is required.")
        profile = RootProfile(
            name=name,
            writable=tuple(str(x).strip() for x in writable if str(x).strip()),
            readonly=tuple(str(x).strip() for x in readonly if str(x).strip()),
        )
        with self._lock:
            self._state.profiles[name] = profile
            self._save()
        return profile

    def delete(self, name: str) -> None:
        name = (name or "").strip()
        with self._lock:
            if name not in self._state.profiles:
                raise ProfileError(f"No such profile: {name}")
            del self._state.profiles[name]
            self._save()
