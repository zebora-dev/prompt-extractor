from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from .google_chrome_factory import (
    NodriverBrowser,
    build_nodriver_browser,
    search_via_box,
    warmup_google_session,
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


def first_line(value: str) -> str:
    return (value or "").split("\n")[0][:200]


@dataclass
class GoogleAIModeCapture:
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
    ai_mode_triggered: bool
    capture_state: str
    error: str | None = None


class GoogleAIModeRunner:
    def __init__(
        self,
        google_url: str = "https://www.google.com/search",
        *,
        headless: bool = False,
        chrome_user_data_dir: str | None = None,
        response_timeout_seconds: int = 90,
        country: str | None = None,
        language: str = "en",
        use_ai_mode_param: bool = True,
        use_advanced_ai_param: bool = True,
        proxy_url: str | None = None,
    ) -> None:
        self.google_url = google_url
        self.headless = headless
        self.chrome_user_data_dir = chrome_user_data_dir  # ignored — nodriver always uses a fresh profile
        self.response_timeout_seconds = response_timeout_seconds
        self.country = country
        self.language = language
        self.use_ai_mode_param = use_ai_mode_param
        self.use_advanced_ai_param = use_advanced_ai_param
        self.proxy_url = proxy_url
        self.browser: NodriverBrowser | None = None

    # Expose self.driver as an alias for self.browser so that extraction.py's
    # _capture_and_save_suggestions(driver=runner.driver) keeps working.
    @property
    def driver(self) -> NodriverBrowser | None:
        return self.browser

    def __enter__(self) -> GoogleAIModeRunner:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        LOGGER.info(
            "Starting GoogleAIModeRunner. headless=%s proxy=%s country=%s language=%s",
            self.headless,
            "yes" if self.proxy_url else "no",
            self.country or "<env>",
            self.language,
        )
        self.browser = build_nodriver_browser(
            headless=self.headless,
            proxy_url=self.proxy_url,
        )
        warmup_google_session(self.browser)
        LOGGER.info("GoogleAIModeRunner ready.")

    def close(self) -> None:
        if self.browser:
            self.browser.quit()
            self.browser = None

    def run_prompt(self, prompt_text: str) -> GoogleAIModeCapture:
        browser = self.require_browser()
        LOGGER.info("Searching via box for AI Mode: %s", prompt_text[:80])
        t0 = time.time()
        search_via_box(browser, prompt_text)
        LOGGER.info("Search submitted in %.1fs. current_url=%s", time.time() - t0, browser.current_url)

        blocked_reason = self.detect_blocking_page()
        if blocked_reason:
            LOGGER.warning("Blocking page detected after search: %s", blocked_reason)
            raise RuntimeError(f"Google blocked the request: {blocked_reason}")

        # Switch to AI Mode via JS navigation (less detectable than direct navigate).
        # Builds the udm=50 URL from the current landed URL so hl/gl params are preserved.
        ai_mode_url = self.build_search_url(prompt_text)
        LOGGER.info("Switching to AI Mode via JS: %s", ai_mode_url)
        browser.execute_script("window.location.assign(arguments[0]);", ai_mode_url)
        time.sleep(2.0)

        blocked_reason = self.detect_blocking_page()
        if blocked_reason:
            LOGGER.warning("Blocking page detected after AI Mode switch: %s", blocked_reason)
            raise RuntimeError(f"Google blocked the request: {blocked_reason}")

        LOGGER.info("No blocking detected — waiting for AI Mode panel.")
        result = self.wait_for_ai_mode()
        current_url = browser.current_url
        LOGGER.info(
            "AI Mode wait finished. triggered=%s state=%s sources=%s",
            result.get("ai_mode_triggered"),
            result.get("capture_state"),
            len(result.get("sources") or []),
        )
        if not result.get("ai_mode_triggered"):
            return GoogleAIModeCapture(
                response="",
                markdown="",
                capture_method="no_ai_mode",
                markdown_capture_method="none",
                raw_html=result.get("raw_html") or "",
                raw_html_capture_method=result.get("raw_html_capture_method") or "none",
                llm_model="google-ai-mode",
                url=current_url,
                sources=[],
                source_capture_method="none",
                ai_mode_triggered=False,
                capture_state=result.get("capture_state") or "no_ai_mode",
                error=result.get("error") or "no_ai_mode",
            )

        # Capture markdown first (button is ready as soon as content is detected).
        clipboard_markdown, clipboard_method = self.capture_markdown_via_copy_button()

        # Expand the sources panel ("Show all") so every source is in the DOM,
        # then re-extract to pick up the complete source list and final raw_html.
        self.expand_sources_panel()
        final = self.extract_ai_mode()
        if final.get("ai_mode_triggered"):
            # Merge: prefer re-extracted sources & raw_html; keep original text if re-extract is empty.
            result = {
                **result,
                "sources": final.get("sources") or result.get("sources") or [],
                "raw_html": final.get("raw_html") or result.get("raw_html") or "",
                "raw_html_capture_method": final.get("raw_html_capture_method")
                    or result.get("raw_html_capture_method") or "",
            }
            LOGGER.info(
                "Post-expand re-extraction: sources=%s raw_html_len=%s",
                len(result["sources"]),
                len(result["raw_html"]),
            )

        markdown = clipboard_markdown
        markdown_capture_method = clipboard_method

        if markdown:
            response = markdown
            capture_method = clipboard_method
        else:
            response = clean_text(result.get("text"))
            capture_method = str(result.get("capture_method") or "ai_mode_dom_text")

        sources = normalize_sources(result.get("sources") if isinstance(result.get("sources"), list) else [])
        raw_html = str(result.get("raw_html") or "")
        if not response and not sources:
            raise RuntimeError("Google AI Mode container was detected, but extracted content was empty")

        return GoogleAIModeCapture(
            response=response,
            markdown=markdown,
            capture_method=capture_method,
            markdown_capture_method=markdown_capture_method,
            raw_html=raw_html,
            raw_html_capture_method=str(result.get("raw_html_capture_method") or "ai_mode_container_outer_html"),
            llm_model="google-ai-mode",
            url=current_url,
            sources=sources,
            source_capture_method="aim_corroboration_panel" if sources else "none",
            ai_mode_triggered=True,
            capture_state=str(result.get("capture_state") or "complete"),
            error=None,
        )

    def expand_sources_panel(self) -> bool:
        """Click the AI Mode 'Show all' sources button and wait for the panel to expand.

        Uses the stable aria-label selector. Returns True if the button was found and clicked.
        """
        browser = self.require_browser()
        try:
            buttons = browser.find_elements_by_css('[aria-label="Show all related links"]')
            if not buttons:
                LOGGER.info("expand_sources_panel: 'Show all' button not found — sources panel may already be expanded.")
                return False
            LOGGER.info("expand_sources_panel: Clicking 'Show all' button.")
            browser.execute_script(
                """
                const el = arguments[0];
                el.scrollIntoView({block: 'center', inline: 'nearest'});
                el.click();
                """,
                buttons[0],
            )
            time.sleep(2.5)  # Wait for panel to fully expand and render all sources
            LOGGER.info("expand_sources_panel: Done (waited 2.5s for expansion).")
            return True
        except Exception as exc:
            LOGGER.warning("expand_sources_panel failed: %s", first_line(str(exc)))
            return False

    def capture_markdown_via_copy_button(self) -> tuple[str, str]:
        """Click the AI Mode 'Copy text' button and return (markdown, capture_method).

        Uses a three-layer clipboard interception strategy with JS dispatched events
        for the click. Returns ('', reason) on any failure.
        """
        browser = self.require_browser()
        LOGGER.info("capture_markdown_via_copy_button: Starting clipboard capture attempt.")
        try:
            # Intercept all three clipboard write paths before the click fires
            browser.execute_script(
                """
                window.__clipboardCapture = null;

                if (navigator.clipboard) {
                    if (navigator.clipboard.writeText) {
                        const _origWriteText = navigator.clipboard.writeText.bind(navigator.clipboard);
                        navigator.clipboard.writeText = async function(text) {
                            window.__clipboardCapture = text;
                            try { return await _origWriteText(text); } catch(e) {}
                        };
                    }
                    if (navigator.clipboard.write) {
                        const _origWrite = navigator.clipboard.write.bind(navigator.clipboard);
                        navigator.clipboard.write = async function(items) {
                            try {
                                for (const item of items) {
                                    if (item.types && item.types.includes('text/plain')) {
                                        const blob = await item.getType('text/plain');
                                        window.__clipboardCapture = await blob.text();
                                        break;
                                    }
                                }
                            } catch(e) {}
                            try { return await _origWrite(items); } catch(e) {}
                        };
                    }
                }

                const _origExecCommand = document.execCommand.bind(document);
                document.execCommand = function(command, ...args) {
                    if (command === 'copy') {
                        const sel = window.getSelection();
                        if (sel && sel.toString()) window.__clipboardCapture = sel.toString();
                    }
                    return _origExecCommand(command, ...args);
                };
                """
            )

            buttons = browser.find_elements_by_css('button[aria-label="Copy text"]')
            if not buttons:
                LOGGER.info("capture_markdown_via_copy_button: Copy text button NOT found in DOM.")
                return "", "copy_button_not_found"

            LOGGER.info("capture_markdown_via_copy_button: Found %s copy button(s) — clicking.", len(buttons))
            button = buttons[0]
            # Use JS dispatched pointer/mouse events — ActionChains is Selenium-specific
            browser.execute_script(
                """
                const el = arguments[0];
                el.scrollIntoView({block: 'center', inline: 'center'});
                el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerType: 'mouse'}));
                el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                el.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerType: 'mouse'}));
                el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                el.click();
                """,
                button,
            )

            # Poll for any of the three intercept paths to fire
            deadline = time.time() + 5
            while time.time() < deadline:
                text = browser.execute_script("return window.__clipboardCapture;")
                if text and str(text).strip():
                    LOGGER.info("Captured markdown via copy button (%s chars).", len(text))
                    return clean_markdown(str(text)), "copy_button_clipboard"
                time.sleep(0.2)

            LOGGER.info("Copy button clicked but no clipboard content captured after 5s.")
            return "", "copy_button_empty"
        except Exception as exc:
            LOGGER.debug("Copy button capture failed: %s", exc)
            return "", "copy_button_error"

    def build_search_url(self, prompt_text: str) -> str:
        base = self.google_url.rstrip("/")
        query_params = [("q", prompt_text), ("hl", self.language or "en")]
        if self.country:
            query_params.append(("gl", self.country.lower()))
        if self.use_ai_mode_param:
            query_params.append(("udm", "50"))
        if self.use_advanced_ai_param:
            query_params.append(("arv", "1"))

        separator = "&" if "?" in base else "?"
        encoded = "&".join(f"{key}={quote_plus(str(value))}" for key, value in query_params)
        return f"{base}{separator}{encoded}"

    def wait_for_ai_mode(self) -> dict[str, Any]:
        """Poll until AI Mode content is detected and stable (or a streaming fallback fires).

        AI Mode streams content progressively into the DOM. The stability check uses
        text + sources only (not markdown, which can vary with whitespace normalisation).
        If content has been present for more than 15 s and is meaningful, we capture
        immediately rather than waiting for two identical consecutive signatures — this
        handles the case where the stream never fully settles.
        """
        deadline = time.time() + self.response_timeout_seconds
        start = time.time()
        last_result: dict[str, Any] = {
            "ai_mode_triggered": False,
            "capture_state": "no_ai_mode",
            "error": "no_ai_mode",
        }
        last_signature = ""
        stable_checks = 0
        first_detected_at: float | None = None
        poll = 0

        while time.time() < deadline:
            blocked_reason = self.detect_blocking_page()
            if blocked_reason:
                raise RuntimeError(f"Google blocked the request: {blocked_reason}")

            result = self.extract_ai_mode()
            last_result = result
            poll += 1
            elapsed = round(time.time() - start, 1)

            if not result.get("ai_mode_triggered"):
                LOGGER.info(
                    "wait_for_ai_mode poll#%s (%.1fs): no AI Mode yet — state=%s url=%s",
                    poll, elapsed, result.get("capture_state"),
                    self.require_browser().current_url[:120],
                )
                time.sleep(1)
                continue

            # Content first detected — record timestamp and log
            if first_detected_at is None:
                first_detected_at = time.time()
                LOGGER.info(
                    "wait_for_ai_mode poll#%s (%.1fs): AI Mode content first detected. "
                    "text_len=%s sources=%s",
                    poll, elapsed,
                    len(result.get("text") or ""),
                    len(result.get("sources") or []),
                )

            # Fast path: if DOM signals streaming is complete and content is meaningful,
            # wait 1s for any late DOM rendering then capture.
            if result.get("is_complete") and has_meaningful_content(result):
                LOGGER.info(
                    "wait_for_ai_mode poll#%s (%.1fs): data-complete=true and meaningful content — "
                    "waiting 1s for DOM to settle then capturing.",
                    poll, elapsed,
                )
                time.sleep(1.0)
                # Re-extract after settle to get the most complete snapshot
                settled = self.extract_ai_mode()
                if settled.get("ai_mode_triggered"):
                    result = settled
                return {**result, "capture_state": "complete"}

            # Stability check — use text + sources only (markdown varies with whitespace)
            signature = f"{result.get('text') or ''}\n---\n{result.get('sources') or []}"
            if signature and signature == last_signature and has_meaningful_content(result):
                stable_checks += 1
                LOGGER.info(
                    "wait_for_ai_mode poll#%s (%.1fs): stable_checks=%s text_len=%s sources=%s",
                    poll, elapsed, stable_checks,
                    len(result.get("text") or ""), len(result.get("sources") or []),
                )
            else:
                if stable_checks > 0:
                    LOGGER.info(
                        "wait_for_ai_mode poll#%s (%.1fs): content changed — resetting stable_checks.",
                        poll, elapsed,
                    )
                stable_checks = 0
                last_signature = signature

            if stable_checks >= 2:
                LOGGER.info("wait_for_ai_mode: content stable — capturing.")
                return {**result, "capture_state": "complete"}

            # Streaming fallback: if content has been present >15s and is meaningful,
            # capture now rather than waiting for perfect stability.
            seconds_detected = time.time() - first_detected_at
            if seconds_detected >= 15 and has_meaningful_content(result):
                LOGGER.info(
                    "wait_for_ai_mode poll#%s: content present for %.1fs — capturing (streaming fallback).",
                    poll, seconds_detected,
                )
                return {**result, "capture_state": "complete"}

            time.sleep(0.5)

        if last_result.get("ai_mode_triggered"):
            LOGGER.warning(
                "wait_for_ai_mode: timed out after %ss — returning timeout_partial "
                "(text_len=%s sources=%s)",
                self.response_timeout_seconds,
                len(last_result.get("text") or ""),
                len(last_result.get("sources") or []),
            )
            return {**last_result, "capture_state": "timeout_partial"}

        LOGGER.warning(
            "wait_for_ai_mode: timed out after %ss with no AI Mode detected — "
            "final state=%s final_url=%s",
            self.response_timeout_seconds,
            last_result.get("capture_state"),
            self.require_browser().current_url[:120],
        )
        return last_result

    def extract_ai_mode(self) -> dict[str, Any]:
        try:
            result = self.require_browser().execute_script(AI_MODE_EXTRACTION_SCRIPT)
            if isinstance(result, dict):
                return result
            LOGGER.warning("Google AI Mode extraction script returned unexpected type: %s", type(result))
        except Exception as exc:
            LOGGER.warning("Google AI Mode extraction script failed: %s", first_line(str(exc)))
        return {
            "ai_mode_triggered": False,
            "capture_state": "extraction_error",
            "error": "extraction_error",
        }

    def detect_blocking_page(self) -> str:
        browser = self.require_browser()
        current_url = (browser.current_url or "").lower()
        if any(pattern in current_url for pattern in BLOCKING_URL_PATTERNS):
            return current_url
        try:
            body_text = str(browser.execute_script("return document.body.innerText") or "").lower()
        except Exception:
            return ""
        for pattern in BLOCKING_TEXT_PATTERNS:
            if pattern in body_text:
                return pattern
        return ""

    def require_browser(self) -> NodriverBrowser:
        if not self.browser:
            raise RuntimeError("Browser has not been started")
        return self.browser

    # Legacy alias kept so any code holding a reference to require_driver() still works.
    def require_driver(self) -> NodriverBrowser:
        return self.require_browser()


AI_MODE_EXTRACTION_SCRIPT = r"""
return (function() {
// Wrapped in an IIFE starting with `return` so execute_script() activates its
// JSON.stringify path: (a) top-level `return` becomes valid inside the function
// body, and (b) the result is deserialised as a proper Python dict.
//
// AI Mode DOM structure (stable data-* selectors):
//   [data-scope-id="turn"][data-complete="true"]  — completed streaming turn
//   [data-container-id="main-col"]               — main content column
//   [data-xid="VpUvz"]                           — inner content area (fallback)
//   [data-container-id="rhs-col"]                — sources sidebar

const cleanText = (value) => (value || '').replace(/\s+/g, ' ').trim();

function unwrapGoogleUrl(href) {
  if (!href) return "";
  try {
    const url = new URL(href, "https://www.google.com");
    if (url.pathname === "/url") {
      return url.searchParams.get("q") || url.searchParams.get("url") || href;
    }
    return url.href;
  } catch (e) {
    return href;
  }
}

function isUsefulUrl(url) {
  if (!url || !/^https?:\/\//i.test(url)) return false;
  return !(
    url.includes("google.com/search") ||
    url.includes("accounts.google.com") ||
    url.includes("policies.google.com") ||
    url.includes("support.google.com") ||
    url.includes("webcache.googleusercontent.com")
  );
}

function domainFromUrl(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return ""; }
}

// AI Mode: detect a completed streaming turn or any turn with enough content.
function findAIModeContainer() {
  // Primary: completed turn (data-complete="true" set when streaming finishes)
  const completedTurn = document.querySelector('[data-scope-id="turn"][data-complete="true"]');
  if (completedTurn) return completedTurn;

  // Secondary: any turn that has meaningful content (still streaming but readable)
  const anyTurn = document.querySelector('[data-scope-id="turn"]');
  if (anyTurn && (anyTurn.innerText || "").trim().length > 50) return anyTurn;

  // Tertiary: main content column is present even before turn wrapper is set
  const mainCol = document.querySelector('[data-container-id="main-col"]');
  if (mainCol && (mainCol.innerText || "").trim().length > 50) return mainCol;

  // Quaternary: stable xid content area
  const xidArea = document.querySelector('[data-xid="VpUvz"]');
  if (xidArea && (xidArea.innerText || "").trim().length > 50) return xidArea;

  return null;
}

// Extract sources from the AI Mode corroboration (sources) panel.
//
// The panel container uses a stable data-xid attribute:
//   [data-xid="aim-aside-initial-corroboration-container"]
//
// Inside that container, each source is an <li> containing:
//   a.NDNGvf[target="_blank"]       ← the link (href to source page)
//   .Nn35F span                     ← page title (stable class for the title div)
//   [data-crb-snippet-text]         ← description snippet span
//   .R0r5R span                     ← site/publisher name
//
// The "Show all" button must be clicked before calling this function
// to ensure all sources are present in the DOM.
function extractAiModeSources() {
  const seen = new Set();
  const sources = [];

  // Primary: the dedicated corroboration container (stable data-xid)
  // Fallback: rhs-col (older DOM shape) or the full document
  const container =
    document.querySelector('[data-xid="aim-aside-initial-corroboration-container"]') ||
    document.querySelector('[data-container-id="rhs-col"]');

  if (!container) return sources;

  // Source links use class NDNGvf — this is the standard AI Mode source link class.
  // Fall back to any target="_blank" link if NDNGvf is absent.
  const linkSelector = container.querySelector("a.NDNGvf")
    ? "a.NDNGvf[target='_blank']"
    : "a[href][target='_blank']";

  for (const link of container.querySelectorAll(linkSelector)) {
    const href = link.getAttribute("href") || "";
    const url = unwrapGoogleUrl(href).replace(/#:~:text=.*$/, "");
    if (!isUsefulUrl(url) || seen.has(url)) continue;
    seen.add(url);

    const li = link.closest("li");

    // Page title: .Nn35F span (the title container div has this stable class)
    let title = "";
    if (li) {
      const titleEl = li.querySelector(".Nn35F span");
      if (titleEl) title = cleanText(titleEl.innerText || titleEl.textContent || "");
    }
    // Fallback: strip ". Opens in a new tab." suffix from aria-label
    if (!title) {
      title = cleanText(link.getAttribute("aria-label") || "")
        .replace(/\.\s*Opens in a new tab\.?$/i, "").trim();
    }

    // Description snippet: [data-crb-snippet-text] span
    let description = "";
    if (li) {
      const descEl = li.querySelector("[data-crb-snippet-text]");
      if (descEl) description = cleanText(descEl.innerText || descEl.textContent || "");
    }

    // Publisher / site name: .R0r5R span
    let siteName = "";
    if (li) {
      const siteEl = li.querySelector(".R0r5R span");
      if (siteEl) siteName = cleanText(siteEl.innerText || siteEl.textContent || "");
    }

    const domain = domainFromUrl(url);
    // Use Google's favicon service — avoids embedding large base64 images
    const faviconUrl = domain
      ? "https://www.google.com/s2/favicons?domain=" + domain + "&sz=32"
      : null;

    sources.push({
      index: sources.length + 1,
      url,
      source: siteName || domain,
      title: title || url,
      description,
      favicon_url: faviconUrl,
      extraction_source: "aim_corroboration_panel",
    });
  }
  return sources;
}

// Check streaming completion flag
const isComplete = !!document.querySelector('[data-scope-id="turn"][data-complete="true"]');

const container = findAIModeContainer();
if (!container) {
  return {
    ai_mode_triggered: false,
    capture_state: "no_ai_mode",
    error: "no_ai_mode",
    is_complete: false,
  };
}

const visibleText = cleanText(container.innerText || container.textContent || "");
if (/you['']?ve reached your daily limit/i.test(visibleText)) {
  return {
    ai_mode_triggered: true,
    capture_state: "quota_exhausted",
    error: "quota_exhausted",
    text: "",
    markdown: "",
    raw_html: container.outerHTML || "",
    raw_html_capture_method: "ai_mode_container_outer_html",
    sources: [],
    is_complete: isComplete,
  };
}

// Prefer the main-col sub-element for raw_html to avoid capturing the full turn shell.
// Fall back to container itself if main-col is not a child (it may be a sibling).
const mainCol =
  container.querySelector('[data-container-id="main-col"]') ||
  document.querySelector('[data-container-id="main-col"]') ||
  container;

const rawHtml = mainCol.outerHTML || "";
const textContent = cleanText(mainCol.innerText || mainCol.textContent || "");
const sources = extractAiModeSources();

return {
  ai_mode_triggered: true,
  capture_state: (textContent.length > 20 || sources.length > 0) ? "content_detected" : "empty_ai_mode_extraction",
  error: null,
  text: textContent,
  markdown: textContent,
  raw_html: rawHtml,
  raw_html_capture_method: "ai_mode_main_col_outer_html",
  capture_method: "ai_mode_dom_text",
  markdown_capture_method: "ai_mode_dom_text",
  sources,
  is_complete: isComplete,
};

})();
"""


def normalize_sources(raw_sources: list[Any]) -> list[dict[str, Any]]:
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


def clean_google_url(url: str) -> str:
    raw_url = (url or "").strip()
    if not raw_url:
        return ""
    try:
        parts = urlsplit(raw_url)
        if parts.netloc.endswith("google.com") and parts.path == "/url":
            params = dict(parse_qsl(parts.query, keep_blank_values=True))
            raw_url = params.get("q") or params.get("url") or raw_url
            parts = urlsplit(raw_url)
        query = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if not key.startswith("utm_")
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except ValueError:
        return raw_url


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_markdown(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", str(value or "").strip())


def markdown_to_text(value: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value or "")
    text = re.sub(r"[`*_#>-]+", " ", text)
    return clean_text(text)


def has_meaningful_content(result: dict[str, Any]) -> bool:
    text = clean_text(result.get("markdown") or result.get("text"))
    sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    alnum_length = len(re.sub(r"[^A-Za-z0-9]", "", text))
    return alnum_length >= 20 or bool(sources)
