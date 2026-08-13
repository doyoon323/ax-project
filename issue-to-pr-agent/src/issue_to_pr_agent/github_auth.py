from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import jwt
import requests

from .config import Settings


class GitHubAppAuthError(RuntimeError):
    """Raised when GitHub App credentials or installation access are invalid."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class GitHubAppAuthenticator:
    """Mints and refreshes short-lived GitHub App installation tokens."""

    REQUIRED_PERMISSIONS = {
        "checks": "write",
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
    }

    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
        now: Callable[[], float] = time.time,
        jwt_encoder: Callable[..., str] = jwt.encode,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self._now = now
        self._jwt_encoder = jwt_encoder
        self._token = ""
        self._expires_at = 0.0
        self._permissions: dict[str, str] = {}

    @property
    def expected_login(self) -> str:
        return f"{self.settings.github_app_slug}[bot]"

    def installation_token(self) -> str:
        if self._token and self._now() < self._expires_at - 300:
            return self._token

        installation_id = self.settings.github_app_installation_id
        response = self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            authorization=self._app_jwt(),
            expected={201},
        )
        token = str(response.get("token") or "")
        expires_at = self._parse_expiry(response.get("expires_at"))
        if not token or expires_at <= self._now():
            raise GitHubAppAuthError("GitHub returned an invalid installation token response")
        self._token = token
        self._expires_at = expires_at
        permissions = response.get("permissions")
        self._permissions = (
            {str(name): str(level) for name, level in permissions.items()}
            if isinstance(permissions, dict)
            else {}
        )
        return token

    def validate(self) -> str:
        app = self._request(
            "GET",
            "/app",
            authorization=self._app_jwt(),
            expected={200},
        )
        app_id = app.get("id")
        slug = str(app.get("slug") or "")
        if app_id != self.settings.github_app_id or slug != self.settings.github_app_slug:
            raise GitHubAppAuthError(
                "GitHub App identity mismatch; check GITHUB_APP_ID and GITHUB_APP_SLUG"
            )

        installation_token = self.installation_token()
        self._validate_permissions()

        repository = self._request(
            "GET",
            f"/repos/{self.settings.github_repository}",
            authorization=installation_token,
            expected={200},
        )
        if str(repository.get("full_name") or "") != self.settings.github_repository:
            raise GitHubAppAuthError(
                "GitHub App installation cannot access the configured repository"
            )
        return self.expected_login

    def _validate_permissions(self) -> None:
        missing = [
            f"{name}={required}"
            for name, required in self.REQUIRED_PERMISSIONS.items()
            if self._permissions.get(name) != required
        ]
        if missing:
            raise GitHubAppAuthError(
                "GitHub App installation is missing required permissions: " + ", ".join(missing)
            )

    def _app_jwt(self) -> str:
        private_key_path = self.settings.github_app_private_key_path
        if private_key_path is None:
            raise GitHubAppAuthError("GitHub App private key path is not configured")
        try:
            private_key = Path(private_key_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise GitHubAppAuthError("GitHub App private key could not be read") from exc

        issued_at = int(self._now()) - 60
        payload = {
            "iat": issued_at,
            "exp": issued_at + 9 * 60,
            "iss": str(self.settings.github_app_id),
        }
        try:
            encoded = self._jwt_encoder(payload, private_key, algorithm="RS256")
        except Exception as exc:
            raise GitHubAppAuthError("GitHub App private key could not sign a JWT") from exc
        return str(encoded)

    def _request(
        self,
        method: str,
        path: str,
        *,
        authorization: str,
        expected: set[int],
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {authorization}",
            "X-GitHub-Api-Version": self.settings.github_api_version,
            "User-Agent": "issue-to-pr-agent/0.1",
        }
        try:
            response = self.session.request(
                method,
                f"{self.settings.github_api_url.rstrip('/')}{path}",
                headers=headers,
                timeout=30,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise GitHubAppAuthError(
                f"GitHub App authentication request failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.status_code not in expected:
            request_id = response.headers.get("X-GitHub-Request-Id", "unknown")
            raise GitHubAppAuthError(
                f"GitHub App authentication returned {response.status_code}; "
                f"request id={request_id}",
                status_code=response.status_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubAppAuthError("GitHub App authentication returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubAppAuthError("GitHub App authentication returned an invalid payload")
        return payload

    @staticmethod
    def _parse_expiry(value: object) -> float:
        if not isinstance(value, str):
            return 0.0
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
