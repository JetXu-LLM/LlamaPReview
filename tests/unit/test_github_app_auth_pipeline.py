import unittest
from unittest.mock import Mock, patch

from tests.unit.fakes import (
    ensure_repo_root_on_path,
    install_fake_jwt_module,
    install_fake_requests_module,
    set_default_env,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_jwt_module()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline import github_app_auth
from lambdas.LlamaPReviewPipeline.errors import GitHubAuthConfigurationError, classify_failure


class TestGitHubAppAuthPipeline(unittest.TestCase):
    def test_installation_token_request_uses_caller_deadline_timeout(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"token": "installation-token"}
        with patch.object(github_app_auth.jwt, "encode", return_value="app-jwt"), patch.object(
            github_app_auth.requests,
            "post",
            return_value=response,
        ) as post:
            token = github_app_auth.get_installation_token(
                123,
                app_id="app",
                private_key="key",
                timeout_seconds=4.5,
            )
        self.assertEqual(token, "installation-token")
        self.assertEqual(post.call_args.kwargs["timeout"], 4.5)

    def test_escaped_lambda_env_newlines_are_normalized_before_signing(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"token": "installation-token"}
        escaped_key = "line-one\\nline-two"
        with patch.object(github_app_auth.jwt, "encode", return_value="app-jwt") as encode, patch.object(
            github_app_auth.requests,
            "post",
            return_value=response,
        ):
            github_app_auth.get_installation_token(
                123,
                app_id="app",
                private_key=escaped_key,
            )

        self.assertEqual(
            encode.call_args.args[1],
            "line-one\nline-two",
        )

    def test_invalid_signing_key_is_a_terminal_configuration_failure(self):
        with patch.object(github_app_auth.jwt, "encode", side_effect=ValueError("bad key")):
            with self.assertRaises(GitHubAuthConfigurationError) as raised:
                github_app_auth.get_installation_token(
                    123,
                    app_id="app",
                    private_key="not-a-key",
                )

        classified = classify_failure(raised.exception, stage="context")
        self.assertEqual(classified.kind, "github_auth_configuration_error")
        self.assertEqual(classified.stage, "github.auth.jwt_signing")
        self.assertFalse(classified.retryable)


if __name__ == "__main__":
    unittest.main()
