"""Account creation and sign-in on company application portals. Passwords go only from Keychain to the page."""
import re
from urllib.parse import urlparse

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from . import credentials

_CREATE_LINK = re.compile(r"create (an )?account|sign ?up|register|new (user|candidate)|join now", re.I)
_CREATE_SUBMIT = re.compile(r"^(create( (an )?account)?|sign ?up|register|join)\b", re.I)
_SIGNIN_SUBMIT = re.compile(r"^(sign ?in|log ?in|login)\b", re.I)
_GENERIC_SUBMIT = re.compile(r"^(continue|next|submit)\b", re.I)
_VERIFY = re.compile(
    r"verify your e-?mail|check your (e-?mail|inbox)|verification (link|code|e-?mail)|confirm your e-?mail"
    r"|(we('ve| have)|has been) sent (you )?(an? )?(e-?mail|link|code)", re.I)
_BAD_LOGIN = re.compile(r"incorrect|invalid|wrong|not recognized|does not match|failed", re.I)
_PW_ERROR = re.compile(r"password (must|should|needs|requires)|too (weak|short)|at least \d+", re.I)


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


async def _visible(locator) -> list:
    return [el for el in await locator.all() if await el.is_visible()]


async def _click_submit(page: Page, *patterns: re.Pattern) -> bool:
    # Workday ignores clicks on its visible buttons; only the transparent click_filter layer acts.
    overlay = page.locator("[data-automation-id=click_filter]")
    for pat in patterns:
        for i in range(await overlay.count()):
            el = overlay.nth(i)
            if not await el.is_visible():
                continue
            label = (await el.get_attribute("aria-label")) or await el.evaluate(
                "e => (e.parentElement && e.parentElement.innerText) || ''")
            print(f"[auth] overlay {i}: label={label.strip()[:30]!r} matches={bool(pat.search(label.strip()))}", flush=True)
            if pat.search(label.strip()):
                await el.click(force=True)
                return True
    for pat in patterns:
        btn = page.get_by_role("button", name=pat).or_(page.get_by_role("link", name=pat))
        for i in range(await btn.count()):
            el = btn.nth(i)
            if await el.is_visible():
                try:
                    await el.click(timeout=3000)
                except PlaywrightTimeout:
                    await el.click(force=True)  # an overlay (e.g. Workday's click_filter) intercepts the pointer
                return True
    return False


async def _fill_known(page: Page, profile: dict) -> None:
    """Fill empty name/email style fields from the profile answers and tick allowed consent boxes."""
    from .applier import _answer_for, _hint  # lazy: applier imports this module

    answers = profile.get("answers", {})
    for el in await _visible(page.locator("input[type=text], input[type=email], input[type=tel], input:not([type])")):
        if await el.input_value():
            continue
        label = await _hint(el)
        if re.search(r"robots? only|do not (enter|fill)|honeypot|\bbots?\b", label, re.I):
            continue  # bot-trap field
        ans = _answer_for(label, answers)
        if ans is not None:
            await el.fill(ans)
    consents = [p.lower() for p in profile.get("consent_phrases", [])]
    for cb in await _visible(page.locator("input[type=checkbox]")):
        if await cb.is_checked():
            continue
        label = (await _hint(cb)).lower()
        if any(p in label for p in consents):
            await cb.check(force=True)


async def _signup(page: Page, domain: str, email: str, profile: dict, attempt: int = 0) -> tuple[str, str] | None:
    body = await page.inner_text("body")
    password = credentials.get(domain, email)  # reuse: never overwrite a password that may already be live
    if not password or attempt:
        password = credentials.generate(credentials.rules_from_text(body))
        if attempt:
            password = credentials.generate({"min_len": 20, "symbols": True})
        # Saved before submitting, so a password is never lost if the site accepts it.
        credentials.save(domain, email, password)
    await _fill_known(page, profile)
    for el in await _visible(page.locator("input[type=password]")):
        await el.fill(password)
    del password
    if not await _click_submit(page, _CREATE_SUBMIT, _GENERIC_SUBMIT):
        return "needs_manual", f"Account form for {domain} found, but no create-account button."
    await page.wait_for_timeout(5000)
    text = await page.inner_text("body")
    if _VERIFY.search(text):
        credentials.set_state(domain, email, "created")
        return "needs_manual", (f"Account created on {domain} with {email}. Verify the email the site sent, "
                                f"then rerun; the password is in Keychain (service '{credentials.SERVICE}').")
    if await _visible(page.locator("input[type=password]")) and _PW_ERROR.search(text) and attempt == 0:
        return await _signup(page, domain, email, profile, attempt=1)
    return None


async def _login(page: Page, domain: str, email: str, profile: dict) -> tuple[str, str] | None:
    password = credentials.get(domain, email)
    await _fill_known(page, profile)
    for el in await _visible(page.locator("input[type=email], input[type=text]")):
        if await el.input_value():
            continue
        from .applier import _hint  # lazy: applier imports this module

        label = await _hint(el)
        if re.search(r"robots? only|do not (enter|fill)|honeypot|\bbots?\b", label, re.I):
            continue  # bot-trap field
        await el.fill(email)
        break  # only the one email field
    for el in await _visible(page.locator("input[type=password]")):
        await el.fill(password)
    del password
    await _click_submit(page, _SIGNIN_SUBMIT, _GENERIC_SUBMIT)
    for n in range(30):  # wait for the sign-in to resolve: the password field goes away, or an error appears
        await page.wait_for_timeout(2000)
        pw_visible = bool(await _visible(page.locator("input[type=password]")))
        err = bool(_BAD_LOGIN.search(await page.inner_text("body")))
        if n % 3 == 0:
            print(f"[auth] sign-in wait {n}: password_visible={pw_visible} error_text={err} url=...{page.url[-40:]}", flush=True)
        if not pw_visible:
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            return None
        if _BAD_LOGIN.search(await page.inner_text("body")):
            break
    if await _visible(page.locator("input[type=password]")) and _BAD_LOGIN.search(await page.inner_text("body")):
        return "bad_login", f"Stored credentials for {domain} were rejected."
    return None


async def handle_auth(page: Page, profile: dict) -> tuple[str, str] | None:
    """If the page asks for an account, sign in or create one. None means 'carry on'; a tuple means 'stop'."""
    fields = await _visible(page.locator("input[type=password]"))
    if not fields and re.search(r"login|sign-?in|auth|register|sign-?up", page.url, re.I):
        try:  # SPA login forms render a moment after the page loads
            await page.wait_for_selector("input[type=password]", state="visible", timeout=10000)
        except PlaywrightTimeout:
            return None
        fields = await _visible(page.locator("input[type=password]"))
    if not fields:
        return None
    email = profile.get("answers", {}).get("email", "")
    if not email:
        return "needs_manual", "A portal account is needed but no email is set in profile answers."
    domain = domain_of(page.url)
    print(f"[auth] {domain}: {len(fields)} password field(s), account marker={credentials.get_state(domain, email)}", flush=True)
    if len(fields) >= 2 and credentials.get_state(domain, email):
        print("[auth] account already exists -> switching from the create form to sign in", flush=True)
        switch = page.locator("[data-automation-id=signInLink]")  # Workday's create->sign-in toggle
        if await switch.count():
            await switch.first.click(force=True)
            await page.wait_for_timeout(3000)
            fields = await _visible(page.locator("input[type=password]"))
        if len(fields) >= 2:
            return "needs_manual", f"{domain}: account exists but I could not reach its sign-in view."
    if len(fields) >= 2:
        print("[auth] two password fields -> treating as account CREATION form", flush=True)
        return await _signup(page, domain, email, profile)
    if credentials.get_state(domain, email):
        print("[auth] signing IN with stored credentials", flush=True)
        result = await _login(page, domain, email, profile)
        if not result or result[0] != "bad_login":
            return result
        # Rejected once: the account probably does not exist yet, so create it (never retry the login).
    link = page.get_by_role("button", name=_CREATE_LINK).or_(page.get_by_role("link", name=_CREATE_LINK))
    if await link.count():
        try:
            await link.first.click(timeout=3000)
        except PlaywrightTimeout:
            await link.first.click(force=True)
        await page.wait_for_timeout(3000)
        if len(await _visible(page.locator("input[type=password]"))) >= 1:
            return await _signup(page, domain, email, profile)
    return "needs_manual", f"{domain} needs a login and offers no create-account option I could use."
