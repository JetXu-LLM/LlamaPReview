"""The selected Luna profile reaches one existing fenced provider lifecycle."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from tests.unit.fakes import ensure_repo_root_on_path, install_fake_requests_module, set_default_env

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline import deepseek_client as transport  # noqa: E402
from lambdas.LlamaPReviewPipeline.deepseek_client import DeepSeekClient  # noqa: E402


class _Response:
    status_code = 200
    headers = {}

    @staticmethod
    def json():
        return {
            "id": "chatcmpl-synthetic",
            "model": "openai/gpt-6-luna-20260922",
            "service_tier": "default",
            "openrouter_metadata": {
                "endpoints": {"available": [
                    {"provider": "OpenAI", "model": "openai/gpt-6-luna-20260922", "selected": True},
                    {"provider": "Azure", "model": "openai/gpt-6-luna-20260922", "selected": False},
                ]}
            },
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"ok":true}'},
                }
            ],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 33,
                "total_tokens": 153,
                "prompt_tokens_details": {"cached_tokens": 90},
                "completion_tokens_details": {"reasoning_tokens": 20},
                "cost": 0.00003,
                "is_byok": False,
                "cost_details": {"upstream_inference_cost": 0.000025},
            },
        }


class OpenRouterLunaTransportTests(unittest.TestCase):
    def _client(self):
        return DeepSeekClient(
            api_key="synthetic-key",
            provider="openrouter",
            model="openai/gpt-6-luna",
            reasoning_effort="max",
            transport_model_override="",
        )

    def test_request_and_ledger_preserve_max_reasoning_and_reported_cost(self):
        client = self._client()
        fenced = []
        recorded = []
        client.set_provider_dispatch_fence_sink(fenced.append)
        client.set_provider_call_sink(recorded.append)
        with patch.object(transport.requests, "post", return_value=_Response()) as post:
            result = client.chat(
                [{"role": "system", "content": "Answer in JSON."},
                 {"role": "user", "content": "Test"}],
                thinking=False,
                reasoning_effort="high",  # legacy caller cannot lower Luna
                response_format={"type": "json_object"},
                max_tokens=256,
                max_retries=1,
                trace_phase="final_presentation",
            )

        self.assertEqual(post.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions")
        request = post.call_args.kwargs["json"]
        self.assertEqual(request["model"], "openai/gpt-6-luna")
        self.assertEqual(request["reasoning"], {"effort": "max"})
        self.assertEqual(request["provider"], {"require_parameters": True})
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["max_completion_tokens"], 256)
        self.assertNotIn("thinking", request)
        self.assertNotIn("reasoning_effort", request)
        self.assertNotIn("max_tokens", request)
        self.assertEqual(post.call_args.kwargs["headers"]["X-OpenRouter-Metadata"], "enabled")
        self.assertEqual(len(fenced), 1)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(fenced[0]["call_id"], recorded[0]["call_id"])
        self.assertEqual(recorded[0]["provider"], "openrouter")
        self.assertEqual(recorded[0]["reasoning_effort"], "max")
        self.assertTrue(recorded[0]["thinking"])
        self.assertEqual(recorded[0]["response_model"], "openai/gpt-6-luna-20260922")
        self.assertEqual(recorded[0]["upstream_provider"], "OpenAI")
        self.assertEqual(recorded[0]["generation_id"], "chatcmpl-synthetic")
        self.assertIs(recorded[0]["is_byok"], False)
        self.assertEqual(recorded[0]["usage_state"], "reported")
        self.assertEqual(recorded[0]["usage"]["prompt_tokens_details"]["cached_tokens"], 90)
        self.assertEqual(recorded[0]["usage"]["completion_tokens_details"]["reasoning_tokens"], 20)
        self.assertEqual(recorded[0]["usage"]["cost"], 0.00003)
        self.assertEqual(result[transport.PROVIDER_CALL_RECORD_KEY]["call_id"], recorded[0]["call_id"])

    def test_luna_has_no_deepseek_key_fallback_or_provider_tool_call_at_max(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-deepseek"}, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"):
                DeepSeekClient(provider="openrouter")
        with self.assertRaisesRegex(ValueError, "requires Responses API"):
            self._client().build_payload(
                [{"role": "user", "content": "x"}],
                tools=[{"type": "function", "function": {"name": "lookup"}}],
            )

    def test_one_provider_setting_selects_complete_profiles(self):
        code = (
            "import json; from lambdas.LlamaPReviewPipeline import config as c; "
            "print(json.dumps([c.MODEL_PROVIDER,c.DEEPSEEK_MODEL,"
            "c.DEEPSEEK_TRANSPORT_MODEL_OVERRIDE,c.ANALYZER_MODEL,"
            "c.PFR_NORMAL_MODEL,c.LOW_REVIEW_MODEL,c.NORMAL_REVIEW_MODEL,"
            "c.ANALYZER_EFFORT,c.PFR_NORMAL_EFFORT,c.LOW_REVIEW_EFFORT,"
            "c.NORMAL_REVIEW_EFFORT,c.DEEPSEEK_EFFORT]))"
        )
        base = os.environ.copy()
        base.update({
            "DEEPSEEK_MODEL": "deepseek-v4-pro",
            "DEEPSEEK_TRANSPORT_MODEL_OVERRIDE": "deepseek-v4-flash",
            "ANALYZER_MODEL": "deepseek-v4-flash",
            "PFR_NORMAL_MODEL": "deepseek-v4-flash",
            "LOW_REVIEW_MODEL": "deepseek-v4-flash",
            "NORMAL_REVIEW_MODEL": "deepseek-v4-pro",
            "ANALYZER_EFFORT": "high",
            "PFR_NORMAL_EFFORT": "high",
            "LOW_REVIEW_EFFORT": "high",
            "NORMAL_REVIEW_EFFORT": "high",
        })
        for provider in ("openrouter", "deepseek"):
            with self.subTest(provider=provider):
                env = {**base, "MODEL_PROVIDER": provider}
                result = subprocess.run(
                    [sys.executable, "-c", code],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                selected = json.loads(result.stdout)
                self.assertEqual(selected[0], provider)
                if provider == "openrouter":
                    self.assertEqual(selected[1], "openai/gpt-6-luna")
                    self.assertEqual(selected[2], "")
                    self.assertEqual(selected[3:7], ["openai/gpt-6-luna"] * 4)
                    self.assertEqual(selected[7:], ["max"] * 5)
                else:
                    self.assertEqual(selected[1:7], [
                        "deepseek-v4-pro", "deepseek-v4-flash",
                        "deepseek-v4-flash", "deepseek-v4-flash",
                        "deepseek-v4-flash", "deepseek-v4-pro",
                    ])
                    self.assertEqual(selected[7:11], ["high"] * 4)
                    self.assertEqual(selected[11], "max")

    @unittest.skipUnless(hasattr(signal, "setitimer"), "POSIX wall timer required")
    def test_slow_drip_expires_one_fenced_dispatch_without_retry(self):
        # An isolated process imports real Requests even when other unit
        # modules installed their fake requests module in this test process.
        script = '''
import http.server, json, threading, time
from lambdas.LlamaPReviewPipeline.deepseek_client import DeepSeekClient
from lambdas.LlamaPReviewPipeline.deadline import DeadlineExceeded

body = json.dumps({"choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}).encode()
class Handler(http.server.BaseHTTPRequestHandler):
    calls = 0
    def log_message(self, *_args): pass
    def do_POST(self):
        Handler.calls += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for byte in body:
            try:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
            except OSError:
                break
            time.sleep(0.02)
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
client = DeepSeekClient(api_key="synthetic", provider="openrouter", model="openai/gpt-6-luna", reasoning_effort="max", transport_model_override="", timeout=0.16)
client.OPENROUTER_API_BASE = "http://127.0.0.1:%d" % server.server_port
fences, records = [], []
client.set_provider_dispatch_fence_sink(fences.append)
client.set_provider_call_sink(records.append)
started = time.monotonic()
try:
    client.chat([{"role":"user","content":"local drip"}], max_retries=3)
    outcome = "completed"
except DeadlineExceeded:
    outcome = "deadline"
elapsed = time.monotonic() - started
server.shutdown()
print(json.dumps({"outcome":outcome,"elapsed":elapsed,"http_calls":Handler.calls,"fences":len(fences),"records":len(records),"status":records[0]["status"] if records else "","usage_state":records[0]["usage_state"] if records else ""}))
'''
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        observed = json.loads(completed.stdout)
        self.assertEqual(observed["outcome"], "deadline")
        self.assertLess(observed["elapsed"], 0.33)
        self.assertEqual(observed["http_calls"], 1)
        self.assertEqual(observed["fences"], 1)
        self.assertEqual(observed["records"], 1)
        self.assertEqual(observed["status"], "transport_error")
        self.assertEqual(observed["usage_state"], "unreported")

    @unittest.skipUnless(hasattr(signal, "setitimer"), "POSIX wall timer required")
    def test_inner_timer_restores_outer_review_timer_remaining(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signal alarms require main thread")
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)

        def outer_expired(_signum, _frame):
            raise AssertionError("outer timer should not fire in this test")

        try:
            signal.signal(signal.SIGALRM, outer_expired)
            signal.setitimer(signal.ITIMER_REAL, 0.4)
            with transport._absolute_request_timeout(0.2, stage="test.inner"):
                time.sleep(0.03)
            remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertIs(signal.getsignal(signal.SIGALRM), outer_expired)
            self.assertGreater(remaining, 0.25)
            self.assertLess(remaining, 0.4)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, *previous_timer)


if __name__ == "__main__":
    unittest.main()
