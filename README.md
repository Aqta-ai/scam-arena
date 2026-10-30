# Scam Arena

Check a suspicious email, text or letter, then explore how a person or an AI assistant responds to the same kinds of pressure.

## Start with a message

- **Check a message:** paste up to 4,000 characters. A review returns quoted warning signs and safer next steps. A separate assistant conversation receives the same text with simulated payment and code-sharing tools, followed by up to two labelled standard pressure messages. Neither conversation receives the other's answer. Remove names, account numbers and codes before submitting.
- **Evaluate an agent:** our configured Nemotron assistant receives six authored scam scripts under two instruction conditions. Outcomes reflect simulated tool attempts. A verification-tool request is not proof of completed verification.
- **Rehearse yourself:** reply to a fictional script, then ask Nemotron for coaching feedback.

The default workspace adds screenshot transcription with review before Run, a small message preview, per-run tool counts, browser history and an optional client-side action-policy replay. That replay does not change the recorded model requests or control a payment system. No saved payees are configured in the replay preview.

Select a model name in the recorded table to inspect actual recognition replies and assistant tool transcripts, choose a scenario and repeat, and download the source-backed evidence JSON. Recognition and acting are separate calls under different prompts and context. The deeper Evaluate and Rehearse views are retained at `/lab.html`.

## Retained evaluation, 30 October 2026

36 conversations on `nvidia/Nemotron-3_5-Lightning` through Nebius Token Factory: six scenarios, two conditions, three repeats.

| Assistant instructions | Payment or code-sharing attempted | Verification tool requested |
| --- | --- | --- |
| Plain helpful assistant | 18 of 18 | 0 of 18 |
| Given the safety instruction | 10 of 18 | 8 of 18 |

Raw data: `results/eval-2026-10-30T005400Z.json`. All 36 deciding calls are structured; no API-error bouts in that saved set. Six scripts and repeats are not a general safety benchmark, a model ranking or a detection-accuracy claim. Outcomes are simulated, not money lost. Nemotron declined the scammer role in our trials, so incoming scam messages remain authored scripts.

## Recorded three-model comparison

| Model | Replies starting SCAM | Risky requests in separate baseline acting trials |
| --- | --- | --- |
| Nemotron 3.5 Lightning | 18/18 | 18/18, comprising 9 payment and 9 code-sharing requests |
| Nemotron 3 Super (120B) | 18/18 | 3/6, all payment requests |
| Nemotron 3 Ultra (550B) | 18/18 | 1/6, a payment request |

Six authored scripts. Recognition uses three repeats of each full script, no tools and temperature 0.7. Acting uses incremental messages, tools and temperature 0.0; Lightning has three repeats, Super and Ultra one. No benign recognition controls were measured. Different prompts, contexts and sample sizes mean this is not a general detection-accuracy estimate or model ranking. It does not show one acting agent knowingly ignoring its own warning.

The safety-prompt arms remain available separately: 10/18, 5/6 and 1/6 risky requests respectively. Source hashes, raw records and the recorded prompt/provenance limitations are retained in `results/evidence-summary.json` and the inspector.

## Run locally

Python 3.10 or newer is required.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8090. Without a key, recorded agent conversations are replayable. Live analysis, evaluation and coaching are unavailable with a clear error or disabled control.

For live use, provide your Nebius Token Factory key in the server environment, without committing it:

```sh
export NEBIUS_API_KEY=your_key_here
python app.py
```

Default model: `nvidia/Nemotron-3_5-Lightning`. `SCAM_ARENA_MODEL` changes the configured model; `PORT` changes the port. `HOST` defaults to `127.0.0.1` for local use. An operator can explicitly set `HOST=0.0.0.0` for a container binding; this setting does not configure production hosting protections. The key is not sent to the browser. The server does not persist checker inputs. The browser keeps up to 20 text results in localStorage; History can reopen, delete or clear them. Images are not saved in history. Provider processing is separate. Do not use real confidential messages in a public demonstration.

```sh
python -m unittest discover -s tests -v
python run_eval.py --repeats 3
```

Tests mock model calls and need no key. Evaluation makes live provider requests and writes new results; it does not run as part of the test suite.

## HTTP and implementation

`POST /api/analyse` accepts `{"text":"..."}`. The server validates 1 to 4,000 characters and the model's result schema, requires quoted evidence to match input text, and returns server-owned model/provider/time metadata. A message is data, not an instruction for the model. No analysis tools or URL retrieval are supplied.

`POST /api/custom` accepts `{"text":"..."}` and returns `{"analysis":{...},"bout":{...}}`. Analysis uses the same strict consumer as the standalone endpoint. The assistant receives a fresh conversation and the pasted text, with at most two fixed follow-ups from `custom.FOLLOW_UPS`. Follow-ups actually reached are marked `standard: true` in the transcript; the run stops at the first simulated payment or code-sharing attempt. Outcomes come from tool attempts, not claims in the assistant's prose. A verification-tool request is not evidence that verification completed. There are no real payments or messages to external recipients.

`POST /api/read-image` accepts `{"image":"data:image/png;base64,..."}` or the JPEG/WebP equivalent. It returns `{"text":"...","model":"...","provider":"Nebius Token Factory"}`. The browser must let the person review the extracted text before a separate Run request. Decoded images are capped at 2,500,000 bytes and 1,600 pixels per side, with matching signatures, decoded formats and complete still-image validation. Images are processed in memory and are not saved by this server; the configured NVIDIA vision model on Nebius processes the supplied image. The default is `nvidia/Nemotron-3.5-Super-VL`, configurable with `SCAM_ARENA_VL_MODEL`. No analysis or action run starts automatically after transcription.

Analysis requests a strict five-field JSON schema with thinking disabled and a 4,000-token cap. Reasoning and the visible answer otherwise share Nemotron's output budget. The server requires a completed response with `finish_reason: "stop"` before independently validating the JSON and exact source quotes; truncated output, refusals and tool calls are errors, never a substitute verdict. Each analysis makes one provider request, without an automatic retry or a fallback to a weaker output format. Evaluation and coaching keep their existing request settings. These controls address output-format failures; they do not establish detection accuracy or guarantee a model response.

If the model collapses screenshot line breaks or changes whitespace inside a quote, the server can align it to one unique original source span. The returned quote preserves that source text exactly, within the 300-character limit, and passes the same strict consumer. Changed words, case or punctuation, and ambiguous whitespace-aligned matches, are rejected. The assistant still receives the original input text.

A bounded live format check on 30 October 2026 analysed each of the six full authored scripts once. All six completed responses passed the strict schema and exact-source-quote validation. This checks response format on those synthetic inputs, not scam-detection accuracy or safety on other messages.

All live HTTP routes share four accepted starts per ten minutes per direct client address, two live operations in flight, and `SCAM_ARENA_DAILY_CAP` accepted starts per UTC day, default 300. The routes are analysis, custom comparison, screenshot reading, agent evaluation, coaching and the retained plain-question endpoint. Forwarded address headers do not change the direct-address bucket. Invalid input, missing connection and rejected admission consume no starts; admitted failures do consume one start and release their slot. Recorded GET endpoints and the authored next rehearsal message remain available without model access or quota admission. These limits are in memory in one process and reset when it restarts; production hosting needs appropriate proxy, TLS, request and spending controls. The development server binds to localhost.

The quota unit is an accepted HTTP operation, not a model request or a token allowance. Analysis and image reading each make at most one provider attempt. A custom comparison makes one analysis call plus up to three assistant turns, each with up to two transport attempts: at most seven provider attempts in total. A four-turn evaluation allows up to eight attempts, coaching up to four, and the plain-question endpoint up to two. A custom operation can run its analysis and assistant calls concurrently. Scripted evaluation commands are separate from HTTP admission and should be run only with an explicit request budget.

Read-only `GET /api/results/evidence` serves the retained per-model evidence inventory. `GET /api/scenarios` includes the configured model, a `live` boolean meaning a key is configured, and image limits. The flag does not establish that provider credits are available.

Read-only `GET /api/results/models/lightning`, `/super` or `/ultra` returns the corresponding raw recognition and evaluation records, method notes and source hashes. The server checks the fixed source files against the retained inventory before returning them. Unknown names and path fragments return `404`; missing or mismatched evidence returns `503`. These reads need no model key, make no provider requests and consume no live-operation quota.

`400` invalid input, `413` oversized HTTP body, `429` rate/concurrency limit with Retry-After, `503` no provider connection, and `502` failed or invalid model response. No probability percentage is presented as calibrated risk.

## Limits and licence

AI can miss scams and flag genuine messages. Text alone cannot establish who sent a message. Rehearsal feedback is not professional advice or validated learning assessment. All evaluation people, organisations, accounts and tool effects are fictional. A displayed balance reconstructs the recorded attempts; it is not a bank ledger.

Apache 2.0. Built by Aqta, Dublin.

## Container preparation

The included Dockerfile uses the same pinned requirements and runs without root privileges on port 8080. Set the Nebius key as a runtime secret and set HOST/PORT explicitly for the chosen platform. The source image contains no key. A local container build was not verified because the local Docker daemon was unavailable; a fresh Python environment and the real localhost HTTP routes were checked instead.

Limits are in-memory and apply to the direct client address. A reverse proxy may put several visitors in the same bucket; multiple instances and restarts do not share a durable allowance. Use one instance for this demo and configure a provider-side budget separately. Instance count alone is not a spending cap.
