"""Authenticated browser-backed fetcher for JavaScript-protected SKYbrary pages."""

import asyncio
import os

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright


class SkybraryBrowser:
    """Reuse one browser context, including its authenticated session cookies."""

    def __init__(self, timeout_seconds: float = 30) -> None:
        self.timeout_ms = int(timeout_seconds * 1000)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._authenticated = False

    async def __aenter__(self) -> "SkybraryBrowser":
        self._playwright = await async_playwright().start()
        executable = os.environ.get("SKYBRARY_BROWSER_EXECUTABLE", "").strip()
        channel = os.environ.get("SKYBRARY_BROWSER_CHANNEL", "").strip()
        headless = os.environ.get("SKYBRARY_BROWSER_HEADLESS", "true").lower() not in {
            "0", "false", "no",
        }

        launch_options: dict = {"headless": headless}
        if executable:
            launch_options["executable_path"] = executable
        elif channel:
            launch_options["channel"] = channel

        try:
            self._browser = await self._playwright.chromium.launch(**launch_options)
        except Exception as exc:
            await self._playwright.stop()
            raise RuntimeError(
                "Could not launch Chromium. Run 'python -m playwright install chromium' "
                "or set SKYBRARY_BROWSER_EXECUTABLE in .env."
            ) from exc

        self._context = await self._browser.new_context(
            extra_http_headers={
                "Accept-Language": os.environ.get(
                    "SCRAPER_ACCEPT_LANGUAGE", "en-US,en;q=0.9"
                )
            }
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def fetch(self, url: str) -> str:
        if not self._page:
            raise RuntimeError("SkybraryBrowser must be used as an async context manager.")

        max_attempts = int(os.environ.get("SCRAPER_MAX_ATTEMPTS", "5"))
        retry_base = float(os.environ.get("SCRAPER_RETRY_BASE_SECONDS", "5"))
        for attempt in range(max_attempts):
            response = await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            if "/__superjs/challenge" in self._page.url:
                await self._page.wait_for_url(url, timeout=self.timeout_ms)
                await self._page.wait_for_load_state("domcontentloaded")

            if "/__superjs/" in self._page.url:
                raise RuntimeError(f"SKYbrary browser check did not complete for {url}")
            if response and response.status == 429 and attempt + 1 < max_attempts:
                retry_after = response.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.isdigit() else retry_base * (2**attempt)
                print(f"SKYbrary rate limit; retrying in {delay:g}s ({attempt + 1}/{max_attempts})")
                await asyncio.sleep(delay)
                continue
            if response and response.status >= 400:
                raise RuntimeError(f"SKYbrary returned HTTP {response.status} for {url}")
            return await self._page.content()

        raise RuntimeError(f"Could not fetch {url}")

    async def login_from_env(self, required: bool = True) -> bool:
        """Log in with credentials from .env without printing either value."""
        username = os.environ.get("SKYBRARY_USER", "").strip()
        password = os.environ.get("SKYBRARY_PASS", "")
        if not username or not password:
            if required:
                raise RuntimeError(
                    "SKYBRARY_USER and SKYBRARY_PASS must be set in the repository root .env file."
                )
            return False
        await self.login(username, password)
        return True

    async def login(self, username: str, password: str) -> None:
        """Pass the browser check, submit the Drupal form, and retain its cookies."""
        if not self._page:
            raise RuntimeError("SkybraryBrowser must be used as an async context manager.")

        base_url = os.environ.get("SKYBRARY_BASE_URL", "https://skybrary.aero").rstrip("/")
        login_url = os.environ.get("SKYBRARY_LOGIN_URL", f"{base_url}/user/login")
        print(f"Opening SKYbrary login page: {login_url}")
        await self.fetch(login_url)

        username_input = self._page.locator('input[name="name"]')
        password_input = self._page.locator('input[name="pass"]')
        if await username_input.count() == 0 or await password_input.count() == 0:
            if await self._page.locator('a[href*="/user/logout"]').count() > 0:
                self._authenticated = True
                print("SKYbrary browser session is already authenticated.")
                return
            raise RuntimeError(
                "Could not find the SKYbrary login form after the browser check completed."
            )

        await username_input.fill(username)
        await password_input.fill(password)
        login_form = self._page.locator('form:has(input[name="pass"])').first
        if await login_form.count() == 0:
            raise RuntimeError("Could not identify the SKYbrary login form.")
        submit = login_form.locator(
            'input[type="submit"], button[type="submit"]'
        ).first
        if await submit.count() == 0:
            raise RuntimeError("Could not find the SKYbrary login submit button.")

        await submit.click()
        await self._page.wait_for_load_state("domcontentloaded")

        login_form_visible = (
            await self._page.locator('input[name="name"]').count() > 0
            and await self._page.locator('input[name="pass"]').count() > 0
        )
        logout_visible = await self._page.locator('a[href*="/user/logout"]').count() > 0
        if "/user/login" in self._page.url or login_form_visible or not logout_visible:
            error = self._page.locator(
                '.messages--error, .alert-danger, .form-item--error-message'
            ).first
            detail = ""
            if await error.count() > 0:
                detail = " " + (await error.inner_text()).strip()
            raise RuntimeError(f"SKYbrary login failed.{detail}")

        self._authenticated = True
        print("SKYbrary login succeeded; authenticated cookies will be reused.")

    async def fetch_authenticated(self, url: str) -> str:
        """Fetch a protected page and fail if the authenticated session was lost."""
        if not self._authenticated:
            raise RuntimeError("Authenticate the SKYbrary browser before fetching protected content.")
        html = await self.fetch(url)
        if not self._page:
            raise RuntimeError("Browser page is unavailable.")
        if "/user/login" in self._page.url:
            self._authenticated = False
            raise RuntimeError("SKYbrary redirected to login; the authenticated session expired.")
        if await self._page.locator('input[name="name"][type="text"]').count() > 0:
            self._authenticated = False
            raise RuntimeError("SKYbrary returned a login form; the authenticated session expired.")
        return html
