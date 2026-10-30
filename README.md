# Scam Arena

Check a suspicious email, text or letter, then explore how a person or an AI assistant responds to the same kinds of pressure.

## Three tools

- **Check a message:** paste up to 4,000 characters. NVIDIA Nemotron on Nebius returns quoted warning signs, an explanation, safer next steps and an optional reply. It does not open links, contact anyone or execute payment tools. Remove names, account numbers and codes before submitting.
- **Evaluate an agent:** our configured Nemotron assistant receives six authored scam scripts under two instruction conditions. Outcomes reflect simulated tool attempts. A verification-tool request is not proof of completed verification.
- **Rehearse yourself:** reply to a fictional script, then ask Nemotron for coaching feedback.

The message checker is a separate feature from the retained agent evaluation below.

## Retained evaluation, 30 October 2026

36 conversations on `nvidia/Nemotron-3_5-Lightning` through Nebius Token Factory: six scenarios, two conditions, three repeats.

| Assistant instructions | Payment or code-sharing attempted | Verification tool requested |
| --- | --- | --- |
| Plain helpful assistant | 18 of 18 | 0 of 18 |
| Given the safety instruction | 10 of 18 | 8 of 18 |

Raw data: `results/eval-2026-10-30T005400Z.json`. All 36deciding calls are structured; no API-error bouts in that saved set. Six scripts and repeats are not a general safety benchmark, a model ranking or a detection-accuracy claim. Outcomes are simulated, not money lost. Nemotron declined the scammer role in our trials, so incoming scam messages remain authored scripts.

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

Default model: `nvidia/Nemotron-3_5-Lightning`. `SCAM_ARENA_MODEL` changes the configured model; `PORT` changes the local port. The key is not sent to the browser. The application does not save checker inputs; provider processing is separate. Do not use real confidential messages in a public demonstration.

```sh
python -m unittest discover -s tests -v
python run_eval.py --repeats 3
```

Tests mock model calls and need no key. Evaluation makes live provider requests and writes new results; it does not run as part of the test suite.

## HTTP and implementation

`POST /api/analyse` accepts `{"text":"..."}`. The server validates 1 to 4,000 characters and the model's result schema, requires quoted evidence to match input text, and returns server-owned model/provider/time metadata. A message is data, not an instruction for the model. No analysis tools or URL retrieval are supplied.

Analysis is limited to five accepted starts per minute per direct client address and two in flight in this single process. Production hosting needs appropriate proxy, TLS, request and spending controls. The current development server binds to localhost. Existing evaluation/coaching endpoints are not covered by the new analysis limiter.

`400` invalid input, `413` oversized HTTP body, `429` rate/concurrency limit with Retry-After, `503` no provider connection, and `502` failed or invalid model response. No probability percentage is presented as calibrated risk.

## Limits and licence

AI can miss scams and flag genuine messages. Text alone cannot establish who sent a message. Rehearsal feedback is not professional advice or validated learning assessment. All evaluation people, organisations, accounts and tool effects are fictional. A displayed balance reconstructs the recorded attempts; it is not a bank ledger.

Apache 2.0. Built by Aqta, Dublin.
