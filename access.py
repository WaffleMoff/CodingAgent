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
# looks like a credential. The value is the signal, so `SECRET_PATTERNS = [` and
# `password = None` do not match.
#
# These fire on ordinary source code — `TOKEN = os.getenv("TOKEN")`,
# `api_key = "placeholder"`, doc/example strings — which is the noise that
# blinded the model to source files. They are confined to the "strict" profile.
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

# Extensions treated as source code. Under the "code" profile these read
# verbatim with NO content redaction. Extend by adding an extension here; the
# profile lookup generalizes from this set.
CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".h", ".cc",
    ".cpp", ".hpp", ".cs", ".swift", ".scala", ".sh", ".bash", ".zsh",
    ".sql", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".json",
}

# Profiles keyed by extension class. "strict" applies the full rule set to every
# other file type. "code" applies NO content rules, so source is returned
# verbatim. Even under "code", secret *filenames* are refused by
# `assert_content_allowed`, so `.env` stays unreadable and a renamed copy is the
# only residual route.
_PROFILES: dict[str, list[re.Pattern[str]]] = {
    "strict": HIGH_CONFIDENCE_PATTERNS + ASSIGNMENT_PATTERNS,
    "code": [],
}
DEFAULT_PROFILE = "strict"


def profile_for(path: str | None) -> str:
    """Rule profile name for a path, by extension. Unknown extensions are strict."""
    if not path:
        return DEFAULT_PROFILE
    suffix = PurePosixPath(path).suffix.lower()
    return "code" if suffix in CODE_SUFFIXES else DEFAULT_PROFILE


def _patterns_for(path: str | None, *, code: bool | None = None) -> list[re.Pattern[str]]:
    """Active rule set for a call.

    `path` selects the profile by extension. `code` forces the profile
    explicitly, for callers with no single file (a directory grep, a stream).
    """
    if code is None:
        return _PROFILES[profile_for(path)]
    return _PROFILES["code" if code else "strict"]


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
        on the bytes afterward for non-code files. This name gate applies to
        every file, including source, so `.env` and key/cert files stay blocked.
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


def _line_is_secret(line: str, patterns: list[re.Pattern[str]]) -> bool:
    if any(p.search(line) for p in patterns):
        return True
    return bool(_DOTENV_KEY.search(line))


def _is_comment(line: str) -> bool:
    s = line.strip()
    return s.startswith(("#", "//", "--", "*", ";"))


def looks_secret(text: str, path: str | None = None, *, code: bool | None = None) -> bool:
    """True if `text` contains a pattern that is unsafe to return verbatim."""
    if not text:
        return False
    patterns = _patterns_for(path, code=code)
    return any(_line_is_secret(line, patterns) for line in text.splitlines())


def _redact_line(line: str, patterns: list[re.Pattern[str]]) -> str:
    for pattern in patterns:
        line = pattern.sub("[REDACTED_SECRET]", line)
    return _DOTENV_KEY.sub(lambda m: f"{m.group(1)}=[REDACTED_SECRET]", line)


def redact_secrets(text: str, path: str | None = None, *, code: bool | None = None) -> str:
    """Replace detected secrets with a marker. Used on every outbound string.

    `path` selects the profile by extension; `code` forces it when there is no
    single file (a directory-wide grep, blended command output). Source files
    ("code" profile) pass through unchanged.
    """
    patterns = _patterns_for(path, code=code)
    if not patterns:
        return text
    return "\n".join(_redact_line(line, patterns) for line in text.splitlines())


def assert_no_secrets(text: str, path: str, *, code: bool | None = None) -> None:
    """Refuse only when redaction would be meaningless.

    For non-code files a content match is redacted and returned, so one
    key-shaped line no longer blinds the model to the whole file. The single
    refusal case is a file where every substantive line is secret-shaped (a
    dotenv under any name): returning it redacted would still leak the key
    schema, so it is refused whole. Source files carry no rules, so this is a
    no-op for them.
    """
    patterns = _patterns_for(path, code=code)
    if not patterns:
        return
    substantive = [ln for ln in text.splitlines() if ln.strip() and not _is_comment(ln)]
    if not substantive:
        return
    if all(_line_is_secret(ln, patterns) for ln in substantive):
        raise PermissionError(
            f"Refusing to return {path}: every line is a secret "
            f"(dotenv-style assignment or credential token)."
        )
