"""Exercise real boto3 serialization and transaction expressions in Moto.

A subprocess isolates the suite's historical fake boto3 modules. All AWS calls
are intercepted in process; the socket guard proves there is no network escape.
"""

from pathlib import Path
import os
import subprocess
import sys
import unittest


class DirectOpenAIDynamoDBTests(unittest.TestCase):
    def test_real_sdk_atomic_admission_and_decimal_settlement(self):
        code = r"""
import json, os, socket, time
from pathlib import Path
from unittest.mock import patch
import boto3
from moto import mock_aws
from botocore.exceptions import ClientError
from lambdas.LlamaPReviewPipeline import persistence
from lambdas.LlamaPReviewPipeline.direct_openai_budget import PRICING_VERSION, reservation
from lambdas.LlamaPReviewPipeline.provider_model_routing import parse_direct_openai_profile
from lambdas.LlamaPReviewPipeline.deepseek_client import DeepSeekClient

profile_raw = Path("config/direct_openai_profile.example.json").read_text()
profile = parse_direct_openai_profile(profile_raw)
now = int(time.time())
policy = {"cycle_id": "synthetic", "valid_from": now-10, "expires_at": now+86400,
 "allocation_nanousd": 150_000_000_000, "verified_available_nanousd": 180_000_000_000,
 "organization_id": "org-synthetic", "project_id": "proj-synthetic", "profile_sha256": profile.sha256,
 "pricing_version": PRICING_VERSION, "pricing_verified_until": now+86400, "authorization": "approved"}
amount = reservation(policy, model="gpt-6-luna", max_output_tokens=100)["reserved_nanousd"]
policy["allocation_nanousd"] = amount
os.environ.update(DIRECT_OPENAI_PROFILE=profile_raw, DIRECT_OPENAI_BUDGET=json.dumps(policy))

with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), mock_aws():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(TableName="pipeline-offline", BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName":"repo","KeyType":"HASH"},{"AttributeName":"pr_number","KeyType":"RANGE"}],
        AttributeDefinitions=[{"AttributeName":"repo","AttributeType":"S"},{"AttributeName":"pr_number","AttributeType":"N"}])
    client = DeepSeekClient(api_key="synthetic",provider="direct_openai",model="gpt-6-luna",reasoning_effort="max",transport_model_override="")
    metadata = {"run_id":"r", "head_sha":"b"*40, "pipeline_phase":"context", "pipeline_attempt":1, **profile.select("route").metadata()}
    operation = client._begin_provider_operation(trace_phase="route",trace_metadata=metadata)
    payload = {"_llamapreview_logical_model":"gpt-6-luna","_llamapreview_billed_model":"gpt-6-luna", "reasoning":{"effort":"max"},
               "_llamapreview_budget":reservation(policy,model="gpt-6-luna",max_output_tokens=100)}
    fence = client._build_provider_dispatch_fence(payload=payload,operation=operation,transport_attempt_index=1)
    claim={"phase":"context","owner_id":"w","stream_event_id":"e","attempt":1}
    for n in (1,2):
        table.put_item(Item={"repo":"owner/public","pr_number":n,"status":"CONTEXT_RUNNING","run_id":"r","head_sha":"b"*40,"context_attempt":1,"context_claim":claim,"ttl_epoch":now+86400})
    assert persistence.begin_provider_call_dispatch("owner/public",1,expected_status="CONTEXT_RUNNING",record=fence,phase_claim=claim,table=table)
    assert not persistence.begin_provider_call_dispatch("owner/public",2,expected_status="CONTEXT_RUNNING",record=fence,phase_claim=claim,table=table)
    assert persistence.provider_call_records(persistence.get_item("owner/public",2,table=table)) == []
    counter_key={"repo":fence["budget"]["counter_repo"],"pr_number":0}
    counter=table.get_item(Key=counter_key,ConsistentRead=True)["Item"]
    assert counter["charged_nanousd"] == amount and counter["reserved_nanousd"] == amount
    # Real DynamoDB reads use Decimal. Original fence tariff/coordinates are
    # authoritative even if the terminal callback injects another budget.
    terminal={**fence,"status":"completed","usage_state":"reported","response_model":"gpt-6-luna","service_tier":"default",
              "usage":{"input_tokens":100,"prompt_tokens":100,"output_tokens":20,"completion_tokens":20,"total_tokens":120,
                       "input_tokens_details":{"cached_tokens":0,"cache_write_tokens":0},"output_tokens_details":{"reasoning_tokens":17}},
              "budget":{"counter_repo":"bad","reserved_nanousd":0}}
    table.update_item(Key={"repo":"owner/public","pr_number":1},UpdateExpression="SET #s = :s",ExpressionAttributeNames={"#s":"status"},ExpressionAttributeValues={":s":"SUPERSEDED"})
    os.environ["DIRECT_OPENAI_BUDGET"] = "invalid-after-dispatch"
    assert persistence.record_provider_call("owner/public",1,expected_status="CONTEXT_RUNNING",record=terminal,phase_claim=claim,table=table)
    durable=persistence.provider_call_records(persistence.get_item("owner/public",1,table=table))[0]
    assert durable["budget"]["actual_nanousd"] == 20000
    assert persistence.record_provider_call("owner/public",1,expected_status="ignored",record=durable,table=table)
    counter=table.get_item(Key=counter_key,ConsistentRead=True)["Item"]
    assert counter["charged_nanousd"] == counter["settled_nanousd"] == 20000
    assert counter["reserved_nanousd"] == 0 and "ttl_epoch" not in counter
    assert "ttl_epoch" in persistence.get_item("owner/public",1,table=table)
print("offline real-SDK transaction contract passed")
"""
        env = {
            **os.environ,
            "MODEL_PROVIDER": "deepseek",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "synthetic",
            "AWS_SECRET_ACCESS_KEY": "synthetic",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("offline real-SDK transaction contract passed", result.stdout)
