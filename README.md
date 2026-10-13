# Vivek Job Automation — Phases 1, 2, 3, and 4

The local auto-apply project consumes jobs from the existing alerts queue through `worker/sync_queue.py`; the separate `vivek-job-alert` project remains an input source. The local Playwright worker validates/deduplicates queue entries, analyzes forms, fills only confident values from a local applicant profile, and pauses for human verification or unresolved answers.

## Architecture

```text
alerts queue → local JSON queue → identity/filter/recovery
     → Playwright worker → form analysis/fill → human pause + optional Telegram notice
                                  → ready / manual submit or guarded submit (off by default)
                                  → local application tracker
```

Queue persistence, stable identity, status protections, scoring, retries, and crash recovery remain in `worker/job_queue.py`, `worker/sync_queue.py`, and `worker/tracker.py`.

## Applicant profile

`worker/applicant.py:load_applicant_profile()` reads `profile/applicant.json`. The local file contains only information already configured in this project. It is ignored by the existing `profile/*` Git ignore rule. Add only verified answers. A missing or invalid `resume_path` remains unset; the worker marks resume fields for user attention and does not invent a path.

The Chromium profile also lives under `profile/`. Keep browser state and applicant information private. Do not add passwords, cookies, OTPs, API tokens, or authentication details to source/configuration.

## Form analysis and safe filling

`worker/form_adapter.py:identify_application_form()` first scores each form using form-local labels, attributes, headings, buttons, and applicant-field combinations. Newsletter, subscribe, contact, search, login, registration, feedback, support, and comment forms are rejected. A generic email or phone field is never sufficient. The worker selects one positively identified form only; ambiguous candidates stop as `NeedsUserInput`, and pages without an application form stop as `UNSUPPORTED_APPLICATION_FLOW`.

All field analysis, deterministic filling, AI fallback field selection, validity checks, and submit-button detection are scoped to the selected form. On a listing page, the worker follows only one uniquely labeled application link (`Apply`, `Apply Now`, `Apply for this position`, or an explicit application-form label); otherwise it stops. The original job URL remains on the job record and the resolved `application_url` is stored separately in queue/tracker metadata. Form controls are then classified using associated label, placeholder, name, ID, ARIA label, type, required state, and select options. The adapter supports text inputs, textareas, select controls, radio groups, checkboxes, and file inputs.

This is a conservative generic detector, not a guarantee that every employer page can be safely automated. If the page structure is uncertain, the worker must stop for a person rather than infer an application form.

## Navigation host authorization

`worker/navigation.py` is the project-level navigation guard. `config.json` has an explicit `navigation.allowed_hosts` list, empty by default. Add each approved DNS hostname as an exact entry; a `www` hostname must be listed separately. The worker checks queued URLs and application destinations before navigation, installs a Playwright browser-context request route that aborts unapproved HTTP(S) requests (including redirect requests), and checks the final page URL before form inspection. If the route cannot be installed, the worker fails closed. Local file/HTTP fixture access is available only through the explicit test-only `NavigationPolicy.for_local_tests()` helper and is not enabled by production configuration.

The route policy is host-based: every HTTP(S) subresource host must also be allowed, so some sites may need additional exact host entries. Redirect blocking is covered with a local route mock; this suite does not contact real hosts or perform an external redirect integration test. The earlier `unauthorized_host` message came from an unidentified external guard and is not claimed to be fixed here.

Only explicit profile values are filled. Existing values are preserved. Select/radio choices require an exact match; checkboxes require an explicit boolean. Legal/work-authorization, sponsorship, salary, education, notice-period, skills, and other absent answers become `needs_user_input`; the worker does not guess. Resume upload requires a configured existing file. Optional empty fields are reported without blocking unless they need user attention (for example, a resume field with no configured file).

Per-field results are stored in the tracker without storing entered form values. Required/unresolved fields are listed in `missing_fields`.

## Human verification and resuming

`worker/detector.py:detect_verification()` recognizes CAPTCHA/reCAPTCHA/hCaptcha, OTP/code prompts, email/phone verification, login approval, Cloudflare/browser challenges, and security-check text/markers. It only detects signals; it never solves or bypasses them.

When a gate is detected, the queue and tracker are set to `WaitingForHuman` with `waiting_reason`, `detected_at`, `current_url`, queue ID/job key, and a `last_wait_event`. The same visible browser page remains open. The worker polls for the gate to clear, then records `resumed_at` and continues. Required unanswered fields use `NeedsUserInput` and similarly wait for the person to complete them. If the process restarts, the worker prioritizes persisted waiting jobs and resumes their saved page URL.

### Telegram human-intervention notifications (Phase 3)

The worker can use the official Telegram Bot HTTP API to notify you when it pauses for CAPTCHA, OTP, email/phone verification, login approval, browser/security challenges, or required user input. Telegram is a notification channel only: it cannot solve or bypass verification, approve a submission, or submit an application.

Set these environment variables in the same shell that starts the worker:

```powershell
$env:TELEGRAM_ENABLED = "true"
$env:TELEGRAM_BOT_TOKEN = "<your_bot_token>"
$env:TELEGRAM_CHAT_ID = "<your_chat_id>"
```

Create a bot with Telegram's BotFather, open a conversation with that bot, and send `/start`. Obtain the destination `chat_id` from the `message.chat.id` field in the Bot API update for that conversation, using a trusted client. Keep the bot token private and do not paste it into source, config, issue reports, or logs. These values are read only from environment variables; `.env` files are not loaded by the worker.

`TELEGRAM_ENABLED` must be `true` (case-insensitive) and both credentials must be present before messages are sent. If disabled or not configured, the worker logs that Telegram is unavailable and continues with the existing browser/console workflow. Telegram HTTP/API errors, invalid credentials/chat IDs, timeouts, and connection errors return safe result codes and do not fail the application attempt. The token is never included in application or queue metadata.

Each human-intervention event is persisted with a waiting event ID and notification attempt/result metadata. The worker sends at most one notification attempt for the same waiting event, including after restart. When verification clears, the worker rechecks the page and form through the existing Phase 2 logic, changes the queue/tracker state back to `Processing`, and continues. A later distinct human gate can notify again. Ready-for-manual-submission and final submitted/failed notices are also sent once per queue record when Telegram is configured.

The queue and tracker can recover a `WaitingForHuman` job after restart and suppress a repeat notification for that event. A browser process restart closes the previous live page; the persistent Chromium profile retains browser storage, and the worker reopens the saved URL, but a site's in-progress page state or verification session may expire or restart. The worker re-detects the page state and waits for human action again when needed; it cannot guarantee that every site preserves the exact pre-restart form state.

## AI browser fallback (Phase 4)

Phase 2 remains the primary form handler. The opt-in Phase 4 fallback runs only when deterministic form handling finds no controls or an unresolved required field that Phase 2 could not classify. It does not run for forms Phase 2 already handles or for recognized sensitive/legal questions. If AI is disabled or unavailable, the existing manual/human-intervention path remains in control.

The provider interface is in `worker/ai/base.py`; the initial provider in `worker/ai/provider.py` uses OpenAI Chat Completions structured JSON output. The constrained executor is `worker/ai/browser_agent.py`. It receives a scrubbed snapshot of visible labels, control metadata, option labels, button labels, headings, and the current host/domain. It does not send applicant profile values, field contents, cookies, browser storage, passwords, or authentication tokens. Provider calls may transmit that limited page metadata to OpenAI when explicitly enabled. API usage and cost depend on your provider/model; the worker makes no AI requests in default configuration.

The model can classify unknown controls, but the local executor validates every response and chooses values only from the local applicant profile. It rejects arbitrary selectors, code, URLs, unrecognized actions, sensitive classifications, and unexpected domain changes. Profile-backed select options must match both the model's option label and the local profile value. The only AI click target allowed is a visible `type=button` with a small exact safe-navigation label (`Next`, `Continue`, `Save and continue`, `Add another`, or `Add experience`). AI never clicks submit. After any AI action, Phase 2 re-analyzes the form and validates required fields and browser validity; the normal `AUTO_SUBMIT` guard still applies.

Configure AI only through environment variables in the worker's launch shell:

```powershell
$env:AI_AGENT_ENABLED = "true"
$env:AI_PROVIDER = "openai"
$env:AI_MODEL = "<a model enabled for your API account>"
$env:AI_API_KEY = "<your API key>"
$env:AI_MIN_CONFIDENCE = "0.90"
$env:AI_MAX_ACTIONS_PER_JOB = "50"
$env:AI_MAX_PAGE_ANALYSIS = "3"
$env:AI_MAX_RETRIES = "0"
$env:AI_TIMEOUT_SECONDS = "20"
```

AI defaults to disabled. Limits are clamped to safe ranges: confidence 0.50–1.0, actions 1–100, page analyses 1–10, retries 0–2, and timeout 3–60 seconds. Retries are only for timeout, network, and rate-limit errors. Identical page snapshots are not analyzed repeatedly. Logs include provider, host, action category, confidence, and outcome, but not entered values or credentials. AI uncertainty, provider failure, or suspected human verification routes through the existing `NeedsUserInput`/`WaitingForHuman` callbacks and Phase 3 Telegram integration when configured. CAPTCHA, OTP, login approval, security challenges, and anti-bot checks are never solved or bypassed.

AI can classify safe profile fields such as name, email, phone, location, resume, links, skills, experience, education, and cover-letter fields. The executor only acts on controls Phase 2 classified as unknown. Work authorization, sponsorship, salary, criminal history, demographic information, legal questions, and notice period require a person; no answer is invented. Current action whitelist: fill, exact-match select, explicit-boolean check/uncheck, configured local resume upload, constrained safe navigation click, bounded scroll, ignore, or request human input. There is no arbitrary JavaScript or AI-generated code execution.

Run mocked Phase 4 tests with the normal suite:

```powershell
python -m unittest discover -s tests -v
```

Optional manual provider smoke test (only if AI is enabled and credentials are configured):

```powershell
python -m worker.ai_test
```

That command opens only local `test_application.html`, sends the scrubbed fixture snapshot to the configured provider, executes zero browser actions, and never submits. If provider configuration is absent it reports `AI live test skipped — credentials/configuration not configured.` Automated tests never contact the AI provider.

## Submission control and result tracking

`config.json` contains `"auto_submit": false`. Missing, malformed, or non-boolean configuration fails closed. With this default, the worker fills and validates the form, records `ReadyForSubmission`, and does not click submit; a person may submit in the open browser. A submit click is blocked on non-local pages even if the option is passed as true; opt-in click behavior is restricted to `file:` fixtures, loopback addresses, and reserved `.invalid` test hosts. Success/failure is detected from the local fixture selector, conservative confirmation URL/text signals, or clear submission-error text.

Setting `auto_submit` to `true` is an explicit opt-in. Even then the worker rechecks that no verification is active, no fields need user input, the form passes browser validity, and a submit control is identifiable before clicking. Automated submission has only been tested against local/mock forms. Keep it false unless you intentionally authorize and validate a supported site flow.

Tracker records include stable job identity, queue ID, status, form field results, missing fields, submission setting, failure reason, timestamps, and concise last-failure details. Existing history is retained.

## Statuses

Queue statuses: `Queued`, `Started`, `Processing`, `WaitingForHuman`, `NeedsUserInput`, `ReadyForSubmission`, `Retry`, `Submitted`, `Skipped`, `Failed`. Existing Phase 1 legacy statuses remain readable. Waiting states are not reset by sync or stale-job retry recovery.

## Run locally

Use the verified Python 3.12 environment in `.venv-clean`. The older `.venv` and
`.venv-1` environments are retained for diagnosis but currently fail to launch
with `Access is denied`; their exact cause has not been determined.

PowerShell:

```powershell
Set-Location 'D:\project\JOB\vivek-job-automation'
.\.venv-clean\Scripts\Activate.ps1
python -m worker.sync_queue
python -m worker.runner
```

CMD:

```bat
cd /d D:\project\JOB\vivek-job-automation
.venv-clean\Scripts\activate.bat
python -m worker.sync_queue
python -m worker.runner
```

Sync performs the pre-existing `git pull --ff-only` in the separate alerts checkout. It is not necessary when testing with local queue data. To start only the local worker against the current queue, run `python -m worker.runner` without running sync.

Run tests from the clean environment:

```powershell
.\.venv-clean\Scripts\python.exe -m unittest discover -s tests -v
```

Or, after activation:

```powershell
python -m unittest discover -s tests -v
```

The test suite uses temporary JSON files, exact-byte guards for the persistent queue/tracker files, mocked pages, and the local `test_application.html`. Phase 6 includes a Chromium-backed local worker/AI-mock/human-pause/restart/tracker integration test. It does not visit employer sites. Browser-backed tests require the project Playwright package and installed Chromium.

## Tests and security

Run all local/mock tests:

```powershell
python -m unittest discover -s tests -v
```

Telegram tests mock the HTTP request and never send a live message. The local worker tests use temporary queue/tracker files and local pages/mocks; they do not visit employer sites or submit real applications. Browser-backed fixture tests require a runnable project Python environment with Playwright and Chromium/Chrome.

Keep `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, passwords, cookies, OTPs, and authentication data out of source, `config.json`, tracker/queue files, logs, screenshots, HTML snapshots, and Git. `.env` and `.env.*`, the `secrets/` directory, local logs, and the persistent `profile/` are ignored by Git. Do not commit a real bot token.

## Known limitations

- Generic form matching cannot support every employer ATS; unsupported forms fail closed rather than clicking ambiguous controls.
- A person must answer unresolved/legal fields and complete verification in the open browser. Telegram delivery requires the opt-in environment variables above.
- Result signals are conservative but site-specific confirmation states may need an adapter.
- `.venv` and `.venv-1` were created from Python 3.14.4 and still fail with `Access is denied`; the exact cause is not determined. Use the working `.venv-clean` environment with Python 3.12.14 and Playwright 1.55.0.
- Phase 2 does not add an AI/browser-agent framework. CAPTCHA/OTP/anti-bot challenges are never solved or bypassed.
- Phase 3 does not add Telegram reply controls, automatic verification, CAPTCHA solving, or AI/browser-agent behavior.
