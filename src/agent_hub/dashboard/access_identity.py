"""Verified Cloudflare Access identity for dashboard requests."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt


class AccessIdentityError(ValueError):
    """Raised when a Cloudflare Access assertion cannot be trusted."""


@dataclass(frozen=True)
class OperatorIdentity:
    """Identity Cloudflare Access verified for one dashboard operator."""

    email: str
    subject: str


class AccessIdentityVerifier:
    """Verify Cloudflare Access JWTs against asynchronously cached signing keys."""

    def __init__(
        self,
        team_domain: str,
        audience: str,
        *,
        cache_seconds: int = 3600,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Configure a verifier for one Cloudflare Access application.

        Args:
            team_domain: Cloudflare Access team domain, without a URL path.
            audience: Application audience (AUD) tag.
            cache_seconds: How long to cache Cloudflare signing keys.
            http_client: Optional injected asynchronous client for tests.
        """
        clean_domain = team_domain.removeprefix("https://").rstrip("/")
        if not clean_domain or not audience:
            raise ValueError("Cloudflare Access team domain and audience are required")
        self.team_domain = clean_domain
        self.audience = audience
        self.issuer = f"https://{clean_domain}"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.cache_seconds = max(1, cache_seconds)
        self._http_client = http_client
        self._keys: dict[str, Any] = {}
        self._keys_expire_at = 0.0
        self._keys_lock = asyncio.Lock()

    async def verify(self, assertion: str) -> OperatorIdentity:
        """Validate an Access assertion and return its verified operator.

        Args:
            assertion: JWT from the ``Cf-Access-Jwt-Assertion`` header.

        Returns:
            Verified operator identity.

        Raises:
            AccessIdentityError: If the token is absent, malformed, expired,
                incorrectly signed, or intended for another Access app.
        """
        if not assertion:
            raise AccessIdentityError("Cloudflare Access assertion is missing")
        try:
            header = jwt.get_unverified_header(assertion)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise AccessIdentityError("Cloudflare Access assertion header is invalid")
            kid = str(header["kid"])
            key = await self._key_for(kid)
            claims = jwt.decode(
                assertion,
                key=key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "email"]},
            )
        except AccessIdentityError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise AccessIdentityError("Cloudflare Access assertion is invalid") from exc

        email = claims.get("email")
        subject = claims.get("sub")
        if not isinstance(email, str) or not email or not isinstance(subject, str) or not subject:
            raise AccessIdentityError("Cloudflare Access identity is incomplete")
        return OperatorIdentity(email=email, subject=subject)

    async def _key_for(self, kid: str) -> Any:
        keys = await self._load_keys(force=False)
        key = keys.get(kid)
        if key is None:
            keys = await self._load_keys(force=True)
            key = keys.get(kid)
        if key is None:
            raise AccessIdentityError("Cloudflare Access signing key is unknown")
        return key

    async def _load_keys(self, *, force: bool) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._keys and now < self._keys_expire_at:
            return self._keys
        async with self._keys_lock:
            now = time.monotonic()
            if not force and self._keys and now < self._keys_expire_at:
                return self._keys
            payload = await self._fetch_certs()
            raw_keys = payload.get("keys")
            if not isinstance(raw_keys, list):
                raise AccessIdentityError("Cloudflare Access signing keys are invalid")
            keys: dict[str, Any] = {}
            try:
                for raw_key in raw_keys:
                    if not isinstance(raw_key, dict) or not isinstance(raw_key.get("kid"), str):
                        continue
                    keys[str(raw_key["kid"])] = jwt.PyJWK.from_dict(raw_key).key
            except (jwt.PyJWTError, ValueError, TypeError) as exc:
                raise AccessIdentityError("Cloudflare Access signing keys are invalid") from exc
            if not keys:
                raise AccessIdentityError("Cloudflare Access returned no signing keys")
            self._keys = keys
            self._keys_expire_at = now + self.cache_seconds
            return self._keys

    async def _fetch_certs(self) -> dict[str, Any]:
        try:
            if self._http_client is not None:
                response = await self._http_client.get(self.certs_url)
            else:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(self.certs_url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AccessIdentityError("Could not load Cloudflare Access signing keys") from exc
        if not isinstance(payload, dict):
            raise AccessIdentityError("Cloudflare Access signing keys are invalid")
        return payload
