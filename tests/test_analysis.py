"""Offline analysis contract tests. Provider access is always replaced or forbidden."""
import http.client
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import arena


SOURCE = "Your account is frozen. Send your one-time code now to release the refund."


def review():
    return {"assessment": "warning-signs", "summary": "The message asks for a security code under pressure.",
            "red_flags": [{"quote": "Send your one-time code now", "why": "An inbound request for a security code is a warning sign."}],
            "safer_steps": ["Contact the organisation using details you already know."],
            "suggested_reply": ""}


def completion(message, finish_reason="stop"):
    return {"message": message, "finish_reason": finish_reason}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.no_network = patch.object(arena.httpx, "post", side_effect=AssertionError("External calls are forbidden in offline tests."))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def test_input_is_trimmed_and_limited(self):
        self.assertEqual(arena.validate_analysis_text("  hello\n"), "hello")
        self.assertEqual(len(arena.validate_analysis_text("x" * 4000)), 4000)
        for value in (None, [], {}, True, 123, "", " \n ", "x" * 4001):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    arena.validate_analysis_text(value)

    def test_schema_requires_exact_source_quotes(self):
        for quote in ("not in the source", "send your one-time code now", ""):
            obj = review()
            obj["red_flags"][0]["quote"] = quote
            with self.subTest(quote=quote), self.assertRaises(arena.AnalysisResponseError):
                arena.validate_analysis_result(obj, SOURCE)

    def test_schema_rejects_invalid_fields_and_lengths(self):
        cases = []
        for key, value in (("assessment", "safe"), ("summary", []), ("summary", "x" * 701),
                           ("red_flags", []), ("red_flags", "flags"), ("safer_steps", []),
                           ("safer_steps", ["x" * 301]), ("suggested_reply", False),
                           ("suggested_reply", "x" * 601)):
            obj = review()
            obj[key] = value
            cases.append(obj)
        missing = review()
        del missing["summary"]
        cases.append(missing)
        extra = review()
        extra["confidence"] = 99
        cases.append(extra)
        for obj in cases:
            with self.subTest(obj=obj), self.assertRaises(arena.AnalysisResponseError):
                arena.validate_analysis_result(obj, SOURCE)

    def test_no_safe_verdict_or_percentage_certainty(self):
        for summary in ("This message is safe.", "This appears to be completely legitimate.",
                        "There is 90% certainty.", "There is 90 per cent certainty.",
                        "This is 90% likely to be a scam.", "Confidence is 90%."):
            obj = review()
            obj["summary"] = summary
            with self.subTest(summary=summary), self.assertRaises(arena.AnalysisResponseError):
                arena.validate_analysis_result(obj, SOURCE)

    def test_percentages_in_message_evidence_are_not_confidence_scores(self):
        obj = review()
        obj["summary"] = "The promised 50% return needs independent verification."
        self.assertEqual(arena.validate_analysis_result(obj, SOURCE)["summary"], obj["summary"])

    def test_unclear_can_have_no_quoted_flags(self):
        obj = review()
        obj.update(assessment="unclear", summary="The text alone is insufficient to authenticate the sender.", red_flags=[])
        self.assertEqual(arena.validate_analysis_result(obj, SOURCE)["assessment"], "unclear")

    def test_analysis_treats_submission_as_data_and_has_no_tools(self):
        with patch.object(arena, "chat", return_value=completion({"content": json.dumps(review())})) as model:
            result = arena.analyse(" \n" + SOURCE + "\n ")
        args, kwargs = model.call_args
        self.assertEqual(json.loads(args[0][1]["content"]), {"message_text": SOURCE})
        self.assertIn("untrusted evidence", args[0][0]["content"])
        self.assertNotIn("tools", kwargs)
        self.assertEqual(kwargs["response_format"], arena.ANALYSIS_RESPONSE_FORMAT)
        self.assertIs(kwargs["enable_thinking"], False)
        self.assertIs(kwargs["return_choice"], True)
        self.assertEqual(kwargs["attempts"], 1)
        self.assertEqual(result["model"], arena.MODEL)
        self.assertEqual(result["provider"], "Nebius Token Factory")
        self.assertTrue(result["analysed_at_utc"].endswith("Z"))
        self.assertEqual(set(result), arena.ANALYSIS_KEYS | {"model", "provider", "analysed_at_utc"})

    def test_analysis_rejects_invalid_json_or_model_actions(self):
        for response in ({"content": "not JSON"}, {"content": "{}"}, {"content": None},
                         {"content": json.dumps(review()), "tool_calls": [{"name": "send_payment"}]},
                         {"content": json.dumps(review()), "function_call": {"name": "send_payment"}},
                         {"content": json.dumps(review()), "refusal": "Cannot review this."}):
            with self.subTest(response=response), patch.object(arena, "chat", return_value=completion(response)):
                with self.assertRaises(arena.AnalysisResponseError):
                    arena.analyse(SOURCE)

    def test_unfinished_analysis_is_rejected_even_with_valid_json(self):
        for reason in ("length", "content_filter", "tool_calls", None):
            response = completion({"content": json.dumps(review())}, reason)
            with self.subTest(finish_reason=reason), patch.object(arena, "chat", return_value=response) as model:
                with self.assertRaisesRegex(arena.AnalysisResponseError, "did not finish"):
                    arena.analyse(SOURCE)
                model.assert_called_once()
        reasoning_only = completion({"content": "", "reasoning_content": "unfinished reasoning"}, "length")
        with patch.object(arena, "chat", return_value=reasoning_only):
            with self.assertRaisesRegex(arena.AnalysisResponseError, "did not finish"):
                arena.analyse(SOURCE)

    def test_observed_three_field_response_is_rejected_without_local_fill(self):
        incomplete = review()
        del incomplete["safer_steps"]
        del incomplete["suggested_reply"]
        with patch.object(arena, "chat", return_value=completion({"content": json.dumps(incomplete)})) as model:
            with self.assertRaisesRegex(arena.AnalysisResponseError, "analysis fields only"):
                arena.analyse(SOURCE)
        self.assertEqual(set(incomplete), {"assessment", "summary", "red_flags"})
        model.assert_called_once()

    def test_analysis_transport_uses_bounded_json_nonreasoning_request(self):
        choice = completion({"content": json.dumps(review())})
        response = Mock()
        response.json.return_value = {"choices": [choice]}
        with patch.dict(os.environ, {"NEBIUS_API_KEY": "offline-test-only"}), \
                patch.object(arena.httpx, "post", return_value=response) as post:
            self.assertEqual(arena.analyse(SOURCE)["assessment"], "warning-signs")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["response_format"]["type"], "json_schema")
        contract = body["response_format"]["json_schema"]
        self.assertIs(contract["strict"], True)
        schema = contract["schema"]
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), arena.ANALYSIS_KEYS)
        self.assertEqual(set(schema["properties"]), arena.ANALYSIS_KEYS)
        self.assertEqual(set(schema["properties"]["assessment"]["enum"]), arena.ANALYSIS_ASSESSMENTS)
        flags = schema["properties"]["red_flags"]
        self.assertEqual(flags["maxItems"], 8)
        self.assertIs(flags["items"]["additionalProperties"], False)
        self.assertEqual(set(flags["items"]["required"]), {"quote", "why"})
        self.assertEqual(schema["properties"]["safer_steps"]["minItems"], 1)
        self.assertIn('"safer_steps":["..."]', body["messages"][0]["content"])
        self.assertIn('"suggested_reply":""', body["messages"][0]["content"])
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual((body["temperature"], body["max_tokens"]), (0.1, 4000))
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        post.assert_called_once()

    def test_analysis_transport_preserves_truncation_and_does_not_retry(self):
        response = Mock()
        response.json.return_value = {"choices": [completion({"content": json.dumps(review())}, "length")]}
        with patch.dict(os.environ, {"NEBIUS_API_KEY": "offline-test-only"}), \
                patch.object(arena.httpx, "post", return_value=response) as post:
            with self.assertRaisesRegex(arena.AnalysisResponseError, "did not finish"):
                arena.analyse(SOURCE)
        post.assert_called_once()
        with patch.dict(os.environ, {"NEBIUS_API_KEY": "offline-test-only"}), \
                patch.object(arena.httpx, "post", side_effect=arena.httpx.ConnectTimeout("Offline fixture")) as post, \
                patch.object(arena.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                arena.analyse(SOURCE)
        post.assert_called_once()
        sleep.assert_not_called()
        response = Mock()
        response.raise_for_status.side_effect = arena.httpx.HTTPStatusError(
            "Unsupported schema fixture", request=arena.httpx.Request("POST", "https://fixture.invalid"),
            response=arena.httpx.Response(400))
        with patch.dict(os.environ, {"NEBIUS_API_KEY": "offline-test-only"}), \
                patch.object(arena.httpx, "post", return_value=response) as post:
            with self.assertRaises(RuntimeError):
                arena.analyse(SOURCE)
        post.assert_called_once()

    def test_evaluation_and_coaching_transport_options_are_unchanged(self):
        response = Mock()
        response.json.side_effect = [
            {"choices": [completion({"content": None, "tool_calls": [{"id": "fictional-1", "function": {
                "name": "send_payment", "arguments": '{"account":"fictional","amount_eur":10}'}}]})]},
            {"choices": [completion({"content": '{"score": 50, "verdict": "Practise independent verification."}'})]},
        ]
        sid = next(iter(app.BY_ID))
        with patch.dict(os.environ, {"NEBIUS_API_KEY": "offline-test-only"}), \
                patch.object(arena.httpx, "post", return_value=response) as post:
            self.assertEqual(arena.run_bout(sid, "baseline")["outcome"], "compromised")
            self.assertEqual(arena.debrief(sid, [])["score"], 50)
        self.assertEqual(post.call_count, 2)
        agent_body, coach_body = [call.kwargs["json"] for call in post.call_args_list]
        self.assertEqual((agent_body["temperature"], agent_body["max_tokens"]), (0.0, 1200))
        self.assertEqual(agent_body["tools"], arena.TOOLS)
        self.assertEqual(agent_body["tool_choice"], "auto")
        self.assertEqual((coach_body["temperature"], coach_body["max_tokens"]), (0.2, 4000))
        for body in (agent_body, coach_body):
            self.assertNotIn("response_format", body)
            self.assertNotIn("chat_template_kwargs", body)

    def test_rate_window_and_concurrency_limits(self):
        now = [100.0]
        limits = app.AnalysisLimits(clock=lambda: now[0])
        self.assertIsNone(limits.acquire("one"))
        self.assertIsNone(limits.acquire("two"))
        self.assertEqual(limits.acquire("three"), 1)
        limits.release()
        limits.release()
        for _ in range(4):
            self.assertIsNone(limits.acquire("one"))
            limits.release()
        self.assertEqual(limits.acquire("one"), 60)
        now[0] = 160.0
        self.assertIsNone(limits.acquire("one"))
        limits.release()


class AnalysisHttpTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"NEBIUS_API_KEY": "offline-test-only"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.no_network = patch.object(arena.httpx, "post", side_effect=AssertionError("External calls are forbidden in offline tests."))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.limits = patch.object(app, "ANALYSIS_LIMITS", app.AnalysisLimits())
        self.limits.start()
        self.addCleanup(self.limits.stop)
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, body, path="/api/analyse", headers=None, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        data = body if raw else json.dumps(body).encode()
        connection.request("POST", path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), json.loads(response.read()))
        connection.close()
        return result

    def test_analysis_does_not_require_a_scenario(self):
        with patch.object(arena, "chat", return_value=completion({"content": json.dumps(review())})):
            status, headers, body = self.request({"text": "  " + SOURCE + "  "})
        self.assertEqual(status, 200)
        self.assertEqual(body["red_flags"][0]["quote"], "Send your one-time code now")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_bad_inputs_are_clean_400s_without_provider_calls(self):
        for body in ({}, {"text": None}, {"text": 1}, {"text": " "}, {"text": "x" * 4001}, []):
            with self.subTest(body_type=type(body).__name__):
                status, _, error = self.request(body)
                self.assertEqual(status, 400)
                self.assertTrue(error["error"])
        for raw in (b"{", b"\xff", b"null"):
            self.assertEqual(self.request(raw, raw=True)[0], 400)

    def test_oversized_and_invalid_lengths_are_4xx(self):
        self.assertEqual(self.request(b"x" * (app.MAX_BODY + 1), raw=True)[0], 413)
        self.assertEqual(self.request(b"", raw=True, headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.request(b"", raw=True, headers={"Content-Length": "wrong"})[0], 400)

    def test_absent_key_is_useful_503(self):
        with patch.dict(os.environ, {}, clear=True):
            status, _, body = self.request({"text": SOURCE})
        self.assertEqual(status, 503)
        self.assertIn("Nebius API key", body["error"])
        self.assertEqual(app.ANALYSIS_LIMITS.active, 0)

    def test_invalid_model_response_is_502_and_releases_slot(self):
        for choice in (completion({"content": "not JSON"}),
                       completion({"content": json.dumps(review())}, "length")):
            with self.subTest(choice=choice), patch.object(arena, "chat", return_value=choice):
                status, _, body = self.request({"text": SOURCE})
            self.assertEqual(status, 502)
            self.assertIn("evidence-grounded", body["error"])
            self.assertEqual(app.ANALYSIS_LIMITS.active, 0)

    def test_five_starts_per_ip_and_forwarded_headers_do_not_bypass(self):
        with patch.object(arena, "chat", return_value=completion({"content": json.dumps(review())})) as model:
            statuses = [self.request({"text": SOURCE}, headers={"X-Forwarded-For": f"192.0.2.{n}"}) for n in range(6)]
        self.assertEqual([r[0] for r in statuses], [200] * 5 + [429])
        self.assertGreaterEqual(int(statuses[-1][1]["Retry-After"]), 1)
        self.assertEqual(model.call_count, 5)

    def test_two_concurrent_analyses_and_busy_response(self):
        entered = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        count = [0]
        outcomes = []

        def slow_model(*args, **kwargs):
            with lock:
                count[0] += 1
                if count[0] == 2:
                    entered.set()
            release.wait(4)
            return completion({"content": json.dumps(review())})

        def run_request():
            outcomes.append(self.request({"text": SOURCE})[0])

        with patch.object(arena, "chat", side_effect=slow_model):
            workers = [threading.Thread(target=run_request) for _ in range(2)]
            for worker in workers:
                worker.start()
            try:
                self.assertTrue(entered.wait(2))
                status, headers, _ = self.request({"text": SOURCE})
                self.assertEqual(status, 429)
                self.assertEqual(headers["Retry-After"], "1")
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
        self.assertEqual(sorted(outcomes), [200, 200])
        self.assertEqual(app.ANALYSIS_LIMITS.active, 0)

    def test_existing_rehearsal_and_evaluation_routes_are_preserved(self):
        sid = next(iter(app.BY_ID))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.request({"scenario": sid, "history": []}, "/api/rehearse/next")[0], 200)
        with patch.object(arena, "run_bout", return_value={"outcome": "resisted"}) as bout:
            status, _, body = self.request({"scenario": sid, "condition": "baseline"}, "/api/eval/bout")
        self.assertEqual((status, body), (200, {"outcome": "resisted"}))
        bout.assert_called_once_with(sid, "baseline")


if __name__ == "__main__":
    unittest.main()
