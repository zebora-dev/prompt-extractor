#!/usr/bin/env python3
"""
Debug script: submits a prompt, then pauses mid-stream to inspect DOM
so we can tune the completion-detection selectors.
"""
from __future__ import annotations
import logging, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automated_extraction.config import load_dotenv_if_available
from automated_extraction.perplexity_runner import PerplexityRunner, CHAT_INPUT_SELECTORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
LOGGER = logging.getLogger("debug_perplexity")

load_dotenv_if_available()
profile = os.getenv("PERPLEXITY_CHROME_USER_DATA_DIR") or str(Path(__file__).resolve().parents[1] / ".perplexity-profile")

runner = PerplexityRunner("https://www.perplexity.ai", headless=False, chrome_user_data_dir=profile,
                          login_wait_seconds=60, response_timeout_seconds=180)
runner.start()
driver = runner.driver

try:
    runner.create_fresh_chat()
    el = runner.wait_for_input()
    runner.type_prompt(el, "What is 2+2?")
    runner.click_send(el)

    # Wait 5s for streaming to start then probe DOM
    time.sleep(5)
    LOGGER.info("=== DOM PROBE ===")

    # Dump all buttons
    btns = driver.execute_script("""
        return Array.from(document.querySelectorAll('button')).map(b => ({
            tag: b.tagName,
            text: b.innerText?.trim()?.slice(0,40),
            ariaLabel: b.getAttribute('aria-label'),
            ariaDisabled: b.getAttribute('aria-disabled'),
            disabled: b.disabled,
            type: b.getAttribute('type'),
            className: b.className?.slice(0,80),
        }));
    """)
    LOGGER.info("Buttons on page (%d total):", len(btns))
    for b in btns:
        LOGGER.info("  %s", b)

    # Dump SVG buttons (Perplexity often uses SVG-only buttons)
    svg_btns = driver.execute_script("""
        return Array.from(document.querySelectorAll('button svg')).map(s => {
            const b = s.closest('button');
            return {
                ariaLabel: b?.getAttribute('aria-label'),
                ariaDisabled: b?.getAttribute('aria-disabled'),
                disabled: b?.disabled,
                dataTestid: b?.getAttribute('data-testid'),
                className: b?.className?.slice(0,80),
            };
        });
    """)
    LOGGER.info("SVG buttons (%d):", len(svg_btns))
    for b in svg_btns:
        LOGGER.info("  %s", b)

    # What .prose elements exist?
    prose = driver.execute_script("""
        const els = document.querySelectorAll('.prose, [class*="prose"]');
        return Array.from(els).map(e => ({
            className: e.className?.slice(0,80),
            textLen: e.innerText?.length,
            textSnippet: e.innerText?.slice(0,100),
        }));
    """)
    LOGGER.info("Prose elements (%d):", len(prose))
    for p in prose:
        LOGGER.info("  %s", p)

    # Any stop / cancel button?
    stop = driver.execute_script("""
        const keywords = ['stop', 'cancel', 'interrupt'];
        return Array.from(document.querySelectorAll('button')).filter(b => {
            const label = (b.getAttribute('aria-label') || b.innerText || '').toLowerCase();
            return keywords.some(k => label.includes(k));
        }).map(b => ({
            text: b.innerText?.trim()?.slice(0,40),
            ariaLabel: b.getAttribute('aria-label'),
            ariaDisabled: b.getAttribute('aria-disabled'),
            disabled: b.disabled,
        }));
    """)
    LOGGER.info("Stop/cancel buttons: %s", stop)

    # Probe again after 10 more seconds (should be done or still streaming)
    time.sleep(10)
    LOGGER.info("=== DOM PROBE AFTER +10s ===")
    btns2 = driver.execute_script("""
        return Array.from(document.querySelectorAll('button')).map(b => ({
            ariaLabel: b.getAttribute('aria-label'),
            ariaDisabled: b.getAttribute('aria-disabled'),
            disabled: b.disabled,
            dataTestid: b.getAttribute('data-testid'),
            text: b.innerText?.trim()?.slice(0,30),
        }));
    """)
    for b in btns2:
        if b.get('ariaLabel') or b.get('dataTestid'):
            LOGGER.info("  %s", b)

    prose2 = driver.execute_script("const e = document.querySelectorAll('.prose, [class*=\"prose\"]'); return e.length > 0 ? e[e.length-1].innerText?.slice(0, 200) : 'NONE';")
    LOGGER.info("Latest prose text: %r", prose2)

finally:
    runner.stop()
    LOGGER.info("Done.")
