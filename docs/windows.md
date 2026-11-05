# Jamgraf on Windows

Jamgraf started as a macOS `.app` and the pip package was the server half — "the
half that runs anywhere", with the window left in the cask. This is what changed
when Windows got a window of its own, what it costs, and which of the answers
here were measured rather than assumed.

## What you install

```powershell
pip install "jamgraf[desktop]"
jamgraf-app          # the window
```

Three entry points, all landing on the same FastAPI app and the same `store`:

| command | what it is | needs |
| --- | --- | --- |
| `jamgraf` | the server, no window; open it in a browser | nothing extra |
| `jamgraf-mcp` | the stdio MCP server, for Claude Desktop | nothing extra |
| `jamgraf-app` | Jamgraf's own window | the `desktop` extra, and WebView2 |

`pip install jamgraf` without the extra stays deliberately GUI-free: it has to
remain installable on a headless box, and on a Mac that already has the cask.

The engine needed nothing new. Every runtime dependency publishes a `win_amd64`
wheel — apsw, sqlite-vec, pycrdt, cryptography, tokenizers and onnxruntime
(under fastembed) — and `uvicorn[standard]`'s own marker keeps uvloop, which has
no Windows support, off the platform. So this was never a port of the app; it
was a shell.

## The window is Chromium, which is why the Windows code is nearly empty

`jamgraf/desktop/` is one launcher with a native module per platform. The macOS
module is 150 lines and the Windows one is 90, and the asymmetry is the finding
rather than an omission.

Three of the macOS natives exist because WKWebView withholds something a browser
gives the page, and each replaces a web API that is *present and non-functional*
in the packaged app: `navigator.clipboard.readText()` (⌘V did nothing at all),
`webkitSpeechRecognition` (the object exists; `start()` fails `not-allowed`), and
an Edit menu, which WKWebView simply has none of. A WebView2 window withholds
none of them.

So Windows does not reimplement those. It leaves the methods **off**, and that
is a design decision with teeth: `base.html` decides whether to take the native
dictation route by asking whether `window.pywebview.api.start_dictation` exists.
A stub returning an error would make the page take a native route that cannot
work; absence makes it use the browser's own, which on Chromium is the route
that does. `tests/test_desktop_shell.py` fails if either method moves into the
shared API.

## No WebView2, no window — said out loud

pywebview falls back to **MSHTML**, the Trident engine from Internet Explorer,
when the WebView2 runtime is missing. Jamgraf's editor would not render in it.
A person who clicks and finds a broken app has been told nothing, which is the
failure CLAUDE.md rule 9 exists to prevent, so:

* the runtime is checked first, by Microsoft's documented detection — the `pv`
  version under the `EdgeUpdate\Clients\{F3017226-…}` key, per-machine or
  per-user. The uninstaller leaves the key behind, so the version string is what
  counts and `0.0.0.0` means "not installed";
* the backend is pinned to `edgechromium`, so there is no silent downgrade even
  if the check is wrong;
* a machine without it is told what is missing, where it comes from, and what is
  happening instead — Jamgraf opens in the system browser, which is a complete
  Jamgraf minus the native Save/Open dialogs.

The fallback deliberately does **not** set `JAMGRAF_DESKTOP`. `config.is_desktop()`
is what tells the help and capabilities pages that this install has native
dictation and native file dialogs; a browser standing in for a window must not
claim them, or both pages describe affordances the page has not got.

## Credentials: a real store, not an exception

`secretstore` refuses to fall back to a file when a machine has no secure store
(rule 11), and the reason is specific: the wiki file *is* what "Save wiki"
exports, and `app_config.json` is plaintext in the data directory. A Windows
build could have honoured that by refusing to sign in to Kahala at all. Windows
has a real store, so it got a real backend instead — `jamgraf/wincreds.py`, the
Credential Manager through `advapi32`.

Three decisions sit *above* the ctypes seam, where a test can see them on any
platform:

* **`CRED_PERSIST_LOCAL_MACHINE`, never `ENTERPRISE`.** Enterprise credentials
  roam with the user's profile to every machine they sign in to, which would
  hand this machine's Kahala refresh token to all of them — rule 11's "a
  credential must not travel", with a Win32 constant on it.
* **The target carries the service name**, so a person can find and delete
  everything Jamgraf owns in Credential Manager without guessing.
* **A control character is refused on Windows too**, although `CredWriteW` has
  no command parser to confuse the way `security -i` does. "Storable on a Mac,
  refused on Windows" would be a sign-in that fails for a reason nobody can see.

What it buys is the same as the Keychain note next door, and no more: the token
is encrypted at rest under DPAPI and is not in a file you can export, mail or
restore from a copied data directory. It does **not** protect against code
already running as you — `CredRead` hands the blob back without a prompt,
exactly as Jamgraf calls it. That is the trust boundary the whole app already
sits on (`auth.py` grants loopback callers owner rights), so it is not a new
exposure, and a document implying a prompt here would be false.

## What Windows does not get

| | why |
| --- | --- |
| `jamgraf://` deep links | They need a registry key naming an executable. A pip install's launcher lives inside whichever virtualenv it landed in, so the path goes stale when that moves — and writing a URL-scheme claim into someone's registry from a `pip install` is more than they asked for. Reported, not quietly claimed. |
| Self-update | Rule 8 updates a signature-verified `.app` bundle, and there isn't one. `can_update()` already failed closed here; what changed is that it now names the step that works — `pip install -U jamgraf` — instead of offering to replace the build. |
| Native dictation | No macOS Dictation to click. The 🎤 uses the web speech API, which an embedded WebView2 may not provide; the capability says "may not" and names the browser, because this is the one claim here that was not measured. |
| Doorman | macOS-only sibling app. Optional everywhere by rule, absent here, and never a remedy. |

Node and cloudflared **are** installable, through winget rather than Homebrew,
using winget's own published identifiers (`OpenJS.NodeJS`,
`Cloudflare.cloudflared`). The `curl | bash` nvm path does not appear on Windows
at all: winget installs Node outright, so the riskiest remedy in the app has
nothing to rescue there.

## What is actually verified, and by what

Worth stating plainly, because the honest answer is not "all of it".

* **Measured on macOS, by running it.** The split shell opens its window, loads
  the app and exposes exactly the six-method native API. The Mac path is
  unchanged in substance — same launcher, same menus, same natives.
* **Tested on every platform.** Everything above the two seams: which methods
  each platform's API exposes, the WebView2 reason text, the browser fallback
  not claiming to be the desktop shell, the credential policy (persistence,
  target, refusals), the winget argv shape, and what each install kind is told
  about updating.
* **Covered by the `windows-latest` CI job and nothing else.** The ctypes
  marshalling in `wincreds._load` (struct layout, blob pointer, `CredFree`), the
  registry walk, that the whole suite passes on Windows at all, and that
  `jamgraf[desktop]` resolves and imports there. A runner has no interactive
  desktop, so it cannot open a window.
* **Not verified anywhere yet.** That a WebView2 window renders Jamgraf
  correctly on a real Windows desktop, and whether the 🎤 finds a speech API
  there. Both need someone at a Windows machine.
