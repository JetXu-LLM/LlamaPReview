"""Hosted Webhook admission and private-event privacy boundary."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from tests.unit.fakes import (
    FakeDynamoResource,
    build_webhook_event,
    ensure_repo_root_on_path,
    install_fake_aws_modules,
    set_default_env,
)


ensure_repo_root_on_path()
set_default_env()
fake_dynamo = FakeDynamoResource()
install_fake_aws_modules(fake_dynamo)

from lambdas.LlamaPReviewWebhookHandler import lambda_function as webhook


HEAD = "a" * 40


def public_payload(*, action: str = "opened", draft: bool = False) -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": 42,
            "state": "open",
            "draft": draft,
            "title": "Public change",
            "head": {"sha": HEAD, "ref": "feature"},
            "base": {"ref": "main"},
        },
        "repository": {
            "full_name": "owner/repo",
            "private": False,
            "default_branch": "main",
        },
        "installation": {"id": 123},
    }


def private_payload(*, action: str = "opened") -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": 987654,
            "state": "open",
            "draft": False,
            "title": "identity-must-not-escape",
            "head": {"sha": "b" * 40, "ref": "private-branch"},
            "base": {"ref": "main"},
        },
        "repository": {
            "full_name": "private-owner/private-repo",
            "private": True,
            "default_branch": "main",
        },
        "installation": {"id": 999999},
        "sender": {"login": "private-account"},
    }


class WebhookAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        original_dynamodb = webhook.dynamodb
        original_table = webhook.table
        webhook.dynamodb = fake_dynamo
        webhook.table = fake_dynamo.Table("llamapreview-pipeline-test")
        self.addCleanup(setattr, webhook, "table", original_table)
        self.addCleanup(setattr, webhook, "dynamodb", original_dynamodb)
        webhook.table.reset()

    def signed_event(self, payload: dict, *, event_name: str = "pull_request"):
        return build_webhook_event(
            payload,
            secret=os.environ["GITHUB_WEBHOOK_SECRET"],
            event_name=event_name,
        )

    def test_signature_accepts_exact_body_and_rejects_tampering(self):
        event = self.signed_event({"hello": "world"}, event_name="ping")
        self.assertTrue(webhook.verify_signature(event))
        event["body"] = '{"hello":"changed"}'
        self.assertFalse(webhook.verify_signature(event))

    def test_signature_fails_closed_when_secret_is_empty(self):
        event = build_webhook_event({}, secret="", event_name="ping")
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": ""}):
            self.assertFalse(webhook.verify_signature(event))

    def test_supported_public_event_writes_one_minimum_pending_item(self):
        response = webhook.lambda_handler(self.signed_event(public_payload()), None)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(len(webhook.table.put_calls), 1)
        item = webhook.table.put_calls[0]
        self.assertEqual(item["repo"], "owner/repo")
        self.assertEqual(item["pr_number"], 42)
        self.assertEqual(item["head_sha"], HEAD)
        self.assertEqual(item["installation_id"], 123)
        self.assertEqual(item["status"], "PENDING")
        self.assertNotIn("webhook_payload", item)
        self.assertNotIn("is_private", item)
        self.assertNotIn("dry_run", item)

    def test_ready_for_review_public_event_is_supported(self):
        response = webhook.lambda_handler(
            self.signed_event(public_payload(action="ready_for_review")), None
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(len(webhook.table.put_calls), 1)

    def test_draft_and_unsupported_public_actions_do_not_write(self):
        cases = [
            public_payload(draft=True),
            public_payload(action="synchronize"),
            public_payload(action="closed"),
        ]
        for payload in cases:
            with self.subTest(action=payload["action"], draft=payload["pull_request"]["draft"]):
                webhook.table.reset()
                response = webhook.lambda_handler(self.signed_event(payload), None)
                self.assertEqual(response["statusCode"], 200)
                self.assertEqual(webhook.table.put_calls, [])

    def test_duplicate_public_delivery_does_not_overwrite_existing_item(self):
        event = self.signed_event(public_payload())
        first = webhook.lambda_handler(event, None)
        original = dict(webhook.table.put_calls[0])
        second = webhook.lambda_handler(event, None)

        self.assertEqual(first["statusCode"], 200)
        self.assertEqual(second["statusCode"], 200)
        self.assertEqual(len(webhook.table.put_calls), 1)
        key = tuple(sorted({"repo": "owner/repo", "pr_number": 42}.items()))
        self.assertEqual(webhook.table.items[key], original)

    def test_private_pull_request_actions_have_zero_product_side_effects(self):
        for action in (
            "opened",
            "ready_for_review",
            "synchronize",
            "reopened",
            "closed",
        ):
            with self.subTest(action=action):
                webhook.table.reset()
                payload = private_payload(action=action)
                event = self.signed_event(payload)
                with patch.object(
                    webhook, "process_public_pull_request"
                ) as public_path, self.assertNoLogs(level="INFO"):
                    response = webhook.lambda_handler(event, None)

                self.assertEqual(response["statusCode"], 200)
                self.assertEqual(
                    json.loads(response["body"]),
                    {"message": "Webhook accepted"},
                )
                public_path.assert_not_called()
                self.assertEqual(webhook.table.put_calls, [])
                self.assertEqual(webhook.table.update_calls, [])
                self.assertEqual(webhook.table.get_calls, [])

    def test_private_event_with_malformed_optional_fields_still_discards_early(self):
        payload = {
            "action": "ready_for_review",
            "repository": {
                "private": True,
                "full_name": "must-not-be-logged/private",
            },
            "pull_request": None,
            "installation": {"id": "must-not-be-persisted"},
        }
        with patch.object(
            webhook, "process_public_pull_request"
        ) as public_path, self.assertNoLogs(level="INFO"):
            response = webhook.lambda_handler(self.signed_event(payload), None)

        self.assertEqual(response["statusCode"], 200)
        public_path.assert_not_called()
        self.assertEqual(webhook.table.put_calls, [])

    def test_non_pull_request_event_is_ignored_without_product_write(self):
        response = webhook.lambda_handler(
            self.signed_event({"installation": {"id": 123}}, event_name="installation"),
            None,
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(webhook.table.put_calls, [])

    def test_malformed_public_event_fails_content_free(self):
        payload = {
            "action": "opened",
            "repository": {"private": False, "full_name": "public/missing"},
            "pull_request": {"state": "open", "draft": False},
        }
        with self.assertLogs(level="ERROR") as captured:
            response = webhook.lambda_handler(self.signed_event(payload), None)

        self.assertEqual(response["statusCode"], 500)
        rendered = "\n".join(captured.output)
        self.assertIn("class=ValueError", rendered)
        self.assertNotIn("public/missing", rendered)
        self.assertEqual(webhook.table.put_calls, [])

    def test_error_metric_and_log_do_not_expose_exception_text(self):
        event = self.signed_event(public_payload())
        with patch.object(
            webhook,
            "process_public_pull_request",
            side_effect=RuntimeError("secret provider payload"),
        ), patch("builtins.print") as emitted, self.assertLogs(level="ERROR") as logs:
            response = webhook.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 500)
        metric = emitted.call_args.args[0]
        self.assertEqual(json.loads(metric)["Errors"], 1)
        rendered = metric + "\n" + "\n".join(logs.output)
        self.assertNotIn("provider payload", rendered)
        self.assertNotIn("owner/repo", rendered)

    def test_invalid_signature_never_reaches_payload_parser_or_state(self):
        event = build_webhook_event(
            private_payload(),
            secret=os.environ["GITHUB_WEBHOOK_SECRET"],
            event_name="pull_request",
            force_invalid_signature=True,
        )
        with patch.object(webhook.json, "loads") as loads:
            response = webhook.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 401)
        loads.assert_not_called()
        self.assertEqual(webhook.table.put_calls, [])


if __name__ == "__main__":
    unittest.main()
