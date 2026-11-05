"""Signing in to Kahala — OpenID Connect, authorization code + PKCE.

Kahala authenticates against Keycloak natively (good-place decision D5), so
Jamgraf is an OIDC **public client**: ``good-place-waikiki``, standard flow with
PKCE S256 and no client secret. A desktop binary cannot keep a secret -- anyone
holding the .app holds it -- which is why the client is public and PKCE, not the
secret, is what binds the code to the app that asked for it.

The dance, and where each piece lives
-------------------------------------
1. :func:`begin` mints a verifier + state, remembers them **in memory only**,
   and returns Keycloak's authorize URL for the browser to open.
2. Keycloak redirects back to ``/kahala/callback`` on this app's own loopback
   port. That is deliberate: Jamgraf already runs an HTTP server on 127.0.0.1,
   so there is no second listener to start, no port race, and no window where an
   unrelated socket is open. RFC 8252 §7.3 prefers loopback for native apps for
   the same reason we can't use a custom scheme -- any other app on the machine
   can claim ``jamgraf://`` and intercept the code.
3. :func:`complete` exchanges the code (plus the verifier) for tokens.
4. The **refresh token** goes to the Keychain via :mod:`secretstore`; the access
   token is held in memory with its expiry and never written down at all.

Refusals that are on purpose
----------------------------
* **No secure store, no sign-in.** If :func:`secretstore.available` is False we
  refuse rather than keeping the refresh token in ``app_config.json``. A
  credential in a plaintext file is the thing we are avoiding; doing it quietly
  as a fallback would be worse than not shipping the feature.
* **https only**, except loopback for developing against a local Kahala. The
  token is a bearer credential: over http, anything on the path can lift it.
* **Redirects are never followed.** ``httpx`` follows them by default, and a
  followed redirect re-sends the ``Authorization`` header to whatever host the
  response names -- which is how a bearer token leaks to a third party. Every
  request here and in :mod:`kahala` sets ``follow_redirects=False`` and treats a
  3xx as a failure.
* **A state that we did not mint is refused**, and each state is single-use, so
  a replayed or forged callback cannot complete a sign-in.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx

from . import appconfig, config, secretstore

DEFAULT_ISSUER = "https://app.kwirker.com/kc/realms/good-place"
DEFAULT_CLIENT_ID = "good-place-waikiki"

_TIMEOUT = 20.0
_FLOW_TTL = 600          # a sign-in the user never finishes expires quietly
_SKEW = 30               # refresh this many seconds before the token expires

# RFC 6749 §5.2: the token endpoint says `invalid_grant` when the credential it
# was handed is expired, revoked, or was issued to another client. It is the one
# refusal that tells us the stored refresh token is *gone* rather than that this
# attempt went wrong, so it is the only one that may delete a credential -- a
# malformed request earns `invalid_request` or `invalid_client`, which must not.
# Kept as a set because the decision is "is this code in the dead list", not
# "does it equal a string", and a second provider may yet justify a second code.
_DEAD_GRANT = frozenset({"invalid_grant"})


class GrantRejected(ValueError):
    """The sign-in server refused the credential, not the request.

    Separate from every other token-endpoint failure because the handling is
    opposite: a 5xx, a timeout or an unreachable host say nothing about the
    refresh token and want it kept for the next attempt, while this says it will
    never work again and keeping it strands the account (see `access_token`).
    """


# Pending sign-ins, keyed by state. In memory only: an interrupted flow should
# not survive a restart, and a verifier on disk is a credential on disk.
_pending: dict[str, dict] = {}

# Access tokens, keyed by issuer: {"token": str, "expires": float}. Also memory
# only -- the refresh token in the Keychain is what survives, by design.
_access: dict[str, dict] = {}


def issuer() -> str:
    return (appconfig.get("kahala_issuer") or DEFAULT_ISSUER).rstrip("/")


def client_id() -> str:
    return appconfig.get("kahala_client_id") or DEFAULT_CLIENT_ID


def redirect_uri() -> str:
    """Always loopback, never ``config.HOST``.

    ``HOST`` becomes ``0.0.0.0`` when LAN sharing is on, which is not an address
    a browser can come back to and not one we would want registered. The port is
    the app's real port so the URI matches what the browser will actually reach.
    """
    return f"http://127.0.0.1:{config.PORT}/kahala/callback"


def secure_url(url: str) -> bool:
    """https, or http on loopback (developing against a local Kahala)."""
    try:
        parts = urlparse(url)
    except Exception:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    return (parts.scheme == "http"
            and parts.hostname in ("127.0.0.1", "localhost", "::1"))


def can_sign_in() -> tuple[bool, str]:
    """Whether a sign-in is possible at all, and why not when it isn't."""
    if not secretstore.available():
        return False, secretstore.unavailable_reason()
    if not secure_url(issuer()):
        return False, (f"The sign-in server address ({issuer()}) isn't https, "
                       "so a token sent to it could be read in transit.")
    return True, ""


def signed_in() -> bool:
    """Whether a refresh token is stored -- NOT whether it still works.

    Proving it works means spending it against the sign-in server, which is a
    network round trip this is called too often to make. `access_token` is what
    finds out, and it clears the credential when the server rejects it, so the
    two only disagree until the next push or pull.
    """
    return bool(secretstore.get_secret(_account()))


def sign_out() -> bool:
    """Forget the refresh token and any cached access token."""
    _access.pop(issuer(), None)
    return secretstore.delete_secret(_account())


# --- the flow ----------------------------------------------------------------


def begin(next_url: str = "/kahala", external: bool = False) -> tuple[str, str]:
    """Start a sign-in. Returns (authorize_url, state), or raises ValueError.

    ``external`` records that this flow will finish in a browser that is *not*
    the app's own window (the desktop shell hands the URL to the system
    browser). The callback needs to know, because redirecting into the app UI
    would then land in a stray tab rather than in the window the person is
    actually looking at.
    """
    ok, why = can_sign_in()
    if not ok:
        raise ValueError(why)

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)

    _sweep()
    _pending[state] = {"verifier": verifier, "born": time.time(),
                       "next": next_url, "external": external}

    meta = _discover()
    query = urlencode({
        "client_id": client_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "scope": "openid profile email",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{meta['authorization_endpoint']}?{query}", state


def is_external(state: str) -> bool:
    """Whether ``state`` belongs to a flow started outside the app's window.

    Read without consuming the flow — ``complete`` still pops it — so the
    callback can decide how to answer before and after the exchange, including
    when the provider came back with an error and there is nothing to exchange.
    An unknown state is treated as external: a callback we did not start is not
    something to redirect the app's own UI on the strength of.
    """
    flow = _pending.get(state)
    return True if flow is None else bool(flow.get("external"))


def complete(code: str, state: str) -> dict:
    """Finish a sign-in. Returns {"ok": bool, "error": str, "next": str}."""
    _sweep()
    flow = _pending.pop(state, None)      # single use: pop, never peek
    if not flow:
        return {"ok": False, "next": "/kahala",
                "error": "That sign-in link has expired or was already used. "
                         "Start again from this page."}
    if not code:
        return {"ok": False, "next": flow["next"],
                "error": "Kahala did not send an authorization code back."}

    try:
        meta = _discover()
        data = _post_token({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": client_id(),
            "code_verifier": flow["verifier"],
        }, meta)
    except Exception as exc:
        return {"ok": False, "next": flow["next"],
                "error": f"Signing in failed: {_clean(exc)}"}

    refresh = data.get("refresh_token")
    if not refresh:
        return {"ok": False, "next": flow["next"],
                "error": "Kahala returned no refresh token, so there is nothing "
                         "to stay signed in with."}
    if not secretstore.set_secret(_account(), refresh):
        return {"ok": False, "next": flow["next"],
                "error": f"Signed in, but the token could not be saved to "
                         f"{secretstore.store_name()} — so it was discarded "
                         f"rather than written somewhere less safe. Nothing was "
                         f"stored."}
    _remember_access(data)
    return {"ok": True, "error": "", "next": flow["next"]}


def access_token() -> str | None:
    """A usable access token, refreshed if needed. None when not signed in.

    Never raises: every caller of this is about to make a network request that
    has its own error path, and "we could not get a token" is the same outcome
    as "not signed in" from the caller's point of view.

    **Signs out** when the server rejects the stored refresh token, so that the
    reported state is the true one. A failure that says nothing about the
    credential -- unreachable, 5xx, timeout -- leaves it alone.
    """
    iss = issuer()
    held = _access.get(iss)
    if held and held["expires"] - _SKEW > time.time():
        return held["token"]

    refresh = secretstore.get_secret(_account())
    if not refresh:
        return None
    try:
        data = _post_token({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id(),
        }, _discover())
    except GrantRejected:
        # The stored token is dead and every later refresh will fail the same
        # way. Leaving it in the Keychain is what makes `signed_in` keep
        # answering True while push and pull fail -- an account that reads as
        # signed in, with nothing on screen the person can act on, and no way
        # out except a sign-out they have no reason to press. Drop it, so the
        # pane offers the sign-in that actually fixes this.
        sign_out()
        return None
    except Exception:
        # Everything else -- a 5xx, a timeout, Kahala unreachable, a discovery
        # document we could not fetch -- says nothing about the credential. Keep
        # it: signing someone out because their train went into a tunnel loses a
        # sign-in they still have.
        return None

    # Keycloak rotates refresh tokens by default, and the one just spent is dead
    # the moment this call succeeds. So the new one has to land: if the Keychain
    # write fails there is nothing left to stay signed in with, and keeping the
    # spent token would leave an account that reads as signed in while every
    # later refresh fails with a token the server was told to stop honouring.
    # Drop it and report signed out -- which is what has actually happened, and
    # is something the person can act on by signing in again.
    if data.get("refresh_token"):
        if not secretstore.set_secret(_account(), data["refresh_token"]):
            sign_out()
            return None
    return _remember_access(data)


# --- internals ---------------------------------------------------------------


def _account() -> str:
    """Keychain account for the current realm.

    Keyed by issuer, not by Kahala base URL: the realm is what mints the token,
    so two wikis on the same Kahala share one sign-in, and signing out of one
    signs out of the identity rather than of a single link.
    """
    return f"kahala:{issuer()}"


def _discover() -> dict:
    """The realm's OIDC metadata. Raises on anything that isn't usable."""
    url = f"{issuer()}/.well-known/openid-configuration"
    if not secure_url(url):
        raise ValueError("the sign-in server address is not https")
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
        resp = client.get(url)
    if resp.status_code != 200:
        raise ValueError(f"the sign-in server answered {resp.status_code}")
    meta = resp.json()
    for key in ("authorization_endpoint", "token_endpoint"):
        if not secure_url(str(meta.get(key, ""))):
            raise ValueError(f"the sign-in server's {key} is missing or not https")
    return meta


def _post_token(form: dict, meta: dict) -> dict:
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
        resp = client.post(meta["token_endpoint"], data=form)
    if resp.status_code != 200:
        # Keycloak puts a machine-readable reason in the body; surface that
        # rather than the body itself, which can echo the code back.
        try:
            body = resp.json()
        except Exception:
            body = {}
        # Valid JSON that isn't an object -- a bare string or array from a proxy
        # rather than the IdP -- would make `.get` raise out of here, and this
        # function's contract is that a non-200 always arrives as a written
        # reason (`_clean` says as much, and `complete` puts it on screen).
        if not isinstance(body, dict):
            body = {}
        code = body.get("error") or ""
        reason = body.get("error_description") or code
        message = (f"the sign-in server answered {resp.status_code}"
                   + (f" ({reason})" if reason else ""))
        # The machine-readable code, not the status: Keycloak answers 400 both
        # for a dead refresh token and for a request we built wrong, and only
        # the first of those should cost the person their sign-in.
        if code in _DEAD_GRANT:
            raise GrantRejected(message)
        raise ValueError(message)
    return resp.json()


def _remember_access(data: dict) -> str | None:
    token = data.get("access_token")
    if not token:
        return None
    try:
        ttl = float(data.get("expires_in") or 300)
    except (TypeError, ValueError):
        ttl = 300.0
    _access[issuer()] = {"token": token, "expires": time.time() + ttl}
    return token


def _sweep() -> None:
    cutoff = time.time() - _FLOW_TTL
    for state in [s for s, f in _pending.items() if f["born"] < cutoff]:
        _pending.pop(state, None)


def _clean(exc: Exception) -> str:
    """An exception's message with no token material in it.

    Only ever called on exceptions this module raised itself (which carry
    written reasons) or on httpx transport errors (which carry addresses, not
    credentials) -- but the guard is cheap and the cost of being wrong is a
    credential in a rendered error page.
    """
    text = str(exc) or exc.__class__.__name__
    return text if len(text) < 200 else text[:200] + "…"


def forget() -> None:
    """Drop in-memory state. For tests and for sign-out."""
    _pending.clear()
    _access.clear()
