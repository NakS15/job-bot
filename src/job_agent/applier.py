"""Best-effort LinkedIn Easy Apply automation using a dedicated browser profile."""
import asyncio
import json
import random
import re

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from . import db, portal_auth, sheet
from .config import BROWSER_PROFILE, HOME, ensure_dirs, load_profile


def _answer_for(label: str, answers: dict) -> str | None:
    """Answer for the most specific (longest) key found in the label; a blank value means 'leave empty'."""
    low = label.lower()
    hits = [k for k in answers if k.lower() in low]
    if not hits:
        return None
    val = str(answers[max(hits, key=len)]).strip()
    return val or None


async def _select_matching(el, pattern: re.Pattern) -> tuple[bool, list[str]]:
    """Select the first option whose text matches (select_option's label= takes plain text only)."""
    options = await el.evaluate("e => [...e.options].map(o => o.text.trim())")
    for text in options:
        if text and pattern.search(text):
            await el.select_option(label=text)
            return True, options
    return False, options


async def _label_of(page: Page, el) -> str:
    return (await el.evaluate(
        """e => {
            const id = e.id && e.getRootNode().querySelector(`label[for="${e.id}"]`);
            const l = id || e.closest('.fb-dash-form-element, .jobs-easy-apply-form-element, fieldset');
            return ((l && l.innerText) || e.getAttribute('aria-label') || '').trim();
        }"""
    )) or ""


async def _fill_step(page: Page, modal, resume_pdf: str, answers: dict) -> str | None:
    """Fill the current step. Returns the label of an unanswerable field, if any."""
    for f in await modal.locator("input[type=file]").all():
        await f.set_input_files(resume_pdf)

    upload_btn = modal.get_by_role("button", name=re.compile("upload resume", re.I))
    if await upload_btn.count():
        async with page.expect_file_chooser(timeout=5000) as fc:
            await upload_btn.first.click()
        await (await fc.value).set_files(resume_pdf)
        await page.wait_for_timeout(2500)
        name = resume_pdf.rsplit("/", 1)[-1]
        # Never fall through to the preselected generic resume.
        if not await modal.get_by_role("button", name=re.compile(re.escape(name))).count():
            return f"tailored resume upload ({name}) did not appear"

    for el in await modal.locator("input[type=text], input[type=number], input[type=tel], textarea").all():
        label = await _label_of(page, el)
        ans = _answer_for(label, answers)
        if ans is None:
            if await el.input_value():
                continue  # keep LinkedIn's prefilled value
            return label or "unlabelled text field"
        await el.fill(ans)  # profile answers override prefilled values

    for el in await modal.locator("select").all():
        label = await _label_of(page, el)
        current = await el.evaluate("e => e.selectedIndex > 0")
        if current:
            continue
        ans = _answer_for(label, answers)
        if ans is None:
            return label or "unlabelled dropdown"
        try:
            ok, options = await _select_matching(el, re.compile(re.escape(ans), re.I))
        except Exception:
            ok, options = False, []
        if not ok:
            return f"{label} (no option matching '{ans}'; options: {options[:8]})"

    for fs in await modal.locator("fieldset").all():
        if await fs.locator("input[type=radio]:checked").count():
            continue
        label = await _label_of(page, fs)
        ans = _answer_for(label, answers)
        if ans is None:
            return label or "unlabelled radio group"
        opt = fs.locator("label", has_text=re.compile(rf"^\s*{re.escape(ans)}\s*$", re.I))
        if not await opt.count():
            return f"{label} (no option matching '{ans}')"
        await opt.first.click()
    return None


async def _dismiss(page: Page) -> None:
    try:
        await page.get_by_role("button", name=re.compile("dismiss|close", re.I)).first.click(timeout=3000)
        await page.get_by_role("button", name=re.compile("discard", re.I)).first.click(timeout=3000)
    except Exception:
        pass


async def apply_to_job(page: Page, job: dict, answers: dict, dry_run: bool) -> tuple[str, str]:
    await page.goto(job["url"], wait_until="domcontentloaded")
    btn = page.get_by_role("button", name=re.compile("easy apply", re.I)).first
    try:
        await btn.click(timeout=10000)
    except Exception:
        return "needs_manual", "No Easy Apply button (external or already applied)."

    modal = page.get_by_role("dialog").first  # native <dialog> in shadow DOM; "[role=dialog]" misses it
    await modal.wait_for(timeout=10000)
    await page.wait_for_timeout(1500)  # dialog shell renders before its buttons

    for _ in range(15):
        unanswered = await _fill_step(page, modal, job["tailored_pdf"], answers)
        if unanswered:
            await _dismiss(page)
            return "needs_manual", f"Unanswered question: {unanswered}"

        follow = modal.get_by_role("checkbox", name=re.compile(r"^follow\b", re.I))
        if await follow.count() and await follow.first.is_checked():
            await follow.first.uncheck(force=True)

        submit = modal.get_by_role("button", name=re.compile("submit application", re.I))
        if await submit.count():
            if dry_run:
                await page.screenshot(path=str(HOME / "debug" / f"{job['job_id']}_review.png"))
                (HOME / "debug" / f"{job['job_id']}_review.txt").write_text(await modal.inner_text())
                await _dismiss(page)
                return "dry_run_ok", "Reached submit step; not submitted (dry run)."
            await submit.first.click()
            sent = False
            for _ in range(10):
                await page.wait_for_timeout(1500)
                body = (await page.inner_text("body")).lower()
                if re.search(r"application (was )?(sent|submitted)|your application was submitted", body):
                    sent = True
                    break
            await page.screenshot(path=str(HOME / "debug" / f"{job['job_id']}_after_submit.png"))
            await _dismiss(page)
            if not sent:
                return "needs_manual", "Clicked Submit but LinkedIn showed no 'application sent' confirmation. Check LinkedIn."
            return "applied", "LinkedIn confirmed the application was sent."

        nxt = modal.get_by_role("button", name=re.compile(r"^(next|review|continue)", re.I))
        if not await nxt.count():
            break
        before = await modal.inner_text()
        await nxt.first.click()
        for _ in range(10):  # wait for the next step to render instead of a fixed delay
            await page.wait_for_timeout(1000)
            if await modal.inner_text() != before:
                break
        await page.wait_for_timeout(1000)
        if await modal.locator(".artdeco-inline-feedback--error").count():
            err = (await modal.locator(".artdeco-inline-feedback--error").first.inner_text()).strip()
            await _dismiss(page)
            return "needs_manual", f"Form validation error: {err}"

    await _dismiss(page)
    return "needs_manual", "Could not complete the form flow."


async def run_login() -> None:
    ensure_dirs()
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(BROWSER_PROFILE), headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.linkedin.com/login")
        print("Sign in to LinkedIn in the opened window, then press Enter here.")
        await asyncio.get_running_loop().run_in_executor(None, input)
        await ctx.close()


async def apply_approved(dry_run: bool = True) -> list[dict]:
    """Apply to jobs whose Approve cell in the spreadsheet is YES."""
    profile = load_profile()
    cfg = profile.get("apply", {})
    approvals = sheet.read_approvals()
    todo = [
        j for j in db.list_jobs()
        if approvals.get(j["job_id"]) == "YES"
        and j["status"] in ("tailored", "needs_manual", "dry_run_ok")
        and j["apply_type"] != "external"
        and j["tailored_pdf"]
    ][: int(cfg.get("max_per_run", 10))]

    results = []
    if not todo:
        return results

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(BROWSER_PROFILE), headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for i, job in enumerate(todo):
            try:
                status, note = await apply_to_job(page, job, profile.get("answers", {}), dry_run)
            except Exception as e:
                status, note = "needs_manual", f"Error: {e}"
            db.upsert_job(job["job_id"], status=status, notes=note)
            results.append({"job_id": job["job_id"], "title": job["title"],
                            "company": job["company"], "status": status, "notes": note})
            if i < len(todo) - 1:
                await asyncio.sleep(random.uniform(
                    cfg.get("min_delay_seconds", 25), cfg.get("max_delay_seconds", 60)))
        await ctx.close()

    sheet.build_sheet()
    return results


async def _hint(el) -> str:
    return (await el.evaluate(
        """e => {
            const lab = e.id && e.getRootNode().querySelector(`label[for="${CSS.escape(e.id)}"]`);
            return ((lab && lab.innerText) || e.getAttribute('aria-label') || (e.closest('label') || {}).innerText || (e.tagName === 'TEXTAREA' && (e.closest('.application-question, li') || {}).innerText) || e.placeholder || e.name || e.id || '').trim();
        }"""
    )) or ""


_NEXT = re.compile(r"^(next|continue|save (and|&) (continue|next)|proceed)\b", re.I)
_SUBMIT = re.compile(r"^submit\b", re.I)
_FIELDS = "input[type=text], input[type=email], input[type=tel], input[type=number], input[type=url], input:not([type]), textarea"


_TRAP = re.compile(r"robots? only|do not (enter|fill)|leave (this )?(blank|empty)|honeypot|\bbots?\b", re.I)


async def _is_required(el, hint: str) -> bool:
    return "*" in hint or await el.evaluate("e => e.required || e.getAttribute('aria-required') === 'true'")


_EEO = re.compile(r"gender|race|ethnic|veteran|disabilit|sexual orientation|pronoun|hispanic", re.I)
_DECLINE = re.compile(r"prefer not|decline|do not wish|don'?t wish|not (to )?(say|disclose|answer|identify)|rather not", re.I)
_ENTRY_KEYS = {  # form label (whole word) -> profile entry key; first match wins
    "description": "description", "responsibilities": "description", "duties": "description",
    "job title": "title", "title": "title", "position": "title", "role": "title",
    "company": "company", "employer": "company", "organization": "company",
    "location": "location", "city": "location",
    "from": "start", "start": "start", "to": "end", "end": "end",
    "school": "school", "university": "school", "institution": "school", "college": "school",
    "degree": "degree", "field of study": "field", "major": "field", "gpa": "gpa", "grade": "gpa",
}
_ADD_KINDS = {
    "work_history": re.compile(r"^add\b.*\b(work|employment|experience|job)\b", re.I),
    "education": re.compile(r"^add\b.*\b(education|school|degree)\b", re.I),
}


def _entry_answer(hint: str, entry: dict) -> str | None:
    low = hint.lower()
    for key, field in _ENTRY_KEYS.items():
        if re.search(rf"\b{re.escape(key)}\b", low) and str(entry.get(field, "")).strip():
            return str(entry[field])
    return None


async def _fill_entries(page: Page, profile: dict, state: set) -> int:
    """Click Add once per resume entry and fill the section that appears. Returns fields touched."""
    touched = 0
    for kind, add_re in _ADD_KINDS.items():
        add = page.get_by_role("button", name=add_re)
        for idx, entry in enumerate(profile.get(kind, [])):
            if (kind, idx) in state or not await add.count():
                continue
            await add.first.click()
            await page.wait_for_timeout(1500)
            state.add((kind, idx))
            for el in await page.locator(_FIELDS).all():
                if not await el.is_visible() or await el.input_value():
                    continue
                ans = _entry_answer(await _hint(el), entry)
                if ans is not None:
                    await el.fill(ans)
                    touched += 1
            if entry.get("current"):
                cur = page.get_by_role("checkbox", name=re.compile(r"currently|present", re.I))
                if await cur.count() and not await cur.last.is_checked():
                    await cur.last.check(force=True)
                    touched += 1
    return touched


async def _fill_text(page: Page, el, ans: str) -> bool:
    """Fill a field and confirm it stuck. Autocomplete widgets need a suggestion clicked.
    Never presses Enter: that can submit the whole form."""
    auto = await el.evaluate(
        "e => !!(e.getAttribute('aria-autocomplete') || e.getAttribute('role') === 'combobox'"
        " || (e.parentElement && e.parentElement.querySelector('.dropdown-results')))")
    if not auto:
        await el.fill(ans)
        return bool(await el.input_value())
    parts = [p.strip() for p in ans.split(",") if p.strip()]
    await el.click()
    await el.fill("")
    await el.press_sequentially(parts[0], delay=80)  # full "City, State, Country" often finds nothing
    await page.wait_for_timeout(2000)
    options = page.locator(".dropdown-location, [role=option], .pac-item")
    pick = None
    for i in range(await options.count()):
        if len(parts) < 2 or parts[1].lower() in (await options.nth(i).inner_text()).lower():
            pick = options.nth(i)
            break
    if pick is None and await options.count():
        pick = options.first
    if pick is None:
        return False
    await pick.click()
    await page.wait_for_timeout(500)
    return bool(await el.input_value())


async def _fill_portal_page(page: Page, resume_pdf: str, profile: dict, state: set) -> tuple[int, list[str]]:
    """Fill the visible fields we can answer. Returns (fields touched, required questions we could not answer)."""
    answers = profile.get("answers", {})
    touched, missing = 0, []
    name = resume_pdf.rsplit("/", 1)[-1]
    files = page.locator("input[type=file]")
    if not await files.count() and not await page.get_by_text(name).count():
        # Workday-style drop zone: a "Select file" control that opens a file chooser.
        pick = page.get_by_role("button", name=re.compile(r"select files?|upload", re.I)).or_(
            page.get_by_role("link", name=re.compile(r"select files?", re.I)))
        if re.search(r"resume|autofill|upload", page.url, re.I):
            try:  # the drop zone renders client-side
                await pick.first.wait_for(state="visible", timeout=10000)
            except PlaywrightTimeout:
                pass
        print(f"[upload] url=...{page.url[-32:]} select-file controls={await pick.count()}", flush=True)
        if await pick.count() and await pick.first.is_visible():
            try:
                async with page.expect_file_chooser(timeout=8000) as fc:
                    await pick.first.click()
                await (await fc.value).set_files(resume_pdf)
                print("[upload] resume handed to the file chooser", flush=True)
            except PlaywrightTimeout:
                print("[upload] the click opened no file chooser", flush=True)
            await page.wait_for_timeout(6000)  # the portal parses the resume before Continue works
            touched += 1
    if await files.count() and not await page.get_by_text(name).count():
        await files.first.set_input_files(resume_pdf)
        await page.wait_for_timeout(2000)
        touched += 1
        try:  # the parser may overwrite fields once it finishes
            await page.get_by_text(re.compile("analy[sz]ing resume", re.I)).first.wait_for(state="hidden", timeout=25000)
        except PlaywrightTimeout:
            pass

    touched += await _fill_entries(page, profile, state)

    for el in await page.locator(_FIELDS).all():
        if not await el.is_visible():
            continue
        hint = await _hint(el)
        if _TRAP.search(hint):
            continue  # bot-trap field: real users never see or fill it
        ans = None
        if await el.evaluate("e => e.tagName") == "TEXTAREA":
            ans = _answer_for(hint, profile.get("long_answers", {}))
        if ans is None:
            ans = _answer_for(hint, answers)
        current = await el.input_value()
        if ans is not None:
            if current != ans:  # portals often prefill from the parsed resume; the profile wins
                if await _fill_text(page, el, ans):
                    touched += 1
                else:
                    missing.append(f"{hint[:60]} (value not accepted)")
        elif not current and await _is_required(el, hint):
            # Workday multiselect pickers show the choice as a chip next to the input, not as its value.
            chip = await el.evaluate(
                "e => { const c = e.closest('[data-automation-id*=multiSelect], [data-automation-id*=Select]');"
                " return !!(c && c.querySelector('[data-automation-id=selectedItem], [data-automation-id*=selectedItem]')); }")
            if not chip:
                missing.append(hint or "unlabelled field")

    for el in await page.locator("select").all():
        if not await el.is_visible() or await el.evaluate("e => e.selectedIndex > 0"):
            continue
        hint = await _hint(el)
        pattern = _DECLINE if _EEO.search(hint) else None
        ans = _answer_for(hint, answers)
        if pattern is None and ans is not None:
            pattern = re.compile(re.escape(ans), re.I)
        if pattern is not None:
            ok, options = await _select_matching(el, pattern)
            if ok:
                touched += 1
                continue
            hint += f" (no matching option; options: {options[:8]})"
        if await _is_required(el, hint):
            missing.append(hint or "unlabelled dropdown")

    for dd in await page.locator("button[aria-haspopup=listbox]").all():  # Workday-style custom dropdowns
        if not await dd.is_visible():
            continue
        current = (await dd.inner_text()).strip()
        if current and not re.match(r"^select one$", current, re.I):
            continue
        question = await dd.evaluate(
            "e => { const f = e.closest('[data-automation-id^=formField], [data-automation-id*=formField]') || e.parentElement.parentElement;"
            " return (f && f.innerText) || ''; }")
        ans = _answer_for(question, answers)
        if ans is None:
            missing.append(question.strip().split("\n")[0][:110] or "unlabelled dropdown")
            continue
        await dd.click()
        await page.wait_for_timeout(700)
        option = page.get_by_role("option", name=re.compile(rf"^\s*{re.escape(ans)}\s*$", re.I))
        if await option.count():
            await option.first.click()
            await page.wait_for_timeout(500)
            touched += 1
        else:
            await page.keyboard.press("Escape")
            missing.append(f"{question.strip().splitlines()[0][:80]} (no option '{ans}')")

    for group in await page.locator("fieldset, [role=radiogroup]").all():
        if not await group.is_visible() or not await group.get_by_role("radio").count():
            continue
        if await group.locator("input[type=radio]:checked, [role=radio][aria-checked=true]").count():
            continue
        question = (await group.inner_text()).strip().split("\n")[0]
        ans = _answer_for(question, answers)
        if _EEO.search(question):
            pattern = _DECLINE
        elif ans is not None:
            pattern = re.compile(rf"^\s*{re.escape(ans)}\b", re.I)
        else:
            if "*" in question or await group.get_attribute("aria-required") == "true":
                missing.append(question[:100])
            continue
        option = group.get_by_role("radio", name=pattern)
        if await option.count():
            await option.first.check(force=True)
            touched += 1
        else:
            missing.append(f"{question[:80]} (no option matching)")

    consents = [p.lower() for p in profile.get("consent_phrases", [])]
    choices = [c.lower() for c in profile.get("checkbox_choices", [])]
    for cb in await page.locator("input[type=checkbox]").all():
        if await cb.is_checked():
            continue
        hint = await _hint(cb)
        value = (await cb.input_value()).lower()
        chosen = value in choices or hint.lower().strip() in choices
        if not chosen and not await cb.is_visible():
            continue
        if chosen or any(p in hint.lower() for p in consents):
            await cb.check(force=True)
            touched += 1
        elif await _is_required(cb, hint):
            missing.append(f"checkbox: {hint[:80]}")
    return touched, missing


async def _fingerprint(page: Page) -> str:
    heading = await page.locator("h1, h2").first.inner_text() if await page.locator("h1, h2").count() else ""
    return f"{page.url}|{heading.strip()[:60]}|{await page.locator(_FIELDS).count()}"


async def _submitted(page: Page) -> bool:
    body = (await page.inner_text("body")).lower()
    return bool(re.search(r"thank|application (has been )?(submitted|received)|successfully submitted", body)
                or re.search(r"confirm|thanks|thank-you", page.url.lower()))


async def _captcha_shown(page: Page) -> bool:
    if await page.locator("iframe[src*=hcaptcha], iframe[src*=recaptcha], iframe[title*=captcha i]").count():
        return True
    return bool(re.search(r"drag the letter|captcha|i'?m not a robot", (await page.inner_text("body")).lower()))


async def _run_portal(page: Page, resume_pdf: str, profile: dict, dry_run: bool) -> tuple[str, str]:
    """Walk a company application form page by page. Stops on any unanswerable required field."""
    state = set()
    for n in range(1, 13):
        try:  # SPA portals (Workday) render after load; give them time to settle
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            pass
        auth = await portal_auth.handle_auth(page, profile)
        if auth:
            return auth
        try:  # after a sign-in the wizard renders late; let it settle before reading fields
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            pass
        await page.wait_for_timeout(2000)
        touched, missing = await _fill_portal_page(page, resume_pdf, profile, state)
        if missing:
            return "needs_manual", f"Page {n}: needs your answer for: {'; '.join(missing[:6])}"
        if n == 1 and not touched:
            return "", ""  # not an application form (e.g. a job description page)
        submit = page.get_by_role("button", name=_SUBMIT).or_(page.get_by_role("link", name=_SUBMIT))
        if await submit.count() and await submit.first.is_visible():
            if dry_run:
                return "external_opened", f"Reached the final submit on page {n}; not submitted (dry run)."
            await portal_auth._click_submit(page, _SUBMIT)
            for _ in range(10):
                await page.wait_for_timeout(1500)
                if await _submitted(page):
                    return "applied", "Submitted; the portal showed a confirmation."
            if await _captcha_shown(page):
                print("A captcha is showing. Solve it in the browser window; waiting up to 4 minutes...", flush=True)
                for _ in range(160):  # a human must solve it; the form submits itself afterwards
                    await page.wait_for_timeout(1500)
                    if await _submitted(page):
                        return "applied", "Submitted after you solved the captcha; the portal showed a confirmation."
                return "needs_manual", "Captcha was not solved within 4 minutes; nothing was submitted."
            return "needs_manual", "Clicked Submit but saw no confirmation (validation error?). Check the portal."
        nxt = page.get_by_role("button", name=_NEXT).or_(page.get_by_role("link", name=_NEXT))
        if not await nxt.count():
            return "needs_manual", f"Page {n}: no Next or Submit button found."
        before = await _fingerprint(page)
        await portal_auth._click_submit(page, _NEXT)  # overlay-aware: Workday ignores clicks on its visible buttons
        for _ in range(14):  # portals show a spinner for a while before the next page appears
            await page.wait_for_timeout(1500)
            if await _fingerprint(page) != before:
                break
        else:
            errs = await page.locator("[role=alert], [aria-invalid=true], .error, .field-error").count()
            return "needs_manual", f"Page {n}: did not advance after 20s (validation errors on the page: {errs})."
    return "needs_manual", "Stopped after 12 pages without reaching Submit."


async def _debug_dump(page: Page, tag: str) -> str:
    """Save a screenshot and the visible form controls so a failed run can be diagnosed afterwards."""
    d = HOME / "debug"
    d.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(d / f"{tag}.png"), full_page=True)
    rows = await page.evaluate(
        """() => [...document.querySelectorAll('input,select,textarea')].filter(e => e.offsetParent).map(e => ({
            tag: e.tagName, type: e.type, name: e.name || e.id,
            label: ((e.id && e.getRootNode().querySelector(`label[for="${CSS.escape(e.id)}"]`)) || {}).innerText || e.getAttribute('aria-label') || '',
            required: !!(e.required || e.getAttribute('aria-required') === 'true'),
            value: e.type === 'password' ? '[hidden]' : (e.value || '').slice(0, 40), checked: !!e.checked,
            options: e.tagName === 'SELECT' ? [...e.options].map(o => o.text.trim()).slice(0, 15) : undefined}))"""
    )
    popups = await page.evaluate(
        """() => [...document.querySelectorAll('[aria-haspopup], [role=combobox], [role=listbox], [data-automation-id*=ropdown], [data-automation-id*=elect]')]
            .filter(e => e.offsetParent).map(e => ({tag: e.tagName, aid: e.getAttribute('data-automation-id'),
            role: e.getAttribute('role'), popup: e.getAttribute('aria-haspopup'), text: (e.innerText || '').trim().slice(0, 30)})).slice(0, 14)"""
    )
    (d / f"{tag}.json").write_text(json.dumps({"url": page.url, "controls": rows, "popups": popups}, indent=1))
    return str(d / f"{tag}.png")


async def _dismiss_cookies(page: Page) -> None:
    """Close a cookie banner via a reject / necessary-only button; never accepts tracking cookies."""
    reject = re.compile(r"reject( all)?( cookies)?|decline|(only )?(strictly )?necessary( only)?|essential only", re.I)
    btn = page.get_by_role("button", name=reject)
    try:
        if await btn.count() and await btn.first.is_visible():
            await btn.first.click(timeout=3000)
            await page.wait_for_timeout(1000)
    except PlaywrightTimeout:
        pass


async def _choose_start(page: Page) -> bool:
    """Workday-style 'Start Your Application': autofill from the tailored resume, else apply manually."""
    pat = re.compile(r"^(autofill with resume|apply manually)", re.I)  # autofill comes first in the DOM
    btn = page.get_by_role("button", name=pat).or_(page.get_by_role("link", name=pat))
    try:
        await btn.first.wait_for(timeout=6000)
        try:
            await btn.first.click(timeout=5000)
        except PlaywrightTimeout:
            await btn.first.click(force=True)
        try:  # the wizard renders client-side; wait for its first inputs
            await page.wait_for_selector("input", state="visible", timeout=20000)
        except PlaywrightTimeout:
            pass
        await page.wait_for_timeout(1500)
        return True
    except PlaywrightTimeout:
        return False


async def open_external(ctx, page: Page, job: dict, profile: dict, dry_run: bool = True) -> tuple[str, str]:
    await page.goto(job["url"], wait_until="domcontentloaded")
    apply = page.get_by_role("button", name=re.compile(r"^apply\b", re.I)).or_(
        page.get_by_role("link", name=re.compile(r"^apply\b", re.I)))
    try:
        async with ctx.expect_page(timeout=8000) as info:
            await apply.first.click(timeout=10000)
        portal = await info.value
    except PlaywrightTimeout:
        portal = page  # opened in the same tab, or the click failed
    await portal.wait_for_load_state("domcontentloaded")
    await portal.wait_for_timeout(3000)
    if "linkedin.com/jobs/view" in portal.url:
        return "needs_manual", "Could not open the company portal from LinkedIn."
    await _dismiss_cookies(portal)
    status, notes = await _run_portal(portal, job["tailored_pdf"], profile, dry_run)
    for attempt in range(3):  # sign-in often redirects back to the posting, so Apply may be needed again
        print(f"[flow] attempt {attempt}: status={status!r} url=...{portal.url[-50:]}", flush=True)
        if status:
            break
        # Often the portal lands on the job description; step through its own Apply button.
        again = portal.get_by_role("button", name=re.compile(r"^apply( now)?\b", re.I)).or_(
            portal.get_by_role("link", name=re.compile(r"^apply( now)?\b", re.I)))
        try:
            await again.first.wait_for(timeout=10000)  # portal renders client-side
            print(f"[flow] Apply control found ({await again.count()} match(es)); clicking", flush=True)
            await again.first.click(timeout=5000)
            await portal.wait_for_timeout(6000)
            portal = ctx.pages[-1]  # may open another tab
            await _dismiss_cookies(portal)
            chose = await _choose_start(portal)
            print(f"[flow] start option chosen={chose} url=...{portal.url[-50:]}", flush=True)
            status, notes = await _run_portal(portal, job["tailored_pdf"], profile, dry_run)
            print(f"[flow] portal run -> status={status!r} notes={notes[:90]!r}", flush=True)
        except PlaywrightTimeout:
            print("[flow] Apply control not found/clickable (timeout)", flush=True)
            break
    if "alreadyapplied" in portal.url.lower():
        return "applied", "Portal reports an application already exists for this job (alreadyApplied)."
    if not status:
        status, notes = "needs_manual", "Application form not reached (may need sign-in/account)."
    if status in ("needs_manual", "external_opened"):
        notes += f" | debug: {await _debug_dump(portal, job['job_id'])}"
    return status, f"{portal.url[:100]} | {notes}"


async def apply_external(dry_run: bool = True, wait: bool = True, only: str | None = None) -> list[dict]:
    """Walk approved external applications page by page. Dry run stops at the final Submit."""
    profile = load_profile()
    approvals = sheet.read_approvals()
    todo = [
        j for j in db.list_jobs()
        if approvals.get(j["job_id"]) == "YES"
        and (not only or j["job_id"] == only)
        and j["apply_type"] == "external"
        and j["status"] in ("tailored", "needs_manual", "external_opened")
        and j["tailored_pdf"]
    ]
    results = []
    loop = asyncio.get_running_loop()
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(BROWSER_PROFILE), headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for job in todo:
            try:
                status, note = await open_external(ctx, page, job, profile, dry_run)
            except Exception as e:
                status, note = "needs_manual", f"Error: {e}"
            print(f"\n{job['company']} - {job['title']}: {status}\n  {note}")
            if wait and status in ("external_opened", "needs_manual"):
                print("The browser is left as-is. Finish or inspect the form there; it was not submitted by this tool.")
                reply = await loop.run_in_executor(
                    None, input, "Did you submit it? [y/N], then press Enter: ")
                if reply.strip().lower() == "y":
                    status, note = "applied", "Submitted manually via company portal."
            db.upsert_job(job["job_id"], status=status, notes=note)
            results.append({"job_id": job["job_id"], "title": job["title"],
                            "company": job["company"], "status": status, "notes": note})
            for extra in ctx.pages:
                if extra is not page:
                    await extra.close()
        await ctx.close()
    sheet.build_sheet()
    return results
