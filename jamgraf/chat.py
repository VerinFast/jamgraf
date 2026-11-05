"""Chat with an article, powered by a Doorman agent or a local CLI.

The user asks a question about a page; we ground the answer in (a) the page's
own markdown and (b) hybrid-RAG excerpts from elsewhere in the *same* wiki, then
ask a model.

Two backends, one prompt. When Doorman is running and offers an agent, the
question goes to `POST /api/ask` there — the user configured model access in
that app once, and shouldn't have to configure a second set of API keys and
CLIs here. Otherwise (and whenever Doorman is absent, older, or has no agent) we
shell out to the `claude` or `gemini` CLI exactly as before, in one-shot "print"
mode — no interactive session — so this stays a simple request/response. The
answer always carries a `label` naming whichever one spoke.

The system prompt is an editable page in the Help wiki, so it can be tuned
without code changes. Nothing here crosses wiki boundaries: retrieval always
runs under the caller's active wiki.
"""
from __future__ import annotations

import re
import subprocess
import sys

from . import clirun, config, db, shellenv, store

DEFAULT_CHAT_SYSTEM = (
    "You are a helpful assistant answering questions about a single wiki article. "
    "Ground every answer in the article and the provided wiki excerpts. If the "
    "answer isn't in them, say so plainly rather than guessing. Be concise and "
    "use Markdown."
)
CHAT_PROMPT_SLUG = "chat-system-prompt"


def find_cli(name: str) -> str | None:
    """Locate a CLI binary using the login-shell PATH (Finder-launched apps get a
    minimal PATH otherwise)."""
    return shellenv.which(name)


def chat_system_prompt() -> str:
    from . import ai
    return ai.help_prompt(CHAT_PROMPT_SLUG) or DEFAULT_CHAT_SYSTEM


def build_prompt(title: str, article_md: str, excerpts: str,
                 history: list[dict], question: str, system: str,
                 wiki: str = "") -> str:
    """Assemble the single prompt string handed to the CLI. Pure — unit-tested.

    `title` may be empty: chat is reachable from pages that aren't an article
    (Settings, the Index), where the question is about the wiki rather than one
    page. The agent is expected to look things up itself in that case — see
    issue #30 for the MCP access that makes that possible.
    """
    parts = [system]
    # What this agent can and cannot do to the page. Deliberately NOT part of the
    # editable system prompt: that page is for tone, and a user rewording it must
    # not be able to switch the feature off by accident. Without this the model
    # reaches for edit_page, the CLI refuses a tool it was never granted, and the
    # human sees a failure instead of an offer.
    parts.append(
        "# Editing\n\n"
        "You cannot change this page directly, and you are not meant to — a "
        "conversation never silently rewrites someone's wiki. When you are asked "
        "for a change, call `propose_edit` with the FULL revised page and say "
        "that it is waiting for review; the human applies or rejects it on the "
        "page itself. Read the page with `get_page` first so your proposal keeps "
        "everything you were not asked to change. Never claim you have edited "
        "the page — you have proposed.")
    if wiki:
        parts.append(
            f"# Wiki\n\nYou are answering about the '{wiki}' wiki.\n\n"
            f"You have Jamgraf's own tools available and can read the wiki "
            f"directly — search it, open pages, follow [[links]], check "
            f"backlinks — rather than relying only on what is quoted below.\n\n"
            f"Call `switch_wiki(\"{wiki}\")` first: a fresh session has no "
            f"active wiki and every content tool will refuse until you do.")
    if title:
        parts.append(f"# Article: {title}\n\n{article_md.strip()}")
    if excerpts.strip():
        parts.append("# Other relevant excerpts from this wiki\n\n" + excerpts.strip())
    convo = []
    for turn in history or []:
        role = "User" if turn.get("role") == "user" else "Assistant"
        text = (turn.get("content") or "").strip()
        if text:
            convo.append(f"{role}: {text}")
    if convo:
        parts.append("# Conversation so far\n\n" + "\n\n".join(convo))
    parts.append(f"# New question\n\n{question.strip()}")
    return "\n\n---\n\n".join(parts)


# Read-only MCP tools the chat agent may call. Deliberately a whitelist rather
# than "everything": a chat session should be able to look things up, not edit
# the wiki. switch_wiki is in here because it MUST be — a fresh MCP session has
# no active wiki by design (issue #11), so without it every content call errors.
CHAT_TOOLS = [
    "switch_wiki", "current_wiki", "list_wikis",
    "get_page", "get_metadata", "get_property", "check_pages",
    "search", "search_subpages", "list_pages", "list_children",
    "backlinks", "broken_links", "list_tags", "pages_by_tag",
    "list_docs", "read_doc", "list_templates", "list_elements", "get_element",
    "list_comments", "list_suggestions", "changes_since",
    # The one tool here that writes anything, and it deliberately does not touch
    # the page: propose_edit records a suggestion for the human to apply or
    # reject. Chat asked to make a change used to fail outright, because the
    # model reached for edit_page and the CLI refused a tool it was not granted.
    # Proposing keeps the property that matters — a conversation never silently
    # rewrites your wiki — while letting the thing you asked for happen.
    "propose_edit",
]


def _mcp_config_path() -> str:
    """Write this install's MCP config where the CLI can read it.

    Lives in DATA_DIR rather than a temp file so it survives for the life of the
    subprocess and is inspectable when chat misbehaves. Contains no secrets —
    just how to launch our own server.
    """
    import json

    path = config.DATA_DIR / "mcp-chat.json"
    path.write_text(json.dumps(config.mcp_server_config(), indent=2),
                    encoding="utf-8")
    return str(path)


def _cli_args(provider: str, cli: str, model: str, prompt: str) -> list[str]:
    """Argv for one-shot print mode. Claude Code: `-p`; Gemini: `-p`, `-m`."""
    if provider == "gemini":
        # Gemini's CLI takes a different MCP configuration; until that is wired
        # it runs without wiki access, on the prompt alone.
        args = [cli, "-p", prompt]
        if model:
            args += ["-m", model]
    else:  # claude (default)
        args = [cli, "-p", prompt]
        if model:
            args += ["--model", model]
        try:
            args += [
                "--mcp-config", _mcp_config_path(),
                # Only our server: the user's own MCP setup is theirs, and a
                # chat about a wiki page has no business reaching into it.
                "--strict-mcp-config",
                "--allowedTools", *[f"mcp__jamgraf__{t}" for t in CHAT_TOOLS],
            ]
        except Exception as exc:   # never let this break plain chat
            print(f"[jamgraf] chat MCP config unavailable: {exc}", file=sys.stderr)
    return args


def answer(slug: str | None, question: str, provider: str = "claude",
           model: str = "", history: list[dict] | None = None,
           timeout: int = 180) -> dict:
    """Answer a question about page `slug` using the selected CLI. Synchronous
    (subprocess) — call it in a worker thread from async code."""
    provider = provider if provider in ("claude", "gemini") else "claude"
    if not (question or "").strip():
        return {"ok": False, "error": "Ask a question first."}
    page = store.get_page(slug) if slug else None
    if slug and not page:
        return {"ok": False, "error": f"No page '{slug}'."}

    prompt = build_prompt(
        page["title"] if page else "",
        page["markdown"] if page else "",
        "",
        history or [], question, chat_system_prompt(),
        wiki=db.active_wiki())

    # Doorman first, when it has an agent. It gets `wiki` and `page` because it
    # keys a real, audited conversation per (wiki, page, agent) — that is what
    # keeps those threads separate and legible on its side. A None answer means
    # "not available": fall through to the CLI without a word about it.
    from . import doorman
    got = doorman.ask(prompt, wiki=db.active_wiki(), page=slug or "",
                      agent=provider, timeout=timeout)
    if got is not None:
        if not got.get("ok"):
            return {"ok": False, "error": got.get("error", "Doorman couldn't answer.")}
        return {"ok": True, "provider": "doorman", "backend": "doorman",
                "label": got.get("label", "Doorman"), "answer": got["answer"]}

    binary = "gemini" if provider == "gemini" else "claude"
    cli = find_cli(binary)
    if not cli:
        hint = ("Install Google's Gemini CLI (`npm i -g @google/gemini-cli`)"
                if provider == "gemini" else
                "Install the Claude Code CLI (`npm i -g @anthropic-ai/claude-code`)")
        return {"ok": False,
                "error": f"`{binary}` CLI not found on this machine. {hint}, "
                         f"then reopen Jamgraf."}

    # Note the prompt above carries no pre-retrieved excerpts: the agent has the
    # wiki's own search and page tools, and five chunks guessed in advance are
    # strictly worse than letting it look up what it actually needs (issue #30).
    try:
        proc = clirun.run(f"{binary}:chat", _cli_args(provider, cli, model, prompt),
                          timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"The {binary} CLI timed out after {timeout}s."}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to run {binary}: {exc}"}

    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return {"ok": False, "error": f"{binary} error: {err[:400]}"}
    # Some CLIs prepend a stray login/banner line; strip common ANSI noise.
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    return {"ok": True, "provider": provider, "backend": provider,
            "label": f"{binary} CLI" + (f" · {model}" if model else ""),
            "answer": out}
