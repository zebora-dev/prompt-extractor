from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlsplit

from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException, WebDriverException
from selenium.webdriver import ActionChains, Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from .chatgpt_runner import detect_chrome_major_version, first_line
from .google_ai_mode_runner import (
    clean_google_url,
    clean_markdown,
    clean_text,
    has_meaningful_content,
)

LOGGER = logging.getLogger(__name__)

BLOCKING_URL_PATTERNS = [
    "google.com/sorry",
    "recaptcha",
    "captcha",
]
BLOCKING_TEXT_PATTERNS = [
    "unusual traffic",
    "our systems have detected",
    "verify you are human",
    "captcha",
]

# Stable semantic selector — tied to feature purpose, not CSS class or jsname.
# aria-label="Show more AI Overview" is present whenever the AIO box is truncated
# and doubles as the presence-of-AIO detector.
SHOW_MORE_BTN_SELECTOR = '[aria-label="Show more AI Overview"]'


@dataclass
class GoogleAIOverviewCapture:
    response: str
    markdown: str
    capture_method: str
    markdown_capture_method: str
    raw_html: str
    raw_html_capture_method: str
    llm_model: str
    url: str
    sources: list[dict[str, Any]]
    source_capture_method: str
    ai_overview_triggered: bool
    capture_state: str
    error: str | None = None


class GoogleAIOverviewRunner:
    def __init__(
        self,
        google_url: str = "https://www.google.com/search",
        *,
        headless: bool = False,
        chrome_user_data_dir: str | None = None,
        response_timeout_seconds: int = 90,
        country: str | None = None,
        language: str = "en",
    ) -> None:
        self.google_url = google_url
        self.headless = headless
        self.chrome_user_data_dir = chrome_user_data_dir
        self.response_timeout_seconds = response_timeout_seconds
        self.country = country
        self.language = language
        self.driver: Chrome | None = None

    def __enter__(self) -> GoogleAIOverviewRunner:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-extensions")
        if self.chrome_user_data_dir:
            options.add_argument(f"--user-data-dir={self.chrome_user_data_dir}")
        if self.headless:
            options.add_argument("--headless=new")

        self.driver = self.create_driver(options)
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    def create_driver(self, options: Options) -> Chrome:
        try:
            uc = self._import_uc()
            uc_options = uc.ChromeOptions()
            for argument in options.arguments:
                if argument.startswith("--user-data-dir="):
                    continue
                uc_options.add_argument(argument)
            kwargs: dict[str, Any] = {}
            if self.chrome_user_data_dir:
                kwargs["user_data_dir"] = self.chrome_user_data_dir
            chrome_major = detect_chrome_major_version()
            if chrome_major:
                kwargs["version_main"] = chrome_major
            LOGGER.info("Using undetected-chromedriver for Google AI Overview capture.")
            return uc.Chrome(options=uc_options, **kwargs)
        except (ImportError, ModuleNotFoundError) as error:
            LOGGER.warning("undetected-chromedriver unavailable (%s). Falling back to Selenium Chrome.", error)
            return webdriver.Chrome(options=options)
        except SessionNotCreatedException as error:
            LOGGER.warning(
                "undetected-chromedriver session failed (%s). Falling back to Selenium Chrome.",
                first_line(str(error)),
            )
            return webdriver.Chrome(options=options)

    def _import_uc(self):
        try:
            import undetected_chromedriver as uc

            return uc
        except ModuleNotFoundError as error:
            if error.name != "distutils":
                raise
            try:
                import setuptools._distutils as distutils_module
                import setuptools._distutils.version as distutils_version_module
            except ModuleNotFoundError:
                raise error
            sys.modules.setdefault("distutils", distutils_module)
            sys.modules.setdefault("distutils.version", distutils_version_module)
            import undetected_chromedriver as uc

            return uc

    def close(self) -> None:
        if self.driver:
            self.driver.quit()
            self.driver = None

    def run_prompt(self, prompt_text: str) -> GoogleAIOverviewCapture:
        driver = self.require_driver()
        search_url = self.build_search_url(prompt_text)
        LOGGER.info("Loading Google AI Overview URL: %s", search_url)
        driver.get(search_url)

        blocked_reason = self.detect_blocking_page()
        if blocked_reason:
            raise RuntimeError(f"Google blocked the request: {blocked_reason}")

        result = self.wait_for_ai_overview()
        current_url = driver.current_url

        if not result.get("ai_overview_triggered"):
            return GoogleAIOverviewCapture(
                response="",
                markdown="",
                capture_method="no_ai_overview",
                markdown_capture_method="none",
                raw_html=result.get("raw_html") or "",
                raw_html_capture_method=result.get("raw_html_capture_method") or "none",
                llm_model="google-ai-overview",
                url=current_url,
                sources=[],
                source_capture_method="none",
                ai_overview_triggered=False,
                capture_state=result.get("capture_state") or "no_ai_overview",
                error=result.get("error") or "no_ai_overview",
            )

        # Expand the sidebar "Show all" button — the extra sources are dynamically
        # loaded (not CSS-hidden) so we click, wait, then re-extract.
        existing_raw: list[Any] = result.get("sources") if isinstance(result.get("sources"), list) else []
        existing_urls = {str(s.get("url") or "") for s in existing_raw if isinstance(s, dict)}
        if self._click_show_all_sidebar():
            time.sleep(1.5)
            extra = self._extract_extra_sidebar_sources(existing_urls)
            if extra:
                LOGGER.info("Sidebar 'Show all' revealed %s additional source(s).", len(extra))
                for i, s in enumerate(extra, start=len(existing_raw) + 1):
                    s["index"] = i
                existing_raw = existing_raw + extra

        response = clean_text(result.get("text") or "")
        markdown = clean_markdown(result.get("markdown") or response)
        capture_method = str(result.get("capture_method") or "ai_overview_dom_text")
        markdown_capture_method = str(result.get("markdown_capture_method") or "ai_overview_dom_text")

        raw_sources: list[Any] = existing_raw
        sources = normalize_overview_sources(raw_sources)
        raw_html = str(result.get("raw_html") or "")

        if not response and not sources:
            raise RuntimeError("Google AI Overview container was detected, but extracted content was empty")

        return GoogleAIOverviewCapture(
            response=response,
            markdown=markdown,
            capture_method=capture_method,
            markdown_capture_method=markdown_capture_method,
            raw_html=raw_html,
            raw_html_capture_method=str(result.get("raw_html_capture_method") or "panel_outer_html"),
            llm_model="google-ai-overview",
            url=current_url,
            sources=sources,
            source_capture_method="ai_overview_dom_links" if sources else "none",
            ai_overview_triggered=True,
            capture_state=str(result.get("capture_state") or "complete"),
            error=None,
        )

    def build_search_url(self, prompt_text: str) -> str:
        base = self.google_url.rstrip("/")
        query_params: list[tuple[str, str]] = [("q", prompt_text), ("hl", self.language or "en")]
        if self.country:
            query_params.append(("gl", self.country.lower()))
        separator = "&" if "?" in base else "?"
        encoded = "&".join(f"{key}={quote_plus(str(value))}" for key, value in query_params)
        return f"{base}{separator}{encoded}"

    def wait_for_ai_overview(self) -> dict[str, Any]:
        """Poll until the AI Overview panel is detected and content is stable.

        Google pre-loads the full panel content in the DOM before the Show more
        button is clicked — clicking it only removes the CSS height clamp. We
        therefore extract content regardless of aria-expanded state and just
        click Show more once to ensure any lazily-rendered extra content appears.
        """
        deadline = time.time() + self.response_timeout_seconds
        last_result: dict[str, Any] = {
            "ai_overview_triggered": False,
            "capture_state": "no_ai_overview",
            "error": "no_ai_overview",
        }
        last_signature = ""
        stable_checks = 0
        show_more_clicked = False

        while time.time() < deadline:
            blocked_reason = self.detect_blocking_page()
            if blocked_reason:
                raise RuntimeError(f"Google blocked the request: {blocked_reason}")

            result = self.extract_ai_overview()
            last_result = result

            if not result.get("ai_overview_triggered"):
                time.sleep(1)
                continue

            # Click Show more once as soon as the AIO box is detected.
            # Content is already in the DOM; the click removes the height clamp
            # and may trigger any lazy follow-on content.
            if not show_more_clicked:
                self.click_show_more()
                show_more_clicked = True
                time.sleep(1)
                continue

            # Panel expanded — wait for content to stabilise
            signature = f"{result.get('text') or ''}\n---\n{result.get('sources') or []}"
            if signature and signature == last_signature and has_meaningful_content(result):
                stable_checks += 1
            else:
                stable_checks = 0
                last_signature = signature
            if stable_checks >= 2:
                return {**result, "capture_state": "complete"}

            time.sleep(0.5)

        if last_result.get("ai_overview_triggered"):
            return {**last_result, "capture_state": "timeout_partial"}
        return last_result

    def click_show_more(self) -> bool:
        """Click the 'Show more AI Overview' button. Returns True if found, False otherwise."""
        driver = self.require_driver()
        try:
            btn = driver.find_element(By.CSS_SELECTOR, SHOW_MORE_BTN_SELECTOR)
            if (btn.get_attribute("aria-expanded") or "").lower() == "true":
                LOGGER.debug("'Show more AI Overview' already expanded — skipping click.")
                return True
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.3)
            try:
                ActionChains(driver).move_to_element(btn).click().perform()
            except Exception:
                driver.execute_script("arguments[0].click();", btn)
            LOGGER.info("Clicked 'Show more AI Overview' button.")
            return True
        except WebDriverException:
            LOGGER.debug("'Show more AI Overview' button not found — panel may already be fully expanded.")
            return False

    def _click_show_all_sidebar(self) -> bool:
        """Click the sidebar 'Show all related links' button if present.

        Returns True when the button was found and clicked. The extra sources
        are loaded dynamically so the caller must wait before re-extracting.
        """
        driver = self.require_driver()
        try:
            btn = driver.find_element(By.CSS_SELECTOR, '[aria-label="Show all related links"]')
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.2)
            try:
                ActionChains(driver).move_to_element(btn).click().perform()
            except Exception:
                driver.execute_script("arguments[0].click();", btn)
            LOGGER.info("Clicked 'Show all related links' sidebar button.")
            return True
        except WebDriverException:
            LOGGER.debug("'Show all related links' sidebar button not found.")
            return False

    def _extract_extra_sidebar_sources(self, existing_urls: set[str]) -> list[dict[str, Any]]:
        """Extract sidebar sources that weren't present before 'Show all' was clicked."""
        try:
            result = self.require_driver().execute_script(SIDEBAR_EXTRA_SOURCES_SCRIPT, list(existing_urls))
            if isinstance(result, list):
                return result
        except WebDriverException as exc:
            LOGGER.debug("Extra sidebar source extraction failed: %s", first_line(str(exc)))
        return []

    def extract_ai_overview(self) -> dict[str, Any]:
        try:
            result = self.require_driver().execute_script(AI_OVERVIEW_EXTRACTION_SCRIPT)
            if isinstance(result, dict):
                return result
        except WebDriverException as exc:
            LOGGER.debug("Google AI Overview extraction script failed: %s", first_line(str(exc)))
        return {
            "ai_overview_triggered": False,
            "capture_state": "extraction_error",
            "error": "extraction_error",
        }

    def detect_blocking_page(self) -> str:
        driver = self.require_driver()
        current_url = (driver.current_url or "").lower()
        if any(pattern in current_url for pattern in BLOCKING_URL_PATTERNS):
            return current_url
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        except WebDriverException:
            return ""
        for pattern in BLOCKING_TEXT_PATTERNS:
            if pattern in body_text:
                return pattern
        return ""

    def require_driver(self) -> Chrome:
        if not self.driver:
            raise RuntimeError("Browser has not been started")
        return self.driver


# Extracts sidebar sources that are NEW since the last extraction pass.
# Called with a list of already-seen URLs so deduplication works across passes.
SIDEBAR_EXTRA_SOURCES_SCRIPT = r"""
(function(existingUrls) {
  const seen = new Set(existingUrls || []);
  const cleanText = (v) => (v || '').replace(/\s+/g, ' ').trim();
  function isUsefulUrl(url) {
    if (!url || !/^https?:\/\//i.test(url)) return false;
    return !(
      url.includes('google.com/search') || url.includes('accounts.google.com') ||
      url.includes('policies.google.com') || url.includes('support.google.com')
    );
  }
  const container = document.querySelector('[data-xid="aim-aside-initial-corroboration-container"]');
  if (!container) return [];
  const sources = [];
  for (const li of container.querySelectorAll('li')) {
    const link = li.querySelector('a[href]');
    if (!link) continue;
    const url = (link.getAttribute('href') || '').replace(/#:~:text=.*$/, '');
    if (!isUsefulUrl(url) || seen.has(url)) continue;
    seen.add(url);
    const title = (link.getAttribute('aria-label') || '').replace(/\.\s*opens in a new tab\.?$/i, '').trim();
    const descEl = li.querySelector('[data-crb-snippet-text]');
    const description = cleanText(descEl?.innerText || '');
    const sourceEl = li.querySelector('.R0r5R, .Z1JFYc');
    const sourceName = cleanText(sourceEl?.innerText || '');
    sources.push({ index: 0, url, source: sourceName, title, description, favicon_url: null, extraction_source: 'sidebar' });
  }
  return sources;
})(arguments[0])
"""

# Detection and extraction are combined in one script.
#
# Detection: keyed on [aria-label="Show more AI Overview"] — a stable semantic
# attribute that only exists when Google renders an AI Overview box.
#
# Content panel: aria-controls on that button points to the panel ID (e.g.
# "m-x-content"), so we can find it without fragile class or jsname selectors.
#
# Sources: two link types live inside the panel —
#   muU3oe  — inline citation chips; href is the real URL, name is in parent span
#   H23r4e  — source links embedded in body text; link.innerText is the name
# Both are deduplicated by URL.
AI_OVERVIEW_EXTRACTION_SCRIPT = r"""
(function() {
  const cleanText = (v) => (v || '').replace(/\s+/g, ' ').trim();

  function unwrapGoogleUrl(href) {
    if (!href) return '';
    try {
      const url = new URL(href, 'https://www.google.com');
      if (url.pathname === '/url') return url.searchParams.get('q') || url.searchParams.get('url') || href;
      return url.href;
    } catch { return href; }
  }

  function isUsefulUrl(url) {
    if (!url || !/^https?:\/\//i.test(url)) return false;
    return !(
      url.includes('google.com/search') ||
      url.includes('accounts.google.com') ||
      url.includes('policies.google.com') ||
      url.includes('support.google.com') ||
      url.includes('webcache.googleusercontent.com')
    );
  }

  function getSourceName(link) {
    // Citation chips (muU3oe): name is in the parent span text, trailing " +N" stripped
    if (link.classList.contains('muU3oe')) {
      const raw = (link.parentElement?.innerText || link.parentElement?.textContent || '');
      return raw.replace(/\s*\+\d+\s*$/, '').replace(/^\s+/, '').trim();
    }
    return cleanText(link.innerText || link.textContent || '');
  }

  function extractPanelSources(root) {
    if (!root) return [];
    const seen = new Set();
    const sources = [];
    for (const link of root.querySelectorAll('a[href]')) {
      const rawHref = link.getAttribute('href') || '';
      const url = unwrapGoogleUrl(rawHref).replace(/#:~:text=.*$/, '');
      if (!isUsefulUrl(url) || seen.has(url)) continue;
      seen.add(url);
      const name = getSourceName(link);
      const isCitation = link.classList.contains('muU3oe');
      const isInline = link.classList.contains('H23r4e');
      sources.push({
        index: sources.length + 1,
        url,
        source: name,
        title: '',
        description: '',
        favicon_url: link.querySelector('img')?.src || null,
        extraction_source: isCitation ? 'citation' : (isInline ? 'inline' : 'more_links'),
      });
    }
    return sources;
  }

  // Sidebar corroboration panel — the numbered source cards shown beside the AIO box.
  // Anchored on data-xid which is tied to the component name, not CSS classes.
  function extractSidebarSources(seen) {
    const container = document.querySelector('[data-xid="aim-aside-initial-corroboration-container"]');
    if (!container) return [];
    const sources = [];
    for (const li of container.querySelectorAll('li')) {
      // NDNGvf links carry a direct href (not a Google redirect)
      const link = li.querySelector('a[href]');
      if (!link) continue;
      const url = (link.getAttribute('href') || '').replace(/#:~:text=.*$/, '');
      if (!isUsefulUrl(url) || seen.has(url)) continue;
      seen.add(url);
      // Title from aria-label — strip the " Opens in a new tab." suffix added for a11y
      const ariaLabel = link.getAttribute('aria-label') || '';
      const title = ariaLabel.replace(/\.\s*opens in a new tab\.?$/i, '').trim();
      // Snippet text is in the element annotated with data-crb-snippet-text
      const descEl = li.querySelector('[data-crb-snippet-text]');
      const description = cleanText(descEl?.innerText || descEl?.textContent || '');
      // Display source name (domain brand, e.g. "Money Saving Expert")
      const sourceEl = li.querySelector('.R0r5R, .Z1JFYc');
      const sourceName = cleanText(sourceEl?.innerText || sourceEl?.textContent || '');
      sources.push({
        index: 0,
        url,
        source: sourceName,
        title,
        description,
        favicon_url: null,
        extraction_source: 'sidebar',
      });
    }
    return sources;
  }

  function extractSources(panel) {
    const seen = new Set();
    const panelSources = extractPanelSources(panel);
    panelSources.forEach(s => seen.add(s.url));
    const sidebarSources = extractSidebarSources(seen);
    const merged = [...panelSources, ...sidebarSources];
    merged.forEach((s, i) => { s.index = i + 1; });
    return merged;
  }

  const btn = document.querySelector('[aria-label="Show more AI Overview"]');
  if (!btn) {
    return { ai_overview_triggered: false, capture_state: 'no_ai_overview', error: 'no_ai_overview' };
  }

  const isExpanded = (btn.getAttribute('aria-expanded') || '').toLowerCase() === 'true';
  const panelId = btn.getAttribute('aria-controls');
  const panel = panelId ? document.getElementById(panelId) : null;

  // Google pre-loads full content in the panel DOM before the button is clicked —
  // clicking just removes the CSS height clamp. Extract immediately; is_expanded
  // is passed back so Python knows whether the click has registered yet.
  if (!panel) {
    return {
      ai_overview_triggered: true,
      is_expanded: false,
      capture_state: 'awaiting_expansion',
      text: '',
      markdown: '',
      raw_html: '',
      raw_html_capture_method: 'none',
      sources: [],
      capture_method: 'awaiting_expansion',
      markdown_capture_method: 'none',
    };
  }

  let text = (panel.innerText || panel.textContent || '').replace(/^AI Overview[\s\n]*/, '').trim();

  if (/you['']?ve reached your daily limit/i.test(text)) {
    return {
      ai_overview_triggered: true,
      is_expanded: true,
      capture_state: 'quota_exhausted',
      error: 'quota_exhausted',
      text: '',
      markdown: '',
      raw_html: panel.outerHTML || '',
      raw_html_capture_method: 'panel_outer_html',
      sources: [],
      capture_method: 'quota_exhausted',
      markdown_capture_method: 'none',
    };
  }

  const sources = extractSources(panel);

  return {
    ai_overview_triggered: true,
    is_expanded: true,
    capture_state: text ? 'content_detected' : 'empty_ai_overview_extraction',
    error: text ? null : 'empty_ai_overview_extraction',
    text,
    markdown: text,
    raw_html: panel.outerHTML || '',
    raw_html_capture_method: 'panel_outer_html',
    capture_method: 'ai_overview_dom_text',
    markdown_capture_method: 'ai_overview_dom_text',
    sources,
  };
})()
"""


def normalize_overview_sources(raw_sources: list[Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        raw_url = str(source.get("url") or "").strip()
        clean_url = clean_google_url(raw_url)
        if not clean_url or clean_url in seen_urls:
            continue
        seen_urls.add(clean_url)
        domain = urlsplit(clean_url).netloc.replace("www.", "")
        entry: dict[str, Any] = {
            "index": len(sources) + 1,
            "url": raw_url,
            "clean_url": clean_url,
            "source": clean_text(source.get("source")) or domain,
            "title": clean_text(source.get("title")),
            "description": clean_text(source.get("description")),
            "favicon_url": str(source.get("favicon_url") or "").strip()
            or (f"https://www.google.com/s2/favicons?domain={domain}&sz=32" if domain else None),
            "extraction_source": clean_text(source.get("extraction_source")) or "more_links",
        }
        if source.get("citation_count") is not None:
            entry["citation_count"] = int(source["citation_count"])
        sources.append(entry)
    return sources
