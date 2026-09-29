from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

# Files whose contents should never be exposed to the model.
BLOCKED_BASENAMES = {
    ".env", ".env.local", ".env.development", ".env.production", ".env.test",
    ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
}
BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
BLOCKED_PARTS = {".git", ".ssh", ".aws", ".azure", ".config/gcloud"}

# Cheap name signal, kept for callers that want it. The content gate is the
# real protection: renaming `.env` to `notes.txt` defeats any name policy.
SECRET_FILENAME_TOKENS = re.compile(r"(?i)(^|[._-])(env|secret|secrets|credential|credentials|token|apikey|api[_-]?key)([._-]|$)")

# High-confidence value shapes. These are credentials on their own, with no
# assignment context required, so a single match is enough to redact.
HIGH_CONFIDENCE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

# Assignment-shaped secrets: a credential-ish name bound to a value that itself
# looks like a credential (quoted string, or a long unquoted token). The value
# is the signal, so `SECRET_PATTERNS = [` and `password = None` do not match.
ASSIGNMENT_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|private[_-]?key)\b\s*[:=]\s*['\"]([^'\"]{8,})['\"]"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|private[_-]?key)\b\s*[:=]\s*([A-Za-z0-9/+_=.-]{16,})"),
]

# Dotenv-style line: a credential-named key bound to any value. Signature of a
# `.env` under any name, and the most likely single-line leak.
_DOTENV_KEY = re.compile(
    r"(?im)^[ \t]*(?:export[ \t]+)?"
    r"([A-Za-z0-9.-]*[._-]?(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|DSN))"
    r"[ \t]*=[ \t]*(\S.*)$"
)

SECRET_PATTERNS = HIGH_CONFIDENCE_PATTERNS + ASSIGNMENT_PATTERNS


@dataclass(frozen=True)
class AccessPolicy:
    writable_prefix: str = "/workspace/write"
    readonly_prefix: str = "/workspace/read"

    def normalize(self, path: str) -> PurePosixPath:
        p = PurePosixPath(path)
        if not p.is_absolute():
            raise ValueError("Paths must be absolute sandbox paths")
        if ".." in p.parts:
            raise ValueError("Parent traversal is not allowed")
        return p

    def can_read(self, path: str) -> bool:
        p = str(self.normalize(path))
        return p == self.writable_prefix or p.startswith(self.writable_prefix + "/") or \
               p == self.readonly_prefix or p.startswith(self.readonly_prefix + "/")

    def can_write(self, path: str) -> bool:
        p = str(self.normalize(path))
        return p == self.writable_prefix or p.startswith(self.writable_prefix + "/")

    def assert_read(self, path: str) -> None:
        if not self.can_read(path):
            raise PermissionError("Read access is restricted to /workspace/write and /workspace/read")

    def assert_write(self, path: str) -> None:
        if not self.can_write(path):
            raise PermissionError("Writes are restricted to /workspace/write")

    def assert_content_allowed(self, path: str) -> None:
        """Reject reads of secret-bearing FILES by name or path shape.

        Pre-read gate only: it cannot see content, so `assert_no_secrets` runs
        on the bytes afterward and is what covers the copy-to-another-name route.
        """
        p = self.normalize(path)
        lower_parts = [x.lower() for x in p.parts]
        basename = p.name.lower()
        joined = "/".join(lower_parts)
        if basename in BLOCKED_BASENAMES or basename.startswith(".env."):
            raise PermissionError(f"Reading secret-bearing file is blocked: {path}")
        if any(basename.endswith(s) for s in BLOCKED_SUFFIXES):
            raise PermissionError(f"Reading key/certificate file is blocked: {path}")
        if any(part in lower_parts or part in joined for part in BLOCKED_PARTS):
            raise PermissionError(f"Reading sensitive directory is blocked: {path}")


def _line_is_secret(line: str) -> bool:
    if any(p.search(line) for p in SECRET_PATTERNS):
        return True
    return bool(_DOTENV_KEY.search(line))


def _is_comment(line: str) -> bool:
    s = line.strip()
    return s.startswith(("#", "//", "--", "*", ";"))


def looks_secret(text: str) -> bool:
    """True if `text` contains a pattern that is unsafe to return verbatim."""
    if not text:
        return False
    return any(_line_is_secret(line) for line in text.splitlines())


def _redact_line(line: str) -> str:
    for pattern in SECRET_PATTERNS:
        line = pattern.sub("[REDACTED_SECRET]", line)
    return _DOTENV_KEY.sub(lambda m: f"{m.group(1)}=[REDACTED_SECRET]", line)


def redact_secrets(text: str) -> str:
    """Replace detected secrets with a marker. Used on every outbound string."""
    return "\n".join(_redact_line(line) for line in text.splitlines())


def assert_no_secrets(text: str, path: str) -> None:
    """Refuse only when redaction would be meaningless.

    By default a content match is redacted and returned, so one key-shaped line
    in a source file no longer blinds the model to the whole file. The single
    refusal case is a file where every substantive line is secret-shaped (a
    dotenv under any name): returning it redacted would still leak the key
    schema, so it is refused whole.
    """
    substantive = [ln for ln in text.splitlines() if ln.strip() and not _is_comment(ln)]
    if not substantive:
        return
    if all(_line_is_secret(ln) for ln in substantive):
        raise PermissionError(
            f"Refusing to return {path}: every line is a secret "
            f"(dotenv-style assignment or credential token)."
        )
