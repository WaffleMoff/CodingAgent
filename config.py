"""Configuration for the Local Coding Agent.

Owns the .env load and every module-level constant the app reads. Kept
deliberately flat: a setting lives here only if more than one module imports it,
and every value is overridable from the environment so nothing needs editing to
change behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

# --- model / API ------------------------------------------------------------
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
API_KEY = os.getenv("DEEPSEEK_API_KEY")

# --- agent loop -------------------------------------------------------------
MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "100"))
RESEARCH_MAX_ITERATIONS = int(os.getenv("RESEARCH_MAX_ITERATIONS", "60"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "5000"))
MAX_TOOL_OUTPUT = int(os.getenv("MAX_TOOL_OUTPUT", "30000"))

# Research output-contract enforcement. After the research sub-agent produces an
# answer, research.py checks coverage and citations. On failure it feeds the
# violation back and re-runs, up to RESEARCH_VERIFY_PASSES times. 0 disables the
# gate; 2 is a good default (one correction, one grace pass).
RESEARCH_VERIFY_PASSES = int(os.getenv("RESEARCH_VERIFY_PASSES", "2"))

# --- context compaction -----------------------------------------------------
# compaction.py keeps the head and a recent tail, summarizes the middle, and
# re-injects durable state (plan, writes log, git status) before each model call
# once the rendered history crosses COMPACT_AT_TOKENS. Off -> the loop sends
# history unchanged.
COMPACT_ENABLED = os.getenv("COMPACT_ENABLED", "on").strip().lower() != "off"
COMPACT_AT_TOKENS = int(os.getenv("COMPACT_AT_TOKENS", "60000"))
COMPACT_KEEP_RECENT = int(os.getenv("COMPACT_KEEP_RECENT", "8"))
COMPACT_MAX_SUMMARY_TOKENS = int(os.getenv("COMPACT_MAX_SUMMARY_TOKENS", "1500"))

# --- finish gate ------------------------------------------------------------
# Whether the Stop hook enforces its checks, and how many times it may reject a
# candidate finish before the answer is accepted. Bounded so a misconfigured
# gate can never hang a session.
FINISH_GATE_ENABLED = os.getenv("FINISH_GATE_ENABLED", "on").strip().lower() != "off"
FINISH_RETRIES = int(os.getenv("FINISH_RETRIES", "3"))

# --- plan tool --------------------------------------------------------------
# Where manage_plan persists its JSON. Defaults to <app>/.agent/PLAN.json.
AGENT_PLAN_PATH = os.getenv(
    "AGENT_PLAN_PATH", str(APP_DIR / ".agent" / "PLAN.json")
)

# --- web server / sandbox ---------------------------------------------------
IMAGE = os.getenv("AGENT_IMAGE", "python:3.12-slim")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5001"))

# --- knowledge files --------------------------------------------------------
# Excel and research conventions. The agent reads these through the
# read_excel_guide / read_research_guide / read_research_notes tools, so the
# directory must be writable by the host app (not the sandbox).
KNOWLEDGE_DIR = os.getenv("EXCEL_KNOWLEDGE_DIR", str(APP_DIR / "knowledge"))
KNOWLEDGE_MAX_CHARS = int(os.getenv("KNOWLEDGE_MAX_CHARS", "40000"))
KNOWLEDGE_MAX_NOTE_CHARS = int(os.getenv("KNOWLEDGE_MAX_NOTE_CHARS", "4000"))
