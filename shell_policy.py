"""Shell command policy for `run_command`.

Programs are allowlisted by basename, never blocklisted by string. The previous
build defined SAFE_COMMANDS and never enforced it, then substituted a substring
test (`"/workspace/read" in command`) that any quoting, splitting, or symlink
defeats. This module fixes both, and adds the control that was missing entirely:
`run_command` output is the one read channel that never passed through the
secret filter, so `check_command` now refuses to let the sandbox touch
secret-bearing paths at all, and `tools.py` redacts what comes back.

Two gaps this closes, both of which let `.env` out through `run_command`:

  1. Path-looking tokens were the only path check, and tokens beginning with
     `-` were skipped. `sed -n 1p /workspace/read/root0/.env` therefore passed
     because the argument arrived as an option, not a bare path.
  2. Programs on the allowlist (`python3`, `node`, `awk`, `find`) can read any
     file the sandbox can see, so an allowlist alone is not a read control. The
     path check runs against every token, on every stage, regardless of the
     program, so `find ... -exec cat {} +` and `python3 -c` are equally covered.

`check_command` returns "" to allow or a human-readable reason to deny. It never
raises, so it is usable directly as a `pre_tool_use` hook.
"""

from __future__ import annotations

import os
import re
import shlex

# Directory the sandbox runs commands in. Relative paths resolve here.
WORKDIR = "/workspace/write"
WRITE_ROOT = "/workspace/write"
READ_ROOT = "/workspace/read"

# Programs `run_command` may invoke. Matched on the basename of argv[0], so
# `/usr/bin/git` and `git` both resolve to `git`. Extend deliberately: adding a
# program here grants every capability that program has. Note that any program
# here can read files, so the path check below — not this set — is the read
# boundary.
SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "file", "stat", "tree", "du", "df",
    "find", "grep", "rg", "sed", "awk", "sort", "uniq", "cut", "tr", "diff",
    "mkdir", "cp", "mv", "rm", "touch", "ln", "chmod", "basename", "dirname",
    "realpath", "echo", "printf", "tee", "true", "false", "yes", "env",
    "git", "pytest", "python", "python3", "pip", "pip3", "node", "npm",
    "npx", "tsc", "black", "ruff", "flake8", "mypy", "isort",
}

# Programs refused even if someone later adds them to SAFE_COMMANDS. Network
# clients and arbitrary-code evaluators. A guardrail against a careless
# allowlist edit, not a threat model.
ALWAYS_DENY = {"curl", "wget", "nc", "netcat", "ssh", "scp", "socat", "telnet",
               "eval", "exec", "source"}

# Tokens that introduce a command rather than being one (shell keywords and
# wrappers). The token after these is the program.
_COMMAND_INTROS = {"sudo", "env", "command", "time", "nohup", "xargs",
                   "then", "do", "else", "if", "while", "until"}

_OPERATORS = {"|", "&&", "||", ";", ">", ">>", "<", "2>", "2>>", "&"}
_REDIRECT_RE = re.compile(r">>?\s*([^\s;|&]+)")

# A token that skips option characters to reach its payload. `sed -n 1p FILE`,
# `awk '{...}' FILE`, `grep -f PATTERNS FILE`. Detected structurally rather than
# per-program, because a per-program table has to be maintained forever and one
# missed entry is a leak.
_DASH = "-"

# Extensions/dotfiles that are secret-bearing by convention, applied to the
# basename of every path token regardless of which program is reading it.
_SECRET_BASENAMES = {".env", ".env.local", ".env.development", ".env.production",
                     ".env.test", ".npmrc", ".pypirc", ".netrc", "credentials",
                     "credentials.json", ".git-credentials", "id_rsa", "id_ed25519"}
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".env", ".secrets")


def _basename(program: str) -> str:
    return os.path.basename(program.strip().strip("'\""))


def _tokenize(command: str) -> list[str]:
    """Split into tokens, also breaking on the shell operators the sandbox honors."""
    parts = re.split(r"(&&|\|\||[;|\n]|>>?|2>>?|<|&)", command)
    tokens: list[str] = []
    for part in parts:
        if not part or not part.strip():
            continue
        if part in _OPERATORS:
            tokens.append(part)
            continue
        try:
            tokens.extend(shlex.split(part, posix=True))
        except ValueError:
            # Unbalanced quotes: fall back to a whitespace split so the program
            # name is still inspected. A parse error must not be a bypass.
            tokens.extend(part.split())
    return tokens


def _programs(tokens: list[str]) -> list[str]:
    """The program at the head of each pipeline stage.

    Fails closed: a command-intro word or a stage operator arms an expectation
    that the next real token is a program, so `then rm -rf x` is inspected.
    """
    programs: list[str] = []
    expect = True
    for tok in tokens:
        if tok in _OPERATORS:
            expect = True
            continue
        if expect:
            programs.append(tok)
            expect = False
        elif tok in _COMMAND_INTROS:
            expect = True
    return programs


def _resolve(tok: str) -> str:
    """Absolute path for a token, relative to the sandbox working directory."""
    if os.path.isabs(tok):
        return os.path.normpath(tok)
    return os.path.normpath(os.path.join(WORKDIR, tok))


def _candidate_paths(tokens: list[str]) -> list[str]:
    """Every token that could name a file, including ones hidden behind options.

    A plain token is a candidate if it contains a separator, is `.`/`..`, or has
    no leading dash. Skipping a `-`-prefixed token was the earlier bug: `-n 1p
    /path` put the path in the *following* token, which was still checked, but
    `sed -f/path` or `--file=/path` embed it in the option and were not. So the
    check is on the whole token, not just its first character.
    """
    out: list[str] = []
    for tok in tokens:
        if not tok or tok in _OPERATORS:
            continue
        if "=" in tok and tok.startswith("-"):
            out.append(tok.split("=", 1)[1])
            continue
        if tok.startswith(_DASH) and "/" not in tok:
            # Pure option cluster, e.g. `-la`. Not a path.
            continue
        if "/" in tok or tok in {".", ".."} or not tok.startswith(_DASH):
            out.append(tok)
    return out


def _basename_is_secret(path: str) -> bool:
    base = os.path.basename(path).lower()
    if base in _SECRET_BASENAMES or base.startswith(".env."):
        return True
    return any(base.endswith(s) for s in _SECRET_SUFFIXES)


def _under(child: str, parent: str) -> bool:
    child = os.path.normpath(child)
    parent = os.path.normpath(parent)
    return child == parent or child.startswith(parent + os.sep)


def check_command(command: str) -> str:
    """Return "" to allow, else the reason to deny. Never raises."""
    if not command or not command.strip():
        return "Empty command."

    tokens = _tokenize(command)
    if not tokens:
        return "Command has no runnable content."

    # 1. Program allowlist. Primary control on *what* runs.
    for program in _programs(tokens):
        name = _basename(program)
        if name in ALWAYS_DENY:
            return (
                f"'{name}' is never permitted (network client or arbitrary-code "
                f"evaluator). Use the web tools for network access."
            )
        if name not in SAFE_COMMANDS:
            return (
                f"'{name}' is not in the run_command allowlist. Allowed: "
                f"{', '.join(sorted(SAFE_COMMANDS))}. Prefer the dedicated file "
                f"tools (read_file, grep, write_file, apply_patch)."
            )

    # 2. Every candidate path token, resolved against WORKDIR.
    #    (a) anything beneath the read-only root is refused outright. read_file
    #        and friends enforce content filtering; run_command cannot, so it
    #        must not be able to reach read-only material at all.
    #    (b) a secret-shaped basename is refused even inside the writable root,
    #        because a copy landing there is exactly the leak this guards.
    for tok in _candidate_paths(tokens):
        resolved = _resolve(tok)
        if _under(resolved, READ_ROOT):
            return (
                f"'{tok}' resolves beneath {READ_ROOT} (read-only). Access "
                f"read-only material through read_file/list_files/grep, which "
                f"enforce secret filtering."
            )
        if _basename_is_secret(resolved):
            return (
                f"'{tok}' names a secret-bearing file. Reads of it are refused "
                f"in every tool, including through a copy."
            )

    # 3. Redirection targets, resolved the same way.
    for match in _REDIRECT_RE.finditer(command):
        target = match.group(1).strip("'\"")
        if _under(_resolve(target), READ_ROOT):
            return f"Refusing to write into read-only {READ_ROOT}."
        if _basename_is_secret(_resolve(target)):
            return f"Refusing to write to secret-shaped path '{target}'."

    return ""


def is_read_command(command: str) -> bool:
    """True if the command can emit file contents, so output should be filtered.

    Redaction on `run_command` output is a cheap second line of defense, but it
    must not mangle build output. Only commands with a plausible read path are
    filtered; the allowlisted set is small enough to name directly.
    """
    readers = {"cat", "head", "tail", "sed", "awk", "grep", "rg", "sort",
               "uniq", "cut", "tr", "diff", "python", "python3", "node", "tee"}
    return any(_basename(p) in readers for p in _programs(_tokenize(command)))
