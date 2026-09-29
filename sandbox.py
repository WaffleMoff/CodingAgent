from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RootSpec:
    host_path: str


def load_roots(path: str | Path, *, required: bool = True) -> list[RootSpec]:
    p = Path(path)
    if not p.exists():
        if required:
            raise FileNotFoundError(f"Missing {p}")
        return []

    roots = []
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            roots.append(RootSpec(line))
    if required and not roots:
        raise ValueError(f"{p.name} contains no roots")
    return roots


def save_roots(path: str | Path, roots: list[RootSpec], *, header: str = "") -> None:
    """Persist roots atomically, preserving the file's comment header.

    An existing file's leading `#` lines are kept when no explicit header is
    supplied, so hand-written comments are not lost. The write is temp + rename:
    a crash mid-write cannot leave a truncated root list that the next startup
    would fail to parse.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    lines = [header.rstrip("\n")] if header else _existing_header(p)
    lines += [root.host_path for root in roots]

    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(p)


def _existing_header(p: Path) -> list[str]:
    if not p.exists():
        return []
    header: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#") or not line.strip():
            header.append(line)
        else:
            break
    return header


def validate_roots(roots: list[RootSpec], *, require_nonempty: bool = True) -> list[RootSpec]:
    if require_nonempty and not roots:
        raise ValueError("No writable roots configured")

    resolved = []
    for root in roots:
        p = Path(root.host_path).expanduser()
        if not p.is_absolute():
            raise ValueError(f"Root must be absolute: {root.host_path}")
        p = p.resolve(strict=True)
        if not p.is_dir():
            raise ValueError(f"Root is not a directory: {p}")
        resolved.append(RootSpec(str(p)))

    return list(dict.fromkeys(resolved))


def validate_separation(writable: list[RootSpec], readonly: list[RootSpec]) -> None:
    writes = [Path(r.host_path) for r in writable]
    reads = [Path(r.host_path) for r in readonly]
    for w in writes:
        for r in reads:
            if w == r or w in r.parents or r in w.parents:
                raise ValueError(
                    f"Writable and read-only roots may not overlap: {w} / {r}"
                )


@dataclass(frozen=True)
class RootsConfig:
    """One validated snapshot of the writable + read-only root set."""

    writable: tuple[RootSpec, ...]
    readonly: tuple[RootSpec, ...]

    def to_json(self) -> dict[str, list[str]]:
        return {
            "writable": [r.host_path for r in self.writable],
            "readonly": [r.host_path for r in self.readonly],
        }

    @classmethod
    def from_json(cls, payload: dict) -> "RootsConfig":
        writable = [RootSpec(str(x)) for x in payload.get("writable", [])]
        readonly = [RootSpec(str(x)) for x in payload.get("readonly", [])]
        return cls(tuple(writable), tuple(readonly))

    def key(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return (
            tuple(r.host_path for r in self.writable),
            tuple(r.host_path for r in self.readonly),
        )


class RootsConfigError(ValueError):
    """A proposed root set failed validation. Message is user-facing."""


def build_config(
    writable_paths: list[str],
    readonly_paths: list[str],
) -> RootsConfig:
    """Validate raw path strings into a RootsConfig, or raise RootsConfigError."""
    try:
        writable = validate_roots([RootSpec(p) for p in writable_paths])
        readonly = validate_roots(
            [RootSpec(p) for p in readonly_paths], require_nonempty=False
        )
        validate_separation(writable, readonly)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise RootsConfigError(str(exc)) from exc
    return RootsConfig(tuple(writable), tuple(readonly))


class RootManager:
    """Owns the live root configuration and the sandbox container lifetime.

    The container is rebuilt whenever the root set changes, because Docker bind
    mounts are immutable for a running container. `workspace` is a stable proxy
    whose target is swapped under a lock, so sessions holding a reference to it
    never touch a dead container.

    A start that fails is not latched. `state` reports `error` and `ensure_started`
    will try again, so a transient Docker failure (daemon still booting, image
    pull in flight) does not permanently brick the app the way a one-shot
    `start()` with no retry path would.
    """

    def __init__(
        self,
        writable_file: str | Path,
        readonly_file: str | Path,
        image: str,
        *,
        writable_header: str = "",
        readonly_header: str = "",
        workspace_factory=None,
    ):
        from workspace import DockerWorkspace, WorkspaceProxy

        self.writable_file = Path(writable_file)
        self.readonly_file = Path(readonly_file)
        self.image = image
        self._writable_header = writable_header
        self._readonly_header = readonly_header
        self._factory = workspace_factory or DockerWorkspace

        self._lock = threading.RLock()
        self._config = build_config(
            [r.host_path for r in load_roots(self.writable_file)],
            [r.host_path for r in load_roots(self.readonly_file, required=False)],
        )
        self.workspace = WorkspaceProxy()
        self._container = None
        self._state = "starting"
        self._error: str | None = None
        self._on_rebuild = []

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Create the initial container. Safe to call again after a failure."""
        self.ensure_started()

    def ensure_started(self) -> None:
        """Start the container if it is not already ready. Retries after error.

        Called by `start()` on the boot thread and by `_new_session` on demand,
        so a first failure is recoverable without restarting the process.
        """
        with self._lock:
            if self._state == "ready" and self._container is not None:
                return
            try:
                self._container = self._factory(
                    list(self._config.writable),
                    list(self._config.readonly),
                    self.image,
                )
                self.workspace.set_target(self._container)
                self._state = "ready"
                self._error = None
            except Exception as exc:
                self._state = "error"
                self._error = str(exc)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def config(self) -> RootsConfig:
        with self._lock:
            return self._config

    def on_rebuild(self, callback) -> None:
        """Register a hook fired after the container is replaced.

        Used to invalidate per-container state, e.g. the Excel bridge's
        'engine already installed' flag.
        """
        with self._lock:
            self._on_rebuild.append(callback)

    # -- mutation --------------------------------------------------------
    def apply(self, writable_paths: list[str], readonly_paths: list[str]) -> RootsConfig:
        """Validate, rebuild the container, then persist. Atomic on failure.

        Validation runs before anything is torn down; if the new container
        cannot start, the old one keeps running and the files are not written.
        """
        new_config = build_config(writable_paths, readonly_paths)
        with self._lock:
            if new_config.key() == self._config.key() and self._state == "ready":
                return self._config

            old_container = self._container
            new_container = self._factory(
                list(new_config.writable), list(new_config.readonly), self.image,
            )

            # New container is up: point the proxy at it, then retire the old.
            self.workspace.set_target(new_container)
            self._container = new_container
            self._config = new_config
            self._state = "ready"
            self._error = None

            if old_container is not None:
                try:
                    old_container.cleanup()
                except Exception:
                    pass

            save_roots(self.writable_file, list(new_config.writable),
                       header=self._writable_header)
            save_roots(self.readonly_file, list(new_config.readonly),
                       header=self._readonly_header)

            for callback in self._on_rebuild:
                try:
                    callback()
                except Exception:
                    pass
            return self._config

    def cleanup(self) -> None:
        with self._lock:
            if self._container is not None:
                try:
                    self._container.cleanup()
                except Exception:
                    pass
