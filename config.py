from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
API_KEY = os.getenv("DEEPSEEK_API_KEY")

MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "100"))
RESEARCH_MAX_ITERATIONS = int(os.getenv("RESEARCH_MAX_ITERATIONS", "60"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "5000"))
MAX_TOOL_OUTPUT = int(os.getenv("MAX_TOOL_OUTPUT", "30000"))

# Research output-contract enforcement (research.py). After the sub-agent
# produces an answer, it is checked against the evidence ledger (evidence.py):
# at least one source must have been fetched, and every cited URL must be one
# the agent actually retrieved. Factual lines must also carry a citation +
# as-of date. RESEARCH_VERIFY_PASSES is how many revision turns the sub-agent
# gets before the answer is returned flagged as unverified.
RESEARCH_VERIFY_PASSES = int(os.getenv("RESEARCH_VERIFY_PASSES", "2"))

# Context compaction (compaction.py). Keep the head and a recent tail, summarize
# the middle, before a model call once the rendered history crosses the
# threshold. Off -> the loop sends history unchanged.
COMPACT_ENABLED = os.getenv("COMPACT_ENABLED", "on").strip().lower() != "off"
COMPACT_AT_TOKENS = int(os.getenv("COMPACT_AT_TOKENS", "60000"))
COMPACT_KEEP_RECENT = int(os.getenv("COMPACT_KEEP_RECENT", "8"))
COMPACT_MAX_SUMMARY_TOKENS = int(os.getenv("COMPACT_MAX_SUMMARY_TOKENS", "1500"))

# Finish gate (gate.py). When the model emits a no-tool-call turn, registered
# checks must pass or the loop feeds the reasons back and continues.
FINISH_GATE_ENABLED = os.getenv("FINISH_GATE_ENABLED", "on").strip().lower() != "off"
FINISH_RETRIES = int(os.getenv("FINISH_RETRIES", "3"))

IMAGE = os.getenv("AGENT_IMAGE", "python:3.12-slim")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5001"))

# Excel and research conventions. The agent reads these through the
# read_excel_guide / read_research_guide / read_research_notes tools, so the
# directory must be writable by the host app (not the sandbox).
KNOWLEDGE_DIR = os.getenv("EXCEL_KNOWLEDGE_DIR", str(APP_DIR / "knowledge"))
KNOWLEDGE_MAX_CHARS = int(os.getenv("KNOWLEDGE_MAX_CHARS", "40000"))
KNOWLEDGE_MAX_NOTE_CHARS = int(os.getenv("KNOWLEDGE_MAX_NOTE_CHARS", "4000"))

# Plan tool state (plan.py). Saved by the host process, not inside the sandbox.
AGENT_PLAN_PATH = os.getenv("AGENT_PLAN_PATH", str(APP_DIR / ".agent" / "PLAN.json"))
