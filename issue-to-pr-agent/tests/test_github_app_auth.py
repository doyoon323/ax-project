from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from issue_to_pr_agent.config import Settings
from issue_to_pr_agent.github_auth import GitHubAppAuthenticator, GitHubAppAuthError
from issue_to_pr_agent.github_client import GitWorkspaceManager


def make_app_settings(tmp_path: Path) -> Settings:
    return Settings(
        gemini_api_key="test-key",
        github_repository="owner/repository",
        workspace_path=tmp_path,
        issue_source="poll",
        github_auth_mode="app",
        github_app_id=4_583_096,
        github_app_installation_id=153_482_646,
        github_app_slug="auto-coding-issues",
        github_app_private_key_path=tmp_path / "github-app.pem",
    )


class FakeResponse:
    headers: dict[str, str] = {}

    def __init__(self, payload: dict[str, Any], status_code: int) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if url.endswith("/app"):
            return FakeResponse({"id": 4_583_096, "slug": "auto-coding-issues"}, 200)
        if url.endswith("/access_tokens"):
            return FakeResponse(
                {
                    "token": "installation-token",
                    "expires_at": "2030-08-14T02:00:00Z",
                    "permissions": {
                        "checks": "write",
                        "contents": "write",
                        "issues": "write",
                        "pull_requests": "write",
                    },
                },
                201,
            )
        if url.endswith("/repos/owner/repository"):
            return FakeResponse({"full_name": "owner/repository"}, 200)
        raise AssertionError(f"unexpected request: {method} {url}")


def test_app_auth_mints_caches_and_validates_installation(tmp_path: Path) -> None:
    (tmp_path / "github-app.pem").write_text("not-a-real-key", encoding="utf-8")
    session = FakeSession()
    authenticator = GitHubAppAuthenticator(
        make_app_settings(tmp_path),
        session=session,  # type: ignore[arg-type]
        now=lambda: 1_786_672_800.0,
        jwt_encoder=lambda *_args, **_kwargs: "signed-app-jwt",
    )

    assert authenticator.validate() == "auto-coding-issues[bot]"
    assert authenticator.installation_token() == "installation-token"
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer signed-app-jwt"
    assert session.calls[2][2]["headers"]["Authorization"] == "Bearer installation-token"


def test_app_auth_rejects_wrong_repository_installation(tmp_path: Path) -> None:
    (tmp_path / "github-app.pem").write_text("not-a-real-key", encoding="utf-8")
    session = FakeSession()

    original_request = session.request

    def wrong_repository(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if url.endswith("/repos/owner/repository"):
            return FakeResponse({"full_name": "owner/other"}, 200)
        return original_request(method, url, **kwargs)

    session.request = wrong_repository  # type: ignore[method-assign]
    authenticator = GitHubAppAuthenticator(
        make_app_settings(tmp_path),
        session=session,  # type: ignore[arg-type]
        now=lambda: 1_786_672_800.0,
        jwt_encoder=lambda *_args, **_kwargs: "signed-app-jwt",
    )

    with pytest.raises(GitHubAppAuthError, match="cannot access"):
        authenticator.validate()


def test_app_auth_rejects_missing_required_permissions(tmp_path: Path) -> None:
    (tmp_path / "github-app.pem").write_text("not-a-real-key", encoding="utf-8")
    session = FakeSession()
    original_request = session.request

    def missing_permission(method: str, url: str, **kwargs: Any) -> FakeResponse:
        response = original_request(method, url, **kwargs)
        if url.endswith("/access_tokens"):
            response.payload["permissions"].pop("checks")
        return response

    session.request = missing_permission  # type: ignore[method-assign]
    authenticator = GitHubAppAuthenticator(
        make_app_settings(tmp_path),
        session=session,  # type: ignore[arg-type]
        now=lambda: 1_786_672_800.0,
        jwt_encoder=lambda *_args, **_kwargs: "signed-app-jwt",
    )

    with pytest.raises(GitHubAppAuthError, match="checks=write"):
        authenticator.validate()


def test_app_mode_requires_complete_credentials_and_no_pat(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="GITHUB_APP_ID"):
        Settings(
            gemini_api_key="test-key",
            github_repository="owner/repository",
            workspace_path=tmp_path,
            issue_source="poll",
            github_auth_mode="app",
        )

    with pytest.raises(ValidationError, match="GITHUB_TOKEN must be empty"):
        Settings(
            gemini_api_key="test-key",
            github_token="legacy-token",
            github_repository="owner/repository",
            workspace_path=tmp_path,
            issue_source="poll",
            github_auth_mode="app",
            github_app_id=4_583_096,
            github_app_installation_id=153_482_646,
            github_app_slug="auto-coding-issues",
            github_app_private_key_path=tmp_path / "github-app.pem",
        )


def test_git_network_commands_use_short_lived_installation_token(tmp_path: Path) -> None:
    class FakeAuthenticator:
        @staticmethod
        def installation_token() -> str:
            return "short-lived-token"

    manager = object.__new__(GitWorkspaceManager)
    manager.settings = make_app_settings(tmp_path)
    manager.app_authenticator = FakeAuthenticator()  # type: ignore[assignment]

    read_environment = manager._git_environment(["status", "--short"])
    push_environment = manager._git_environment(["push", "origin", "main"])

    assert read_environment is None
    assert push_environment is not None
    assert push_environment["GIT_TERMINAL_PROMPT"] == "0"
    assert push_environment["GITHUB_TOKEN"] == "short-lived-token"
    assert "short-lived-token" not in push_environment["GIT_ASKPASS"]
