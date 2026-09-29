from __future__ import annotations

import platform
import subprocess
import threading
import uuid
from pathlib import Path

from sandbox import RootSpec

# Basenames that are shadowed (blanked) inside the sandbox even though they sit
# in a mounted root. Docker bind mounts have no exclude directive, so the file is
# overlayed with an empty, read-only bind mount at the same path: the name still
# exists, its contents are empty, and the sandbox cannot write it. This keeps
# secrets out of the container without moving them off the repo/app directory.
SHADOWED_BASENAMES = (".env", ".env.local", ".env.production", ".env.development")


def _shadow_source(basename: str) -> str:
    """Path to the empty placeholder that is mounted over `basename`.

    Placeholders live in this module's package directory, outside any root, so
    they can never be exposed to the sandbox as ordinary files. Created on first
    use; a prior crash that left one behind is harmless.
    """
    directory = Path(__file__).resolve().parent / ".sandbox_placeholders"
    directory.mkdir(exist_ok=True)
    placeholder = directory / basename
    if not placeholder.exists():
        placeholder.touch()
    return str(placeholder)


class DockerWorkspace:
    """
    Manages the Docker container used as the coding agent's sandbox.

    Writable roots (roots.txt) mount at /workspace/write/rootN (read/write).
    Read-only roots (readonly_roots.txt) mount at /workspace/read/rootN and are
    enforced read-only by Docker.

    Any SHADOWED_BASENAMES found at the top level of a mounted root are overlayed
    with an empty read-only mount, so `.env` and friends are visible by name but
    unreadable and unwritable from inside the container.
    """

    def __init__(
        self,
        writable: list[RootSpec],
        readonly: list[RootSpec],
        image: str,
        *,
        shadowed: tuple[str, ...] = SHADOWED_BASENAMES,
    ):
        self.writable = writable
        self.readonly = readonly
        self.image = image
        self.shadowed = shadowed
        self.container_name = f"local-coding-agent-{uuid.uuid4().hex[:12]}"
        self._start()

    @staticmethod
    def _platform() -> str:
        """linux/arm64 on Apple Silicon, linux/amd64 elsewhere."""
        machine = platform.machine().lower()
        if "arm" in machine or "aarch64" in machine:
            return "linux/arm64"
        return "linux/amd64"

    @staticmethod
    def _docker(
        args: list[str],
        *,
        timeout: int = 120,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run a Docker CLI command on the host (never inside the sandbox)."""
        return subprocess.run(
            ["docker", *args],
            text=True,
            capture_output=capture,
            timeout=timeout,
            check=False,
        )

    def _shadow_mounts(self, host_root: str, sandbox_root: str) -> list[str]:
        """Extra mount args that blank every shadowed file present in a root.

        Only top-level files are checked: that is where `.env` lives in practice
        and it keeps the check a single `stat` per name instead of a tree walk.
        A later mount at a more specific path wins over the root mount.
        """
        args: list[str] = []
        for basename in self.shadowed:
            if (Path(host_root) / basename).is_file():
                source = _shadow_source(basename)
                args += ["--mount",
                         f"type=bind,src={source},dst={sandbox_root}/{basename},readonly"]
        return args

    def _start(self) -> None:
        docker_version = self._docker(["version"])
        if docker_version.returncode != 0:
            raise RuntimeError(
                "Docker is unavailable. Make sure Docker Desktop is running."
            )

        if self._docker(["image", "inspect", self.image]).returncode != 0:
            pull = self._docker(
                ["pull", "--platform", self._platform(), self.image],
                timeout=600,
                capture=False,
            )
            if pull.returncode != 0:
                raise RuntimeError(f"Failed to pull Docker image: {self.image}")

        args = [
            "run", "-d", "--rm",
            "--name", self.container_name,
            "--platform", self._platform(),
            "--workdir", "/workspace",
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "512",
            "--memory", "4g",
            "--cpus", "4",
        ]

        # Bind mounts are read/write by default under --mount; do not append ",rw".
        for i, root in enumerate(self.writable):
            dest = f"/workspace/write/root{i}"
            args += ["--mount", f"type=bind,src={root.host_path},dst={dest}"]
            args += self._shadow_mounts(root.host_path, dest)

        # `readonly` is the --mount field; the kernel rejects writes at the bind mount.
        for i, root in enumerate(self.readonly):
            dest = f"/workspace/read/root{i}"
            args += ["--mount",
                     f"type=bind,src={root.host_path},dst={dest},readonly"]
            args += self._shadow_mounts(root.host_path, dest)

        args += [self.image, "sleep", "infinity"]

        result = self._docker(args)
        if result.returncode != 0:
            error = (
                result.stderr.strip()
                or result.stdout.strip()
                or "Failed to start sandbox"
            )
            raise RuntimeError(error)

    def execute(
        self,
        command: str,
        timeout: int = 120,
    ) -> dict[str, int | str]:
        """Run a shell command inside the sandbox and capture its output."""
        result = self._docker(
            ["exec", self.container_name, "bash", "-lc", command],
            timeout=timeout,
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }

    def cleanup(self) -> None:
        """Stop and remove the container. --rm means this also deletes it."""
        try:
            self._docker(["rm", "-f", self.container_name], timeout=30)
        except Exception:
            pass


class WorkspaceProxy:
    """A stable handle to whichever container is currently live.

    Docker bind mounts are fixed at `docker run`, so changing the root set means
    building a new container. Every tool handler in every session closes over
    *this* object rather than a concrete `DockerWorkspace`, so a rebuild is
    invisible to sessions that are mid-run.

    The lock is held for the whole of `execute`, not just the target lookup. That
    is what makes a concurrent `set_target` safe: a rebuild's `cleanup()` of the
    old container waits until the in-flight command has returned, so no command
    is ever sent to a container that is being torn down.
    """

    def __init__(self, target: DockerWorkspace | None = None):
        self._target = target
        self._lock = threading.RLock()

    def set_target(self, target: DockerWorkspace) -> None:
        with self._lock:
            self._target = target

    @property
    def target(self) -> DockerWorkspace | None:
        with self._lock:
            return self._target

    @property
    def container_name(self) -> str:
        with self._lock:
            return self._target.container_name if self._target is not None else ""

    def execute(self, command: str, timeout: int = 120) -> dict[str, int | str]:
        with self._lock:
            target = self._target
            if target is None:
                raise RuntimeError("Sandbox is not ready")
            return target.execute(command, timeout)

    def cleanup(self) -> None:
        with self._lock:
            if self._target is not None:
                self._target.cleanup()
