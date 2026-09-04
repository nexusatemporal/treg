"""Signed session cookies for the web dashboard (human login).

A session is a tiny HMAC-signed token `<b64(payload)>.<b64(sig)>` carrying the user id + expiry.
Stateless (no DB table): we trust the signature. Agents/CLI keep using `X-Treg-Token`; this is only
for browser sessions after GitHub OAuth. Key = `TREG_SESSION_SECRET` (falls back to the Fernet key).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
import time

from ...config import get_settings

TTL_SECONDS = 7 * 24 * 3600
COOKIE = "treg_session"

# When no signing secret is configured we fall back to a RANDOM per-process key (mirrors
# crypto._EPHEMERAL), NOT a source-visible constant: a static "dev-session-key" would let anyone
# who reads the code forge a session cookie for any user id (incl. a superadmin) — full auth
# bypass. Ephemeral means sessions simply don't survive a restart, the intended loud signal to
# set TREG_SESSION_SECRET / TREG_SECRET_KEY.
_EPHEMERAL_KEY = _secrets.token_bytes(32)


def _key() -> bytes:
    s = get_settings()
    configured = s.session_secret or s.secret_key
    return configured.encode() if configured else _EPHEMERAL_KEY


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make(user_id: int, ttl: int | None = TTL_SECONDS, token_version: int = 0,
         org: str | None = None) -> str:
    # `tv` binds the token to the user's current token_version; bumping that row invalidates every
    # token minted at an older version (see api._revoke path). Callers pass user.token_version.
    #
    # `org` is optional and stateless like the rest of the claim: an identity token that PINS a team.
    # It exists so a copyable "API key" works as a bare bearer where no `X-Treg-Org` header can travel
    # (an MCP server's Authorization header). Omitted → today's org-less token, unchanged. Baking the
    # slug in costs nothing to store and needs no rotation, because the whole token is re-derivable
    # from (uid, tv, org) — the same reason the org-less one can be re-minted on every dashboard load.
    #
    # `ttl=None` omits `exp` entirely: an identity token (the copyable "API key") has no expiry. It
    # is revoked by bumping token_version, never by the clock — a key that silently dies 30 days
    # after signup is the worst possible surprise for an agent nobody is watching. Cookies always
    # pass a ttl (see `read_claims(enforce_exp=True)`), so an exp-less claim can never be a session.
    claims = {"uid": user_id, "tv": token_version}
    if ttl is not None:
        claims["exp"] = int(time.time()) + ttl
    if org:
        claims["org"] = org
    raw = json.dumps(claims, separators=(",", ":")).encode()
    sig = hmac.new(_key(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


def read_claims(cookie: str, *, enforce_exp: bool = True) -> dict | None:
    """Return the token's claims ({uid, exp?, tv, org?}) if validly signed, else None.
    `tv` defaults to 0 for tokens minted before token_version existed, so old tokens stay valid
    against a user whose token_version is still 0 (no forced logout on deploy). `org` is present only
    on a team-pinned identity token (see `make`); absent for a plain one.

    `enforce_exp=True` (the session-cookie path) requires a present, future `exp`: a browser session
    must be time-bounded, and an exp-less identity token pasted as a cookie must not become a
    permanent login. `enforce_exp=False` (the `X-Treg-Token` bearer path) ignores `exp` altogether —
    including on tokens minted with the old 30-day claim, which is what makes every key already
    handed out permanent without anyone re-copying it. Revocation is `tv`, not the clock."""
    if not cookie or "." not in cookie:
        return None
    try:
        p, s = cookie.split(".", 1)
        raw = _unb64(p)
        expected = hmac.new(_key(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(s), expected):
            return None
        data = json.loads(raw)
        if enforce_exp and int(data.get("exp", 0)) < time.time():
            return None
        out = {"uid": int(data["uid"]), "tv": int(data.get("tv", 0))}
        if data.get("exp") is not None:
            out["exp"] = int(data["exp"])
        if data.get("org"):
            out["org"] = str(data["org"])
        return out
    except Exception:  # noqa: BLE001 — any malformed cookie is simply "no session"
        return None


def read(cookie: str) -> int | None:
    """Return just the user id if the cookie is validly signed and unexpired, else None. Does NOT
    check token_version — callers that can load the user (api._user_from_*) use read_claims and
    compare tv against the row; use this only where the DB user isn't available."""
    claims = read_claims(cookie)
    return claims["uid"] if claims else None
