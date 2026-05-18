"""
Centralised Chrome browser factory for Google extraction runners.

Replaced from Selenium/undetected-chromedriver to nodriver (CDP-native).

nodriver communicates directly with Chrome via CDP without Selenium's WebDriver
protocol, making it significantly harder to fingerprint.

Key improvements over the old Selenium factory:
- No Selenium/WebDriver fingerprint — Chrome never runs in "controlled" mode
- Proxy auth via MV2 Chrome extension (webRequest.onAuthRequired)
- Fresh temp profile on every run (no accumulated bot signals)
- navigator.webdriver override injected via Page.addScriptToEvaluateOnNewDocument

The sync/async boundary is bridged by running a dedicated asyncio event loop in
a background daemon thread. All async operations are dispatched into that loop
via asyncio.run_coroutine_threadsafe(), keeping the callers fully synchronous.

Chrome startup: we launch Chrome ourselves (rather than via uc.start) so we can
wait up to 30 seconds for the CDP debug port.  Non-headless Chrome on Fly.io
takes ~28s to open the port (waiting for dbus timeouts); nodriver's built-in
retry window is only 2.75s.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

LOGGER = logging.getLogger(__name__)

# Chrome 135-137 on Windows/macOS — updated May 2026.
_CHROME_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.100 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.93 Safari/537.36",
]

# Injected before any page script runs to mask automation.
_WEBDRIVER_OVERRIDE_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

_WARMUP_QUERIES = [
    "weather today",
    "news today",
    "latest news uk",
    "bbc news",
    "time right now",
]

# ---------------------------------------------------------------------------
# Background event loop thread
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Return the shared background asyncio event loop, creating it if needed."""
    global _loop
    with _loop_lock:
        if _loop is None or not _loop.is_running():
            ready = threading.Event()

            def _run() -> None:
                global _loop
                _loop = asyncio.new_event_loop()
                asyncio.set_event_loop(_loop)
                _loop.call_soon(ready.set)
                _loop.run_forever()

            t = threading.Thread(target=_run, daemon=True, name="nodriver-event-loop")
            t.start()
            ready.wait(timeout=10)
    return _loop  # type: ignore[return-value]


def _run_sync(coro) -> Any:
    """Run a coroutine on the background event loop and return the result."""
    loop = _get_or_create_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=120)


# ---------------------------------------------------------------------------
# Element wrapper
# ---------------------------------------------------------------------------

class NodriverElement:
    """
    Thin sync wrapper around a nodriver Element object.

    Provides a duck-type-compatible surface for the Google runners and the PAA
    suggestions runner (which expects Selenium-ish element objects).

    nodriver Element attributes are accessed via element.attrs (a ContraDict)
    or element[attr_name]. We bridge these to the Selenium-style
    get_attribute(name) API expected by the runners.
    """

    def __init__(self, elem, tab) -> None:
        # elem: a nodriver Element object
        self._elem = elem
        self._tab = tab

    # --- properties --------------------------------------------------------

    @property
    def text(self) -> str:
        """Return element's innerText via JS apply."""
        try:
            result = _run_sync(self._elem.apply(
                "(el) => el.innerText || el.textContent || ''"
            ))
            return str(result or "")
        except Exception:
            # fallback to nodriver's built-in text property (direct text nodes only)
            try:
                return self._elem.text_all or ""
            except Exception:
                return ""

    # --- attribute access --------------------------------------------------

    def get_attribute(self, name: str) -> str | None:
        """Return element attribute value, or None if absent."""
        try:
            # First try the nodriver attrs ContraDict (populated from DOM snapshot)
            val = self._elem.attrs.get(name) if hasattr(self._elem, "attrs") else None
            if val is not None:
                return str(val)
            # Fallback: get via JS in case attrs is stale
            result = _run_sync(self._elem.apply(
                f"(el) => el.getAttribute({name!r})"
            ))
            return result if result is not None else None
        except Exception:
            return None

    # --- interaction -------------------------------------------------------

    def click(self) -> None:
        """Click element via JS dispatched events."""
        try:
            _run_sync(self._elem.apply(
                """(el) => {
                    el.scrollIntoView({block:'center', inline:'center'});
                    el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerType:'mouse'}));
                    el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));
                    el.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerType:'mouse'}));
                    el.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
                    el.click();
                }"""
            ))
        except Exception as exc:
            LOGGER.debug("NodriverElement.click failed: %s", exc)

    def send_keys(self, text: str) -> None:
        """Type text into the element character by character via CDP Input events."""
        import nodriver as uc  # type: ignore[import-untyped]
        cdp = uc.cdp
        # Focus the element first
        try:
            _run_sync(self._elem.apply("(el) => { el.focus(); }"))
        except Exception:
            pass
        time.sleep(0.05)
        for char in text:
            try:
                _run_sync(self._tab.send(
                    cdp.input_.dispatch_key_event(
                        type_="keyDown",
                        key=char,
                        text=char,
                    )
                ))
                _run_sync(self._tab.send(
                    cdp.input_.dispatch_key_event(
                        type_="keyUp",
                        key=char,
                        text=char,
                    )
                ))
            except Exception as exc:
                LOGGER.debug("send_keys CDP key event failed for char %r: %s — skipping", char, exc)
            time.sleep(random.uniform(0.04, 0.12))

    # --- find children -----------------------------------------------------

    def find_element(self, by: str, value: str) -> "NodriverElement":
        """Find first child element matching selector. Raises on not found."""
        results = self.find_elements(by, value)
        if not results:
            raise RuntimeError(f"NoSuchElement: {by}={value!r}")
        return results[0]

    def find_elements(self, by: str, value: str) -> list["NodriverElement"]:
        """Find all child elements matching CSS selector."""
        css = _by_to_css(by, value)
        if css is None:
            return []
        try:
            # Use nodriver's query_selector_all on the element (async)
            elems = _run_sync(self._elem.query_selector_all(css))
            if not elems:
                return []
            return [NodriverElement(e, self._tab) for e in elems]
        except Exception as exc:
            LOGGER.debug("NodriverElement.find_elements failed: %s", exc)
            return []

    # --- internal ----------------------------------------------------------

    def _unwrap(self):
        """Return the underlying nodriver Element object."""
        return self._elem


# ---------------------------------------------------------------------------
# Main browser class
# ---------------------------------------------------------------------------

class NodriverBrowser:
    """
    Synchronous wrapper around a nodriver browser + main tab.

    Designed to be a drop-in replacement for Selenium Chrome in the Google
    extraction runners. Also provides enough duck-typing for the PAA suggestions
    runner which accesses driver.find_elements / driver.execute_script.
    """

    def __init__(self, browser, tab, proxy_server=None) -> None:
        self._browser = browser
        self._tab = tab
        self._proxy_server = proxy_server

    # --- navigation --------------------------------------------------------

    def navigate(self, url: str) -> None:
        """Navigate to URL and wait for load."""
        _run_sync(self._tab.get(url))

    # Alias used by existing code that calls driver.get(url)
    def get(self, url: str) -> None:
        self.navigate(url)

    # --- URL ---------------------------------------------------------------

    @property
    def current_url(self) -> str:
        try:
            result = _run_sync(self._tab.evaluate("window.location.href"))
            return str(result or "")
        except Exception:
            return ""

    # --- JS execution ------------------------------------------------------

    def execute_script(self, script: str, *args) -> Any:
        """
        Execute JavaScript in the page context, returning the result.

        When NodriverElement objects are passed as args, the script is called
        via CDP call_function_on so the element is available as arguments[0],
        arguments[1], etc. — matching the Selenium convention.

        When no element args are present, uses tab.evaluate() directly.
        """
        import nodriver as uc  # type: ignore[import-untyped]
        cdp = uc.cdp

        # Separate element args from plain value args
        nodriver_elements = []
        plain_args = []
        for arg in args:
            if isinstance(arg, NodriverElement):
                nodriver_elements.append(arg._elem)
            else:
                plain_args.append(arg)

        if nodriver_elements:
            # Use the first element as the target for call_function_on.
            # Wrap the script so it receives remaining elements as extra args.
            primary_elem = nodriver_elements[0]

            async def _call_on_elem():
                # Resolve the element to get its remote object id
                import cdp as cdp_mod  # type: ignore[import-not-found]
                remote = await self._tab.send(
                    cdp.dom.resolve_node(backend_node_id=primary_elem.backend_node_id)
                )
                object_id = remote.object_id

                # Build call arguments — primary element first, then any remaining elements
                call_args = [cdp.runtime.CallArgument(object_id=object_id)]
                for extra_elem in nodriver_elements[1:]:
                    extra_remote = await self._tab.send(
                        cdp.dom.resolve_node(backend_node_id=extra_elem.backend_node_id)
                    )
                    call_args.append(
                        cdp.runtime.CallArgument(object_id=extra_remote.object_id)
                    )
                # Plain value args
                for val in plain_args:
                    call_args.append(cdp.runtime.CallArgument(value=val))

                # Wrap the script as a function that receives all args positionally
                # (matches Selenium's arguments[0], arguments[1], ... convention)
                fn = f"function(){{ {script} }}"
                result, exc_details = await self._tab.send(
                    cdp.runtime.call_function_on(
                        fn,
                        object_id=object_id,
                        arguments=call_args,
                        return_by_value=True,
                        user_gesture=True,
                        await_promise=True,
                    )
                )
                if exc_details:
                    LOGGER.debug("execute_script exception: %s", exc_details)
                    return None
                return result.value if result else None

            return _run_sync(_call_on_elem())
        else:
            # No element arguments — use tab.evaluate() with inline args if needed.
            #
            # Two issues to solve:
            # 1. Top-level `return` is a SyntaxError in nodriver's tab.evaluate()
            #    (which runs JS as a module expression, not inside a function body).
            #    Fix: wrap in an IIFE so `return` is valid.
            # 2. nodriver's CDP serialisation converts JS plain objects to
            #    [[key, {type, value}], ...] pairs instead of Python dicts.
            #    Fix: JSON.stringify the IIFE result so nodriver receives a string;
            #    we json.loads() it back to a proper Python value.
            if plain_args:
                args_json = json.dumps(plain_args)
                js = (
                    f"JSON.stringify((function(){{"
                    f"  var arguments = {args_json};"
                    f"  {script}"
                    f"}}()))"
                )
                try:
                    raw = _run_sync(self._tab.evaluate(js))
                    return json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    # Fallback: plain IIFE without JSON wrapper
                    plain = (
                        f"(function(){{"
                        f"  var arguments = {args_json};"
                        f"  {script}"
                        f"}}())"
                    )
                    return _run_sync(self._tab.evaluate(plain))
            else:
                if script.lstrip().startswith("return"):
                    # JSON.stringify bypasses CDP deep-serialisation and guarantees
                    # a plain Python value after json.loads().
                    js = f"JSON.stringify((function(){{ {script} }}()))"
                    try:
                        raw = _run_sync(self._tab.evaluate(js))
                        return json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        # Fallback: IIFE without JSON wrapper
                        return _run_sync(self._tab.evaluate(f"(function(){{ {script} }}())"))
                return _run_sync(self._tab.evaluate(script))

    # Stub for Selenium CDP cmd — stealth is applied at startup.
    def execute_cdp_cmd(self, cmd: str, params: dict) -> None:
        pass

    # --- window size -------------------------------------------------------

    def set_window_size(self, width: int, height: int) -> None:
        try:
            import nodriver as uc  # type: ignore[import-untyped]
            cdp = uc.cdp
            _run_sync(self._tab.send(
                cdp.browser.set_window_bounds(
                    window_id=1,
                    bounds=cdp.browser.Bounds(
                        width=width,
                        height=height,
                    ),
                )
            ))
            LOGGER.debug("Window resized to %sx%s via CDP.", width, height)
        except Exception as exc:
            LOGGER.debug("set_window_size via CDP failed: %s — trying JS.", exc)
            try:
                _run_sync(self._tab.evaluate(f"window.resizeTo({width},{height});"))
            except Exception as exc2:
                LOGGER.debug("set_window_size JS fallback failed: %s", exc2)

    # --- element finding ---------------------------------------------------

    def find_elements_by_css(self, css: str) -> list[NodriverElement]:
        """Find all elements matching a CSS selector."""
        return self._query_css(css)

    def _query_css(self, css: str) -> list[NodriverElement]:
        try:
            elems = _run_sync(self._tab.query_selector_all(css))
            if not elems:
                return []
            return [NodriverElement(e, self._tab) for e in elems]
        except Exception as exc:
            LOGGER.debug("find_elements_by_css(%r) failed: %s", css, exc)
            return []

    # Selenium-compatible aliases used by PAA suggestions runner
    def find_elements(self, by: str, value: str) -> list[NodriverElement]:
        css = _by_to_css(by, value)
        if css is None:
            return []
        return self._query_css(css)

    def find_element(self, by: str, value: str) -> NodriverElement:
        results = self.find_elements(by, value)
        if not results:
            raise RuntimeError(f"NoSuchElement: {by}={value!r}")
        return results[0]

    # --- quit --------------------------------------------------------------

    def quit(self) -> None:
        try:
            _run_sync(self._browser.stop())
        except Exception as exc:
            LOGGER.debug("Browser quit failed (non-fatal): %s", exc)
        if self._proxy_server is not None:
            try:
                loop = _get_or_create_loop()
                asyncio.run_coroutine_threadsafe(
                    _close_server(self._proxy_server), loop
                ).result(timeout=5)
            except Exception as exc:
                LOGGER.debug("Local proxy server close failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# By constants shim (so callers don't need to import from selenium)
# ---------------------------------------------------------------------------

class By:
    CSS_SELECTOR = "css selector"
    XPATH = "xpath"
    ID = "id"
    NAME = "name"
    TAG_NAME = "tag name"
    CLASS_NAME = "class name"
    LINK_TEXT = "link text"
    PARTIAL_LINK_TEXT = "partial link text"


def _by_to_css(by: str, value: str) -> str | None:
    """Convert a Selenium By strategy + value to a CSS selector string."""
    by_lower = by.lower()
    if "css" in by_lower:
        return value
    if by_lower in ("id", "by.id"):
        return f"#{value}"
    if by_lower in ("name", "by.name"):
        return f"[name='{value}']"
    if by_lower in ("tag name", "tag_name", "by.tag_name"):
        return value
    if by_lower in ("class name", "class_name", "by.class_name"):
        return f".{value}"
    # XPATH is not supported via CSS — return None so callers get an empty list
    if by_lower in ("xpath", "by.xpath"):
        LOGGER.debug("XPath selector not supported in NodriverBrowser: %r", value)
        return None
    return value


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def build_nodriver_browser(
    *,
    headless: bool = False,
    proxy_url: str | None = None,
) -> NodriverBrowser:
    """
    Build and return a NodriverBrowser ready for Google extraction.

    - Fresh temp profile on every call (no persistent state).
    - Proxy auth via CDP Fetch.AuthRequired (no Chrome extension).
    - navigator.webdriver override injected via addScriptToEvaluateOnNewDocument.
    - Window resized to VNC_SCREEN dimensions if set.
    """
    user_agent = random.choice(_CHROME_USER_AGENTS)
    LOGGER.info(
        "Building nodriver browser. proxy=%s headless=%s ua=%s",
        "yes" if proxy_url else "no",
        headless,
        user_agent[:80],
    )

    t0 = time.time()
    browser, tab, proxy_server = _run_sync(_start_nodriver(
        headless=headless,
        proxy_url=proxy_url,
        user_agent=user_agent,
    ))
    LOGGER.info("nodriver browser started in %.1fs.", time.time() - t0)

    nb = NodriverBrowser(browser, tab, proxy_server=proxy_server)

    # Resize window to VNC display dimensions.
    vnc_screen = os.getenv("VNC_SCREEN", "")
    if vnc_screen:
        try:
            parts = vnc_screen.split("x")
            w, h = int(parts[0]), int(parts[1])
            nb.set_window_size(w, h)
            LOGGER.info("Window resized to VNC screen: %sx%s", w, h)
        except Exception as exc:
            LOGGER.debug("Window resize failed (non-fatal): %s", exc)

    LOGGER.info("nodriver browser ready.")
    return nb


async def _close_server(server: asyncio.Server) -> None:
    server.close()
    await server.wait_closed()


async def _start_local_auth_proxy(
    upstream_host: str,
    upstream_port: int,
    username: str,
    password: str,
    local_port: int,
) -> asyncio.Server:
    """
    Start a minimal HTTP CONNECT proxy on 127.0.0.1:local_port that adds
    Basic auth when forwarding CONNECT tunnels to the upstream proxy.

    Chrome connects to this local proxy without needing credentials; the local
    proxy injects the Proxy-Authorization header for every CONNECT request.
    This avoids all MV2 extension / CDP Fetch issues with proxy auth.
    """
    import base64
    auth_value = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        try:
            req_line = await asyncio.wait_for(client_r.readline(), timeout=10)
            if not req_line:
                return
            parts = req_line.decode(errors="replace").split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                client_w.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                await client_w.drain()
                return
            target = parts[1]
            while True:
                line = await asyncio.wait_for(client_r.readline(), timeout=5)
                if not line or line in (b"\r\n", b"\n"):
                    break
            up_r, up_w = await asyncio.wait_for(
                asyncio.open_connection(upstream_host, upstream_port), timeout=10
            )
            connect_req = (
                f"CONNECT {target} HTTP/1.1\r\n"
                f"Host: {target}\r\n"
                f"Proxy-Authorization: {auth_value}\r\n"
                f"\r\n"
            )
            up_w.write(connect_req.encode())
            await up_w.drain()
            status_line = await asyncio.wait_for(up_r.readline(), timeout=10)
            while True:
                line = await asyncio.wait_for(up_r.readline(), timeout=5)
                if not line or line in (b"\r\n", b"\n"):
                    break
            if b"200" in status_line:
                client_w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await client_w.drain()
                await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))
            else:
                LOGGER.debug("Upstream proxy refused CONNECT: %s", status_line)
                client_w.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await client_w.drain()
                up_w.close()
        except Exception as exc:
            LOGGER.debug("Local proxy handler error: %s", exc)
            try:
                client_w.close()
            except Exception:
                pass

    server = await asyncio.start_server(_handle, "127.0.0.1", local_port)
    LOGGER.info(
        "Local auth proxy listening on 127.0.0.1:%s → %s:%s",
        local_port, upstream_host, upstream_port,
    )
    return server


async def _wait_for_chrome_debug_port(host: str, port: int, timeout: float = 30.0) -> None:
    """Poll Chrome's debug endpoint until it responds or timeout is reached."""
    url = f"http://{host}:{port}/json/version"
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: urllib.request.urlopen(url, timeout=2)
            )
            return
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(0.5)
    raise TimeoutError(
        f"Chrome debug port {host}:{port} not ready after {timeout}s. Last error: {last_exc}"
    )


async def _start_nodriver(
    *,
    headless: bool,
    proxy_url: str | None,
    user_agent: str,
):
    """Async coroutine: start nodriver with the given config.

    We launch Chrome ourselves (rather than letting nodriver do it) so we can
    wait up to 30 seconds for the CDP debug port to open.  This is necessary
    on Linux / Fly.io where non-headless Chrome can take 15+ seconds to open
    the debug port (waiting for dbus timeouts) — far longer than nodriver's
    built-in 2.75 second retry window.
    """
    import nodriver as uc  # type: ignore[import-untyped]
    import nodriver.core.util as uc_util
    cdp = uc.cdp

    browser_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        f"--user-agent={user_agent}",
        f"--window-size={random.randint(1280, 1920)},{random.randint(720, 1080)}",
    ]
    if headless:
        browser_args.append("--headless=new")

    # Proxy: start a local CONNECT proxy that injects Basic auth credentials.
    # This avoids all MV2 extension / CDP Fetch issues — Chrome talks to a
    # local proxy that requires no auth; the local proxy adds Proxy-Authorization
    # when forwarding CONNECT tunnels to the upstream.
    _local_proxy_server: asyncio.Server | None = None
    if proxy_url:
        parsed = urllib.parse.urlparse(proxy_url)
        upstream_host = parsed.hostname or ""
        upstream_port = parsed.port or 8080
        proxy_username = urllib.parse.unquote(parsed.username or "")
        proxy_password = urllib.parse.unquote(parsed.password or "")
        local_proxy_port = uc_util.free_port()
        _local_proxy_server = await _start_local_auth_proxy(
            upstream_host, upstream_port, proxy_username, proxy_password or "", local_proxy_port
        )
        browser_args.append(f"--proxy-server=http://127.0.0.1:{local_proxy_port}")
        LOGGER.info(
            "Proxy configured via local auth proxy: 127.0.0.1:%s → %s:%s",
            local_proxy_port, upstream_host, upstream_port,
        )

    # Build a nodriver Config so we get the correct binary path + default args.
    # We start Chrome ourselves to bypass nodriver's 2.75s connection timeout.
    config = uc.Config(
        headless=headless,
        sandbox=False,
        browser_args=browser_args,
    )

    debug_host = "127.0.0.1"
    debug_port = uc_util.free_port()
    config.host = debug_host
    config.port = debug_port

    LOGGER.info(
        "Launching Chrome: exe=%s headless=%s port=%s",
        config.browser_executable_path, headless, debug_port,
    )
    _chrome_proc = await asyncio.create_subprocess_exec(
        config.browser_executable_path,
        *config(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Wait up to 30s for Chrome to open its debug port before connecting.
    startup_timeout = 30.0
    LOGGER.info("Waiting up to %.0fs for Chrome debug port %s:%s…", startup_timeout, debug_host, debug_port)
    await _wait_for_chrome_debug_port(debug_host, debug_port, timeout=startup_timeout)
    LOGGER.info("Chrome debug port ready.")

    # Connect nodriver to the already-running Chrome instance.
    browser = await uc.start(host=debug_host, port=debug_port)
    tab = browser.main_tab

    # Inject navigator.webdriver override before any page script runs.
    try:
        await tab.send(
            cdp.page.add_script_to_evaluate_on_new_document(
                source=_WEBDRIVER_OVERRIDE_SCRIPT,
            )
        )
        LOGGER.debug("navigator.webdriver CDP override injected.")
    except Exception as exc:
        LOGGER.debug("navigator.webdriver CDP override failed (non-fatal): %s", exc)

    return browser, tab, _local_proxy_server


# ---------------------------------------------------------------------------
# Warmup and search helpers (sync wrappers keeping original signatures)
# ---------------------------------------------------------------------------

def warmup_google_session(browser: NodriverBrowser, warmup_url: str = "https://www.google.com") -> None:
    """
    Establish a natural-looking Google session before the first real search.

    Steps:
    1. Visit the homepage and pause briefly.
    2. Dismiss any cookie consent overlays.
    3. Type a benign warmup query into the search box character-by-character.
    4. Submit and wait for the results page to load.
    """
    try:
        LOGGER.info("Warming up Google session via %s", warmup_url)
        browser.navigate(warmup_url)
        time.sleep(random.uniform(1.5, 2.5))

        current_url = _log_page_state(browser, "warmup: after navigation")
        _dismiss_google_overlays(browser, origin_url=current_url)
        _log_page_state(browser, "warmup: after overlay dismissal")

        search_box = None
        for css in ("textarea[name='q']", "input[name='q']"):
            elems = browser.find_elements_by_css(css)
            if elems:
                search_box = elems[0]
                break

        if search_box is None:
            LOGGER.warning("Warmup: could not find search box — skipping warmup query.")
            return

        search_box.click()
        time.sleep(random.uniform(0.3, 0.6))
        warmup_query = random.choice(_WARMUP_QUERIES)
        LOGGER.info("Typing warmup query: %r", warmup_query)
        search_box.send_keys(warmup_query)
        time.sleep(random.uniform(0.4, 0.9))

        # Press Enter
        _press_enter(browser)
        time.sleep(random.uniform(2.5, 4.0))
        LOGGER.info("Warmup search complete.")
    except Exception as exc:
        LOGGER.warning("Session warmup failed (non-fatal): %s", exc)


def _log_page_state(browser: NodriverBrowser, label: str) -> str:
    """Log current URL and title; returns the URL string."""
    try:
        url = str(_run_sync(browser._tab.evaluate("window.location.href")) or "")
        title = str(_run_sync(browser._tab.evaluate("document.title")) or "")
        LOGGER.info("%s — url=%s title=%r", label, url, title[:120])
        return url
    except Exception as exc:
        LOGGER.debug("_log_page_state failed: %s", exc)
        return ""


def search_via_box(browser: NodriverBrowser, query: str) -> None:
    """
    Submit a search by navigating to the Google homepage and typing into the
    search box character by character.

    Always returns to the homepage before searching so we get a clean,
    reliably interactable search textarea.
    """
    LOGGER.info("search_via_box: navigating to google.com homepage.")
    browser.navigate("https://www.google.com")
    time.sleep(random.uniform(1.2, 2.0))

    current_url = _log_page_state(browser, "search_via_box: after navigation")
    _dismiss_google_overlays(browser, origin_url=current_url)

    # After overlay dismissal the page may have reloaded; re-check URL.
    current_url = _log_page_state(browser, "search_via_box: after overlay dismissal")

    search_box = None
    for css in ("textarea[name='q']", "input[name='q']"):
        elems = browser.find_elements_by_css(css)
        if elems:
            search_box = elems[0]
            break

    if search_box is None:
        raise RuntimeError(
            f"search_via_box: could not find Google search box (url={current_url!r})"
        )

    search_box.click()
    time.sleep(random.uniform(0.3, 0.6))

    LOGGER.info("search_via_box: typing query (%d chars).", len(query))
    search_box.send_keys(query)
    time.sleep(random.uniform(0.4, 0.8))

    _press_enter(browser)
    # Brief pause for results to start loading.
    time.sleep(random.uniform(1.5, 2.5))


def _press_enter(browser: NodriverBrowser) -> None:
    """Send a Return keypress via CDP Input."""
    try:
        import nodriver as uc  # type: ignore[import-untyped]
        cdp = uc.cdp
        _run_sync(browser._tab.send(
            cdp.input_.dispatch_key_event(
                type_="keyDown",
                key="Return",
                text="\r",
                windows_virtual_key_code=13,
                native_virtual_key_code=13,
            )
        ))
        _run_sync(browser._tab.send(
            cdp.input_.dispatch_key_event(
                type_="keyUp",
                key="Return",
                text="\r",
                windows_virtual_key_code=13,
                native_virtual_key_code=13,
            )
        ))
    except Exception as exc:
        LOGGER.debug("_press_enter CDP failed: %s — trying JS submit.", exc)
        try:
            _run_sync(browser._tab.evaluate(
                "const f=document.activeElement&&document.activeElement.form;if(f)f.submit();"
            ))
        except Exception:
            pass


def _dismiss_google_overlays(browser: NodriverBrowser, origin_url: str = "") -> None:
    """
    Click through Google's cookie consent / sign-in prompts if visible.

    Handles both the standard google.com modal overlay and the redirect to
    consent.google.com that Google UK serves on fresh profiles via UK proxies.
    After accepting, if still on a consent/accounts page, navigates back to
    google.com so subsequent search-box lookups work.
    """
    _CONSENT_TEXTS = [
        "Accept all", "Reject all", "I agree", "Agree", "Accept",
        "Tout accepter", "Alles akzeptieren",  # French / German variants
    ]
    try:
        buttons = browser.find_elements_by_css("button")
        LOGGER.debug("Overlay check: found %d button(s) on page.", len(buttons))
        for btn in buttons:
            btn_text = btn.text.strip()
            if not btn_text:
                continue
            if any(t.lower() in btn_text.lower() for t in _CONSENT_TEXTS):
                LOGGER.info("Dismissing Google overlay button: %r", btn_text[:60])
                btn.click()
                time.sleep(random.uniform(0.8, 1.5))
                break
        else:
            LOGGER.debug("No consent button matched among %d button(s).", len(buttons))
    except Exception as exc:
        LOGGER.debug("Overlay dismissal (non-fatal): %s", exc)

    # After consent, Google may land on consent.google.com or accounts.google.com.
    # Navigate explicitly back to google.com so we can find the search box.
    try:
        current_url = str(_run_sync(browser._tab.evaluate("window.location.href")) or "")
        _NON_SEARCH_HOSTS = ("consent.google.", "accounts.google.", "myaccount.google.")
        if any(h in current_url for h in _NON_SEARCH_HOSTS):
            LOGGER.info(
                "Still on non-search page after consent (%s) — navigating to google.com", current_url
            )
            browser.navigate("https://www.google.com")
            time.sleep(random.uniform(1.2, 2.0))
    except Exception as exc:
        LOGGER.debug("Post-consent redirect check failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Proxy helpers (kept for extraction.py compatibility)
# ---------------------------------------------------------------------------

def resolve_proxy_url(use_proxy: bool) -> str | None:
    """Return the proxy URL to use, or None if proxying is disabled."""
    if not use_proxy:
        return None
    url = os.getenv("GOOGLE_PROXY_URL", "").strip()
    if not url:
        LOGGER.warning("use_proxy=True but GOOGLE_PROXY_URL env var is not set — running without proxy.")
        return None
    LOGGER.info("Proxy URL resolved from GOOGLE_PROXY_URL. host=%s", url.split("@")[-1] if "@" in url else url)
    _verify_proxy_ip(url)
    return url


def _verify_proxy_ip(proxy_url: str) -> None:
    """Make a quick HTTP request through the proxy to log the exit IP. Non-fatal."""
    try:
        import requests

        resp = requests.get(
            "https://api.ipify.org",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=10,
        )
        LOGGER.info("Proxy exit IP: %s", resp.text.strip())
    except Exception as exc:
        LOGGER.warning("Proxy IP verification failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Legacy alias — kept so any remaining internal import of build_google_driver
# raises a clear error rather than an AttributeError.
# ---------------------------------------------------------------------------

def build_google_driver(**kwargs):  # type: ignore[no-untyped-def]
    raise RuntimeError(
        "build_google_driver() has been removed. Use build_nodriver_browser() instead."
    )
