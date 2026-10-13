"""Phase 4 AI fallback tests; provider responses and browser DOM are local mocks."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from worker.ai import AISettings, create_browser_agent
from worker.ai.base import AIProviderError
from worker.ai.browser_agent import BrowserAgent
from worker.ai.provider import create_provider
from worker.form_adapter import prepare_application
from worker.navigation import NavigationPolicy


_TEST_NAVIGATION_POLICY = NavigationPolicy.for_local_tests(
    allowed_hosts=("127.0.0.1", "localhost", "::1", "local.invalid", "different.invalid"),
)
_FORM_POLICY_PATCH = None


def setUpModule():
    global _FORM_POLICY_PATCH
    _FORM_POLICY_PATCH = patch("worker.form_adapter.configured_policy",
                               return_value=_TEST_NAVIGATION_POLICY)
    _FORM_POLICY_PATCH.start()


def tearDownModule():
    if _FORM_POLICY_PATCH:
        _FORM_POLICY_PATCH.stop()


class Loc:
    def __init__(self, values):
        self.values = values

    @property
    def first(self):
        return self.values[0] if self.values else self

    def count(self): return len(self.values)
    def nth(self, i): return self.values[i]
    def is_visible(self): return bool(self.values) and self.values[0].visible
    def is_enabled(self): return bool(self.values) and self.values[0].enabled
    def inner_text(self, **_):
        if not self.values: return ""
        value = self.values[0]
        if hasattr(value, "inner_text"): return value.inner_text()
        return getattr(value, "text", value)


class ApplicationForm:
    def __init__(self, page):
        self.page = page
    def evaluate(self, _):
        return {"action": "/jobs/apply", "id": "applicationForm", "name": "",
                "class_name": "", "text": "Job Application", "heading": "",
                "headings": ["Job Application"], "buttons": ["Submit Application"]}
    def locator(self, selector):
        if selector == "input, textarea, select": return Loc(self.page.fields)
        if selector == "button, input[type='button']": return Loc([b for b in self.page.buttons if b.kind == "button"])
        if selector == "button": return Loc(self.page.buttons)
        if selector == "input[type='submit']": return Loc([])
        return Loc([])


class Field:
    def __init__(self, label="What is your preferred work arrangement?", value="", required=True,
                 tag="input", input_type="text", options=None, checked=False):
        self.data = {"tag": tag, "type": input_type, "name": "q1", "id": "q1",
                     "placeholder": "", "aria_label": "", "label": label,
                     "required": required, "value": value, "checked": checked,
                     "group": "", "options": options or []}
        self.visible = True

    def evaluate(self, _): return self.data
    def is_visible(self): return True
    def fill(self, value): self.data["value"] = value
    def select_option(self, value): self.data["value"] = value
    def check(self): self.data.update(checked=True, value="on")
    def uncheck(self): self.data.update(checked=False, value="")
    def set_input_files(self, value): self.data["value"] = value


class Button:
    def __init__(self, text="Submit Application", kind="submit"):
        self.text, self.kind, self.visible, self.enabled = text, kind, True, True
        self.clicks = 0
    def inner_text(self): return self.text
    def get_attribute(self, name): return self.kind if name == "type" else None
    def is_visible(self): return self.visible
    def is_enabled(self): return self.enabled
    def click(self): self.clicks += 1


class Page:
    def __init__(self, field=None, *, body=""):
        self.fields = [field] if field else []
        self.body = body
        self.url = "https://local.invalid/apply"
        self.buttons = [Button()]
        self.closed = False
        self.form = ApplicationForm(self)
    def locator(self, selector):
        if selector == "form": return Loc([self.form])
        if selector == "input, textarea, select": return Loc(self.fields)
        if selector == "button, input[type='button']": return Loc([b for b in self.buttons if b.kind == "button"])
        if selector == "button": return Loc(self.buttons)
        if selector == "input[type='submit']": return Loc([])
        if selector == "body": return Loc([SimpleNamespace(inner_text=lambda **_: self.body, visible=True)])
        if selector in {"h1", "h2", "h3"}: return Loc([])
        return Loc([])
    def evaluate(self, *_): return True
    def is_closed(self): return self.closed
    def wait_for_timeout(self, _): pass


def valid_plan(*, action="fill", classification="location", confidence=0.99,
               ready=True, verification=False, fields=True, target="field:0", option_label=None,
               scroll_amount=None):
    return {"page_type": "job_application", "confidence": confidence,
            "human_verification": verification, "ready_for_validation": ready,
            "fields": ([{"target_ref": target, "classification": classification,
                         "confidence": confidence, "action": action,
                         "option_label": option_label, "scroll_amount": scroll_amount}] if fields else [])}


def settings(**changes):
    values = dict(enabled=True, provider="openai", model="mock", api_key="mock",
                  min_confidence=.9, max_actions_per_job=10,
                  max_page_analysis=3, max_retries=0, timeout_seconds=5)
    values.update(changes)
    return SimpleNamespace(**values)


class AgentTests(unittest.TestCase):
    def test_ai_is_disabled_by_default_and_factory_makes_no_provider(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(AISettings.from_environment().enabled)
            self.assertIsNone(create_browser_agent())

    def test_settings_are_bounded_and_environment_only(self):
        with patch.dict(os.environ, {"AI_AGENT_ENABLED": "true", "AI_MAX_ACTIONS_PER_JOB": "999",
                                     "AI_MAX_PAGE_ANALYSIS": "0", "AI_MAX_RETRIES": "9"}, clear=True):
            values = AISettings.from_environment()
        self.assertTrue(values.enabled)
        self.assertEqual(values.max_actions_per_job, 100)
        self.assertEqual(values.max_page_analysis, 1)
        self.assertEqual(values.max_retries, 2)

    def test_unavailable_provider_falls_back_without_crashing(self):
        configured = settings(provider="", model="", api_key="")
        provider = create_provider(configured)
        page = Page(Field())
        result = BrowserAgent(provider, configured).assist(page, {})
        self.assertEqual(result["status"], "NeedsUserInput")
        self.assertEqual(result["reason"], "AI_NOT_CONFIGURED")
        self.assertEqual(page.fields[0].data["value"], "")

    def test_only_safe_profile_backed_value_is_filled_and_never_submitted(self):
        page = Page(Field())
        provider = Mock(); provider.analyze_page.return_value = valid_plan()
        agent = BrowserAgent(provider, settings())
        result = agent.assist(page, {"location": "Mumbai"}, required_unknown=True)
        self.assertEqual(result["status"], "ReadyForValidation")
        self.assertEqual(page.fields[0].data["value"], "Mumbai")
        self.assertEqual(page.buttons[0].clicks, 0)
        sent = provider.analyze_page.call_args.args[0]
        self.assertNotIn("Mumbai", str(sent))
        self.assertNotIn("test@example", str(sent))

    def test_sensitive_classification_never_fills_and_routes_to_person(self):
        page = Page(Field("Question 1"))
        provider = Mock(); provider.analyze_page.return_value = valid_plan(classification="work_authorization")
        result = BrowserAgent(provider, settings()).assist(page, {"work_authorization": "yes"})
        self.assertEqual(result["status"], "NeedsUserInput")
        self.assertEqual(page.fields[0].data["value"], "")

    def test_sensitive_page_context_cannot_be_misclassified_as_safe(self):
        page = Page(Field("What is your national ID number?"))
        provider = Mock(); provider.analyze_page.return_value = valid_plan(classification="email")
        result = BrowserAgent(provider, settings()).assist(page, {"email": "safe@example.invalid"})
        self.assertEqual(result["status"], "NeedsUserInput")
        self.assertEqual(page.fields[0].data["value"], "")

    def test_safe_select_checkbox_resume_and_navigation_click(self):
        choices = [{"value": "mumbai", "label": "Mumbai"}, {"value": "remote", "label": "Remote"}]
        select_page = Page(Field("Question 1", tag="select", input_type="select-one", options=choices))
        provider = Mock(); provider.analyze_page.return_value = valid_plan(action="select", option_label="Mumbai")
        result = BrowserAgent(provider, settings()).assist(select_page, {"location": "Mumbai"})
        self.assertEqual(result["status"], "ReadyForValidation")
        self.assertEqual(select_page.fields[0].data["value"], "mumbai")

        checkbox_page = Page(Field("Question 1", input_type="checkbox"))
        provider2 = Mock(); provider2.analyze_page.return_value = valid_plan(action="check", classification="relocation")
        result2 = BrowserAgent(provider2, settings()).assist(checkbox_page, {"relocation": True})
        self.assertEqual(result2["status"], "ReadyForValidation")
        self.assertTrue(checkbox_page.fields[0].data["checked"])

        from pathlib import Path
        resume_page = Page(Field("Attachment", input_type="file"))
        provider3 = Mock(); provider3.analyze_page.return_value = valid_plan(action="upload", classification="resume")
        result3 = BrowserAgent(provider3, settings()).assist(resume_page, {"resume_path": str(Path(__file__).resolve().parents[1] / "README.md")})
        self.assertEqual(result3["status"], "ReadyForValidation")
        self.assertTrue(resume_page.fields[0].data["value"].endswith("README.md"))

        click_page = Page()
        click_page.buttons = [Button("Continue", "button"), Button("Submit Application", "submit")]
        provider4 = Mock(); provider4.analyze_page.return_value = valid_plan(
            action="click", target="button:0", classification="unknown", fields=True)
        result4 = BrowserAgent(provider4, settings()).assist(click_page, {})
        self.assertEqual(result4["status"], "ReadyForValidation")
        self.assertEqual(click_page.buttons[0].clicks, 1)
        self.assertEqual(click_page.buttons[1].clicks, 0)

        implicit_page = Page()
        implicit_page.buttons = [Button("Continue", None)]
        provider5 = Mock(); provider5.analyze_page.return_value = valid_plan(
            action="click", target="button:0", classification="unknown")
        result5 = BrowserAgent(provider5, settings()).assist(implicit_page, {})
        self.assertEqual(result5["status"], "NeedsUserInput")
        self.assertEqual(implicit_page.buttons[0].clicks, 0)

    def test_verification_gate_is_checked_before_provider_call(self):
        page = Page(Field(), body="Please enter your OTP verification code")
        provider = Mock()
        result = BrowserAgent(provider, settings()).assist(page, {})
        self.assertEqual(result["status"], "WaitingForHuman")
        provider.analyze_page.assert_not_called()

    def test_provider_verification_signal_routes_to_human(self):
        page = Page(Field())
        provider = Mock(); provider.analyze_page.return_value = valid_plan(verification=True)
        result = BrowserAgent(provider, settings()).assist(page, {})
        self.assertEqual(result["status"], "NeedsUserInput")
        self.assertEqual(page.fields[0].data["value"], "")

    def test_malformed_plan_low_confidence_and_provider_error_fail_closed(self):
        cases = (({"unexpected": True}, None), (valid_plan(confidence=.4), None),
                 (None, AIProviderError("AI_RATE_LIMIT")), (None, AIProviderError("AI_TIMEOUT")),
                 (None, AIProviderError("AI_INVALID_API_KEY")),
                 (None, AIProviderError("AI_MODEL_UNAVAILABLE")),
                 (None, AIProviderError("AI_CONTEXT_LIMIT")),
                 (None, AIProviderError("AI_PROVIDER_ERROR")))
        for response, error in cases:
            page = Page(Field())
            provider = Mock()
            if error:
                provider.analyze_page.side_effect = error
            else:
                provider.analyze_page.return_value = response
            result = BrowserAgent(provider, settings()).assist(page, {"location": "Mumbai"})
            self.assertEqual(result["status"], "NeedsUserInput")
            self.assertEqual(page.fields[0].data["value"], "")

    def test_bounded_retry_only_retries_transient_provider_failures(self):
        page = Page(Field())
        provider = Mock()
        provider.analyze_page.side_effect = [AIProviderError("AI_TIMEOUT"), valid_plan()]
        result = BrowserAgent(provider, settings(max_retries=1)).assist(page, {"location": "Mumbai"})
        self.assertEqual(result["status"], "ReadyForValidation")
        self.assertEqual(provider.analyze_page.call_count, 2)

    def test_captcha_and_otp_are_detected_without_provider_or_actions(self):
        for prompt in ("Verify you are human", "Please enter your OTP verification code"):
            page = Page(Field(), body=prompt)
            provider = Mock()
            result = BrowserAgent(provider, settings()).assist(page, {})
            self.assertEqual(result["status"], "WaitingForHuman")
            provider.analyze_page.assert_not_called()
            self.assertEqual(page.fields[0].data["value"], "")

    def test_unknown_action_or_untrusted_target_is_rejected(self):
        page = Page(Field())
        plan = valid_plan(action="execute_code")
        provider = Mock(); provider.analyze_page.return_value = plan
        result = BrowserAgent(provider, settings()).assist(page, {"location": "Mumbai"})
        self.assertEqual(result["reason"], "AI_MALFORMED_RESPONSE")
        self.assertEqual(page.fields[0].data["value"], "")

    def test_analysis_and_action_caps_stop_repeated_work(self):
        page = Page(Field())
        provider = Mock(); provider.analyze_page.return_value = valid_plan(fields=False)
        agent = BrowserAgent(provider, settings(max_page_analysis=1))
        self.assertEqual(agent.assist(page, {})["status"], "NeedsUserInput")
        self.assertEqual(agent.assist(page, {})["reason"], "AI_NO_PROGRESS")
        self.assertEqual(provider.analyze_page.call_count, 1)

        page2 = Page(Field())
        provider2 = Mock(); provider2.analyze_page.return_value = valid_plan()
        agent2 = BrowserAgent(provider2, settings(max_actions_per_job=1))
        agent2.action_count = 1
        self.assertEqual(agent2.assist(page2, {"location": "Mumbai"})["reason"], "AI_ACTION_LIMIT")
        self.assertEqual(page2.fields[0].data["value"], "")

    def test_unexpected_domain_shift_is_stopped(self):
        class RedirectButton(Button):
            def click(self):
                super().click()
                page.url = "https://different.invalid/apply"
        page = Page()
        page.buttons = [RedirectButton("Continue", "button")]
        provider = Mock()
        provider.analyze_page.return_value = {
            "page_type": "job_application", "confidence": .99, "human_verification": False,
            "ready_for_validation": True,
            "fields": [{"target_ref": "button:0", "classification": "unknown",
                        "confidence": .99, "action": "click", "option_label": None,
                        "scroll_amount": None}],
        }
        result = BrowserAgent(provider, settings()).assist(page, {})
        self.assertEqual(result["reason"], "AI_DOMAIN_CHANGED")

    def test_fallback_runs_only_for_unknown_required_fields_and_revalidates(self):
        page = Page(Field())
        provider = Mock(); provider.analyze_page.return_value = valid_plan()
        agent = BrowserAgent(provider, settings())
        result = prepare_application(page, {"location": "Mumbai"}, auto_submit=False, ai_agent=agent)
        self.assertEqual(result["status"], "ReadyForSubmission")
        self.assertEqual(page.fields[0].data["value"], "Mumbai")
        self.assertEqual(result["ai_actions"][0]["action"], "fill")
        self.assertFalse(result["submission_enabled"])
        self.assertEqual(page.buttons[0].clicks, 0)
        provider.analyze_page.assert_called_once()

        known_page = Page(Field("Email", required=True))
        provider2 = Mock()
        result2 = prepare_application(known_page, {"email": "a@example.invalid"},
                                      auto_submit=False, ai_agent=BrowserAgent(provider2, settings()))
        self.assertEqual(result2["status"], "ReadyForSubmission")
        provider2.analyze_page.assert_not_called()

    def test_ai_human_result_uses_existing_wait_callback(self):
        page = Page(Field())
        class WaitingAgent:
            def assist(self, *_args, **_kwargs):
                return {"status": "WaitingForHuman", "reason": "CAPTCHA_REQUIRED"}
        notices = []
        def close_after_wait(status, reason, details, url):
            notices.append((status, reason))
            page.closed = True
        result = prepare_application(page, {}, ai_agent=WaitingAgent(),
                                     on_wait=close_after_wait)
        self.assertEqual(result["status"], "WaitingForHuman")
        self.assertEqual(notices[0][0], "WaitingForHuman")


if __name__ == "__main__":
    unittest.main()
