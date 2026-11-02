"""Sign in with ChatGPT (#196): the sign-in, the ID token check and the tokens, against a fake OpenAI."""

import base64
import hashlib
import http.client
import json
import random
import socket
import stat
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from rafiq import chatgpt

# ---------------------------------------------------------------------- a fake OpenAI signing key

def _prime(bits: int, rng: random.Random) -> int:
    while True:
        n = rng.getrandbits(bits) | (1 << bits - 1) | 1
        if all(n % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31)) and _probably_prime(n, rng):
            return n


def _probably_prime(n: int, rng: random.Random) -> bool:
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(24):
        x = pow(rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _int64(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class Key:
    """A 1024-bit RSA key made for the test, published like OpenAI's JWKS."""

    def __init__(self, kid="k1", seed=1):
        rng = random.Random(seed)
        self.e = 65537
        while True:
            p, q = _prime(512, rng), _prime(512, rng)
            phi = (p - 1) * (q - 1)
            if p != q and phi % self.e:
                break
        self.n, self.d, self.kid = p * q, pow(self.e, -1, phi), kid

    def jwk(self) -> dict:
        return {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": self.kid, "n": _int64(self.n), "e": _int64(self.e)}

    def sign(self, claims: dict, header: dict | None = None) -> str:
        head = _b64(json.dumps(header or {"alg": "RS256", "kid": self.kid, "typ": "JWT"}).encode())
        body = _b64(json.dumps(claims).encode())
        size = (self.n.bit_length() + 7) // 8
        digest = chatgpt.SHA256_INFO + hashlib.sha256(f"{head}.{body}".encode()).digest()
        block = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
        signature = pow(int.from_bytes(block, "big"), self.d, self.n).to_bytes(size, "big")
        return f"{head}.{body}.{_b64(signature)}"


@pytest.fixture(scope="module")
def key():
    return Key()


def claims(**changes):
    found = {"iss": "https://auth.openai.com", "aud": "oaiapp_123", "sub": "user-1", "email": "sam@example.com",
             "exp": time.time() + 3600, "nonce": "n0"}
    found.update(changes)
    return found


# ---------------------------------------------------------------------- the host and the authorize URL

def test_the_host_id_is_made_once_kept_and_readable_only_by_the_user():
    first = chatgpt.host()["host_id"]
    assert first.startswith("urn:uuid:")
    assert chatgpt.host()["host_id"] == first
    path = chatgpt.folder() / "host.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(chatgpt.folder().stat().st_mode) == 0o700


def test_the_first_sign_in_registers_rafiq_with_the_dynamic_client():
    url = chatgpt.authorize_url("http://127.0.0.1:1455/auth/callback", "chal", "st", "no")
    parts = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://auth.openai.com/api/accounts/authorize"
    assert query == {
        "client_id": "dynamic_agent_client", "agent_name_hint": "Rafiq", "ext_agent_host_id": chatgpt.host()["host_id"],
        "response_type": "code", "redirect_uri": "http://127.0.0.1:1455/auth/callback",
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
        "resource": "https://api.openai.com/v1", "state": "st", "nonce": "no",
        "code_challenge_method": "S256", "code_challenge": "chal"}


def test_signing_in_again_uses_the_issued_client_and_no_name_hint():
    url = chatgpt.authorize_url("http://127.0.0.1:1455/auth/callback", "c", "s", "n", client_id="oaiapp_123",
                                login_hint="sam@example.com")
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert query["client_id"] == "oaiapp_123"
    assert "agent_name_hint" not in query
    assert query["login_hint"] == "sam@example.com"
    assert "id_token_hint" not in query and "prompt" not in query


def test_plan_usage_declined_before_asks_for_consent_again():
    url = chatgpt.authorize_url("http://127.0.0.1:1/auth/callback", "c", "s", "n", client_id="oaiapp_1", consent=True)
    assert parse_qs(urlparse(url).query)["prompt"] == ["consent"]


def test_the_pkce_challenge_is_the_verifiers_sha256_without_padding():
    verifier, challenge = chatgpt.pkce_pair()
    assert challenge == _b64(hashlib.sha256(verifier.encode()).digest())
    assert "=" not in challenge


# ---------------------------------------------------------------------- the callback

def test_the_callback_gives_the_code_and_the_issued_client():
    assert chatgpt.callback({"state": "s", "code": "c1", "client_id": "oaiapp_9"}, "s") == ("c1", "oaiapp_9")


@pytest.mark.parametrize("answer, saved, why", [
    ({"state": "other", "code": "c", "client_id": "oaiapp_9"}, "", "state"),
    ({"state": "s", "error": "access_denied"}, "", "said no"),
    ({"state": "s", "code": "c"}, "", "registering"),
    ({"state": "s", "code": "c", "client_id": "dynamic_agent_client"}, "", "registering"),
    ({"state": "s", "code": "c", "client_id": "oaiapp_other"}, "oaiapp_9", "another app"),
    ({"state": "s", "client_id": "oaiapp_9"}, "", "no code"),
])
def test_a_callback_that_doesnt_hold_is_refused(answer, saved, why):
    with pytest.raises(chatgpt.SignInError, match=why):
        chatgpt.callback(answer, "s", saved)


def test_a_returning_callback_may_leave_out_the_client():
    assert chatgpt.callback({"state": "s", "code": "c"}, "s", "oaiapp_9") == ("c", "oaiapp_9")


# ---------------------------------------------------------------------- the ID token

def test_an_id_token_signed_by_openai_is_accepted(key):
    found = chatgpt.check_id_token(key.sign(claims()), [key.jwk()], "oaiapp_123", "n0")
    assert found["sub"] == "user-1"


def test_an_audience_list_holding_the_client_is_accepted(key):
    token = key.sign(claims(aud=["https://api.openai.com/v1", "oaiapp_123"]))
    assert chatgpt.check_id_token(token, [key.jwk()], "oaiapp_123", "n0")["sub"] == "user-1"


@pytest.mark.parametrize("change, why", [
    ({"iss": "https://auth.evil.example"}, "issuer"),
    ({"aud": "oaiapp_other"}, "another app"),
    ({"exp": 1}, "expired"),
    ({"nonce": "replayed"}, "nonce"),
    ({"sub": ""}, "no account"),
])
def test_an_id_token_with_a_wrong_claim_is_refused(key, change, why):
    with pytest.raises(chatgpt.SignInError, match=why):
        chatgpt.check_id_token(key.sign(claims(**change)), [key.jwk()], "oaiapp_123", "n0")


def test_an_id_token_signed_by_another_key_is_refused(key):
    other = Key(kid="k1", seed=2)
    with pytest.raises(chatgpt.SignInError, match="signed by OpenAI"):
        chatgpt.check_id_token(other.sign(claims()), [key.jwk()], "oaiapp_123", "n0")


def test_an_id_token_whose_claims_were_changed_after_signing_is_refused(key):
    head, _body, sig = key.sign(claims()).split(".")
    forged = _b64(json.dumps(claims(sub="someone-else")).encode())
    with pytest.raises(chatgpt.SignInError, match="signed by OpenAI"):
        chatgpt.check_id_token(f"{head}.{forged}.{sig}", [key.jwk()], "oaiapp_123", "n0")


@pytest.mark.parametrize("header", [{"alg": "none", "kid": "k1"}, {"alg": "HS256", "kid": "k1"}])
def test_an_id_token_with_another_algorithm_is_refused(key, header):
    with pytest.raises(chatgpt.SignInError, match="RS256"):
        chatgpt.check_id_token(key.sign(claims(), header), [key.jwk()], "oaiapp_123", "n0")


def test_an_id_token_from_an_unpublished_key_is_refused(key):
    with pytest.raises(chatgpt.SignInError, match="signed by OpenAI"):
        chatgpt.check_id_token(key.sign(claims(), {"alg": "RS256", "kid": "k9"}), [key.jwk()], "oaiapp_123", "n0")


@pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "!!.@@.##"])
def test_a_malformed_id_token_is_refused(key, token):
    with pytest.raises(chatgpt.SignInError):
        chatgpt.check_id_token(token, [key.jwk()], "oaiapp_123", "n0")


# ---------------------------------------------------------------------- a fake sign-in server

class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self.body = status, body if body is not None else {}

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeOpenAI:
    """The token, revoke and keys endpoints. `token_answers` are given out in turn."""

    def __init__(self, key, scope=chatgpt.SCOPE):
        self.key, self.scope, self.posts, self.token_answers, self.nonce = key, scope, [], [], ""
        self.sub = "user-1"

    def get(self, url, timeout=None):
        assert url == chatgpt.KEYS
        return Resp(200, {"keys": [self.key.jwk()]})

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, dict(data)))
        if url == chatgpt.REVOKE:
            return self.token_answers.pop(0) if self.token_answers else Resp(200, {})
        if self.token_answers:
            return self.token_answers.pop(0)
        return Resp(200, {"access_token": "at-1", "refresh_token": "rt-1", "token_type": "Bearer",
                          "expires_in": 3600, "scope": self.scope,
                          "id_token": self.key.sign(claims(aud=data["client_id"], nonce=self.nonce, sub=self.sub))})


def fetch(url, host=None, query=None):
    """One GET to the loopback listener, without following a redirect: (status, Location)."""
    parts = urlparse(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    path = parts.path + (f"?{urlencode(query)}" if query else "")
    conn.request("GET", path, headers={"Host": host} if host else {})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp.status, resp.getheader("Location")


def browser(fake, answer=None, before=()):
    """A browser whose user signs in: it opens Rafiq's start page, follows it to OpenAI (here: reads where it
    points), then OpenAI sends it back to the callback. `before` are requests some other page or program makes
    to the listener first: (path, query, Host header)."""
    opened = []

    def open_browser(local):
        def visit():
            status, location = fetch(local)
            assert status == 302
            query = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
            opened.append({"local": local, "location": location, **query})
            fake.nonce = query["nonce"]
            base = query["redirect_uri"]
            for path, extra, host in before:
                fetch(base.replace("/auth/callback", path), host, extra)
            reply = answer if answer is not None else {"code": "code-1", "state": query["state"],
                                                       "client_id": "oaiapp_123"}
            fetch(base, None, reply)

        threading.Thread(target=visit, daemon=True).start()
        return True

    return open_browser, opened


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)


def sign_in(fake, answer=None, before=(), timeout=10, new=False):
    open_browser, opened = browser(fake, answer, before)
    said = chatgpt.login({"chatgpt": {"port": 0}}, open_browser=open_browser, http=fake, timeout=timeout, new=new)
    return said, opened


# ---------------------------------------------------------------------- signing in

def test_signing_in_saves_the_account_readable_only_by_the_user(key, quiet):
    fake = FakeOpenAI(key)
    said, opened = sign_in(fake)
    assert said.startswith("Signed in to ChatGPT as sam@example.com")
    assert "chatgpt.com/settings/usage" in said
    assert "at-1" not in said and "rt-1" not in said
    saved = chatgpt.account()
    assert saved["client_id"] == "oaiapp_123" and saved["subject"] == "user-1"
    assert saved["access_token"] == "at-1" and saved["refresh_token"] == "rt-1"
    assert chatgpt.PLAN in saved["scopes"]
    assert stat.S_IMODE((chatgpt.folder() / "account.json").stat().st_mode) == 0o600
    assert (chatgpt.host()["client_id"], chatgpt.host()["subject"]) == ("oaiapp_123", "user-1")
    assert chatgpt.signed_in()
    url, form = fake.posts[0]
    assert url == chatgpt.TOKEN
    assert form["grant_type"] == "authorization_code" and form["client_id"] == "oaiapp_123"
    assert form["redirect_uri"] == opened[0]["redirect_uri"] and form["resource"] == "https://api.openai.com/v1"
    assert hashlib.sha256(form["code_verifier"].encode()).digest() == base64.urlsafe_b64decode(
        opened[0]["code_challenge"] + "=")


def test_the_browser_is_given_only_a_local_page_so_no_secret_is_on_a_command_line(key, monkeypatch):
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    _said, opened = sign_in(FakeOpenAI(key))
    local = opened[0]["local"]
    assert local.startswith("http://127.0.0.1:") and "/start/" in local
    for secret in (opened[0]["state"], opened[0]["nonce"], opened[0]["code_challenge"]):
        assert secret not in local and not any(secret in line for line in printed)
    assert opened[0]["location"].startswith("https://auth.openai.com/api/accounts/authorize?")


def test_a_wrong_start_page_doesnt_lead_to_the_sign_in(key, quiet):
    said, _ = sign_in(FakeOpenAI(key), before=[("/start/guessed", {}, None)])
    assert said.startswith("Signed in to ChatGPT")      # and the guess was answered 404, not with the URL


def test_requests_without_this_sign_ins_state_dont_end_it(key, quiet):
    stray = [("/favicon.ico", {}, None), ("/auth/callback", {"x": "1"}, None),
             ("/auth/callback", {"code": "attacker", "state": "forged", "client_id": "oaiapp_evil"}, None)]
    said, _ = sign_in(FakeOpenAI(key), before=stray)
    assert said.startswith("Signed in to ChatGPT as sam@example.com")
    assert chatgpt.account()["client_id"] == "oaiapp_123"


def test_a_request_for_another_host_name_is_refused(key, quiet):
    # A page on a name that points at 127.0.0.1 (DNS rebinding) is not the browser coming back from OpenAI.
    fake = FakeOpenAI(key)
    open_browser, opened = browser(fake)
    seen = []

    def rebinding(local):
        port = urlparse(local).port
        def first_the_rebinding_page_then_the_browser():
            seen.append(fetch(local, host=f"evil.example:{port}"))
            open_browser(local)

        threading.Thread(target=first_the_rebinding_page_then_the_browser, daemon=True).start()
        return True

    said = chatgpt.login({"chatgpt": {"port": 0}}, open_browser=rebinding, http=fake, timeout=10)
    assert said.startswith("Signed in to ChatGPT")
    assert seen == [(400, None)]


def test_a_connection_that_sends_nothing_doesnt_hang_the_sign_in(quiet):
    sockets = []

    def silent(local):
        parts = urlparse(local)
        sockets.append(socket.create_connection((parts.hostname, parts.port)))
        return True

    begun = time.monotonic()
    said = chatgpt.login({"chatgpt": {"port": 0}}, open_browser=silent, http=None, timeout=1)
    assert "no answer" in said
    assert time.monotonic() - begun < 9
    sockets[0].close()


def test_signing_in_again_uses_the_issued_client_and_the_email_as_hint(key, quiet):
    fake = FakeOpenAI(key)
    sign_in(fake)
    _said, opened = sign_in(fake)
    assert opened[0]["client_id"] == "oaiapp_123"
    assert "agent_name_hint" not in opened[0]
    assert opened[0]["login_hint"] == "sam@example.com"
    assert "id_token_hint" not in opened[0]
    assert opened[0]["ext_agent_host_id"] == chatgpt.host()["host_id"]


def test_signing_in_again_as_another_account_is_refused(key, quiet):
    fake = FakeOpenAI(key)
    sign_in(fake)
    chatgpt.logout(fake)
    fake.sub = "user-2"
    said, _ = sign_in(fake)
    assert "another ChatGPT account" in said and "--new" in said
    assert chatgpt.account() == {}
    assert chatgpt.host()["subject"] == "user-1"


def test_signing_in_with_new_registers_again_for_another_account(key, quiet):
    fake = FakeOpenAI(key)
    sign_in(fake)
    fake.sub = "user-2"
    said, opened = sign_in(fake, new=True, answer=None)
    assert opened[0]["client_id"] == "dynamic_agent_client" and opened[0]["agent_name_hint"] == "Rafiq"
    assert said.startswith("Signed in to ChatGPT")
    assert chatgpt.account()["subject"] == "user-2" and chatgpt.host()["subject"] == "user-2"


def test_signing_in_without_allowing_the_plan_says_so_and_asks_again_next_time(key, quiet):
    fake = FakeOpenAI(key, scope="openid profile email offline_access")
    said, _ = sign_in(fake)
    assert "may not use your plan" in said and "Plus or Pro" in said
    assert not chatgpt.signed_in()
    fake.scope = chatgpt.SCOPE
    _said, opened = sign_in(fake)
    assert opened[0]["prompt"] == "consent"
    assert chatgpt.signed_in()


def test_only_a_forged_answer_leaves_the_sign_in_unfinished_and_saves_nothing(key, quiet):
    said, _ = sign_in(FakeOpenAI(key), answer={"code": "c", "state": "forged", "client_id": "oaiapp_123"}, timeout=1)
    assert "no answer" in said
    assert chatgpt.account() == {}


def test_an_id_token_for_another_sign_in_is_refused_and_nothing_is_saved(key, quiet):
    fake = FakeOpenAI(key)
    fake.token_answers.append(Resp(200, {"access_token": "at", "refresh_token": "rt", "scope": chatgpt.SCOPE,
                                         "id_token": key.sign(claims(nonce="someone-elses"))}))
    said, _ = sign_in(fake)
    assert "nonce" in said
    assert chatgpt.account() == {}
    assert "client_id" not in chatgpt.host()


def test_a_refused_code_is_said_without_the_answer(key, quiet):
    fake = FakeOpenAI(key)
    fake.token_answers.append(Resp(400, {"error": "invalid_grant", "error_description": "secret-ish detail"}))
    said, _ = sign_in(fake)
    assert said == "ChatGPT sign-in failed: ChatGPT refused the code (invalid_grant)"


def test_no_answer_from_the_browser_in_time_gives_up(quiet):
    said = chatgpt.login({"chatgpt": {"port": 0}}, open_browser=lambda url: True, http=None, timeout=0.5)
    assert "no answer" in said


# ---------------------------------------------------------------------- tokens

def save_account(**changes):
    record = {"client_id": "oaiapp_123", "subject": "user-1", "email": "sam@example.com", "access_token": "at-old",
              "refresh_token": "rt-old", "id_token": "id", "expires_at": time.time() + 3600,
              "scopes": chatgpt.SCOPE.split()}
    record.update(changes)
    chatgpt._save(chatgpt.folder() / "account.json", record)


def test_a_fresh_access_token_is_used_as_it_is(key):
    save_account()
    fake = FakeOpenAI(key)
    assert chatgpt.Session(fake).token() == "at-old"
    assert fake.posts == []


def test_an_access_token_about_to_run_out_is_refreshed_and_the_refresh_token_rotates(key):
    save_account(expires_at=time.time() + 30)
    fake = FakeOpenAI(key)
    fake.token_answers.append(Resp(200, {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600,
                                         "scope": chatgpt.SCOPE}))
    assert chatgpt.Session(fake).token() == "at-new"
    url, form = fake.posts[0]
    assert url == chatgpt.TOKEN
    assert form == {"grant_type": "refresh_token", "client_id": "oaiapp_123", "refresh_token": "rt-old",
                    "resource": "https://api.openai.com/v1"}
    saved = chatgpt.account()
    assert (saved["access_token"], saved["refresh_token"], saved["id_token"]) == ("at-new", "rt-new", "id")
    assert saved["expires_at"] > time.time() + 3000


@pytest.mark.parametrize("code", sorted(chatgpt.SIGNED_OUT))
def test_a_refresh_refused_for_good_signs_out(key, code):
    save_account(expires_at=0)
    fake = FakeOpenAI(key)
    fake.token_answers.append(Resp(400, {"error": code}))
    with pytest.raises(chatgpt.SignedOut, match="chatgpt-login"):
        chatgpt.Session(fake).token()
    assert chatgpt.account() == {}
    assert not chatgpt.signed_in()


def test_a_refresh_token_another_rafiq_already_rotated_doesnt_sign_out(key):
    # `rafiq ask` refreshed while `rafiq run` read the old refresh token: OpenAI says it was reused.
    save_account(expires_at=0)
    fake = FakeOpenAI(key)

    class Rotated(Resp):
        def json(self):
            save_account(access_token="at-other", refresh_token="rt-other")
            return {"error": "refresh_token_reused"}

    fake.token_answers.append(Rotated(400))
    assert chatgpt.Session(fake).token() == "at-other"
    assert chatgpt.account()["refresh_token"] == "rt-other"


def test_refreshes_are_one_at_a_time_across_processes(key, monkeypatch):
    import fcntl

    held = []
    real = fcntl.flock
    monkeypatch.setattr(fcntl, "flock", lambda f, how: held.append((f.name, how)) or real(f, how))
    save_account(expires_at=0)
    fake = FakeOpenAI(key)
    fake.token_answers.append(Resp(200, {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}))
    chatgpt.Session(fake).token()
    assert held == [(str(chatgpt.folder() / ".refresh.lock"), fcntl.LOCK_EX)]


def test_a_sign_in_server_that_is_down_keeps_the_tokens(key):
    save_account(expires_at=0)
    fake = FakeOpenAI(key)
    fake.token_answers.append(Resp(503, ValueError("not json")))
    with pytest.raises(ConnectionError):
        chatgpt.Session(fake).token()
    assert chatgpt.account()["refresh_token"] == "rt-old"


def test_without_a_sign_in_the_session_says_to_sign_in(key):
    with pytest.raises(chatgpt.SignedOut, match="chatgpt-login"):
        chatgpt.Session(FakeOpenAI(key)).token()


def test_the_status_never_holds_a_token():
    save_account()
    found = chatgpt.status()
    assert found == {"signed_in": True, "plan": True, "email": "sam@example.com"}


# ---------------------------------------------------------------------- signing out

def test_signing_out_revokes_the_refresh_token_and_keeps_the_host(key):
    save_account()
    chatgpt._keep_client("oaiapp_123", "user-1")
    host = chatgpt.host()
    fake = FakeOpenAI(key)
    assert chatgpt.logout(fake) == "Signed out of ChatGPT."
    assert fake.posts == [(chatgpt.REVOKE, {"token": "rt-old", "token_type_hint": "refresh_token",
                                             "client_id": "oaiapp_123"})]
    assert chatgpt.account() == {}
    assert chatgpt.host() == host


def test_signing_out_when_chatgpt_cant_be_reached_still_deletes_the_tokens(key, monkeypatch):
    monkeypatch.setattr(chatgpt.time, "sleep", lambda s: None)
    save_account()
    fake = FakeOpenAI(key)
    fake.token_answers += [Resp(503), Resp(503), Resp(503)]
    said = chatgpt.logout(fake)
    assert "didn't confirm" in said
    assert len(fake.posts) == 3
    assert chatgpt.account() == {}
