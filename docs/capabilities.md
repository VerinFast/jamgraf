# Capabilities and remedies

Jamgraf shells out to tools it does not ship: the `claude`/`gemini` CLIs for chat
and drafting, an image CLI for pictures, `cloudflared` for a public link, Ollama
for local generation. On a machine that hasn't got one, the feature used to fail
*after* the click, with an error containing a shell command to copy.

**Settings → Capabilities** replaces that. Implementation:
[`jamgraf/capabilities.py`](../jamgraf/capabilities.py) decides *what is true and
which step is actionable*; [`jamgraf/remedies.py`](../jamgraf/remedies.py) owns
*what may be executed at all* and is the only place an argv is written.

## The rule

> Never expose a command or a message. Expose a button that fixes it. Grey out
> the feature when it can't work, and say how to fix it.

Which becomes four properties, each guarded by
[`tests/test_capabilities.py`](../tests/test_capabilities.py):

1. **Knowable before the click.** Every capability is probed and reported —
   state (`ok` / `degraded` / `unavailable`), what it powers, and a
   plain-language reason. The states reach the templates through `_ctx`, so
   **Chat**, **✦ Generate** and the editor's ✨ image button render disabled and
   linked to this view rather than failing when pressed.
2. **A remedy is a descriptor, not a string of shell.** A stable id, a label, and
   what the user is agreeing to. The routes execute ids from a registry; nothing
   a caller supplies ever reaches an argv.
3. **The chain, not the destination.** A remedy has prerequisites of its own.
   Offering *"install the Claude Code CLI"* on a machine without `npm` is the
   same dead end one step later, so the offer resolves to whatever is actionable
   **now**: no npm makes the button Node.js; no Homebrew either makes it
   instructions with a link, and no button at all. "Now" includes *this
   platform*: the package manager is Homebrew on macOS and winget on Windows,
   using winget's own published identifiers rather than a guess at what a
   package might be called. The `curl | bash` step below has no Windows
   counterpart — winget installs Node outright, so nothing needs rescuing.
4. **Honest when we can't.** Where no vendor publishes an install path, the row
   says the capability needs manual setup and points at the setting that changes
   it. A greyed-out button with instructions beats a fabricated command.

## What is reported

| capability | needs | remedy |
|---|---|---|
| Chat with an article | `claude`/`gemini`, or a Doorman agent | install the CLI (chain) |
| AI drafting of templates and elements | `claude`/`gemini` — **no Doorman path** | install the CLI (chain) |
| AI generation | a Doorman agent, an Anthropic key, or Ollama | manual (key / provider) |
| Image generation | the configured image CLI, or Doorman | see below |
| Read aloud | nothing — the browser always can | none needed |
| Dictation | browser speech, or macOS Accessibility in the app | open the settings pane |
| Dictation, in the app window on Windows | web speech, which an embedded WebView2 may not provide | none — names the browser |
| Doorman | — | **none, deliberately** |
| Automatic updates | a packaged, writable, signed-key build | manual (pip, or git, by install kind) |
| Temporary public link | `cloudflared` | `brew install` / `winget install` (chain) |
| Semantic search | sqlite-vec, which ships inside Jamgraf | none possible |

Drafting is deliberately *not* Doorman-aware: `authoring.draft` has no Doorman
path (it reads the wiki's own elements over MCP), so reporting it as ready
because Doorman is running would be the original bug in a new place.

## Doorman is never a remedy

Doorman's row is informational: present or absent, and what it would improve. No
button, no offer, no install. It is the user's own app and Jamgraf must never
start or install it — see [doorman.md](doorman.md) and CLAUDE.md. An absent
Doorman is marked *optional* so it reads as the ordinary case, not a fault.

## Installing changes the user's machine

Every remedy that runs something is a two-step: the first POST only *offers*,
redirecting to a confirmation that names what will happen. Consent is enforced
server-side, not by a JavaScript `confirm()` a stale form could skip. The work
runs off the event loop, the prerequisite is re-checked at execution time, and
the outcome is reported honestly — a permissions failure on `npm i -g` is the
single most common result and reads as one, with what the tool actually said
kept behind a disclosure. Exit 0 is not accepted as proof: the tool has to
actually be there afterwards. Probes are re-run immediately, so the view is
correct without a restart.

## The one script installer

The default image CLI is Google's Antigravity (`agy`), whose published install
path pipes a downloaded script into a shell. That is a materially bigger ask than
an `npm i -g`, which at least resolves through a registry — and it sits oddly
next to [`updater.py`](../jamgraf/updater.py), which refuses to execute a release
it has not Ed25519-verified against a build-time-pinned key (CLAUDE.md rule 8).

The button stays, because a command to copy is no safer and much less usable.
The safeguards are:

- The URL is a **constant** in `remedies.py`, per platform, and the argv is
  built from it. It never comes from a setting, a page, or any user- or
  agent-supplied value.
- The confirmation **names the host** (`antigravity.google`) and says plainly
  that Jamgraf cannot verify the script the way it verifies its own updates.
- It runs only from a deliberate click plus that confirmation — never from a
  probe, a page render, or any other side effect.
- Failure surfaces as failure: exit status and stderr, never a hang or a silent
  no-op.

`agy` is only the **default**. If the image CLI setting names something else, the
Antigravity installer is not offered for it — that path is manual setup pointing
at the setting, because guessing an install for an unknown binary is exactly the
fabrication this view exists to avoid.

## Cost

The view is rendered on every page (the feature buttons are gated on it), so the
probes are cheap: PATH lookups, cached both ways like `doorman`'s, with the one
network probe (Ollama) only made when Ollama is the selected provider.
