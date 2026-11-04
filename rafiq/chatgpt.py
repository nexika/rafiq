"""Sign in with ChatGPT (#196): Rafiq's brain on the user's ChatGPT plan, with no API key and no second bill.

OpenAI allows this for open-source apps that run locally, as a preview, for Plus and Pro plans
(developers.openai.com/siwc/token-sharing-open-source, read 2026-11-02). The flow:
- OAuth with PKCE, state and nonce, and a loopback callback on 127.0.0.1 whose path is fixed (/auth/callback).
- The first sign-in registers this app with client_id=dynamic_agent_client; OpenAI answers with a client ID of its
  own, bound to the user and workspace, used from then on.
- Each install has a stable, opaque host ID (ext_agent_host_id), made once and kept even after signing out.
- The ID token is checked (RS256 against OpenAI's published keys, issuer, audience, expiry, nonce) before the
  account is saved; the account must have granted chatgpt.tokens.use.direct, plan usage.
- Tokens are saved 0600, written atomically; access tokens last an hour and refresh tokens rotate.

Tokens never go in a URL, a log line or an error message."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from . import config

ISSUER = "https://auth.openai.com"
AUTHORIZE = f"{ISSUER}/api/accounts/authorize"
TOKEN = f"{ISSUER}/api/accounts/oauth/token"
REVOKE = f"{ISSUER}/api/accounts/oauth/revoke"
KEYS = f"{ISSUER}/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"          # the audience of the access token, and the API it is for
PLAN = "chatgpt.tokens.use.direct"               # the scope that lets Rafiq use the plan
SCOPE = f"openid profile email offline_access resource.invoke {PLAN}"
FIRST_CLIENT = "dynamic_agent_client"
APP_NAME = "Rafiq"
PORT = 1455                                      # OpenAI's example; only the port of the callback may change
CALLBACK_PATH = "/auth/callback"
USAGE_URL = "https://chatgpt.com/settings/usage"
REFRESH_EARLY = 60                               # seconds before the access token runs out
# The refresh answers that mean the sign-in is over (the user revoked it, or 30 days passed).
SIGNED_OUT = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
              "refresh_token_invalidated", "refresh_token_reused"}
# DER of SHA-256's DigestInfo: what an RS256 signature holds before the hash (RFC 8017, 9.2).
SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class SignInError(Exception):
    """Signing in did not finish; the message says why, with no token in it."""


class SignedOut(Exception):
    """There is no usable sign-in: run `rafiq chatgpt-login`."""


# ---------------------------------------------------------------------- files

def folder() -> Path:
    path = config.home() / "chatgpt"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _save(path: Path, data: dict) -> None:
    """Owner-only, and atomic: a crash mid-write leaves the old file, never half a token."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out)
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def host() -> dict:
    """This install: its host ID, made once, and the client ID OpenAI issued, once there is one."""
    path = folder() / "host.json"
    data = _read(path)
    if not str(data.get("host_id", "")).startswith("urn:uuid:"):
        data = {"host_id": f"urn:uuid:{uuid.uuid4()}"}
        _save(path, data)
    return data


def _keep_client(client_id: str, subject: str) -> None:
    """The registration OpenAI issued and the account it is for: kept after signing out."""
    data = host()
    if (data.get("client_id"), data.get("subject")) != (client_id, subject):
        _save(folder() / "host.json", {**data, "client_id": client_id, "subject": subject})


def account() -> dict:
    """The signed-in account, with its tokens; {} when signed out."""
    return _read(folder() / "account.json")


def signed_in() -> bool:
    """Signed in, with plan usage granted. No network: a sign-in revoked in ChatGPT shows up on the next request."""
    found = account()
    return bool(found.get("refresh_token")) and PLAN in found.get("scopes", [])


def status() -> dict:
    """For `rafiq doctor` and the panel: never a token."""
    found = account()
    return {"signed_in": bool(found.get("refresh_token")), "plan": PLAN in found.get("scopes", []),
            "email": found.get("email", "")}


# ---------------------------------------------------------------------- the authorization request

def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge): the challenge is the verifier's SHA-256, base64url without padding."""
    verifier = secrets.token_urlsafe(64)
    return verifier, _b64(hashlib.sha256(verifier.encode()).digest())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def authorize_url(redirect: str, challenge: str, state: str, nonce: str, client_id: str = "",
                  login_hint: str = "", consent: bool = False) -> str:
    """The page that signs in. Without a client ID it registers Rafiq (first time); with one it signs in again,
    with the saved email as login_hint: not the ID token, which would stay in the browser's history."""
    query = {"client_id": client_id or FIRST_CLIENT, "ext_agent_host_id": host()["host_id"],
             "response_type": "code", "redirect_uri": redirect, "scope": SCOPE, "resource": RESOURCE,
             "state": state, "nonce": nonce, "code_challenge_method": "S256", "code_challenge": challenge}
    if not client_id:
        query["agent_name_hint"] = APP_NAME
    if login_hint:
        query["login_hint"] = login_hint
    if consent:                                  # plan usage was declined before: ask for it again
        query["prompt"] = "consent"
    return f"{AUTHORIZE}?{urlencode(query)}"


def callback(answer: dict, state: str, client_id: str = "") -> tuple[str, str]:
    """(code, client ID) from what the browser brought back, or SignInError. The state is checked first,
    so a page that sends the browser to the callback can't sign the user in to its own account."""
    if not secrets.compare_digest(str(answer.get("state", "")), state):
        raise SignInError("the answer wasn't for this sign-in (state)")
    if answer.get("error"):
        raise SignInError("you said no in the browser" if answer["error"] == "access_denied"
                          else f"ChatGPT answered {str(answer['error'])[:60]}")
    issued = str(answer.get("client_id") or client_id)
    if not issued or issued == FIRST_CLIENT:
        raise SignInError("ChatGPT didn't finish registering Rafiq; try again")
    if client_id and issued != client_id:
        raise SignInError("ChatGPT answered for another app registration")
    if not answer.get("code"):
        raise SignInError("ChatGPT sent no code")
    return str(answer["code"]), issued


# ---------------------------------------------------------------------- the ID token

def _rs256(signed: bytes, signature: bytes, key: dict) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256, checked by building the whole expected block and comparing it,
    so there is no parsing of the signature to get wrong (the Bleichenbacher-style forgeries)."""
    try:
        n, e = int.from_bytes(_unb64(key["n"]), "big"), int.from_bytes(_unb64(key["e"]), "big")
    except (KeyError, TypeError, ValueError):
        return False
    size = (n.bit_length() + 7) // 8
    digest = SHA256_INFO + hashlib.sha256(signed).digest()
    if len(signature) != size or size < len(digest) + 11:
        return False
    value = int.from_bytes(signature, "big")
    if value >= n:
        return False
    expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    return hmac.compare_digest(pow(value, e, n).to_bytes(size, "big"), expected)


def check_id_token(token: str, keys: list[dict], client_id: str, nonce: str, now: float | None = None) -> dict:
    """The ID token's claims, once its signature (OpenAI's key, RS256 only), issuer, audience, expiry and
    nonce hold; otherwise SignInError."""
    try:
        head, body, sig = token.split(".")
        header, claims = json.loads(_unb64(head)), json.loads(_unb64(body))
        signature = _unb64(sig)
    except (ValueError, AttributeError) as exc:
        raise SignInError("the ID token is malformed") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256":
        raise SignInError("the ID token isn't signed with RS256")
    key = next((k for k in keys if isinstance(k, dict) and k.get("kid") == header.get("kid")
                and k.get("kty") == "RSA"), None)
    if key is None or not _rs256(f"{head}.{body}".encode(), signature, key):
        raise SignInError("the ID token isn't signed by OpenAI")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    now = time.time() if now is None else now
    if claims.get("iss") != ISSUER:
        raise SignInError("the ID token is from another issuer")
    if client_id not in audiences:
        raise SignInError("the ID token is for another app")
    if not isinstance(claims.get("exp"), int | float) or claims["exp"] <= now:
        raise SignInError("the ID token has expired")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SignInError("the ID token is for another sign-in (nonce)")
    if not claims.get("sub"):
        raise SignInError("the ID token names no account")
    return claims


def _keys(http) -> list[dict]:
    resp = http.get(KEYS, timeout=15)
    resp.raise_for_status()
    found = resp.json().get("keys")
    return found if isinstance(found, list) else []


# ---------------------------------------------------------------------- tokens

def _post(http, data: dict) -> tuple[int, dict]:
    resp = http.post(TOKEN, data=data, timeout=15)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _error_code(body: dict) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        error = error.get("code") or error.get("type")
    return str(error or "")


def _scopes(answer: dict, previous: list[str] | None = None) -> list[str]:
    scope = answer.get("scope")
    return scope.split() if isinstance(scope, str) and scope else list(previous or [])


def _record(answer: dict, previous: dict) -> dict:
    """The saved account after a token answer: the access token, its expiry, the scopes and the rotated
    refresh token are replaced together."""
    return {**previous, "access_token": answer["access_token"],
            "refresh_token": answer.get("refresh_token") or previous.get("refresh_token", ""),
            "id_token": answer.get("id_token") or previous.get("id_token", ""),
            "expires_at": time.time() + int(answer.get("expires_in", 3600)),
            "scopes": _scopes(answer, previous.get("scopes"))}


class Session:
    """The signed-in account's access token, refreshed when it is about to run out. One refresh at a time,
    across every Rafiq process too (`rafiq run`, `rafiq ask` and `rafiq mcp` share the file): a refresh token
    is used once, and a second use would end the sign-in."""

    _lock = threading.Lock()

    def __init__(self, http=None):
        import httpx

        self.http = http or httpx

    def token(self, force: bool = False) -> str:
        from . import desktop

        with self._lock, open(folder() / ".refresh.lock", "a") as lock:
            desktop.lock_exclusive(lock)          # released when the file closes
            found = account()
            if not found.get("refresh_token"):
                raise SignedOut("not signed in to ChatGPT: run `rafiq chatgpt-login`")
            if PLAN not in found.get("scopes", []):
                raise SignedOut("ChatGPT plan usage wasn't allowed: run `rafiq chatgpt-login` again")
            if not force and found.get("access_token") and found.get("expires_at", 0) > time.time() + REFRESH_EARLY:
                return found["access_token"]
            sent = found["refresh_token"]
            try:
                code, body = _post(self.http, {"grant_type": "refresh_token", "client_id": found.get("client_id", ""),
                                               "refresh_token": sent, "resource": RESOURCE})
            except Exception as exc:   # the network; the next request tries again
                raise ConnectionError(f"ChatGPT's sign-in server is not reachable ({type(exc).__name__})") from exc
            if code != 200 or not body.get("access_token"):
                reason = _error_code(body)
                if reason in SIGNED_OUT:
                    now = account()
                    if now.get("refresh_token") and now["refresh_token"] != sent and now.get("access_token"):
                        return now["access_token"]   # another process rotated it first: its tokens are good
                    forget()
                    raise SignedOut("the ChatGPT sign-in has ended: run `rafiq chatgpt-login` again")
                if reason == "invalid_client":   # OpenAI no longer knows this registration
                    raise SignedOut("ChatGPT doesn't accept Rafiq's registration: run `rafiq chatgpt-login --new`")
                raise ConnectionError(f"ChatGPT's sign-in server answered {code}")
            record = _record(body, found)
            _save(folder() / "account.json", record)
            return record["access_token"]


def forget() -> None:
    """Delete the saved tokens. The host ID, the issued client ID and whose it is stay: signing in again is quick,
    and must be the same account."""
    (folder() / "account.json").unlink(missing_ok=True)


# ---------------------------------------------------------------------- signing in and out

def login(cfg: dict, open_browser=None, http=None, timeout: float = 300, new: bool = False) -> str:
    """Sign in in the browser; a listener on 127.0.0.1 catches the answer. Returns what to tell the user.

    The browser is sent to a one-time page on the listener, which redirects to OpenAI: the state, nonce and
    challenge never appear on a command line, where any user of the computer could read them (/proc) and sign
    Rafiq in to their own account. Only an answer with this attempt's state ends the wait. With `new`, Rafiq
    registers again, to sign in to another ChatGPT account."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    import httpx

    http = http or httpx
    if open_browser is None:
        import webbrowser

        open_browser = webbrowser.open
    saved, before = host(), account()
    client_id = "" if new else str(saved.get("client_id", ""))
    subject = "" if new else str(saved.get("subject", ""))
    state, nonce, start = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    answer: dict = {}
    page: dict = {}

    class Callback(BaseHTTPRequestHandler):
        timeout = 5                              # a client that connects and says nothing can't hang the sign-in

        def do_GET(self):  # noqa: N802 (http.server's name)
            url = urlparse(self.path)
            if self.headers.get("Host") != page["host"]:
                self.send_error(400)
                return
            if url.path == f"/start/{start}":
                self.send_response(302)
                self.send_header("Location", page["authorize"])
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path != CALLBACK_PATH or not secrets.compare_digest(query.get("state", ""), state):
                self.send_error(404)             # a favicon, a stray page or a forged answer: keep waiting
                return
            answer.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Rafiq: you can close this tab.")

        def log_message(self, *args):         # the query holds the code: never logged
            pass

    port = int(cfg.get("chatgpt", {}).get("port", PORT))
    try:
        server = HTTPServer(("127.0.0.1", port), Callback)
    except OSError as exc:
        return f"ChatGPT sign-in failed: port {port} is in use ({exc.strerror}); set [chatgpt] port"
    page["host"] = f"127.0.0.1:{server.server_address[1]}"
    redirect = f"http://{page['host']}{CALLBACK_PATH}"
    with server:
        hint = str(before.get("email", "")) if client_id and before.get("client_id") == client_id else ""
        consent = bool(before.get("refresh_token")) and PLAN not in before.get("scopes", [])
        page["authorize"] = authorize_url(redirect, challenge, state, nonce, client_id, hint, consent)
        local = f"http://{page['host']}/start/{start}"
        print(f"Opening ChatGPT's sign-in. If no browser opens, visit:\n{local}", flush=True)
        open_browser(local)
        server.timeout = 1
        deadline = time.monotonic() + timeout
        while not answer and time.monotonic() < deadline:
            server.handle_request()
    if not answer:
        return "ChatGPT sign-in failed: no answer from the browser in time"
    try:
        code, issued = callback(answer, state, client_id)
        status_code, body = _post(http, {"grant_type": "authorization_code", "client_id": issued, "code": code,
                                         "code_verifier": verifier, "redirect_uri": redirect, "resource": RESOURCE})
        if status_code != 200 or not body.get("access_token") or not body.get("id_token"):
            raise SignInError(f"ChatGPT refused the code ({_error_code(body) or status_code})")
        claims = check_id_token(body["id_token"], _keys(http), issued, nonce)
        if subject and claims["sub"] != subject:
            # One registration is one account: another account's tokens are never kept under it.
            raise SignInError("that's another ChatGPT account than Rafiq's; to switch, run "
                              "`rafiq chatgpt-login --new`")
    except SignInError as exc:
        return f"ChatGPT sign-in failed: {exc}"
    except Exception as exc:                     # the network
        return f"ChatGPT sign-in failed: {type(exc).__name__}"
    _keep_client(issued, claims["sub"])
    record = _record(body, {"client_id": issued, "subject": claims["sub"], "email": str(claims.get("email", ""))})
    _save(folder() / "account.json", record)
    who = f" as {record['email']}" if record["email"] else ""
    if PLAN not in record["scopes"]:
        return (f"Signed in to ChatGPT{who}, but Rafiq may not use your plan: run `rafiq chatgpt-login` again "
                "and allow it. Plan usage needs ChatGPT Plus or Pro.")
    return (f"Signed in to ChatGPT{who}. Rafiq now uses your ChatGPT plan; set a weekly limit for it at "
            f"{USAGE_URL}")


def logout(http=None) -> str:
    """Revoke the refresh token at OpenAI, then delete it here. When OpenAI can't be reached, the tokens are
    still deleted, and the user is told to remove Rafiq in ChatGPT's settings."""
    import httpx

    http = http or httpx
    found = account()
    if not found.get("refresh_token"):
        forget()
        return "Rafiq wasn't signed in to ChatGPT."
    revoked = False
    for wait in (0, 1, 3):
        time.sleep(wait)
        try:
            resp = http.post(REVOKE, data={"token": found["refresh_token"], "token_type_hint": "refresh_token",
                                           "client_id": found.get("client_id", "")}, timeout=15)
        except Exception:
            continue
        if resp.status_code == 200:
            revoked = True
            break
        if resp.status_code < 500:
            break
    forget()
    if revoked:
        return "Signed out of ChatGPT."
    return ("Signed out here, but ChatGPT didn't confirm it: remove Rafiq under Settings, Security in ChatGPT "
            "to be sure.")
