"""GitHub App authentication for the new pipeline."""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import jwt
import requests

from .errors import GitHubAuthConfigurationError

logger = logging.getLogger(__name__)


def _normalized_private_key(value: str) -> str:
    """Accept both real PEM newlines and the escaped form used by Lambda envs."""
    normalized = (
        str(value or "")
        .replace("\\r", "")
        .replace("\\n", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )
    if not normalized:
        raise GitHubAuthConfigurationError(
            "GITHUB_PRIVATE_KEY is missing or empty",
            stage="github.auth.jwt_signing",
        )
    return normalized


def get_installation_token(
    installation_id: int,
    *,
    app_id: Optional[str] = None,
    private_key: Optional[str] = None,
    timeout_seconds: float = 15,
) -> Optional[str]:
    app_id = str(app_id if app_id is not None else os.environ.get("GITHUB_APP_ID", "")).strip()
    if not app_id:
        raise GitHubAuthConfigurationError(
            "GITHUB_APP_ID is missing or empty",
            stage="github.auth.jwt_signing",
        )
    raw_private_key = (
        private_key
        if private_key is not None
        else os.environ.get("GITHUB_PRIVATE_KEY", "")
    )
    private_key = _normalized_private_key(raw_private_key)
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
    try:
        app_jwt = jwt.encode(payload, private_key, algorithm="RS256")
    except Exception as exc:
        raise GitHubAuthConfigurationError(
            f"GitHub App JWT signing failed ({exc.__class__.__name__})",
            stage="github.auth.jwt_signing",
        ) from exc
    response = requests.post(
        f"https://api.github.com/app/installations/{int(installation_id)}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        },
        timeout=max(0.1, float(timeout_seconds)),
    )
    response.raise_for_status()
    token = response.json().get("token")
    if not token:
        logger.error("GitHub installation token response did not include token")
        return None
    return token
