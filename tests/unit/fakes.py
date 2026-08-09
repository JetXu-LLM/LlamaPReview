import hashlib
import hmac
import json
import os
import sys
import types
from pathlib import Path
from typing import Dict, Optional


ROOT = Path(__file__).resolve().parents[2]


def ensure_repo_root_on_path() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def set_default_env() -> None:
    defaults = {
        "DYNAMODB_PIPELINE_TABLE": "llamapreview-pipeline-test",
        "PUBLICATION_ARTIFACT_BUCKET": "pipeline-publication-artifacts",
        "GITHUB_WEBHOOK_SECRET": "test-secret",
        "GITHUB_APP_ID": "123456",
        "GITHUB_PRIVATE_KEY": "test-private-key-placeholder",
        "DEEPSEEK_API_KEY": "fake-deepseek",
        "DEEPSEEK_TRANSPORT_MODEL_OVERRIDE": "deepseek-v4-flash",
        "ANALYZER_MODEL": "deepseek-v4-flash",
        "ANALYZER_EFFORT": "high",
        "LOW_REVIEW_MODEL": "deepseek-v4-flash",
        "LOW_REVIEW_EFFORT": "high",
        "PFR_NORMAL_MODEL": "deepseek-v4-flash",
        "PFR_NORMAL_EFFORT": "high",
        "NORMAL_REVIEW_MODEL": "deepseek-v4-pro",
        "NORMAL_REVIEW_EFFORT": "high",
        "PR_DETAILS_MAX_CHARS": "250000",
        "PR_ANALYZER_MAX_CHARS": "30000",
        "LARGE_PR_MAX_CHARS": "600000",
        "PFR_HIGH_MAX_CONTEXT_CHARS": "600000",
        "REVIEW_INPUT_MAX_CHARS": "850000",
        # Historical unit fixtures below construct review v2 objects unless a
        # test explicitly selects v3. Production defaults to v3 in config.py.
        "PFR_HIGH_TOKEN_BUDGET": "750000",
        "PFR_NORMAL_TIME_BUDGET_SECONDS": "240",
        "PFR_NORMAL_MAX_TOOL_ROUNDS": "3",
        "PFR_NORMAL_TOKEN_BUDGET": "200000",
        "PFR_NORMAL_MAX_CONTEXT_CHARS": "150000",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


class FakeClientError(Exception):
    def __init__(self, code="ClientError"):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class FakeTable:
    def __init__(self, name: str):
        self.name = name
        self.items = {}
        self.put_calls = []
        self.update_calls = []
        self.get_calls = []

    def get_item(self, Key, **kwargs):
        self.get_calls.append({"Key": Key, **kwargs})
        key = tuple(sorted(Key.items()))
        if key in self.items:
            return {"Item": self.items[key]}
        return {}

    def put_item(self, Item, **kwargs):
        key_fields = []
        if "repo" in Item and "pr_number" in Item:
            key_fields = [("repo", Item["repo"]), ("pr_number", Item["pr_number"])]
        elif "repo_full_name" in Item:
            key_fields = [("repo_full_name", Item["repo_full_name"])]
        key = tuple(sorted(key_fields)) if key_fields else (("id", len(self.items)),)
        condition = kwargs.get("ConditionExpression")
        if condition == "attribute_not_exists(repo) AND attribute_not_exists(pr_number)" and key in self.items:
            raise FakeClientError("ConditionalCheckFailedException")
        self.items[key] = dict(Item)
        self.put_calls.append(Item)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def update_item(self, **kwargs):
        self.update_calls.append(kwargs)
        key = tuple(sorted(kwargs.get("Key", {}).items()))
        item = self.items.setdefault(key, dict(kwargs.get("Key", {})))
        values = kwargs.get("ExpressionAttributeValues", {})
        names = kwargs.get("ExpressionAttributeNames", {})
        condition = kwargs.get("ConditionExpression")
        if condition and "#s = :expected_status" in condition:
            status_name = names.get("#s", "status")
            if item.get(status_name) != values.get(":expected_status"):
                raise FakeClientError("ConditionalCheckFailedException")
        elif condition and "#s = :expected" in condition:
            status_name = names.get("#s", "status")
            if item.get(status_name) != values.get(":expected"):
                raise FakeClientError("ConditionalCheckFailedException")
        if condition and "attribute_not_exists(#claim)" in condition:
            claim_name = names.get("#claim", "#claim")
            existing_claim = item.get(claim_name)
            expires_name = names.get(
                "#expires_at_epoch",
                "expires_at_epoch",
            )
            expires_at = (
                existing_claim.get(expires_name)
                if isinstance(existing_claim, dict)
                else None
            )
            if (
                existing_claim is not None
                and not (
                    isinstance(expires_at, (int, float))
                    and expires_at < values.get(":now_epoch", 0)
                )
                and not (
                    values.get(":stream_event_id")
                    and isinstance(existing_claim, dict)
                    and existing_claim.get(
                        names.get(
                            "#stream_event_id",
                            "stream_event_id",
                        )
                    )
                    == values.get(":stream_event_id")
                )
            ):
                raise FakeClientError("ConditionalCheckFailedException")
        if condition and "#claim.#owner_id = :owner_id" in condition:
            claim_name = names.get("#claim", "#claim")
            owner_name = names.get("#owner_id", "owner_id")
            active_claim = item.get(claim_name)
            if (
                not isinstance(active_claim, dict)
                or active_claim.get(owner_name) != values.get(":owner_id")
            ):
                raise FakeClientError("ConditionalCheckFailedException")
        if (
            condition
            and "#claim.#stream_event_id = :stream_event_id" in condition
            and "attribute_not_exists(#claim)" not in condition
        ):
            claim_name = names.get("#claim", "#claim")
            stream_name = names.get(
                "#stream_event_id",
                "stream_event_id",
            )
            active_claim = item.get(claim_name)
            if (
                not isinstance(active_claim, dict)
                or active_claim.get(stream_name)
                != values.get(":stream_event_id")
            ):
                raise FakeClientError("ConditionalCheckFailedException")
        if condition and "#head = :head" in condition:
            if item.get(names.get("#head", "head_sha")) != values.get(
                ":head"
            ):
                raise FakeClientError("ConditionalCheckFailedException")
        if condition and "#intent = :expected_intent" in condition:
            if item.get(
                names.get("#intent", "publication_intent")
            ) != values.get(":expected_intent"):
                raise FakeClientError("ConditionalCheckFailedException")
        for raw_name, attribute in names.items():
            if not raw_name.startswith("#expected"):
                continue
            suffix = raw_name[len("#expected") :]
            if item.get(attribute) != values.get(f":expected{suffix}"):
                raise FakeClientError("ConditionalCheckFailedException")
        for raw_name, attribute in names.items():
            if raw_name.startswith("#missing") and attribute in item:
                raise FakeClientError("ConditionalCheckFailedException")
        for raw_name in ("#intent", "#receipt"):
            if (
                condition
                and f"attribute_not_exists({raw_name})" in condition
                and names.get(raw_name, raw_name) in item
            ):
                raise FakeClientError("ConditionalCheckFailedException")
        if condition and "attribute_not_exists(#call)" in condition:
            call_name = names.get("#call", "#call")
            if call_name in item:
                raise FakeClientError("ConditionalCheckFailedException")
        if condition and "#call = :dispatching_record" in condition:
            call_name = names.get("#call", "#call")
            if item.get(call_name) != values.get(":dispatching_record"):
                raise FakeClientError("ConditionalCheckFailedException")
        update = kwargs.get("UpdateExpression", "")
        updated = {}
        if update.startswith("ADD "):
            add_part, _, set_suffix = update[4:].partition(" SET ")
            for expression in add_part.split(","):
                pieces = expression.strip().split()
                if len(pieces) != 2:
                    continue
                raw_attr, value_key = pieces
                attr = names.get(raw_attr, raw_attr)
                item[attr] = item.get(attr, 0) + values.get(value_key, 0)
                updated[attr] = item[attr]
            if set_suffix:
                update = "SET " + set_suffix
            else:
                update = ""
        if update.startswith("SET "):
            set_payload, separator, remove_payload = update[4:].partition(
                " REMOVE "
            )
            parts = [part.strip() for part in set_payload.split(",")]
            for part in parts:
                if "=" not in part:
                    continue
                left, right = [piece.strip() for piece in part.split("=", 1)]
                attr = names.get(left, left)
                item[attr] = values.get(right)
                updated[attr] = item[attr]
            if separator:
                for raw_attr in remove_payload.split(","):
                    attr = names.get(raw_attr.strip(), raw_attr.strip())
                    item.pop(attr, None)
        response = {"ResponseMetadata": {"HTTPStatusCode": 200}}
        if kwargs.get("ReturnValues") == "UPDATED_NEW":
            response["Attributes"] = updated
        return response

    def reset(self):
        self.items.clear()
        self.put_calls.clear()
        self.update_calls.clear()
        self.get_calls.clear()


class FakeDynamoResource:
    def __init__(self):
        self.tables = {}

    def Table(self, name: str):
        if name not in self.tables:
            self.tables[name] = FakeTable(name)
        return self.tables[name]


class _FakeBody:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.get_calls = []

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = Body
        self.put_calls.append({"Bucket": Bucket, "Key": Key, "Body": Body, **kwargs})
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_object(self, Bucket, Key):
        self.get_calls.append({"Bucket": Bucket, "Key": Key})
        return {"Body": _FakeBody(self.objects[(Bucket, Key)])}


def install_fake_aws_modules(fake_dynamo: FakeDynamoResource, fake_s3: Optional[FakeS3Client] = None) -> None:
    resolved_s3 = fake_s3 or FakeS3Client()
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.resource = lambda service: fake_dynamo
    fake_boto3.client = lambda service: resolved_s3
    sys.modules["boto3"] = fake_boto3

    fake_botocore = types.ModuleType("botocore")
    fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
    fake_botocore_exceptions.ClientError = FakeClientError
    fake_botocore.exceptions = fake_botocore_exceptions
    sys.modules["botocore"] = fake_botocore
    sys.modules["botocore.exceptions"] = fake_botocore_exceptions


def install_fake_github_module() -> None:
    fake_github = types.ModuleType("github")

    class _FakeGithub:
        def __init__(self, token):
            self.token = token

    class _FakeGithubIntegration:
        pass

    fake_github.Github = _FakeGithub
    fake_github.GithubIntegration = _FakeGithubIntegration
    sys.modules["github"] = fake_github


def install_fake_jwt_module() -> None:
    if "jwt" in sys.modules:
        return

    fake_jwt = types.ModuleType("jwt")

    class _Exceptions:
        InvalidKeyError = Exception

    fake_jwt.exceptions = _Exceptions
    fake_jwt.encode = lambda payload, key, algorithm=None: "fake-jwt"
    sys.modules["jwt"] = fake_jwt


def install_fake_requests_module() -> None:
    if "requests" in sys.modules:
        return

    fake_requests = types.ModuleType("requests")

    class _FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    fake_requests.post = lambda *args, **kwargs: _FakeResponse({"token": "fake-token"})
    fake_requests.get = lambda *args, **kwargs: _FakeResponse(
        {"tree": [], "truncated": False}
    )
    fake_requests.Timeout = TimeoutError
    fake_requests.RequestException = Exception
    fake_requests.exceptions = types.SimpleNamespace(RequestException=Exception, Timeout=TimeoutError)
    sys.modules["requests"] = fake_requests


def install_fake_llama_github_module() -> None:
    fake_llama = types.ModuleType("llama_github")

    class _FakeSimpleLLM:
        def __init__(self):
            self.temperature = 0.0

    class _FakeGithubRAG:
        def __init__(self, **kwargs):
            self.github_instance = object()
            self.github_api_handler = object()
            self.llm_manager = types.SimpleNamespace(llm_simple=_FakeSimpleLLM())
            self.RepositoryPool = types.SimpleNamespace(
                get_repository=lambda **_args: object()
            )

    fake_llama.GithubRAG = _FakeGithubRAG
    sys.modules["llama_github"] = fake_llama


def build_webhook_event(
    payload: Dict[str, object],
    secret: str,
    event_name: str = "pull_request",
    force_invalid_signature: bool = False,
) -> Dict[str, object]:
    body = json.dumps(payload)
    signature = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if force_invalid_signature:
        signature = "deadbeef"
    return {
        "body": body,
        "headers": {
            "X-GitHub-Event": event_name,
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    }


def build_stream_record(
    repo: str,
    pr_number: int,
    new_status: str,
    old_status: Optional[str] = None,
    extra_new_image: Optional[Dict[str, Dict[str, str]]] = None,
    event_id: str = "stream-event-1",
) -> Dict[str, object]:
    new_image = {
        "repo": {"S": repo},
        "pr_number": {"N": str(pr_number)},
        "status": {"S": new_status},
    }
    if extra_new_image:
        new_image.update(extra_new_image)

    old_image = {}
    if old_status is not None:
        old_image["status"] = {"S": old_status}

    return {
        "eventID": event_id,
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": new_image,
            "OldImage": old_image,
        },
    }
