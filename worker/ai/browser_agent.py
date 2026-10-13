"""Constrained AI fallback: models classify page metadata; local code owns actions."""

import hashlib
import re
from urllib.parse import urlsplit

from worker.ai.base import AIProviderError
from worker.form_adapter import APPLICANT_KEYS, analyze_form, identify_application_form, normalize
from worker.detector import detect_verification


_SENSITIVE = {"work_authorization", "sponsorship", "salary", "criminal_history",
              "demographic_information", "legal_questions", "notice_period"}
_ALLOWED_CLASSIFICATIONS = set(APPLICANT_KEYS) | {"unknown", "sensitive"}
_ACTIONS = {"fill", "select", "check", "uncheck", "upload", "click", "scroll",
            "ignore", "needs_user_input"}
_SAFE_BUTTONS = {"next", "continue", "save and continue", "add another", "add experience"}
_SECRET = re.compile(r"(?i)(?:[\w.+-]+@[\w.-]+\.[a-z]{2,}|\+?\d[\d ()-]{7,}\d|\b(?:token|password|secret|authorization)\b\s*[:=]\s*\S+)")
_SENSITIVE_HINT = re.compile(
    r"(?i)work authorization|authorized to work|sponsor|visa|immigration|salary|compensation|"
    r"criminal|conviction|ethnicity|race|gender|disability|demographic|legal|notice period|"
    r"date of birth|birth date|social security|national id"
)


def _scrub(value, limit=180):
    value = _SECRET.sub("[redacted]", str(value or ""))
    return re.sub(r"\s+", " ", value).strip()[:limit]


class BrowserAgent:
    def __init__(self, provider, settings, job_id=None):
        self.provider = provider
        self.settings = settings
        self.job_id = _scrub(job_id, 80) if job_id else "unknown"
        self.action_count = 0
        self.analysis_count = 0
        self._seen = set()
        self.action_log = []

    def _snapshot(self, page, fields, form=None):
        host = urlsplit(str(getattr(page, "url", ""))).hostname or ""
        field_rows = []
        for index, field in enumerate(fields):
            field_rows.append({
                "target_ref": f"field:{index}", "tag": _scrub(field.get("tag"), 20),
                "type": _scrub(field.get("type"), 20),
                "label": _scrub(field.get("label")), "name": _scrub(field.get("name"), 80),
                "id": _scrub(field.get("id"), 80), "placeholder": _scrub(field.get("placeholder")),
                "aria_label": _scrub(field.get("aria_label")), "required": bool(field.get("required")),
                "has_value": bool(str(field.get("value") or "").strip()),
                "options": [_scrub(option.get("label")) for option in field.get("options", [])][:40],
            })
        buttons = []
        locator = (form or page).locator("button, input[type='button']")
        for index in range(min(locator.count(), 40)):
            button = locator.nth(index)
            try:
                if button.is_visible():
                    buttons.append({"target_ref": f"button:{index}",
                                    "label": _scrub(button.inner_text() or button.get_attribute("value")),
                                    "type": _scrub(button.get_attribute("type") or "unspecified", 20)})
            except Exception:
                continue
        headings = []
        for selector in ("h1", "h2", "h3"):
            locator = page.locator(selector)
            for index in range(min(locator.count(), 10)):
                try:
                    headings.append(_scrub(locator.nth(index).inner_text()))
                except Exception:
                    pass
        return {"domain": host, "headings": headings[:20], "fields": field_rows, "buttons": buttons}

    @staticmethod
    def _validate_plan(plan, snapshot):
        if not isinstance(plan, dict) or set(plan) != {"page_type", "confidence", "human_verification", "ready_for_validation", "fields"}:
            raise AIProviderError("AI_MALFORMED_RESPONSE")
        if plan["page_type"] not in {"job_application", "other"}:
            raise AIProviderError("AI_MALFORMED_RESPONSE")
        for flag in ("human_verification", "ready_for_validation"):
            if not isinstance(plan[flag], bool):
                raise AIProviderError("AI_MALFORMED_RESPONSE")
        if not isinstance(plan["confidence"], (float, int)) or isinstance(plan["confidence"], bool) or not 0 <= plan["confidence"] <= 1:
            raise AIProviderError("AI_MALFORMED_RESPONSE")
        refs = {item["target_ref"] for item in snapshot["fields"] + snapshot["buttons"]}
        if not isinstance(plan["fields"], list):
            raise AIProviderError("AI_MALFORMED_RESPONSE")
        if len(plan["fields"]) > 100:
            raise AIProviderError("AI_MALFORMED_RESPONSE")
        seen_targets = set()
        for item in plan["fields"]:
            required = {"target_ref", "classification", "confidence", "action", "option_label", "scroll_amount"}
            if not isinstance(item, dict) or set(item) != required or item["target_ref"] not in refs:
                raise AIProviderError("AI_MALFORMED_RESPONSE")
            if item["target_ref"] in seen_targets:
                raise AIProviderError("AI_MALFORMED_RESPONSE")
            seen_targets.add(item["target_ref"])
            if item["classification"] not in _ALLOWED_CLASSIFICATIONS or item["action"] not in _ACTIONS:
                raise AIProviderError("AI_MALFORMED_RESPONSE")
            if not isinstance(item["confidence"], (float, int)) or isinstance(item["confidence"], bool) or not 0 <= item["confidence"] <= 1:
                raise AIProviderError("AI_MALFORMED_RESPONSE")
            if item["option_label"] is not None and not isinstance(item["option_label"], str):
                raise AIProviderError("AI_MALFORMED_RESPONSE")
            if item["scroll_amount"] is not None and (not isinstance(item["scroll_amount"], int) or isinstance(item["scroll_amount"], bool)):
                raise AIProviderError("AI_MALFORMED_RESPONSE")
        return plan

    def assist(self, page, applicant, *, required_unknown=False, unsupported=False, form=None):
        """Return structured status; this method never submits a form."""
        gate = detect_verification(page)
        if gate:
            return {"status": "WaitingForHuman", "reason": gate}
        selection = identify_application_form(page) if form is None else {"status": "Selected", "form": form}
        if selection.get("status") != "Selected":
            return {"status": "NeedsUserInput", "reason": selection.get("reason", "UNSUPPORTED_APPLICATION_FLOW")}
        form = selection["form"]
        fields = analyze_form(page, form=form)
        snapshot = self._snapshot(page, fields, form=form)
        fingerprint = hashlib.sha256(repr(snapshot).encode("utf-8")).hexdigest()
        if fingerprint in self._seen:
            return {"status": "NeedsUserInput", "reason": "AI_NO_PROGRESS"}
        if self.analysis_count >= self.settings.max_page_analysis:
            return {"status": "NeedsUserInput", "reason": "AI_ANALYSIS_LIMIT"}
        self._seen.add(fingerprint)
        self.analysis_count += 1
        provider_name = _scrub(getattr(self.provider, "name", "unknown"), 40)
        model_name = _scrub(getattr(self.provider, "model", "unknown"), 80)
        print(f"[AI] Job={self.job_id}; analysis {self.analysis_count + 1}/{self.settings.max_page_analysis}; "
              f"provider={provider_name}; model={model_name}; domain={snapshot['domain']}")
        try:
            attempts = 0
            while True:
                try:
                    response = self.provider.analyze_page(snapshot)
                    break
                except AIProviderError as exc:
                    if attempts >= self.settings.max_retries or exc.code not in {
                        "AI_TIMEOUT", "AI_NETWORK_ERROR", "AI_RATE_LIMIT",
                    }:
                        raise
                    attempts += 1
            plan = self._validate_plan(response, snapshot)
        except AIProviderError as exc:
            print(f"[AI] Provider failure; reason={exc.code}")
            return {"status": "NeedsUserInput", "reason": exc.code}
        except Exception:
            print("[AI] Provider failure; reason=AI_PROVIDER_ERROR")
            return {"status": "NeedsUserInput", "reason": "AI_PROVIDER_ERROR"}
        detected_gate = detect_verification(page)
        if detected_gate:
            return {"status": "WaitingForHuman", "reason": detected_gate}
        if plan["human_verification"]:
            # A model suspicion alone is not a detector signal; pause through the
            # ordinary user-input gate until the page visibly changes or closes.
            return {"status": "NeedsUserInput", "reason": "AI_SUSPECTED_HUMAN_VERIFICATION"}
        if plan["page_type"] != "job_application" or plan["confidence"] < self.settings.min_confidence:
            return {"status": "NeedsUserInput", "reason": "AI_LOW_CONFIDENCE"}

        field_locators = {f"field:{i}": (field, field["locator"]) for i, field in enumerate(fields)}
        button_locators = {}
        buttons = form.locator("button, input[type='button']")
        for i in range(min(buttons.count(), 40)):
            button_locators[f"button:{i}"] = buttons.nth(i)
        original_host = urlsplit(str(getattr(page, "url", ""))).hostname
        actions = 0
        needs_input = False
        for item in plan["fields"]:
            if item["action"] in {"ignore", "needs_user_input"}:
                if item["action"] == "needs_user_input":
                    needs_input = True
                continue
            if item["confidence"] < self.settings.min_confidence:
                needs_input = True
                continue
            if self.action_count >= self.settings.max_actions_per_job:
                return {"status": "NeedsUserInput", "reason": "AI_ACTION_LIMIT", "actions": actions}
            self.action_count += 1
            target = item["target_ref"]
            classification = item["classification"]
            action = item["action"]
            try:
                gate = detect_verification(page)
                if gate:
                    return {"status": "WaitingForHuman", "reason": gate, "actions": actions}
                if target.startswith("button:"):
                    button = button_locators[target]
                    label = normalize(button.inner_text() or button.get_attribute("value"))
                    # A button without an explicit type defaults to submit inside
                    # forms, so only an explicit type="button" is ever clickable.
                    if (classification != "unknown" or action != "click"
                            or (button.get_attribute("type") or "").casefold() != "button"
                            or label not in _SAFE_BUTTONS):
                        needs_input = True
                        continue
                    button.click()
                elif target in field_locators:
                    field, control = field_locators[target]
                    # Phase 2 already owns recognized fields. AI may only classify
                    # fields it could not identify deterministically.
                    if field.get("field_type") != "unknown":
                        continue
                    sensitive_context = " ".join(str(field.get(key, "")) for key in
                                                  ("label", "name", "id", "placeholder", "aria_label"))
                    if (classification in _SENSITIVE or classification == "sensitive"
                            or _SENSITIVE_HINT.search(sensitive_context)):
                        needs_input = True
                        continue
                    local_key = APPLICANT_KEYS.get(classification)
                    local_value = applicant.get(local_key) if local_key else None
                    if classification == "first_name" and not local_value and applicant.get("name"):
                        local_value = str(applicant["name"]).strip().split()[0]
                    if classification == "last_name" and not local_value and applicant.get("name"):
                        parts = str(applicant["name"]).strip().split()
                        local_value = parts[-1] if len(parts) > 1 else None
                    if action == "scroll":
                        amount = max(-500, min(500, item["scroll_amount"] or 0))
                        page.evaluate("amount => window.scrollBy(0, amount)", amount)
                    elif action == "upload" and classification == "resume":
                        path = applicant.get("resume_path")
                        if not path or not __import__("pathlib").Path(path).is_file():
                            needs_input = True
                            continue
                        control.set_input_files(str(__import__("pathlib").Path(path).resolve()))
                    elif action in {"fill", "select", "check", "uncheck"}:
                        if local_value is None or isinstance(local_value, (dict, list)):
                            needs_input = True
                            continue
                        if action == "fill" and field["tag"] != "select" and field["type"] not in {"checkbox", "radio", "file"}:
                            control.fill(str(local_value))
                        elif action == "select" and field["tag"] == "select":
                            options = field.get("options", [])
                            option = next((o for o in options if normalize(o["label"]) == normalize(item["option_label"])
                                           and normalize(o["label"]) == normalize(local_value)), None)
                            if not option:
                                needs_input = True
                                continue
                            control.select_option(value=option["value"])
                        elif action in {"check", "uncheck"} and field["type"] == "checkbox" and isinstance(local_value, bool):
                            if action == "check" and local_value:
                                control.check()
                            elif action == "uncheck" and not local_value:
                                control.uncheck()
                            else:
                                needs_input = True
                                continue
                        else:
                            needs_input = True
                            continue
                    else:
                        needs_input = True
                        continue
                else:
                    needs_input = True
                    continue
                actions += 1
                log_row = {"action": action, "field": classification,
                           "confidence": float(item["confidence"]), "result": "success"}
                self.action_log.append(log_row)
                print(f"[AI] Action={action}; field={classification}; "
                      f"confidence={item['confidence']:.2f}; result=success")
                if detect_verification(page):
                    return {"status": "WaitingForHuman", "reason": detect_verification(page), "actions": actions}
                host = urlsplit(str(getattr(page, "url", ""))).hostname
                if host != original_host:
                    return {"status": "NeedsUserInput", "reason": "AI_DOMAIN_CHANGED", "actions": actions}
                try:
                    context_pages = list(page.context.pages)
                except Exception:
                    context_pages = []
                if any(urlsplit(str(getattr(open_page, "url", ""))).hostname != original_host
                       for open_page in context_pages):
                    return {"status": "NeedsUserInput", "reason": "AI_DOMAIN_CHANGED", "actions": actions}
                refreshed = self._snapshot(page, analyze_form(page, form=form), form=form)
                refreshed_fingerprint = hashlib.sha256(repr(refreshed).encode("utf-8")).hexdigest()
                if refreshed_fingerprint != fingerprint:
                    # A value change or navigation may re-render/reorder controls.
                    # End this plan and let Phase 2 re-read the live DOM before any
                    # additional locator can be used.
                    return {"status": "ReadyForValidation", "actions": actions,
                            "page_changed": True}
            except Exception:
                needs_input = True

        if needs_input:
            return {"status": "NeedsUserInput", "reason": "AI_UNRESOLVED_FIELDS", "actions": actions}
        if not actions or not plan["ready_for_validation"]:
            return {"status": "NeedsUserInput", "reason": "AI_NOT_READY", "actions": actions}
        return {"status": "ReadyForValidation", "actions": actions}
