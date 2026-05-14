from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from selenium.common.exceptions import WebDriverException
from selenium.webdriver import ActionChains, Chrome
from selenium.webdriver.common.by import By

from .google_ai_mode_runner import clean_google_url, clean_text

LOGGER = logging.getLogger(__name__)

PAA_QUESTION_SELECTOR = "div.related-question-pair[data-q]"
PAA_EXPAND_BUTTON_SELECTOR = "div[jsname='tJHJj'][role='button']"
PAA_CONTENT_PANEL_SELECTOR = "div[jsname='NRdf4c']"
PAA_SHOW_MORE_SELECTOR = "span.PBBEhf, div.ZFiwCf span"

PAA_EXTRACTION_SCRIPT = r"""return (function(containerEl) {
  const cleanText = (v) => (v || '').replace(/\s+/g, ' ').trim();

  // Link texts that indicate a UI control rather than a real source name
  const SKIP_SOURCE_TEXTS = /^(more items[.…]*|show more|see more|show all|view more|read more|\+\d+.*|feedback)$/i;

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
      url.includes('webcache.googleusercontent.com') ||
      url.includes('youtube.com/shorts')
    );
  }

  function hostnameFromUrl(url) {
    try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; }
  }

  function extractSources(root) {
    const seen = new Set();
    const sources = [];
    for (const link of root.querySelectorAll('a[href]')) {
      const url = unwrapGoogleUrl(link.getAttribute('href') || '').replace(/#:~:text=.*$/, '');
      if (!isUsefulUrl(url) || seen.has(url)) continue;
      seen.add(url);
      const rawText = cleanText(link.innerText || link.textContent || '');
      // Use link text as source name only when it looks like a real name, not a UI control
      const sourceName = (rawText && !SKIP_SOURCE_TEXTS.test(rawText))
        ? rawText.split(/\n/)[0].trim()
        : hostnameFromUrl(url);
      sources.push({
        index: sources.length + 1,
        url,
        source: sourceName,
        title: '',
        description: '',
        favicon_url: link.querySelector('img')?.src || null,
      });
    }
    return sources;
  }

  function htmlToText(root) {
    const clone = root.cloneNode(true);
    clone.querySelectorAll('style, script, noscript, template, svg, button').forEach(n => n.remove());
    // Remove anchor and cite elements — their text (source names, URL breadcrumbs,
    // "More items…") is noise in the response body; captured separately by extractSources.
    clone.querySelectorAll('a, cite').forEach(n => n.remove());
    let text = cleanText(clone.innerText || clone.textContent || '');
    // Strip "AI Overview not available" notice that appears when a PAA panel loads
    // a nested AI Overview widget and quota is exhausted
    text = text.replace(/^An AI Overview is not available[^.]*\.?\s*(Can'?t generate[^.]*\.?\s*)?(Try again later\.?\s*)?/i, '');
    // Strip "AI Overview" header that sometimes leads the expanded panel content
    text = text.replace(/^AI Overview\s*/i, '');
    return text;
  }

  if (!containerEl) return { text: '', sources: [], raw_html: '' };
  return {
    text: htmlToText(containerEl),
    sources: extractSources(containerEl),
    raw_html: containerEl.outerHTML || '',
  };
})(arguments[0]);
"""


@dataclass
class PAASuggestionCapture:
    index: int
    text: str
    response: str
    sources: list[dict[str, Any]]
    raw_html: str
    capture_method: str
    error: str | None = None


@dataclass
class PAASectionCapture:
    suggestions: list[PAASuggestionCapture] = field(default_factory=list)
    capture_method: str = "paa_dom"
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.suggestions)


def capture_people_also_ask(driver: Chrome, *, max_questions: int = 20, wait_seconds: float = 4.0) -> PAASectionCapture:
    """Click every PAA accordion, expand with Show more, and capture each answer.

    Returns a PAASectionCapture with one entry per question found.
    """
    try:
        questions = driver.find_elements(By.CSS_SELECTOR, PAA_QUESTION_SELECTOR)
    except WebDriverException as exc:
        LOGGER.warning("Could not locate PAA questions: %s", exc)
        return PAASectionCapture(capture_method="paa_dom", error=str(exc))

    if not questions:
        LOGGER.info("No 'People also ask' section found on this page.")
        return PAASectionCapture(capture_method="paa_dom_not_found")

    LOGGER.info("Found %s 'People also ask' question(s). Capturing up to %s.", len(questions), max_questions)
    suggestions: list[PAASuggestionCapture] = []

    for idx, question_el in enumerate(questions[:max_questions], start=1):
        question_text = (question_el.get_attribute("data-q") or "").strip()
        if not question_text:
            continue

        suggestion = _capture_single_paa(driver, question_el, idx, question_text, wait_seconds)
        suggestions.append(suggestion)
        if suggestion.error:
            LOGGER.warning(
                "[PAA %s/%s] Error capturing %r: %s",
                idx,
                min(len(questions), max_questions),
                question_text[:60],
                suggestion.error,
            )
        else:
            LOGGER.info(
                "[PAA %s/%s] Captured %r — response_length=%s source_count=%s",
                idx,
                min(len(questions), max_questions),
                question_text[:60],
                len(suggestion.response),
                len(suggestion.sources),
            )

    return PAASectionCapture(suggestions=suggestions, capture_method="paa_dom")


def _capture_single_paa(
    driver: Chrome, question_el, idx: int, question_text: str, wait_seconds: float
) -> PAASuggestionCapture:
    try:
        # Scroll the question into view
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", question_el)
        time.sleep(0.2)

        # Find and click the expand button (role=button) inside the question row
        expand_btn = None
        try:
            expand_btn = question_el.find_element(By.CSS_SELECTOR, PAA_EXPAND_BUTTON_SELECTOR)
        except WebDriverException:
            # Fallback: click the question element itself
            expand_btn = question_el

        aria_expanded = (expand_btn.get_attribute("aria-expanded") or "").lower()
        if aria_expanded == "true":
            # Already expanded from a previous run — collapse first then re-expand
            _click_trusted(driver, expand_btn)
            time.sleep(0.3)

        _click_trusted(driver, expand_btn)

        # Wait for content panel to become visible
        content_panel = _wait_for_content_panel(driver, expand_btn, wait_seconds)
        if content_panel is None:
            return PAASuggestionCapture(
                index=idx,
                text=question_text,
                response="",
                sources=[],
                raw_html="",
                capture_method="paa_expand_timeout",
                error="content_panel_not_visible",
            )

        # Click Show more inside the panel if present
        _click_show_more_in_panel(driver, content_panel)
        time.sleep(0.5)

        # Extract text, sources, raw_html via JS
        result = driver.execute_script(PAA_EXTRACTION_SCRIPT, content_panel)
        if not isinstance(result, dict):
            result = {}

        response_text = clean_text(result.get("text") or "")
        raw_sources: list[Any] = result.get("sources") if isinstance(result.get("sources"), list) else []
        sources = _normalize_paa_sources(raw_sources)
        raw_html = str(result.get("raw_html") or "")

        # Collapse back before moving to next item
        _click_trusted(driver, expand_btn)
        time.sleep(0.2)

        return PAASuggestionCapture(
            index=idx,
            text=question_text,
            response=response_text,
            sources=sources,
            raw_html=raw_html,
            capture_method="paa_dom_expanded",
        )

    except Exception as exc:
        LOGGER.debug("PAA item %s capture failed: %s", idx, exc)
        return PAASuggestionCapture(
            index=idx,
            text=question_text,
            response="",
            sources=[],
            raw_html="",
            capture_method="paa_error",
            error=str(exc)[:500],
        )


def _click_trusted(driver: Chrome, el) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", el)
    time.sleep(0.1)
    try:
        ActionChains(driver).move_to_element(el).click().perform()
    except Exception:
        driver.execute_script(
            """
            const e = arguments[0];
            e.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerType: 'mouse'}));
            e.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
            e.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerType: 'mouse'}));
            e.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
            e.click();
            """,
            el,
        )


def _wait_for_content_panel(driver: Chrome, expand_btn, wait_seconds: float):
    """Wait until aria-expanded becomes 'true' and locate the associated content panel."""
    aria_controls = (expand_btn.get_attribute("aria-controls") or "").strip()
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        expanded = (expand_btn.get_attribute("aria-expanded") or "").lower()
        if expanded == "true":
            # Try aria-controls id first
            if aria_controls:
                try:
                    panel = driver.find_element(By.ID, aria_controls)
                    vis = driver.execute_script(
                        "return arguments[0].offsetParent !== null || arguments[0].offsetHeight > 0;", panel
                    )
                    if vis:
                        return panel
                except WebDriverException:
                    pass
            # Fallback: sibling NRdf4c
            try:
                panel = expand_btn.find_element(By.XPATH, "following-sibling::div[@jsname='NRdf4c']")
                return panel
            except WebDriverException:
                pass
            # Fallback 2: parent's NRdf4c child
            try:
                parent = expand_btn.find_element(
                    By.XPATH, "ancestor::div[@class and contains(@class,'related-question-pair')]"
                )
                panel = parent.find_element(By.CSS_SELECTOR, PAA_CONTENT_PANEL_SELECTOR)
                return panel
            except WebDriverException:
                pass
        time.sleep(0.3)
    return None


def _click_show_more_in_panel(driver: Chrome, panel) -> None:
    """Click the 'Show more' button inside an expanded PAA panel if present."""
    try:
        candidates = panel.find_elements(By.CSS_SELECTOR, PAA_SHOW_MORE_SELECTOR)
        for el in candidates:
            text = (el.text or el.get_attribute("textContent") or "").strip().lower()
            if "show more" in text:
                _click_trusted(driver, el)
                time.sleep(0.4)
                LOGGER.debug("Clicked 'Show more' inside PAA panel.")
                return
    except WebDriverException:
        pass


def _normalize_paa_sources(raw_sources: list[Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("url") or "").strip()
        clean_url = clean_google_url(raw_url)
        if not clean_url or clean_url in seen:
            continue
        seen.add(clean_url)
        sources.append(
            {
                "index": len(sources) + 1,
                "url": raw_url,
                "clean_url": clean_url,
                "source": clean_text(item.get("source")) or "",
                "title": clean_text(item.get("title")),
                "description": clean_text(item.get("description")),
                "favicon_url": str(item.get("favicon_url") or "").strip() or None,
            }
        )
    return sources
