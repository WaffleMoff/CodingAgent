# Local Coding Agent

A local coding agent that runs inside a Docker sandbox. It has its own file
tools, a shell, an Excel workbook engine, and a research sub-agent with web
access. You talk to it in a browser UI; it works on directories you explicitly
expose to it.

This README walks you through setting it up from scratch. Follow the steps in
order. Nothing here requires code changes.

For how the internals work — the sandbox model, the modules, the security
boundaries — see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## What you are building

The app runs on your computer. When the agent writes files or runs commands, it
does so inside a Docker container, not on your real machine. You choose which of
your folders the container can see:

- **Writable roots** — folders the agent can read and write. Any file the agent
  creates lands here on your real disk.
- **Read-only roots** — folders the agent can read but not modify. Use these for
  reference material (a codebase to look at, documents to read).

You set these up with two text files: `roots.txt` and `readonly_roots.txt`.

---

## Before you start

You need three things installed. Check each with the command shown.

**1. Python 3.10 or newer**
```bash
python3 --version
```
If that fails or shows an older version, install Python from python.org.

**2. Docker Desktop**
```bash
docker --version
```
Then make sure Docker is actually **running** (open Docker Desktop and wait for
it to say it's running). The app will not start without it. Docker Desktop is
free for personal use — download from docker.com.

**3. A DeepSeek API key**
Go to platform.deepseek.com, create an account, and generate an API key. It
looks like `sk-...`. Keep this window open; you'll paste the key in step 3.

Optional: **`crw`** — a command-line web scraper. Only needed if you want the
research feature to search and fetch web pages. Without it, everything else
works and research returns a clear "web tools unavailable" message. You can skip
this and add it later.

---

## Step 1 — Get the code onto your machine

Download the project folder (or `git clone` it) and open a terminal in it:

```bash
cd /path/to/LocalCodingAgent4_Modern
```

Everything below assumes you are in this folder. Confirm you can see the files:

```bash
ls
```
You should see `app.py`, `config.py`, `requirements.txt`, and others.

---

## Step 2 — Install the Python packages

```bash
python3 -m pip install -r requirements.txt
```

That installs three packages (Flask, python-dotenv, and the OpenAI client
library). If `pip` complains about permissions, use a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python3 -m pip install -r requirements.txt
```

If you use a virtual environment, run every later `python3` command with it
active.

---

## Step 3 — Create your `.env` file (holds your API key)

The project does **not** ship with a `.env` file — you make it. It holds your
secret API key and is ignored by git, so it will never be uploaded anywhere.

Create a file named exactly `.env` in the project folder (same folder as
`app.py`). Put this inside it:

```
DEEPSEEK_API_KEY=sk-paste-your-real-key-here
```

That is the only line you strictly need. Replace the placeholder with your real
key from the DeepSeek website.

> **Keep `.env` at the top level of the project folder.** The sandbox blanks any
> file named `.env` it finds at the top of a mounted folder, so your key stays
> hidden from the agent. It does *not* reach into nested subfolders — so never
> put `.env` inside a subfolder that is itself inside a mounted root.

Optional extra settings you can add on their own lines (defaults are fine for a
first run):

```
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com
PORT=5001
AGENT_IMAGE=python:3.12-slim
```

---

## Step 4 — Create `roots.txt` (the folders the agent can write to)

Create a file named `roots.txt` in the project folder. Each line is **one
absolute folder path** — a folder you want the agent to be able to read and
write. Lines starting with `#` are comments.

```
# One absolute host directory per line.
# Each is mounted read/write as /workspace/write/rootN.
/Users/yourname/Documents/agent_output
```

Points that matter:

- Paths must start with `/` (on Mac/Linux) or a drive letter (on Windows, e.g.
  `C:/Users/yourname/Documents/agent_output`).
- The folder must already exist. Create it first if needed:
  `mkdir -p ~/Documents/agent_output`.
- You can list as many folders as you want, one per line.
- If you also list the project folder itself here, that's fine — the `.env` at
  its top level is automatically hidden from the agent.

---

## Step 5 — Create `readonly_roots.txt` (optional reference folders)

Create a file named `readonly_roots.txt` in the project folder. Same format, but
these folders are mounted **read-only** — the agent can read them, never change
them. Use this for anything you want the agent to consult but not touch.

```
# Optional read-only reference directories, one absolute host directory per line.
# Each is mounted read-only as /workspace/read/rootN.
/Users/yourname/Documents/Projects
```

This file is optional. If you don't need reference material, create it empty
with just the comment lines. **The agent's own project folder is a good thing to
put here** so it can read its own code.

One rule: a read-only root cannot be the same as, or sit inside, a writable
root (or vice versa). The app will refuse to start and tell you if you do this.

---

## Step 6 — Check your setup

```bash
python3 bootstrap_excel.py --check
```

This reports whether the built-in Excel engine and the knowledge files are in
place. You want it to end with `0 problem(s)`. If something is reported MISSING,
the message tells you what to fix — usually a file didn't download correctly.

---

## Step 7 — Start the app

```bash
python3 app.py
```

The first start can take a minute: Docker downloads a small Python image the
first time. When it's ready you'll see a line like:

```
Running on http://0.0.0.0:5001
```

Open **http://localhost:5001** in your browser. You'll see a chat window.

---

## Step 8 — Use it

Click **New session**, type what you want, and press Send. For example:

> Create a folder called `notes` in the output directory and add a file called
> `hello.txt` with the text "it works".

The agent will use its tools and create the file inside your writable root. Check
your real folder — the file should be there.

The tabs at the top show what's happening:

- **Chat** — just your conversation.
- **Agent** — every step the agent takes, including its reasoning.
- **Tools** — every tool call and its result (file writes, shell commands).
- **Roots** — change which folders are exposed, from the UI instead of editing
  the text files. Clicking Save rebuilds the sandbox with the new folders and
  rewrites `roots.txt` / `readonly_roots.txt` for you.

---

## Optional — turn on web research

The research feature needs the `crw` command-line tool on your computer.

1. Install `crw` (follow its own instructions).
2. Confirm it's found:
   ```bash
   which crw
   ```
   If that prints nothing, either add it to your PATH or place it at
   `/usr/local/bin/crw`.
3. Restart the app. The agent can now search and fetch pages.

Without `crw`, everything except web research works normally.

---

## Optional — customize the Excel and research conventions

The folder `knowledge/` contains three Markdown files:

- `EXCEL_GUIDE.md` — how workbooks should be laid out (sheets, number formats,
  colours). The agent reads it before building a spreadsheet.
- `RESEARCH_GUIDE.md` — how research should be done and cited.
- `RESEARCH_NOTES.md` — your own standing notes; the agent can read and append
  to it.

These are plain text. Edit them to match your preferences — no coding required.
The agent only reads them when a relevant task comes up, so they cost nothing
until needed.

---

## Troubleshooting

**"Docker is unavailable" or the app won't start.**
Docker Desktop isn't running. Open it, wait for it to be ready, and start the
app again. The app retries automatically, so you don't need to change anything.

**"DEEPSEEK_API_KEY is required in .env".**
Your `.env` file is missing, misnamed (it must be exactly `.env`, not
`env.txt`), or doesn't contain the `DEEPSEEK_API_KEY=...` line. Fix and restart.

**"Missing roots.txt" or "roots.txt contains no roots".**
Create `roots.txt` in the project folder with at least one absolute folder path,
each on its own line.

**"Root must be absolute" or "Root is not a directory".**
A path in `roots.txt` is wrong. It must start with `/` (or `C:/` on Windows) and
the folder must exist.

**"Writable and read-only roots may not overlap".**
One of your `readonly_roots.txt` folders is the same as, or inside, a
`roots.txt` folder. Move one of them so they don't overlap.

**The agent can't see my files / writes go to the wrong place.**
Check the **Roots** tab. The folders listed there are the only ones the agent can
reach. Add what's missing and click Save.

**Files the agent creates aren't in my folder.**
They're written inside the writable root, under `/workspace/write/rootN`, which
maps to the folder you listed in `roots.txt`. Open that folder on your disk.

**Research says "web tools unavailable".**
`crw` isn't installed or isn't on your PATH. See the research section above.

---

## Reference — every file you need to create

| File | Required? | What goes in it |
| --- | --- | --- |
| `.env` | Yes | `DEEPSEEK_API_KEY=sk-...` (your key). No other file needed. |
| `roots.txt` | Yes | One absolute folder path per line — folders the agent can write to. |
| `readonly_roots.txt` | No | One absolute folder path per line — folders the agent can only read. |
| `knowledge/*.md` | No | Optional; edit to customize Excel/research behavior. Shipped with the project. |

The app refuses to start without a valid `.env` and a non-empty `roots.txt`.
Everything else is optional.
