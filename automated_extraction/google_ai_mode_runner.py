from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException, WebDriverException
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from .chatgpt_runner import detect_chrome_major_version, first_line


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
    ) -> None:
        self.google_url = google_url
        self.headless = headless
        self.chrome_user_data_dir = chrome_user_data_dir
        self.response_timeout_seconds = response_timeout_seconds
        self.country = country
        self.language = language
        self.use_ai_mode_param = use_ai_mode_param
        self.use_advanced_ai_param = use_advanced_ai_param
        self.driver: Chrome | None = None

    def __enter__(self) -> "GoogleAIModeRunner":
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
            uc = self.import_undetected_chromedriver()
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
            LOGGER.info("Using undetected-chromedriver for Google AI Mode capture.")
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

    def import_undetected_chromedriver(self):
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

    def run_prompt(self, prompt_text: str) -> GoogleAIModeCapture:
        driver = self.require_driver()
        search_url = self.build_search_url(prompt_text)
        LOGGER.info("Loading Google AI Mode URL: %s", search_url)
        driver.get(search_url)

        blocked_reason = self.detect_blocking_page()
        if blocked_reason:
            raise RuntimeError(f"Google blocked the request: {blocked_reason}")

        result = self.wait_for_ai_mode()
        current_url = driver.current_url
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

        response = clean_text(result.get("text"))
        markdown = clean_markdown(result.get("markdown") or response)
        sources = normalize_sources(result.get("sources") if isinstance(result.get("sources"), list) else [])
        raw_html = str(result.get("raw_html") or "")
        if not response and markdown:
            response = markdown_to_text(markdown)
        if not response and not sources:
            raise RuntimeError("Google AI Mode container was detected, but extracted content was empty")

        return GoogleAIModeCapture(
            response=response,
            markdown=markdown,
            capture_method=str(result.get("capture_method") or "ai_mode_dom_text"),
            markdown_capture_method=str(result.get("markdown_capture_method") or "ai_mode_dom_markdown"),
            raw_html=raw_html,
            raw_html_capture_method=str(result.get("raw_html_capture_method") or "ai_mode_container_outer_html"),
            llm_model="google-ai-mode",
            url=current_url,
            sources=sources,
            source_capture_method="ai_mode_dom_links" if sources else "none",
            ai_mode_triggered=True,
            capture_state=str(result.get("capture_state") or "complete"),
            error=None,
        )

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
        deadline = time.time() + self.response_timeout_seconds
        last_result: dict[str, Any] = {
            "ai_mode_triggered": False,
            "capture_state": "no_ai_mode",
            "error": "no_ai_mode",
        }
        last_signature = ""
        stable_checks = 0

        while time.time() < deadline:
            blocked_reason = self.detect_blocking_page()
            if blocked_reason:
                raise RuntimeError(f"Google blocked the request: {blocked_reason}")

            result = self.extract_ai_mode()
            last_result = result
            if result.get("ai_mode_triggered"):
                signature = (
                    f"{result.get('markdown') or ''}\n---\n"
                    f"{result.get('text') or ''}\n---\n"
                    f"{result.get('sources') or []}"
                )
                if signature and signature == last_signature and has_meaningful_content(result):
                    stable_checks += 1
                else:
                    stable_checks = 0
                    last_signature = signature
                if stable_checks >= 2:
                    return {**result, "capture_state": "complete"}
            time.sleep(1)

        if last_result.get("ai_mode_triggered"):
            return {**last_result, "capture_state": "timeout_partial"}
        return last_result

    def extract_ai_mode(self) -> dict[str, Any]:
        try:
            result = self.require_driver().execute_script(AI_MODE_EXTRACTION_SCRIPT)
            if isinstance(result, dict):
                return result
        except WebDriverException as exc:
            LOGGER.debug("Google AI Mode extraction script failed: %s", first_line(str(exc)))
        return {
            "ai_mode_triggered": False,
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


AI_MODE_EXTRACTION_SCRIPT = r"""
const cleanText = (value) => (value || '').replace(/\s+/g, ' ').trim();

function findAIModeContainer() {
  const byAttr =
    document.querySelector('[data-subtree="aimc"]') ||
    document.querySelector('[data-attrid="ai_overview"]');
  if (byAttr) return byAttr;

  const headings = document.querySelectorAll("h2, h3, [role='heading']");
  for (const heading of headings) {
    if (/ai overview/i.test(heading.textContent || "")) {
      let node = heading.parentElement;
      for (let i = 0; i < 6 && node; i++) {
        if (node.querySelectorAll("p, li, span, a").length >= 3) return node;
        node = node.parentElement;
      }
      return heading.parentElement;
    }
  }

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let textNode;
  while ((textNode = walker.nextNode())) {
    if (!/ai overview/i.test(textNode.textContent || "")) continue;
    let node = textNode.parentElement;
    for (let i = 0; i < 8 && node; i++) {
      if (node.querySelectorAll("p, li, span, a").length >= 3) return node;
      node = node.parentElement;
    }
  }
  return null;
}

function unwrapGoogleUrl(href) {
  if (!href) return "";
  try {
    const url = new URL(href, "https://www.google.com");
    if (url.pathname === "/url") {
      return url.searchParams.get("q") || url.searchParams.get("url") || href;
    }
    return url.href;
  } catch {
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

function extractSources(container) {
  const seen = new Set();
  const sources = [];
  for (const link of container.querySelectorAll("a[href]")) {
    const url = unwrapGoogleUrl(link.getAttribute("href") || "").replace(/#:~:text=.*$/, "");
    if (!isUsefulUrl(url) || seen.has(url)) continue;
    seen.add(url);

    const lines = cleanText(link.innerText || link.textContent || "")
      .split(/\n+/)
      .map((line) => cleanText(line))
      .filter(Boolean);
    let source = lines[0] || "";
    let title = lines[1] || "";
    let description = lines.slice(2).join(" ");

    let parent = link.parentElement;
    for (let i = 0; i < 3 && parent && (!title || !description); i++) {
      const parentLines = (parent.innerText || "")
        .split(/\n+/)
        .map((line) => cleanText(line))
        .filter(Boolean);
      if (!title && parentLines.length > 0) title = parentLines.find((line) => line !== source) || "";
      if (!description && parentLines.length > 1) description = parentLines.slice(1, 4).join(" ");
      parent = parent.parentElement;
    }

    sources.push({
      index: sources.length + 1,
      url,
      source,
      title,
      description,
      favicon_url: link.querySelector("img")?.src || null,
      extraction_source: "ai_mode_dom_links",
    });
  }
  return sources;
}

function stripForContent(container) {
  const clone = container.cloneNode(true);
  clone.querySelectorAll("style, script, noscript, template, svg, button, [role='button']").forEach((node) => node.remove());
  clone.querySelectorAll("[data-subtree='aimba'], img[src^='data:']").forEach((node) => node.remove());
  const firstHeading = clone.querySelector("h2, h3, [role='heading']");
  if (firstHeading && /ai overview/i.test(firstHeading.textContent || "")) firstHeading.remove();
  return clone;
}

function htmlToMarkdownish(root) {
  const lines = [];
  const visit = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = cleanText(node.textContent || "");
      if (text) lines.push(text);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const tag = node.tagName.toLowerCase();
    if (tag === "a" && node.href && isUsefulUrl(node.href)) {
      const text = cleanText(node.innerText || node.textContent || node.href);
      lines.push(`[${text}](${unwrapGoogleUrl(node.href)})`);
      return;
    }
    if (["p", "li", "h2", "h3", "h4", "div"].includes(tag)) {
      const before = lines.length;
      for (const child of node.childNodes) visit(child);
      if (lines.length > before) lines.push("");
      return;
    }
    for (const child of node.childNodes) visit(child);
  };
  visit(root);
  return lines
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/[ \t]+\n/g, "\n")
    .trim();
}

const container = findAIModeContainer();
if (!container) {
  return {
    ai_mode_triggered: false,
    capture_state: "no_ai_mode",
    error: "no_ai_mode",
  };
}

const visibleText = cleanText(container.innerText || container.textContent || "");
if (/you['’]?ve reached your daily limit/i.test(visibleText)) {
  return {
    ai_mode_triggered: true,
    capture_state: "quota_exhausted",
    error: "quota_exhausted",
    text: "",
    markdown: "",
    raw_html: container.outerHTML || "",
    raw_html_capture_method: "ai_mode_container_outer_html",
    sources: [],
  };
}

const contentRoot = container.querySelector('[data-container-id="main-col"]') || container;
const cleaned = stripForContent(contentRoot);
const markdown = htmlToMarkdownish(cleaned);
const sources = extractSources(container);
return {
  ai_mode_triggered: true,
  capture_state: markdown || sources.length ? "content_detected" : "empty_ai_mode_extraction",
  error: markdown || sources.length ? null : "empty_ai_mode_extraction",
  text: cleanText(cleaned.innerText || cleaned.textContent || ""),
  markdown,
  raw_html: container.outerHTML || "",
  raw_html_capture_method: "ai_mode_container_outer_html",
  capture_method: "ai_mode_dom_text",
  markdown_capture_method: "ai_mode_dom_markdownish",
  sources,
};
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
        sources.append(
            {
                "index": len(sources) + 1,
                "url": raw_url,
                "clean_url": clean_url,
                "source": clean_text(source.get("source")) or domain,
                "title": clean_text(source.get("title")),
                "description": clean_text(source.get("description")),
                "favicon_url": str(source.get("favicon_url") or "").strip()
                or (f"https://www.google.com/s2/favicons?domain={domain}&sz=32" if domain else None),
                "extraction_source": clean_text(source.get("extraction_source")) or "ai_mode_dom_links",
            }
        )
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
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.startswith("utm_")
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
