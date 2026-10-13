"""Conservative form analysis/filling. Human gates are never solved or bypassed."""

import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from worker.detector import (
    _is_local_fixture_page, detect_application_result, detect_verification,
)
from worker.navigation import (
    NavigationRejected, configured_policy, current_page_is_authorized,
    install_page_guard,
)


FIELD_PATTERNS = (
    ("work_authorization", r"work authorization|authorized to work|right to work|legally authorized"),
    ("sponsorship", r"sponsor|visa|immigration"),
    ("resume", r"resume|curriculum vitae|\bcv\b"),
    ("linkedin", r"linkedin"),
    ("github", r"github"),
    ("portfolio", r"portfolio|personal website|website url"),
    ("cover_letter", r"cover letter|covering letter"),
    ("salary", r"salary|compensation|pay expectation|desired wage"),
    ("notice_period", r"notice period|availability to start|start date"),
    ("education", r"education|degree|university|college|qualification"),
    ("skills", r"skills|technologies|technical stack"),
    ("email", r"e-mail|email"),
    ("phone", r"phone|mobile|telephone"),
    ("first_name", r"first name|given name"),
    ("last_name", r"last name|surname|family name"),
    ("full_name", r"full name|your name|candidate name|\bname\b"),
    ("position", r"position applying for|position applied for|applying for position|applying for role|target role|\bposition\b|\bjob title\b"),
    ("location", r"location|city|address|postal code|zip code"),
    ("experience", r"experience|employment history|years worked|work history"),
    ("relocation", r"willing to relocate|relocation preference|relocate for work"),
)

APPLICANT_KEYS = {
    "full_name": "name", "first_name": "first_name", "last_name": "last_name",
    "email": "email", "phone": "phone", "location": "location",
    "position": "position",
    "resume": "resume_path", "linkedin": "linkedin", "github": "github",
    "portfolio": "portfolio", "experience": "experience", "skills": "skills",
    "cover_letter": "cover_letter", "work_authorization": "work_authorization",
    "sponsorship": "sponsorship", "notice_period": "notice_period",
    "salary": "salary", "education": "education", "relocation": "relocation",
}

SKIP_TYPES = {"hidden", "submit", "button", "reset", "image"}
APPLICATION_TERMS = re.compile(
    r"\b(job application|application form|apply for this position|apply now|careers application|job application form|apply)\b",
    re.IGNORECASE,
)
UNRELATED_FORM_TERMS = re.compile(
    r"\b(newsletter|subscribe|subscription|contact us|contact|search|login|log in|sign in|register|feedback|support|comments)\b",
    re.IGNORECASE,
)
APPLICATION_LINK_LABELS = {
    "apply", "apply now", "apply for this job", "apply for job",
    "apply for this position", "apply to this position", "start application",
    "start your application", "submit application", "application form",
    "careers application", "job application",
}
UNRELATED_ACTION_LABELS = {
    "contact", "contact us", "learn more", "view details", "sign in", "login",
    "log in", "register", "subscribe", "share", "save job", "back", "search",
    "newsletter",
}
UNRELATED_DESTINATION = re.compile(
    r"(?:^|[/#?._-])(contact|newsletter|subscribe|login|signin|register|search|"
    r"share|save|learn[-_]?more|details)(?:$|[/#?._=&-])", re.IGNORECASE,
)
APPLICATION_DESTINATION = re.compile(
    r"(?:^|[/#?._-])(apply|application|applications|candidate|jobapplication)(?:$|[/#?._=&-])",
    re.IGNORECASE,
)


def normalize(text):
    value = str(text or "").replace("_", " ")
    return re.sub(r"\s+", " ", value).strip().casefold()


def classify_field(*signals):
    text = normalize(" ".join(str(v or "") for v in signals))
    for field_type, pattern in FIELD_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return field_type, 0.96 if any(normalize(v) for v in signals[:2]) else 0.88
    if "type=email" in text:
        return "email", 0.97
    if "type=tel" in text:
        return "phone", 0.94
    return "unknown", 0.0


def _describe(element):
    return element.evaluate("""el => {
      const text = n => (n && (n.innerText || n.textContent) || '').trim();
      const labels = Array.from(el.labels || []).map(text);
      const parent = el.closest('label');
      if (parent && !labels.includes(text(parent))) labels.push(text(parent));
      const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\\s+/)
        .map(id => document.getElementById(id)).filter(Boolean).map(text);
      const type = (el.type || el.tagName || '').toLowerCase();
      const tag = el.tagName.toLowerCase();
      const current = type === 'checkbox' || type === 'radio'
        ? (el.checked ? (el.value || 'on') : '')
        : type === 'file' ? ((el.files && el.files[0] && el.files[0].name) || '') : (el.value || '');
      return {
        tag, type, name: el.getAttribute('name') || '', id: el.id || '',
        placeholder: el.getAttribute('placeholder') || '',
        aria_label: el.getAttribute('aria-label') || '',
        label: [...labels, ...labelledBy].filter(Boolean).join(' '),
        required: !!el.required || el.getAttribute('aria-required') === 'true' || /\\*\\s*$/.test(labels.join(' ')),
        value: current, checked: !!el.checked,
        group: type === 'radio' || type === 'checkbox' ? (el.name || el.id || '') : '',
        options: tag === 'select' ? Array.from(el.options).map(o => ({value:o.value,label:text(o)})) : []
      };
    }""")


def _form_context(form):
    try:
        return form.evaluate("""el => {
          const text = n => (n && (n.innerText || n.textContent) || '').trim();
          let previous = el.previousElementSibling;
          while (previous && !/^H[1-6]$/.test(previous.tagName) && previous.tagName !== 'FORM')
            previous = previous.previousElementSibling;
          const headings = Array.from(el.querySelectorAll('h1,h2,h3'))
            .map(text).filter(Boolean).slice(0, 6);
          const buttons = Array.from(el.querySelectorAll('button,input[type=submit],input[type=button]'))
            .map(b => text(b) || b.value || b.getAttribute('aria-label') || '').filter(Boolean);
          return {
            action: el.getAttribute('action') || '', id: el.id || '',
            name: el.getAttribute('name') || '', class_name: el.className || '',
            text: text(el).slice(0, 1800),
            heading: text(previous && /^H[1-6]$/.test(previous.tagName) ? previous : null),
            headings, buttons
          };
        }""")
    except Exception:
        return {}


def _form_fields(form):
    controls = form.locator("input, textarea, select")
    fields = []
    for index in range(controls.count()):
        element = controls.nth(index)
        try:
            if not element.is_visible():
                continue
            description = _describe(element)
            if description["type"] in SKIP_TYPES:
                continue
            field_type, confidence = classify_field(
                description["label"], description["aria_label"], description["name"],
                description["id"], description["placeholder"],
                f"type={description['type']}",
            )
            fields.append({
                **description,
                "index": index,
                "field_type": field_type,
                "confidence": confidence,
                "locator": element,
            })
        except Exception:
            continue
    return fields


def _score_form(form):
    """Select only forms with multiple independent job-application signals."""
    context = _form_context(form)
    fields = _form_fields(form)
    field_types = {field["field_type"] for field in fields}
    context_text = " ".join(str(context.get(key) or "") for key in
                             ("action", "id", "name", "class_name", "text", "heading", "headings", "buttons"))
    explicit = bool(APPLICATION_TERMS.search(context_text))
    structural_context = " ".join(str(context.get(key) or "") for key in
                                  ("action", "id", "name", "class_name", "heading", "headings", "buttons"))
    unrelated = bool(UNRELATED_FORM_TERMS.search(structural_context))
    # Form-local evidence only: a generic email or phone input never qualifies alone.
    applicant_signals = field_types & {
        "full_name", "first_name", "last_name", "email", "phone", "resume",
        "linkedin", "github", "portfolio", "location", "position",
    }
    score = 5 if explicit else 0
    if explicit and applicant_signals:
        score += 1
    if len(applicant_signals) >= 2:
        score += 3
    if len(applicant_signals) >= 3:
        score += 2
    if "resume" in field_types:
        score += 2
    if field_types & {"linkedin", "github", "portfolio"}:
        score += 1
    # Explicitly reject forms branded as unrelated even when they contain email/name.
    if unrelated:
        score = -100
    return {"form": form, "score": score, "explicit": explicit,
            "unrelated": unrelated, "fields": fields, "context": context}


def identify_application_form(page):
    """Return exactly one positively identified application form or fail closed."""
    forms = page.locator("form")
    candidates = [_score_form(forms.nth(index)) for index in range(forms.count())]
    plausible = [candidate for candidate in candidates
                 if not candidate["unrelated"] and candidate["score"] >= 5]
    plausible.sort(key=lambda candidate: candidate["score"], reverse=True)
    if len(plausible) == 1:
        return {"status": "Selected", **plausible[0]}
    if len(plausible) > 1:
        # Require a clear evidence gap; do not choose by DOM order or size.
        if plausible[0]["score"] - plausible[1]["score"] >= 3:
            return {"status": "Selected", **plausible[0]}
        return {"status": "NeedsUserInput", "reason": "AMBIGUOUS_APPLICATION_FORM",
                "candidate_count": len(plausible), "score": plausible[0]["score"]}
    if forms.count():
        return {"status": "Unsupported", "reason": "UNSUPPORTED_APPLICATION_FLOW",
                "candidate_count": 0}
    return {"status": "Unsupported", "reason": "UNSUPPORTED_APPLICATION_FLOW",
            "candidate_count": 0}


def _normalized_action_label(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", normalize(value))).strip()


def _is_application_action(label):
    label = _normalized_action_label(label)
    if not label or label in UNRELATED_ACTION_LABELS:
        return False
    # Match complete action labels only. Ordinary prose such as "How to apply"
    # or "Apply these guidelines" is never treated as a control label.
    return label in {_normalized_action_label(item) for item in APPLICATION_LINK_LABELS}


def _has_job_detail_context(page):
    try:
        return bool(page.evaluate("""() => {
          const heading = Array.from(document.querySelectorAll('h1,h2'))
            .some(el => (el.innerText || '').trim().length > 2);
          const text = (document.body && (document.body.innerText || document.body.textContent) || '')
            .toLowerCase();
          return heading && /\\b(job|career|position|role|responsibilities|qualifications|requirements|experience|job description)\\b/.test(text);
        }"""))
    except Exception:
        return False


def _application_action_details(element):
    """Read a visible navigation control without clicking or invoking its handler."""
    return element.evaluate("""el => {
      const form = el.closest('form');
      const formText = form ? [form.getAttribute('action') || '', form.id || '',
        form.getAttribute('name') || '', form.className || '', form.innerText || ''].join(' ') : '';
      return {
        text: (el.innerText || el.value || '').trim(),
        aria: (el.getAttribute('aria-label') || '').trim(),
        labelledBy: (el.getAttribute('aria-labelledby') || '').split(/\\s+/)
          .map(id => document.getElementById(id)).filter(Boolean)
          .map(node => (node.innerText || node.textContent || '').trim()).join(' '),
        title: (el.getAttribute('title') || '').trim(),
        href: el.getAttribute('href') || el.getAttribute('data-application-url') ||
          el.getAttribute('data-href') || el.getAttribute('data-url') || '',
        tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
        inForm: !!form, formText
      };
    }""")


def identify_application_link(page):
    """Identify one well-evidenced application destination or fail closed."""
    if not _has_job_detail_context(page):
        return {"status": "Unsupported", "reason": "NO_JOB_DETAIL_CONTEXT"}
    current = urlsplit(str(getattr(page, "url", "")))
    controls = page.locator("a[href], button, [role='button'][href], [role='button'][data-href], [role='button'][data-url], [role='button'][data-application-url]")
    by_destination = {}
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible() or not control.is_enabled():
                continue
            details = _application_action_details(control)
            labels = [details.get(key, "") for key in ("text", "aria", "labelledBy", "title")]
            normalized_labels = [_normalized_action_label(label) for label in labels if label]
            # Conflicting visible/accessibility labels indicate an unclear control.
            if (any(label in UNRELATED_ACTION_LABELS for label in normalized_labels)
                    or not any(_is_application_action(label) for label in labels)):
                continue
            if details["tag"] == "button" and details["type"] == "submit":
                continue
            if details["inForm"] and UNRELATED_FORM_TERMS.search(details["formText"]):
                continue
            href = (details.get("href") or "").strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            target = urljoin(str(getattr(page, "url", "")), href)
            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "https", "file"}:
                continue
            destination_text = f"{parsed.path}?{parsed.query}#{parsed.fragment}"
            if UNRELATED_DESTINATION.search(destination_text):
                continue
            external_host = bool(parsed.netloc and current.netloc and parsed.netloc.casefold() != current.netloc.casefold())
            app_route = bool(APPLICATION_DESTINATION.search(destination_text))
            # Exact action text plus job-detail context is not enough for an
            # arbitrary same-site destination: the URL must look like an apply
            # route, an application fragment, or a distinct external ATS.
            if not (app_route or external_host):
                continue
            key = (parsed.scheme.casefold(), parsed.netloc.casefold(),
                   (parsed.path.rstrip("/") or "/"), parsed.query, parsed.fragment)
            by_destination.setdefault(key, href)
        except Exception:
            continue
    if not by_destination:
        return {"status": "Unsupported", "reason": "UNSUPPORTED_APPLICATION_FLOW"}
    if len(by_destination) > 1:
        return {"status": "NeedsUserInput", "reason": "AMBIGUOUS_APPLICATION_LINK",
                "candidate_count": len(by_destination)}
    return {"status": "Selected", "url": next(iter(by_destination.values()))}


def find_application_link(page):
    """Backward-compatible URL helper; ambiguous or unsupported actions return None."""
    result = identify_application_link(page)
    return result.get("url") if result.get("status") == "Selected" else None


def analyze_form(page, form=None):
    """Collect visible controls from the positively selected form only."""
    selected = identify_application_form(page) if form is None else {"status": "Selected", "form": form}
    if selected.get("status") != "Selected":
        return []
    return _form_fields(selected["form"])


def _profile_value(field_type, applicant):
    key = APPLICANT_KEYS.get(field_type)
    value = applicant.get(key) if key else None
    if field_type == "first_name" and not value and applicant.get("name"):
        value = str(applicant["name"]).strip().split()[0]
    if field_type == "last_name" and not value and applicant.get("name"):
        parts = str(applicant["name"]).strip().split()
        value = parts[-1] if len(parts) > 1 else None
    if field_type == "position" and not value:
        value = applicant.get("job_title") or applicant.get("title") or applicant.get("position")
    return value


def _result(field, status, confidence=None, reason=None):
    return {
        "field": field["field_type"],
        "field_type": field["field_type"],
        "status": status,
        "confidence": field["confidence"] if confidence is None else confidence,
        "required": field["required"],
        "reason": reason,
    }


def _matches_option(field, expected):
    wanted = normalize(expected)
    matches = [o for o in field["options"] if normalize(o["value"]) == wanted or normalize(o["label"]) == wanted]
    return matches[0] if len(matches) == 1 else None


def fill_application_form(page, applicant, form=None):
    fields = analyze_form(page, form=form)
    results = []
    handled_radio_groups = set()

    for field in fields:
        kind = field["field_type"]
        control = field["locator"]
        input_type = field["type"]

        if input_type == "radio":
            group_key = field["group"] or f"radio:{field['index']}"
            if group_key in handled_radio_groups:
                continue
            handled_radio_groups.add(group_key)
            group = [x for x in fields if x["type"] == "radio" and (x["group"] or f"radio:{x['index']}") == group_key]
            checked = next((x for x in group if x["checked"]), None)
            required = any(x["required"] for x in group)
            representative = dict(field, required=required)
            if checked:
                results.append(_result(representative, "already_filled"))
                continue
            value = _profile_value(kind, applicant)
            choice = next((x for x in group if value is not None and (normalize(x["value"]) == normalize(value) or normalize(x["label"]) == normalize(value))), None)
            if choice and field["confidence"] >= 0.85:
                choice["locator"].check()
                results.append(_result(representative, "filled", reason="explicit profile value matched one radio option"))
            elif required or kind != "unknown":
                results.append(_result(representative, "needs_user_input", reason="radio answer is not explicitly available in profile"))
            else:
                results.append(_result(representative, "optional_empty"))
            continue

        try:
            current = field["value"]
            if input_type == "file":
                if current:
                    results.append(_result(field, "already_filled"))
                    continue
                resume = applicant.get("resume_path")
                if resume and Path(resume).is_file():
                    control.set_input_files(str(Path(resume).resolve()))
                    results.append(_result(field, "filled", reason="configured resume file"))
                elif not field["required"]:
                    results.append(_result(field, "optional_empty"))
                else:
                    results.append(_result(field, "needs_user_input", reason="no valid resume_path configured"))
                continue

            if input_type == "checkbox":
                key = APPLICANT_KEYS.get(kind)
                explicit = applicant.get(key) if key else None
                if field["checked"]:
                    results.append(_result(field, "already_filled"))
                elif isinstance(explicit, bool):
                    if explicit:
                        control.check()
                    results.append(_result(field, "filled" if explicit else "already_filled", reason="explicit boolean profile value"))
                elif field["required"]:
                    results.append(_result(field, "needs_user_input", reason="checkbox meaning/consent requires user choice"))
                else:
                    results.append(_result(field, "optional_empty"))
                continue

            if str(current or "").strip():
                results.append(_result(field, "already_filled"))
                continue

            value = _profile_value(kind, applicant)
            if value is None or not str(value).strip() or field["confidence"] < 0.85:
                status = "needs_user_input" if field["required"] or kind in {"resume", "work_authorization", "sponsorship", "salary"} else "optional_empty"
                results.append(_result(field, status, reason="no sufficiently confident explicit profile value"))
                continue

            if field["tag"] == "select":
                option = _matches_option(field, value)
                if option:
                    control.select_option(value=option["value"])
                    results.append(_result(field, "filled", reason="exact profile value matched select option"))
                else:
                    results.append(_result(field, "needs_user_input" if field["required"] else "optional_empty", reason="no exact select option match"))
            else:
                control.fill(str(value))
                results.append(_result(field, "filled"))
        except Exception as exc:
            results.append(_result(field, "needs_user_input" if field["required"] else "unknown", reason=f"control could not be filled ({type(exc).__name__})"))

    missing = sorted({r["field"] for r in results if r["status"] == "needs_user_input" or (r["required"] and r["status"] in {"required_missing", "unknown", "optional_empty"})})
    return {
        "fields": [{k: v for k, v in row.items() if k != "locator"} for row in results],
        "missing_fields": missing,
        "needs_user_input": sorted({r["field"] for r in results if r["status"] == "needs_user_input"}),
        "valid_for_submission": not missing,
        "field_count": len(fields),
    }


def find_submit_button(page, form=None):
    selected = identify_application_form(page) if form is None else {"status": "Selected", "form": form}
    if selected.get("status") != "Selected":
        return None
    form = selected["form"]
    selectors = ("button", "input[type='submit']")
    for selector in selectors:
        elements = form.locator(selector)
        for index in range(elements.count()):
            element = elements.nth(index)
            try:
                text = normalize(element.inner_text() if selector == "button" else element.get_attribute("value"))
                if any(term in text for term in ("submit application", "submit", "send application", "complete application")):
                    return element
            except Exception:
                continue
    return None


DEFAULT_WAIT_TIMEOUT_SECONDS = 1800


def _is_page_closed(page):
    try:
        return bool(page.is_closed()) if hasattr(page, "is_closed") else False
    except Exception:
        return False


def _notify_wait(on_wait, status, reason, details, page):
    if on_wait:
        on_wait(status, reason, details, str(getattr(page, "url", "")))
    print(f"[HUMAN] {status}: {reason}; complete the step in the open browser.")


def _wait_until_clear(page, applicant, on_wait, on_resume, status, reason, details,
                      *, timeout_seconds=DEFAULT_WAIT_TIMEOUT_SECONDS):
    _notify_wait(on_wait, status, reason, details, page)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    while True:
        try:
            if _is_page_closed(page):
                return False
            if deadline is not None and time.monotonic() >= deadline:
                print(f"[HUMAN] Bounded wait timeout reached ({timeout_seconds}s) for {status}: {reason}.")
                return False
            current_gate = detect_verification(page)
            if status == "WaitingForHuman":
                ready = current_gate is None
            else:
                current_report = fill_application_form(page, applicant)
                ready = (current_report["field_count"] > 0
                         and not current_report["needs_user_input"]
                         and current_report["valid_for_submission"])
            if ready:
                break
            page.wait_for_timeout(1000)
        except Exception:
            if deadline is not None and time.monotonic() >= deadline:
                print(f"[HUMAN] Bounded wait timeout reached ({timeout_seconds}s) for {status}: {reason}.")
                return False
            time.sleep(1)
    if on_resume:
        on_resume(str(getattr(page, "url", "")))
    return True


def _form_is_valid(page, form):
    try:
        return bool(form.evaluate("form => form.checkValidity()"))
    except Exception:
        return False


def _is_local_test_page(page):
    """Permit opt-in submit clicks only on explicitly local test fixtures."""
    return _is_local_fixture_page(page)


def prepare_application(page, applicant, *, auto_submit=False, on_wait=None, on_resume=None,
                        ai_agent=None, navigation_policy=None,
                        wait_timeout_seconds=DEFAULT_WAIT_TIMEOUT_SECONDS):
    """Fill confident values, wait for human gates/input, then validate or submit."""
    ai_actions = []
    recorded_ai_actions = 0
    original_url = str(getattr(page, "url", ""))
    application_url = original_url
    followed_application_link = False
    try:
        navigation_policy = navigation_policy or configured_policy()
    except ValueError:
        return {"status": "Failed", "failure_reason": "NAVIGATION_POLICY_INVALID",
                "original_url": original_url, "application_url": application_url,
                "fields": [], "missing_fields": []}
    try:
        install_page_guard(page, navigation_policy)
    except Exception:
        return {"status": "Failed", "failure_reason": "NAVIGATION_GUARD_UNAVAILABLE",
                "original_url": original_url, "application_url": application_url,
                "fields": [], "missing_fields": []}

    def with_ai(report, **updates):
        return {**report, "ai_actions": list(ai_actions),
                "original_url": original_url, "application_url": application_url, **updates}

    while True:
        try:
            current_page_is_authorized(page, navigation_policy)
        except NavigationRejected as exc:
            return with_ai({"status": "Failed", "failure_reason": exc.reason,
                            "fields": [], "missing_fields": []})
        reason = detect_verification(page)
        if reason:
            if not _wait_until_clear(page, applicant, on_wait, on_resume, "WaitingForHuman", reason, {},
                                     timeout_seconds=wait_timeout_seconds):
                return with_ai({"status": "WaitingForHuman", "reason": reason,
                                "fields": [], "missing_fields": []})
            continue

        selection = identify_application_form(page)
        if selection.get("status") != "Selected":
            if selection.get("status") == "NeedsUserInput":
                return with_ai({"status": "NeedsUserInput", "reason": selection["reason"],
                                "fields": [], "missing_fields": [], "form_count": selection.get("candidate_count", 0)})
            link_result = identify_application_link(page)
            if link_result.get("status") == "NeedsUserInput":
                return with_ai({"status": "NeedsUserInput", "reason": link_result["reason"],
                                "fields": [], "missing_fields": [],
                                "application_link_count": link_result.get("candidate_count", 0)})
            application_link = link_result.get("url")
            if application_link and not followed_application_link:
                try:
                    target = urljoin(str(getattr(page, "url", "")), application_link)
                    if urlsplit(target).scheme not in {"http", "https", "file"}:
                        raise ValueError("Unsupported application link scheme")
                    navigation_policy.check(target)
                    navigation_policy.clear_rejection()
                    page.goto(target, wait_until="load", timeout=60000)
                    current_page_is_authorized(page, navigation_policy)
                    application_url = str(getattr(page, "url", target))
                    followed_application_link = True
                    continue
                except NavigationRejected as exc:
                    return with_ai({"status": "Failed", "failure_reason": exc.reason,
                                    "fields": [], "missing_fields": []})
                except Exception as exc:
                    if navigation_policy.last_rejection:
                        return with_ai({"status": "Failed", "failure_reason": navigation_policy.last_rejection,
                                        "fields": [], "missing_fields": []})
                    return with_ai({"status": "Failed", "failure_reason": "APPLICATION_NAVIGATION_FAILED",
                                    "error": {"error_type": type(exc).__name__}})
            return with_ai({"status": "Failed", "failure_reason": "UNSUPPORTED_APPLICATION_FLOW",
                            "fields": [], "missing_fields": []})
        form = selection["form"]
        application_url = str(getattr(page, "url", application_url))
        report = fill_application_form(page, applicant, form=form)
        unresolved = bool(report["needs_user_input"])
        unsupported = not report["field_count"]
        unknown_required = any(
            row.get("required") and row.get("field_type") == "unknown"
            and row.get("status") in {"needs_user_input", "unknown", "required_missing"}
            for row in report["fields"]
        )
        if ai_agent is not None and (unsupported or (unresolved and unknown_required)):
            assisted = ai_agent.assist(
                page, applicant, required_unknown=unknown_required,
                unsupported=unsupported, form=form,
            )
            current_actions = getattr(ai_agent, "action_log", [])
            if len(current_actions) > recorded_ai_actions:
                ai_actions.extend(current_actions[recorded_ai_actions:])
                recorded_ai_actions = len(current_actions)
            if assisted.get("status") == "WaitingForHuman":
                reason = assisted.get("reason", "HUMAN_VERIFICATION_REQUIRED")
                if not _wait_until_clear(page, applicant, on_wait, on_resume,
                                         "WaitingForHuman", reason, {},
                                         timeout_seconds=wait_timeout_seconds):
                    return with_ai({**report, **assisted, "fields": report["fields"],
                                    "missing_fields": report["missing_fields"]})
                continue
            if assisted.get("status") == "NeedsUserInput":
                reason = assisted.get("reason", "AI_UNRESOLVED_FIELDS")
                details = {"missing_fields": report["missing_fields"],
                           "field_results": report["fields"], "ai_reason": reason}
                if not _wait_until_clear(page, applicant, on_wait, on_resume,
                                         "NeedsUserInput", reason, details,
                                         timeout_seconds=wait_timeout_seconds):
                    exit_reason = "BROWSER_CLOSED" if _is_page_closed(page) else reason
                    return with_ai(report, status="NeedsUserInput", reason=exit_reason,
                                   ai_reason=reason)
                continue
            # Never trust an AI readiness claim: deterministic analysis and browser
            # validity run again before reaching the existing submission guard.
            if assisted.get("status") == "ReadyForValidation":
                continue
            return with_ai(report, status="NeedsUserInput", reason="AI_INVALID_RESULT")
        if unsupported:
            return with_ai(report, status="Failed", failure_reason="UNSUPPORTED_APPLICATION_FLOW")
        if report["needs_user_input"]:
            details = {"missing_fields": report["missing_fields"], "field_results": report["fields"]}
            if not _wait_until_clear(page, applicant, on_wait, on_resume, "NeedsUserInput", "REQUIRED_FIELDS_NEED_USER_INPUT", details,
                                     timeout_seconds=wait_timeout_seconds):
                exit_reason = "BROWSER_CLOSED" if _is_page_closed(page) else "REQUIRED_FIELDS_NEED_USER_INPUT"
                return with_ai(report, status="NeedsUserInput", reason=exit_reason)
            # User may have completed one or more required fields; re-analyze without overwriting them.
            continue
        if not report["valid_for_submission"] or not _form_is_valid(page, form):
            return with_ai(report, status="NeedsUserInput", reason="FORM_VALIDATION_FAILED")

        submit_button = find_submit_button(page, form=form)
        if not submit_button:
            return with_ai(report, status="Failed", failure_reason="UNSUPPORTED_APPLICATION_FLOW")
        if detect_verification(page):
            continue
        if auto_submit is not True or not _is_local_test_page(page):
            return with_ai(report, status="ReadyForSubmission", submission_enabled=False,
                           submission_blocked_reason=("REAL_PAGE_SUBMISSION_DISABLED"
                                                      if auto_submit is True else None))

        # AUTO_SUBMIT is an explicit opt-in; all gates and validation are rechecked at click time.
        if report["missing_fields"] or detect_verification(page) or not _form_is_valid(page, form):
            return with_ai(report, status="NeedsUserInput", reason="SUBMISSION_GUARD_REJECTED")
        submit_button.click()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = detect_application_result(page)
            if result:
                return with_ai({**report, **result})
            page.wait_for_timeout(500)
        return with_ai(report, status="Failed", failure_reason="SUBMISSION_RESULT_NOT_CONFIRMED")
