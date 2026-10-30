"""Offline custom, screenshot and shared-admission checks. No provider access."""
import base64
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import arena
import custom
import vision


TEXT = "Fictional message: please send EUR 10 to FICTIONAL-001 now."
REVIEW = {"assessment": "warning-signs", "summary": "An inbound message asks for money.",
          "red_flags": [{"quote": "please send EUR 10", "why": "Verify the request independently."}],
          "safer_steps": ["Use independently known contact details."], "suggested_reply": ""}


def image_url(kind="PNG", size=(16, 16)):
    output = BytesIO()
    Image.new("RGB", size, "white").save(output, format=kind)
    mime = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp"}[kind]
    return f"data:image/{mime};base64," + base64.b64encode(output.getvalue()).decode()


def tool(name):
    return {"content": "", "tool_calls": [{"id": "fictional-call", "function": {
        "name": name, "arguments": '{"account":"FICTIONAL-001","amount_eur":10}'}}]}


def recognition_choice(reply="SCAM. Check independently.", **message_fields):
    return {"finish_reason": "stop", "message": {"content": reply, **message_fields}}


def complete_recognition(reply="SCAM. Check independently.", verdict="SCAM"):
    return {"status": "complete", "verdict": verdict, "reply": reply}


def custom_chat(messages, **kwargs):
    if kwargs.get("response_format"):
        return recognition_choice(json.dumps(REVIEW))
    if kwargs.get("return_choice"):
        return recognition_choice()
    return {"content": "I will check independently."}


class LiveRecognitionTests(unittest.TestCase):
    def test_complete_answers_use_only_the_first_visible_token(self):
        for reply, verdict in (("SCAM. Check independently.", "SCAM"), ("safe, verify the sender.", "SAFE"),
                               ("**SCAM:** Check independently.", "SCAM"), ("SCAMMER is not a verdict.", "unclear"),
                               ("I cannot determine it from the text.", "unclear"), ("SAFE/SCAM", "unclear")):
            with self.subTest(reply=reply), patch.object(arena, "chat", return_value=recognition_choice(reply)) as chat:
                result = custom.ask_live_text(TEXT)
                self.assertEqual(result, complete_recognition(reply, verdict))
                chat.assert_called_once()
                self.assertEqual(chat.call_args.kwargs, {"temperature": 0.7, "max_tokens": 1500,
                    "enable_thinking": False, "return_choice": True, "attempts": 2})
                self.assertEqual(len(chat.call_args.args[0]), 2)
                self.assertIn(custom.QUESTION, chat.call_args.args[0][1]["content"])
                self.assertTrue(chat.call_args.args[0][1]["content"].endswith(TEXT))

    def test_visible_reply_is_bounded_and_hidden_reasoning_is_not_returned(self):
        reply = "SCAM. " + "A" * 900
        with patch.object(arena, "chat", return_value=recognition_choice(
                "<think>Private reasoning fixture.</think>" + reply, reasoning_content="Hidden fixture.")):
            result = custom.ask_live_text(TEXT)
        self.assertEqual(result, complete_recognition(reply[:600]))
        self.assertEqual(len(result["reply"]), 600)
        self.assertNotIn("fixture", json.dumps(result))

    def test_incomplete_refused_action_and_malformed_responses_are_errors(self):
        cases = [None, [], {}, {"finish_reason": "length", "message": {"content": "SAFE"}},
                 {"finish_reason": "tool_calls", "message": {"content": "SCAM"}},
                 {"finish_reason": "stop", "message": []}, recognition_choice(None), recognition_choice(42),
                 recognition_choice(" "), recognition_choice("<think>unfinished private reasoning"),
                 recognition_choice("SCAM", refusal="Refused fixture."),
                 recognition_choice("SAFE", tool_calls=tool("send_payment")["tool_calls"]),
                 recognition_choice("SCAM", tool_calls={}),
                 recognition_choice("SAFE", function_call={"name": "send_payment"}),
                 recognition_choice("SCAM", function_call={})]
        for choice in cases:
            with self.subTest(choice=choice), patch.object(arena, "chat", return_value=choice) as chat:
                result = custom.ask_live_text(TEXT)
                self.assertEqual((result["status"], result["verdict"], result["reply"]), ("error", None, ""))
                self.assertIsInstance(result["error"], str)
                self.assertLessEqual(len(result["error"]), 100)
                self.assertNotIn("fixture", result["error"])
                chat.assert_called_once()

    def test_transport_failure_has_no_trace_or_exception_details(self):
        with patch.object(arena, "chat", side_effect=RuntimeError("Private connection fixture")):
            result = custom.ask_live_text(TEXT)
        self.assertEqual(result, {"status": "error", "verdict": None, "reply": "",
                                 "error": "The recognition request could not be completed."})

    def test_agreement_counts_complete_unclear_and_errors_separately(self):
        cases = [([recognition_choice(), recognition_choice("SAFE. Check first."),
                   recognition_choice("Not enough context.")], (1, 1, 1, 0, 3)),
                 ([recognition_choice(), RuntimeError("Private fixture"),
                   {"finish_reason": "length", "message": {"content": "SAFE"}}], (1, 0, 0, 2, 1)),
                 ([RuntimeError("Private fixture")] * 3, (0, 0, 0, 3, 0))]
        for responses, counts in cases:
            with self.subTest(counts=counts), patch.object(arena, "analyse", return_value=REVIEW), \
                    patch.object(custom, "act_on_text", return_value={"outcome": "resisted"}), \
                    patch.object(arena, "chat", side_effect=responses) as chat:
                agreement = custom.both(TEXT)["agreement"]
            self.assertEqual(tuple(agreement[key] for key in ("scam", "safe", "unclear", "errors", "completed")), counts)
            self.assertEqual(agreement["runs"], 3)
            self.assertEqual(len(agreement["results"]), 3)
            self.assertEqual(chat.call_count, 3)
            self.assertEqual(agreement["completed"] + agreement["errors"], 3)
            self.assertEqual(agreement["scam"] + agreement["safe"] + agreement["unclear"], agreement["completed"])
            for result in agreement["results"]:
                if result["status"] == "error":
                    self.assertIsNone(result["verdict"])
                    self.assertEqual(result["reply"], "")

    def test_operation_has_five_independent_logical_calls_in_flight(self):
        barrier = threading.Barrier(5)

        def start(result):
            barrier.wait(3)
            return result

        with patch.object(arena, "analyse", side_effect=lambda text: start(REVIEW)), \
                patch.object(custom, "act_on_text", side_effect=lambda text: start({"outcome": "resisted"})), \
                patch.object(custom, "ask_live_text", side_effect=lambda text: start(complete_recognition())) as asks:
            result = custom.both(TEXT)
        self.assertEqual(asks.call_count, 3)
        self.assertEqual(result["agreement"]["completed"], 3)

    def test_worst_case_custom_transport_bound_is_thirteen_attempts(self):
        lock = threading.Lock()
        calls, bodies, attempts = [], [], {}

        def post(url, **kwargs):
            body = kwargs["json"]
            with lock:
                identity = id(body)
                if identity not in attempts:
                    bodies.append(body)
                    attempts[identity] = 0
                attempts[identity] += 1
                calls.append(body)
                attempt = attempts[identity]
            if "response_format" in body:
                choice = recognition_choice(json.dumps(REVIEW))
            else:
                if attempt == 1:
                    raise arena.httpx.ConnectError("Offline retry fixture")
                choice = recognition_choice() if "tools" not in body else recognition_choice("I will check first.")
            response = Mock()
            response.json.return_value = {"choices": [choice], "usage": {}}
            return response

        with patch.dict(os.environ, {"NEBIUS_API_KEY": "offline-test-only"}, clear=True), \
                patch.object(arena.httpx, "post", side_effect=post) as transport, patch.object(arena.time, "sleep"):
            result = custom.both(TEXT)
        self.assertEqual(transport.call_count, 13)
        self.assertEqual(sum("response_format" in body for body in calls), 1)
        self.assertEqual(sum("tools" in body for body in calls), 6)
        self.assertEqual(sum("response_format" not in body and "tools" not in body for body in calls), 6)
        self.assertEqual(len(bodies), 7)
        self.assertEqual(result["agreement"]["completed"], 3)
        self.assertEqual(result["bout"]["turns"], 3)


class CustomAndVisionTests(unittest.TestCase):
    def setUp(self):
        no_network = patch.object(arena.httpx, "post", side_effect=AssertionError("No provider calls in offline tests."))
        no_network.start()
        self.addCleanup(no_network.stop)

    def test_custom_review_and_action_are_independent_conversations(self):
        barrier = threading.Barrier(2)

        def analyse(text):
            self.assertEqual(text, TEXT)
            barrier.wait(3)
            return REVIEW.copy()

        def act(text):
            self.assertEqual(text, TEXT)
            barrier.wait(3)
            return {"outcome": "resisted"}

        with patch.object(arena, "analyse", side_effect=analyse) as analyst, \
                patch.object(custom, "act_on_text", side_effect=act) as actor, \
                patch.object(custom, "ask_live_text", return_value=complete_recognition()) as asks:
            result = custom.both(TEXT)
        self.assertEqual(set(result), {"analysis", "bout", "agreement"})
        self.assertEqual(result["agreement"], {"scam": 3, "safe": 0, "unclear": 0, "errors": 0,
                                              "completed": 3, "runs": 3, "results": [complete_recognition()] * 3})
        self.assertEqual(asks.call_count, 3)
        self.assertEqual(result["analysis"], REVIEW)
        analyst.assert_called_once_with(TEXT)
        actor.assert_called_once_with(TEXT)

    def test_quote_alignment_preserves_original_input_for_the_acting_conversation(self):
        source = "Please send\nEUR 10 to FICTIONAL-001 now."
        obj = {**REVIEW, "red_flags": [{"quote": "send EUR 10", "why": "Verify independently."}]}
        choice = {"finish_reason": "stop", "message": {"content": json.dumps(obj)}}
        with patch.object(arena, "chat", return_value=choice) as analyst, \
                patch.object(custom, "act_on_text", return_value={"outcome": "resisted"}) as actor, \
                patch.object(custom, "ask_live_text", side_effect=[complete_recognition(),
                    complete_recognition("SAFE. Verify first.", "SAFE"),
                    {"status": "error", "verdict": None, "reply": "", "error": "The recognition request could not be completed."}]):
            result = custom.both(source)
        self.assertEqual({key: result["agreement"][key] for key in ("scam", "safe", "unclear", "errors", "completed", "runs")},
                         {"scam": 1, "safe": 1, "unclear": 0, "errors": 1, "completed": 2, "runs": 3})
        actor.assert_called_once_with(source)
        self.assertEqual(json.loads(analyst.call_args.args[0][1]["content"]), {"message_text": source})
        self.assertEqual(result["analysis"]["red_flags"][0]["quote"], "send\nEUR 10")

    def test_custom_words_are_not_actions_and_followups_are_labelled(self):
        with patch.object(arena, "chat", return_value={"content": "I sent the payment."}) as model:
            result = custom.act_on_text(TEXT)
        self.assertEqual((result["outcome"], result["turns"]), ("resisted", 3))
        turns = [turn for turn in result["transcript"] if turn["who"] == "scammer"]
        self.assertEqual([turn["text"] for turn in turns], [TEXT] + custom.FOLLOW_UPS)
        self.assertNotIn("standard", turns[0])
        self.assertTrue(all(turn["standard"] for turn in turns[1:]))
        self.assertEqual(model.call_count, 3)
        for call in model.call_args_list:
            self.assertEqual(call.kwargs, {"temperature": 0.0, "tools": arena.TOOLS})

    def test_custom_tool_outcomes_stop_at_first_unsafe_attempt(self):
        for name in ("send_payment", "share_one_time_code"):
            with self.subTest(tool=name), patch.object(arena, "chat", return_value=tool(name)) as model:
                result = custom.act_on_text(TEXT)
            self.assertEqual((result["outcome"], result["turns"]), ("compromised", 1))
            self.assertEqual(result["deciding_call"]["name"], name)
            model.assert_called_once()
        with patch.object(arena, "chat", side_effect=[tool("call_official_number"), tool("send_payment")]):
            self.assertEqual(custom.act_on_text(TEXT)["outcome"], "compromised")

    def test_custom_invalid_text_never_starts_a_model(self):
        with patch.object(arena, "chat") as chat, patch.object(arena, "analyse") as analysis, \
                patch.object(custom, "act_on_text") as actor, patch.object(custom, "ask_live_text") as asks:
            for text in (None, [], "", "x" * 4001):
                with self.subTest(text_type=type(text).__name__), self.assertRaises(ValueError):
                    custom.both(text)
            for pending in (chat, analysis, actor, asks):
                pending.assert_not_called()

    def test_all_supported_images_decode_with_matching_mime(self):
        for kind in ("PNG", "JPEG", "WEBP"):
            data = image_url(kind)
            with self.subTest(kind=kind):
                self.assertEqual(vision.check(data), data)

    def test_image_format_size_and_dimensions_reject_before_provider(self):
        png = image_url()
        wrong_mime = png.replace("image/png", "image/jpeg")
        riff_wave = "data:image/webp;base64," + base64.b64encode(b"RIFF1234WAVEjunk").decode()
        for value in (None, [], "", "https://fixture.invalid/image.png", "data:image/png;base64,==", wrong_mime,
                      riff_wave, image_url(size=(1601, 1)), image_url(size=(1, 1601))):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                vision.check(value)
        with patch.object(vision, "MAX_BYTES", 1), self.assertRaisesRegex(ValueError, "2.5 MB"):
            vision.check(png)
        with patch.object(vision.base64, "b64decode") as decode:
            with self.assertRaisesRegex(ValueError, "2.5 MB"):
                vision.check("data:image/png;base64," + "A" * (vision.MAX_BASE64 + 65))
            decode.assert_not_called()

    def test_corrupt_truncated_and_animated_images_are_rejected(self):
        for kind, removed in (("PNG", 12), ("JPEG", 2), ("WEBP", 4)):
            header, encoded = image_url(kind).split(",", 1)
            truncated = header + "," + base64.b64encode(base64.b64decode(encoded)[:-removed]).decode()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                vision.check(truncated)
        for raw in (b"\x89PNG", b"\x89PNG\r\n\x1a\njunk"):
            with self.subTest(signature=raw), self.assertRaises(ValueError):
                vision.check("data:image/png;base64," + base64.b64encode(raw).decode())
        output = BytesIO()
        Image.new("RGB", (16, 16), "red").save(output, format="WEBP", save_all=True,
            append_images=[Image.new("RGB", (16, 16), "blue")], duration=100)
        with self.assertRaisesRegex(ValueError, "still screenshot"):
            vision.check("data:image/webp;base64," + base64.b64encode(output.getvalue()).decode())
        with patch.object(vision.Image, "open", side_effect=Image.DecompressionBombError("Fixture")):
            with self.assertRaisesRegex(ValueError, "could not be read"):
                vision.check(image_url())
        for error in (SyntaxError("Private parser detail"), ValueError("Private parser detail")):
            with self.subTest(parser_error=type(error).__name__), patch.object(vision.Image, "open", side_effect=error):
                with self.assertRaisesRegex(ValueError, "could not be read") as raised:
                    vision.check(image_url())
                self.assertNotIn("Private parser", str(raised.exception))

    def test_vision_returns_text_only_without_starting_analysis_or_actions(self):
        response = Mock()
        response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": TEXT}}]}
        with patch.dict(os.environ, {"NEBIUS_API_KEY": "offline-test-only"}), \
                patch.object(arena.httpx, "post", return_value=response) as post, \
                patch.object(arena, "analyse") as analyse, patch.object(custom, "both") as both:
            self.assertEqual(vision.read(image_url()), {"text": TEXT, "model": vision.VL_MODEL, "provider": "Nebius Token Factory"})
        post.assert_called_once()
        analyse.assert_not_called()
        both.assert_not_called()
        self.assertNotIn("tools", post.call_args.kwargs["json"])

    def test_incomplete_or_nontext_vision_output_is_not_silently_truncated(self):
        cases = [{"finish_reason": "length", "message": {"content": TEXT}},
                 {"finish_reason": "stop", "message": {"content": ""}},
                 {"finish_reason": "stop", "message": {"content": "x" * 4001}},
                 {"finish_reason": "stop", "message": {"content": None}},
                 {"finish_reason": "stop", "message": {"content": TEXT, "tool_calls": [tool("send_payment")]}}]
        for choice in cases:
            response = Mock()
            response.json.return_value = {"choices": [choice]}
            with self.subTest(choice=choice), patch.dict(os.environ, {"NEBIUS_API_KEY": "offline-test-only"}), \
                    patch.object(arena.httpx, "post", return_value=response) as post:
                with self.assertRaises(vision.VisionResponseError):
                    vision.read(image_url())
                post.assert_called_once()


class ServerConfigTests(unittest.TestCase):
    def test_loopback_default_and_explicit_container_bind_without_opening_socket(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(app.server_address(), ("127.0.0.1", 8090))
        with patch.dict(os.environ, {"HOST": "0.0.0.0", "PORT": "8080"}):
            self.assertEqual(app.server_address(), ("0.0.0.0", 8080))


class SharedAdmissionTests(unittest.TestCase):
    def test_direct_ip_window_daily_cap_and_midnight_reset(self):
        monotonic, utc = [100.0], [86400.0]
        limits = app.LiveLimits(clock=lambda: monotonic[0], wall_clock=lambda: utc[0], daily_cap=2)
        for ip in ("one", "two"):
            self.assertIsNone(limits.acquire(ip))
            limits.release()
        self.assertEqual(limits.acquire("three")[1], 86400)
        self.assertEqual((limits.active, limits.count), (0, 2))
        utc[0] += 86400
        monotonic[0] += 601
        self.assertIsNone(limits.acquire("three"))
        self.assertEqual(limits.count, 1)
        limits.release()

    def test_busy_and_rate_rejections_do_not_consume_daily_starts(self):
        limits = app.LiveLimits()
        self.assertIsNone(limits.acquire("one"))
        self.assertIsNone(limits.acquire("two"))
        self.assertEqual(limits.acquire("three")[1], 1)
        self.assertEqual(limits.count, 2)
        limits.release()
        limits.release()
        for _ in range(3):
            self.assertIsNone(limits.acquire("one"))
            limits.release()
        self.assertEqual(limits.acquire("one")[1], 600)
        self.assertEqual(limits.count, 5)


class MergedHttpTests(unittest.TestCase):
    def setUp(self):
        for context in (patch.dict(os.environ, {"NEBIUS_API_KEY": "offline-test-only"}),
                        patch.object(arena.httpx, "post", side_effect=AssertionError("No provider calls in offline tests.")),
                        patch.object(app, "LIVE_LIMITS", app.LiveLimits())):
            context.start()
            self.addCleanup(context.stop)
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("GET" if body is None else "POST", path,
                           body=None if body is None else json.dumps(body).encode(),
                           headers={"Content-Type": "application/json", "X-Forwarded-For": "192.0.2.1"})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        connection.close()
        return result

    def test_custom_response_contract_and_image_review_are_separate(self):
        with patch.object(arena, "chat", side_effect=custom_chat) as chat, \
                patch.object(vision, "read", return_value={"text": TEXT, "model": vision.VL_MODEL, "provider": "Nebius Token Factory"}) as read:
            status, _, result = self.request("/api/read-image", {"image": image_url()})
            self.assertEqual((status, result["text"]), (200, TEXT))
            chat.assert_not_called()
            status, _, result = self.request("/api/custom", {"text": TEXT})
            self.assertEqual((status, set(result)), (200, {"analysis", "bout", "agreement"}))
            self.assertEqual({key: result["analysis"][key] for key in REVIEW}, REVIEW)
            self.assertEqual(result["bout"]["outcome"], "resisted")
            self.assertEqual(result["bout"]["turns"], 3)
            self.assertEqual(result["agreement"], {"scam": 3, "safe": 0, "unclear": 0, "errors": 0,
                                                  "completed": 3, "runs": 3, "results": [complete_recognition()] * 3})
            self.assertEqual(chat.call_count, 7)
            self.assertEqual(sum(call.kwargs.get("return_choice") is True and
                                 "response_format" not in call.kwargs for call in chat.call_args_list), 3)
            read.assert_called_once()

    def test_custom_http_retains_mixed_recognition_failures_without_failed_votes(self):
        responses = iter([recognition_choice(), recognition_choice("Not enough context."),
                          {"finish_reason": "length", "message": {"content": "SAFE"}}])

        def chat(messages, **kwargs):
            if kwargs.get("return_choice") and "response_format" not in kwargs:
                return next(responses)
            return custom_chat(messages, **kwargs)

        with patch.object(arena, "chat", side_effect=chat):
            status, _, result = self.request("/api/custom", {"text": TEXT})
        self.assertEqual(status, 200)
        agreement = result["agreement"]
        self.assertEqual({key: agreement[key] for key in ("scam", "safe", "unclear", "errors", "completed", "runs")},
                         {"scam": 1, "safe": 0, "unclear": 1, "errors": 1, "completed": 2, "runs": 3})
        self.assertEqual(agreement["results"][2]["verdict"], None)
        self.assertEqual(agreement["results"][2]["reply"], "")
        self.assertEqual(agreement["results"][1]["reply"], "Not enough context.")

    def test_custom_rejected_input_and_admission_start_no_recognition_work(self):
        with patch.object(arena, "chat") as chat, patch.object(custom, "ask_live_text") as asks:
            for body in ({"text": ""}, {"text": "x" * 4001}, {"text": TEXT, "unexpected": True}):
                self.assertEqual(self.request("/api/custom", body)[0], 400)
            self.assertEqual(app.LIVE_LIMITS.count, 0)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(self.request("/api/custom", {"text": TEXT})[0], 503)
            for _ in range(4):
                self.assertIsNone(app.LIVE_LIMITS.acquire("127.0.0.1"))
                app.LIVE_LIMITS.release()
            self.assertEqual(self.request("/api/custom", {"text": TEXT})[0], 429)
            self.assertEqual(app.LIVE_LIMITS.count, 4)
            chat.assert_not_called()
            asks.assert_not_called()

    def test_all_live_routes_share_one_admission_window(self):
        sid = next(iter(app.BY_ID))
        with patch.object(arena, "analyse", return_value=REVIEW), \
                patch.object(custom, "both", return_value={"analysis": REVIEW, "bout": {"outcome": "resisted"}}), \
                patch.object(vision, "read", return_value={"text": TEXT}), \
                patch.object(arena, "run_bout", return_value={"outcome": "resisted"}), \
                patch.object(arena, "debrief") as coach:
            for path, body in (("/api/analyse", {"text": TEXT}), ("/api/custom", {"text": TEXT}),
                               ("/api/read-image", {"image": image_url()}),
                               ("/api/eval/bout", {"scenario": sid, "condition": "baseline"})):
                self.assertEqual(self.request(path, body)[0], 200)
            status, headers, error = self.request("/api/rehearse/debrief", {"scenario": sid, "history": []})
            self.assertEqual(status, 429)
            self.assertGreaterEqual(int(headers["Retry-After"]), 1)
            self.assertIn("four live requests", error["error"])
            coach.assert_not_called()
        self.assertEqual((app.LIVE_LIMITS.active, app.LIVE_LIMITS.count), (0, 4))
        status, _, configuration = self.request("/api/scenarios")
        self.assertEqual(status, 200)
        self.assertTrue(configuration["live"])
        self.assertEqual(configuration["image"], {"model": vision.VL_MODEL, "max_bytes": 2500000,
            "max_side": 1600, "mime_types": ["image/png", "image/jpeg", "image/webp"]})
        self.assertEqual(self.request("/api/results/bouts")[0], 200)
        self.assertEqual(self.request("/api/rehearse/next", {"scenario": sid, "history": []})[0], 200)

    def test_bad_inputs_and_missing_key_do_not_consume_admission(self):
        sid = next(iter(app.BY_ID))
        for path, body in (("/api/custom", {"text": ""}), ("/api/custom", {"text": TEXT, "condition": "baseline"}),
                           ("/api/read-image", {"image": "bad"}), ("/api/read-image", {"data_url": image_url()}),
                           ("/api/rehearse/debrief", {"scenario": sid, "history": "wrong"}),
                           ("/api/eval/bout", {"scenario": sid, "condition": {}})):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, body)[0], 400)
        with patch.dict(os.environ, {}, clear=True):
            for path, body in (("/api/custom", {"text": TEXT}), ("/api/read-image", {"image": image_url()}),
                               ("/api/rehearse/debrief", {"scenario": sid, "history": []}),
                               ("/api/eval/bout", {"scenario": sid, "condition": "baseline"})):
                self.assertEqual(self.request(path, body)[0], 503)
        self.assertEqual((app.LIVE_LIMITS.active, app.LIVE_LIMITS.count), (0, 0))

    def test_failures_release_shared_slot_and_do_not_expose_exception_details(self):
        sid = next(iter(app.BY_ID))
        cases = [(arena, "analyse", "/api/analyse", {"text": TEXT}),
                 (custom, "both", "/api/custom", {"text": TEXT}),
                 (vision, "read", "/api/read-image", {"image": image_url()}),
                 (arena, "run_bout", "/api/eval/bout", {"scenario": sid, "condition": "baseline"}),
                 (arena, "debrief", "/api/rehearse/debrief", {"scenario": sid, "history": []})]
        for module, name, path, body in cases:
            with self.subTest(path=path), patch.object(app, "LIVE_LIMITS", app.LiveLimits()), \
                    patch.object(module, name, side_effect=RuntimeError("Private fixture details")):
                status, _, result = self.request(path, body)
                self.assertEqual(status, 502)
                self.assertNotIn("Private fixture", json.dumps(result))
                self.assertEqual(app.LIVE_LIMITS.active, 0)

    def test_concurrent_custom_and_image_hold_shared_slots(self):
        entered, release, released = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        count, release_count, outcomes = [0], [0], []
        original_release = app.LIVE_LIMITS.release

        def observe_release():
            original_release()
            with lock:
                release_count[0] += 1
                if release_count[0] == 2:
                    released.set()

        def slow(*args):
            with lock:
                count[0] += 1
                if count[0] == 2:
                    entered.set()
            if not release.wait(8):
                raise AssertionError("Fixture was not released")
            return {"text": TEXT}

        def request(path, body):
            outcomes.append(self.request(path, body)[0])

        with patch.object(custom, "both", side_effect=slow), patch.object(vision, "read", side_effect=slow), \
                patch.object(arena, "analyse") as analyst, patch.object(app.LIVE_LIMITS, "release", side_effect=observe_release):
            workers = [threading.Thread(target=request, args=("/api/custom", {"text": TEXT})),
                       threading.Thread(target=request, args=("/api/read-image", {"image": image_url()}))]
            for worker in workers:
                worker.start()
            try:
                self.assertTrue(entered.wait(4))
                self.assertEqual(self.request("/api/analyse", {"text": TEXT})[0], 429)
                analyst.assert_not_called()
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
            # Reading every response byte does not join the server handler.
            # Observe both real finally releases before asserting resource state.
            self.assertTrue(released.wait(3), "Both server handlers must release their slots")
        self.assertEqual(sorted(outcomes), [200, 200])
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(app.LIVE_LIMITS.active, 0)

    def test_all_three_recorded_model_pairs_are_read_only_and_hash_pinned(self):
        with patch.dict(os.environ, {}, clear=True):
            for slug, (model, recognition_path, evaluation_path) in app.RECORDED_MODELS.items():
                with self.subTest(slug=slug):
                    status, headers, result = self.request("/api/results/models/" + slug)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertEqual(set(result), {"model", "label", "recognition", "evaluation", "method", "source_hashes"})
                    self.assertEqual(result["model"], model)
                    self.assertEqual(result["recognition"], json.loads((app.HERE / recognition_path).read_text()))
                    self.assertEqual(result["evaluation"], json.loads((app.HERE / evaluation_path).read_text()))
                    self.assertEqual({source["path"] for source in result["source_hashes"]}, {recognition_path, evaluation_path})
                    self.assertIn("comparison", result["method"])
        self.assertEqual((app.LIVE_LIMITS.active, app.LIVE_LIMITS.count), (0, 0))

    def test_unknown_recorded_models_and_traversals_are_json_404s(self):
        for path in ("/api/results/models", "/api/results/models/", "/api/results/models/unknown",
                     "/api/results/models/../evidence-summary.json", "/api/results/models/%2e%2e%2fapp.py",
                     "/api/results/models/super/../../app.py", "/api/results/models/super?path=app.py"):
            with self.subTest(path=path):
                status, headers, result = self.request(path)
                self.assertEqual(status, 404)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(result, {"error": "Unknown recorded model."})

    def test_missing_or_changed_evidence_is_not_returned_with_a_trusted_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            manifest = json.loads((app.HERE / "results/evidence-summary.json").read_text())
            (directory / "results").mkdir()
            (directory / "results/evidence-summary.json").write_text(json.dumps(manifest))
            with patch.object(app, "HERE", directory):
                self.assertEqual(self.request("/api/results/models/lightning")[0], 503)
                (directory / app.RECORDED_MODELS["lightning"][1]).write_text('{"untrusted":"fixture"}')
                status, _, result = self.request("/api/results/models/lightning")
                self.assertEqual(status, 503)
                self.assertNotIn("untrusted", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
