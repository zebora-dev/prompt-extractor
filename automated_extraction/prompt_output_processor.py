from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote

from .api_client import ApiClient
from .config import Settings

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptOutputProcessResult:
    status: str
    processed_count: int
    updated_count: int
    skipped_count: int
    failed_count: int
    failures: list[dict[str, Any]]


def process_prompt_outputs(
    *,
    settings: Settings,
    saved_outputs: list[dict[str, Any]] | None = None,
    output_id: int | str | None = None,
    batch_id: str | None = None,
    brand_id: str | None = None,
    prompt_id: str | None = None,
    limit: int = 50,
) -> PromptOutputProcessResult:
    api = ApiClient(
        settings.api_base_url,
        settings.anon_key,
        supabase_url=settings.supabase_url,
        prompt_outputs_table=settings.prompt_outputs_table,
        prompt_output_products_table=settings.prompt_output_products_table,
        prompt_output_entities_table=settings.prompt_output_entities_table,
    )
    LOGGER.info(
        "Loading prompt outputs for processing. saved_output_refs=%s output_id=%s batch_id=%s brand_id=%s prompt_id=%s limit=%s",
        len(saved_outputs or []),
        output_id,
        batch_id,
        brand_id,
        prompt_id,
        limit,
    )
    outputs = hydrate_prompt_outputs(
        api=api,
        output_refs=saved_outputs,
        output_id=output_id,
        batch_id=batch_id,
        brand_id=brand_id,
        prompt_id=prompt_id,
        limit=limit,
    )
    LOGGER.info("Loaded %s prompt output(s) for processing.", len(outputs))

    if not outputs:
        LOGGER.info(
            "No prompt outputs found to process. output_id=%s batch_id=%s brand_id=%s prompt_id=%s",
            output_id,
            batch_id,
            brand_id,
            prompt_id,
        )
        return PromptOutputProcessResult("no_outputs", 0, 0, 0, 0, [])

    processed_count = 0
    updated_count = 0
    skipped_count = 0
    failed_count = 0
    failures: list[dict[str, Any]] = []

    for output in outputs:
        processed_count += 1
        output_id = output.get("id") or output.get("output_id") or output.get("prompt_output_id")
        prompt_id = output.get("prompt_id")
        try:
            LOGGER.info(
                "Preparing prompt output for comparison. output_id=%s prompt_id=%s has_raw_html=%s markdown_length=%s response_length=%s",
                output_id,
                prompt_id,
                bool(output.get("raw_html")),
                len(str(output.get("markdown") or "")),
                len(str(output.get("response") or "")),
            )
            patch = build_processed_output_patch(output)
            if not patch:
                skipped_count += 1
                LOGGER.info(
                    "Prompt output processing skipped. output_id=%s prompt_id=%s reason=no_raw_html_no_markdown_or_already_processed",
                    output_id,
                    prompt_id,
                )
                continue

            LOGGER.info(
                "Saving processed prompt output. output_id=%s prompt_id=%s fields=%s updated_markdown=%s",
                output_id,
                prompt_id,
                sorted(patch.keys()),
                "markdown" in patch,
            )
            api.update_prompt_output(output, patch)
            updated_count += 1
            LOGGER.info(
                "Saved processed prompt output. output_id=%s prompt_id=%s enriched_markdown_length=%s enrichment_count=%s",
                output_id,
                prompt_id,
                len(patch.get("markdown") or ""),
                ((patch.get("output_metadata") or {}).get("original_metadata") or {}).get("markdown_enrichment_count"),
            )
        except Exception as exc:
            failed_count += 1
            failures.append({"output_id": output_id, "prompt_id": prompt_id, "error": str(exc)})
            LOGGER.exception(
                "Prompt output processing failed. output_id=%s prompt_id=%s: %s", output_id, prompt_id, exc
            )

    status = "completed" if failed_count == 0 else "completed_with_failures"
    return PromptOutputProcessResult(status, processed_count, updated_count, skipped_count, failed_count, failures)


def hydrate_prompt_outputs(
    *,
    api: ApiClient,
    output_refs: list[dict[str, Any]] | None,
    output_id: int | str | None,
    batch_id: str | None,
    brand_id: str | None,
    prompt_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if output_id:
        LOGGER.info("Fetching prompt output by output_id. output_id=%s", output_id)
        output = api.get_prompt_output(output_id)
        if output:
            LOGGER.info(
                "Fetched prompt output by output_id. output_id=%s prompt_id=%s has_raw_html=%s markdown_length=%s",
                output_id,
                output.get("prompt_id"),
                bool(output.get("raw_html")),
                len(str(output.get("markdown") or output.get("response") or "")),
            )
            return [output]
        LOGGER.warning("No prompt output returned for output_id=%s", output_id)
        return []

    if not output_refs:
        LOGGER.info("No saved output refs supplied; fetching latest outputs from API.")
        outputs = api.get_prompt_outputs(batch_id=batch_id, brand_id=brand_id, prompt_id=prompt_id, limit=limit)
        LOGGER.info("Fetched %s latest prompt output(s) from API.", len(outputs))
        return outputs

    hydrated: list[dict[str, Any]] = []
    for ref in output_refs:
        ref_id = ref.get("id") or ref.get("output_id") or ref.get("prompt_output_id")
        prompt_id = ref.get("prompt_id")
        if ref.get("raw_html") and (ref.get("markdown") or ref.get("response")):
            LOGGER.info(
                "Using already-hydrated prompt output ref. output_id=%s prompt_id=%s",
                ref_id,
                prompt_id,
            )
            hydrated.append(ref)
            continue

        if ref_id:
            LOGGER.info(
                "Hydrating prompt output ref from API by output_id. output_id=%s prompt_id=%s",
                ref_id,
                prompt_id,
            )
            output = api.get_prompt_output(ref_id)
            if output:
                LOGGER.info(
                    "Hydrated prompt output by output_id. output_id=%s prompt_id=%s has_raw_html=%s markdown_length=%s",
                    ref_id,
                    output.get("prompt_id") or prompt_id,
                    bool(output.get("raw_html")),
                    len(str(output.get("markdown") or output.get("response") or "")),
                )
                hydrated.append({**ref, **output})
                continue
            LOGGER.warning(
                "Could not hydrate prompt output by output_id; falling back to prompt filters. output_id=%s prompt_id=%s",
                ref_id,
                prompt_id,
            )

        LOGGER.info(
            "Hydrating prompt output ref from API. output_id=%s prompt_id=%s batch_id=%s brand_id=%s",
            ref_id,
            prompt_id,
            ref.get("batch_id") or batch_id,
            ref.get("brand_id") or brand_id,
        )
        candidates = api.get_prompt_outputs(
            batch_id=ref.get("batch_id") or batch_id,
            brand_id=ref.get("brand_id") or brand_id,
            prompt_id=prompt_id,
            limit=1,
        )
        if candidates:
            LOGGER.info(
                "Hydrated prompt output ref. output_id=%s prompt_id=%s has_raw_html=%s markdown_length=%s",
                ref_id,
                prompt_id,
                bool(candidates[0].get("raw_html")),
                len(str(candidates[0].get("markdown") or candidates[0].get("response") or "")),
            )
            hydrated.append({**ref, **candidates[0]})
        else:
            LOGGER.warning(
                "Could not hydrate prompt output ref; processing may skip it. output_id=%s prompt_id=%s",
                ref_id,
                prompt_id,
            )
            hydrated.append(ref)
    return hydrated


def build_processed_output_patch(output: dict[str, Any]) -> dict[str, Any] | None:
    output_id = output.get("id") or output.get("output_id") or output.get("prompt_output_id")
    prompt_id = output.get("prompt_id")
    raw_html = str(output.get("raw_html") or "")
    captured_markdown = str(output.get("markdown") or "")
    response_text = str(output.get("response") or "")
    original_markdown = captured_markdown or response_text
    if not raw_html.strip():
        LOGGER.info(
            "Prompt output comparison skipped: missing raw_html. output_id=%s prompt_id=%s", output_id, prompt_id
        )
        return None
    if not original_markdown.strip():
        LOGGER.info(
            "Prompt output comparison skipped: missing markdown/response. output_id=%s prompt_id=%s raw_html_length=%s",
            output_id,
            prompt_id,
            len(raw_html),
        )
        return None

    raw_html_markdown = html_to_markdown(raw_html)
    original_was_suspicious = bool(captured_markdown) and looks_like_source_list_capture(
        captured_markdown, raw_html_markdown
    )
    if original_was_suspicious:
        LOGGER.warning(
            "Original markdown looks like a source-list miscapture; using raw_html markdown as the enrichment base. output_id=%s prompt_id=%s original_preview=%r",
            output_id,
            prompt_id,
            original_markdown[:200],
        )
        enriched_markdown = raw_html_markdown
        enrichments = ["replaced_suspicious_source_list_markdown_with_raw_html_markdown"]
    else:
        enriched_markdown, enrichments = enrich_markdown(original_markdown, raw_html_markdown)
    changed = normalize_markdown(enriched_markdown) != normalize_markdown(original_markdown)
    LOGGER.info(
        "Compared prompt output markdown. output_id=%s prompt_id=%s raw_html_length=%s raw_html_markdown_length=%s original_markdown_length=%s enriched_markdown_length=%s enrichment_count=%s changed=%s",
        output_id,
        prompt_id,
        len(raw_html),
        len(raw_html_markdown),
        len(original_markdown),
        len(enriched_markdown),
        len(enrichments),
        changed,
    )
    if enrichments:
        LOGGER.info(
            "Detected prompt output enrichments. output_id=%s prompt_id=%s sample=%s",
            output_id,
            prompt_id,
            enrichments[:5],
        )
    metadata = output.get("output_metadata") if isinstance(output.get("output_metadata"), dict) else {}
    original_metadata = metadata.get("original_metadata") if isinstance(metadata.get("original_metadata"), dict) else {}
    processed_at = datetime.now(UTC).isoformat()

    updated_metadata = {
        **metadata,
        "original_metadata": {
            **original_metadata,
            "prompt_output_process_status": "processed",
            # Keep raw_html_markdown in-memory for comparison only; storing the
            # full parsed body makes output_metadata too large for normal runs.
            # "raw_html_markdown": raw_html_markdown,
            "raw_html_markdown_length": len(raw_html_markdown),
            "original_markdown_suspicious": original_was_suspicious,
            "markdown_enrichment_count": len(enrichments),
            "markdown_enrichments": enrichments[:50],
            "prompt_output_processed_at": processed_at,
        },
    }

    if not changed:
        if original_metadata.get("prompt_output_process_status") == "processed":
            LOGGER.info(
                "Prompt output already marked processed and has no markdown changes. output_id=%s prompt_id=%s",
                output_id,
                prompt_id,
            )
            return None
        LOGGER.info(
            "Prompt output has no markdown differences; metadata will be marked processed. output_id=%s prompt_id=%s",
            output_id,
            prompt_id,
        )
        return {"output_metadata": updated_metadata}

    patch = {
        "response": enriched_markdown,
        "output_metadata": updated_metadata,
    }
    if captured_markdown and not original_was_suspicious:
        patch["markdown"] = enriched_markdown
    elif original_was_suspicious:
        patch["markdown"] = ""
    return patch


def enrich_markdown(original_markdown: str, raw_html_markdown: str) -> tuple[str, list[str]]:
    enriched = strip_generated_enrichment_sections(remove_empty_headings(original_markdown)).rstrip()
    enrichments: list[str] = []

    enriched, section_enrichments = merge_rendered_blocks_by_section(enriched, raw_html_markdown)
    enrichments.extend(section_enrichments)

    missing_images = [
        line
        for line in extract_markdown_images(raw_html_markdown)
        if image_url(line) and image_url(line) not in enriched
    ]
    if missing_images:
        enriched += "\n\n## Images\n\n" + "\n".join(missing_images)
        enrichments.extend(f"image:{image_url(line)}" for line in missing_images if image_url(line))

    missing_links = [
        line for line in extract_markdown_links(raw_html_markdown) if link_url(line) and link_url(line) not in enriched
    ]
    if missing_links:
        enriched += "\n\n## Additional Links\n\n" + "\n".join(f"- {line}" for line in missing_links)
        enrichments.extend(f"link:{link_url(line)}" for line in missing_links if link_url(line))

    missing_rendered_blocks = extract_missing_rendered_blocks(raw_html_markdown, enriched)
    if missing_rendered_blocks:
        enriched += "\n\n## Additional Rendered Content\n\n" + "\n".join(
            f"- {line}" for line in missing_rendered_blocks
        )
        enrichments.extend(f"rendered:{line[:160]}" for line in missing_rendered_blocks)

    return enriched + ("\n" if enriched else ""), enrichments


def merge_rendered_blocks_by_section(original_markdown: str, raw_html_markdown: str) -> tuple[str, list[str]]:
    raw_sections = rendered_blocks_by_h2(raw_html_markdown)
    if not raw_sections:
        return original_markdown, []

    output = original_markdown
    enrichments: list[str] = []

    for heading, block in raw_sections.items():
        if not block.strip():
            continue
        pattern = re.compile(rf"(?m)^##\s+{re.escape(heading)}\s*$")
        match = pattern.search(output)
        if not match:
            continue

        section_start = match.end()
        next_heading = re.search(r"(?m)^##\s+", output[section_start:])
        section_end = section_start + next_heading.start() if next_heading else len(output)
        section = output[section_start:section_end]
        block = filter_card_block_for_section(block, section)
        if not block.strip():
            continue
        if normalize_plain_text(block) in normalize_plain_text(section):
            continue

        section = re.sub(r"^\s*#{3,6}\s*\n+", "\n", section)
        insertion = "\n\n" + block.strip() + "\n\n"
        insertion_index = leading_media_end(section)
        updated_section = section[:insertion_index] + insertion + section[insertion_index:].lstrip("\n")
        output = output[:section_start] + updated_section + output[section_end:]
        enrichments.append(f"section-rendered:{heading}")

    return output, enrichments


def rendered_blocks_by_h2(raw_html_markdown: str) -> dict[str, str]:
    lines = raw_html_markdown.splitlines()
    sections: dict[str, str] = {}
    index = 0
    while index < len(lines):
        heading_match = re.match(r"^##\s+(.+?)\s*$", lines[index].strip())
        if not heading_match:
            index += 1
            continue

        heading = heading_match.group(1).strip()
        cursor = index + 1
        block_lines: list[str] = []
        while cursor < len(lines):
            line = lines[cursor].strip()
            if re.match(r"^##\s+", line):
                break
            if is_start_of_answer_content(line):
                break
            if is_rendered_card_line(line):
                block_lines.append(line)
            elif block_lines and not line:
                block_lines.append("")
            elif block_lines and looks_like_rating(strip_markdown_markup(line)):
                block_lines.append(line)
            elif block_lines and line:
                block_lines.append(line)
            cursor += 1

        block = normalize_card_block(block_lines)
        if block:
            sections[heading] = block
        index = max(cursor, index + 1)

    return sections


def is_start_of_answer_content(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped in {"-", "*"}:
        return True
    if re.match(r"^[-*]\s+\S", stripped):
        return True
    if re.match(r"^\d+\.\s+\S", stripped):
        return True
    if stripped.startswith(("👉", "✅", "❌")):
        return True
    return False


def is_rendered_card_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("!["):
        return True
    if re.match(r"^#{3,6}\s+\S", stripped):
        return True
    plain = strip_markdown_markup(stripped)
    return looks_like_rendered_product_detail(plain) or looks_like_rating(plain)


def normalize_card_block(lines: list[str]) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        key = card_line_key(stripped)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        cleaned.append(stripped)

    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return "\n\n".join(line for line in cleaned if line != "")


def filter_card_block_for_section(block: str, section: str) -> str:
    section_plain = normalize_plain_text(section)
    kept: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        url = image_url(stripped)
        if url and url in section:
            continue
        plain = normalize_plain_text(stripped)
        if plain and plain in section_plain:
            continue
        kept.append(stripped)
    while kept and kept[-1] == "":
        kept.pop()
    return "\n\n".join(line for line in kept if line != "")


def leading_media_end(section: str) -> int:
    match = re.match(r"^(\s*(?:!\[[^\]]*]\([^)]+\)\s*)+)", section)
    return match.end() if match else 0


def card_line_key(line: str) -> str:
    if line.startswith("!["):
        return f"image:{image_url(line) or strip_markdown_markup(line)}"
    if re.match(r"^#{3,6}\s+\S", line):
        return f"title:{rendered_block_key(strip_markdown_markup(line))}"
    plain = strip_markdown_markup(line)
    plain = re.sub(r"\s*•\s*$", "", plain)
    if looks_like_rating(plain):
        return f"rating:{plain}"
    if looks_like_rendered_product_detail(plain):
        return f"detail:{rendered_block_key(plain)}"
    return rendered_block_key(plain)


def html_to_markdown(raw_html: str) -> str:
    parser = ChatGPTHTMLMarkdownParser()
    parser.feed(raw_html)
    parser.close()
    return normalize_markdown(parser.markdown())


class ChatGPTHTMLMarkdownParser(HTMLParser):
    BLOCK_TAGS = {
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "figcaption",
        "figure",
        "footer",
        "header",
        "hr",
        "main",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
        "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.href_stack: list[str | None] = []
        self.list_stack: list[str] = []
        self.skip_depth = 0
        self.heading_level: int | None = None
        self.in_pre = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name: value or "" for name, value in attrs}
        if tag in {"script", "style", "svg"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return

        if tag in self.BLOCK_TAGS:
            self._newline()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading_level = int(tag[1])
            self._newline()
            self.parts.append("#" * self.heading_level + " ")
        elif tag == "li":
            self._newline()
            marker = "1. " if self.list_stack and self.list_stack[-1] == "ol" else "- "
            self.parts.append(marker)
        elif tag in {"ul", "ol"}:
            self.list_stack.append(tag)
        elif tag == "a":
            self.href_stack.append(clean_url(attrs_dict.get("href", "")) or None)
            self.parts.append("[")
        elif tag == "img":
            src = clean_url(attrs_dict.get("src", ""))
            if src:
                alt = clean_text(attrs_dict.get("alt", "")) or "image"
                self._newline()
                self.parts.append(f"![{alt}]({src})")
                self._newline()
        elif tag == "strong" or tag == "b":
            self.parts.append("**")
        elif tag == "em" or tag == "i":
            self.parts.append("*")
        elif tag == "code" and not self.in_pre:
            self.parts.append("`")
        elif tag == "pre":
            self.in_pre = True
            self._newline()
            self.parts.append("```text\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return

        if tag == "a":
            href = self.href_stack.pop() if self.href_stack else None
            self.parts.append(f"]({href})" if href else "]")
        elif tag == "strong" or tag == "b":
            self.parts.append("**")
        elif tag == "em" or tag == "i":
            self.parts.append("*")
        elif tag == "code" and not self.in_pre:
            self.parts.append("`")
        elif tag == "pre":
            self.parts.append("\n```")
            self.in_pre = False
            self._newline()
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._newline()
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading_level = None
            self._newline()
        elif tag in self.BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_pre:
            self.parts.append(html.unescape(data))
            return
        text = clean_text(data)
        if not text:
            return
        if self.parts and not self.parts[-1].endswith(("\n", " ", "[", "`", "*")):
            self.parts.append(" ")
        self.parts.append(text)

    def markdown(self) -> str:
        return "".join(self.parts)

    def _newline(self) -> None:
        if not self.parts:
            return
        current = "".join(self.parts[-2:])
        if current.endswith("\n\n"):
            return
        if current.endswith("\n"):
            self.parts.append("\n")
        else:
            self.parts.append("\n\n")


def extract_markdown_images(markdown: str) -> list[str]:
    seen: set[str] = set()
    images: list[str] = []
    for match in re.finditer(r"!\[[^\]]*]\([^)]+\)", markdown):
        line = match.group(0)
        url = image_url(line)
        if url and url not in seen:
            seen.add(url)
            images.append(line)
    return images


def extract_markdown_links(markdown: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for match in re.finditer(r"(?<!!)\[[^\]]+]\([^)]+\)", markdown):
        line = match.group(0)
        url = link_url(line)
        if not url or url in seen:
            continue
        seen.add(url)
        links.append(line)
    return links


def extract_missing_rendered_blocks(raw_html_markdown: str, original_markdown: str) -> list[str]:
    lines = [line.strip() for line in raw_html_markdown.splitlines()]
    original_plain = normalize_plain_text(original_markdown)
    blocks: list[str] = []
    seen: set[str] = set()

    for index, line in enumerate(lines):
        plain_line = strip_markdown_markup(line)
        if not looks_like_rendered_product_detail(plain_line):
            continue
        if len(plain_line) > 180 or any(marker in plain_line for marker in (" Image:", " RRP:", " Success!", "~~RRP")):
            continue
        plain_line = re.sub(r"\s*•\s*$", "", plain_line)

        title = nearest_previous_title(lines, index)
        snippet_parts = [part for part in (title, plain_line) if part]
        rating = nearest_next_rating(lines, index)
        if rating:
            snippet_parts.append(rating)
        snippet = normalize_inline_text(" ".join(snippet_parts))
        snippet = re.sub(r"\s*•\s*$", "", snippet)
        snippet = re.sub(r"\s*•\s*•\s*", " • ", snippet)
        if not snippet or snippet in original_plain:
            continue
        add_unique_rendered_block(blocks, seen, snippet)

    return blocks


def add_unique_rendered_block(blocks: list[str], seen: set[str], snippet: str) -> None:
    key = rendered_block_key(snippet)
    if key in seen:
        return

    for index, existing in enumerate(list(blocks)):
        existing_key = rendered_block_key(existing)
        if key in existing_key:
            return
        if existing_key in key:
            seen.discard(existing_key)
            blocks[index] = snippet
            seen.add(key)
            return

    seen.add(key)
    blocks.append(snippet)


def rendered_block_key(snippet: str) -> str:
    key = normalize_inline_text(snippet).lower()
    key = re.sub(r"\s*•\s*", " ", key)
    key = re.sub(r"\s+", " ", key)
    return key


def nearest_previous_title(lines: list[str], index: int) -> str:
    for cursor in range(index - 1, max(-1, index - 8), -1):
        if not re.match(r"^#{3,6}\s+\S", lines[cursor].strip()):
            continue
        candidate = strip_markdown_markup(lines[cursor])
        if not candidate:
            continue
        if looks_like_rendered_product_detail(candidate) or looks_like_rating(candidate):
            continue
        if lines[cursor].startswith("!["):
            continue
        if len(candidate) <= 120:
            return candidate
    return ""


def nearest_next_rating(lines: list[str], index: int) -> str:
    for cursor in range(index + 1, min(len(lines), index + 4)):
        candidate = strip_markdown_markup(lines[cursor])
        if looks_like_rating(candidate):
            return candidate
    return ""


def looks_like_rendered_product_detail(text: str) -> bool:
    if not text:
        return False
    has_price = bool(re.search(r"[$£€]\s?\d", text))
    has_vendor = any(
        marker in text.lower() for marker in ("seller", "partner", "amazon", "john lewis", "smyths", "argos", "others")
    )
    return has_price or (has_vendor and "•" in text)


def looks_like_rating(text: str) -> bool:
    return bool(re.fullmatch(r"\d(?:\.\d)?\s*\(\d[\d,]*\)", text.strip()))


def strip_markdown_markup(line: str) -> str:
    if not line:
        return ""
    line = re.sub(r"^#{1,6}\s*", "", line.strip())
    line = re.sub(r"^[-*]\s+", "", line)
    line = re.sub(r"!\[([^\]]*)]\([^)]+\)", r"\1", line)
    line = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", line)
    return normalize_inline_text(line)


def normalize_plain_text(markdown: str) -> str:
    text = markdown or ""
    text = re.sub(r"!\[([^\]]*)]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^[-*]\s+", "", text)
    text = re.sub(r"(?m)^\d+\.\s+", "", text)
    text = text.replace("*", "").replace("_", "").replace("`", "")
    return normalize_inline_text(text)


def remove_empty_headings(markdown: str) -> str:
    return re.sub(r"(?m)^#{1,6}\s*$\n?", "", markdown)


def looks_like_source_list_capture(original_markdown: str, raw_html_markdown: str) -> bool:
    original_lines = [line.strip() for line in original_markdown.splitlines() if line.strip()]
    if len(original_lines) < 6:
        return False

    raw_plain = normalize_plain_text(raw_html_markdown)
    original_plain = normalize_plain_text(original_markdown)
    if len(raw_plain) < max(300, len(original_plain) * 2):
        return False

    sourceish_lines = 0
    for line in original_lines:
        plain = strip_markdown_markup(line)
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+.'-]*", plain)
        if plain.startswith("+") or len(words) <= 3:
            sourceish_lines += 1

    sourceish_ratio = sourceish_lines / max(1, len(original_lines))
    return sourceish_ratio >= 0.75


def strip_generated_enrichment_sections(markdown: str) -> str:
    generated_headings = {"Images", "Additional Links", "Additional Rendered Content"}
    lines = markdown.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        heading = re.match(r"^##\s+(.+?)\s*$", lines[index].strip())
        if heading and heading.group(1).strip() in generated_headings:
            index += 1
            while index < len(lines) and not re.match(r"^##\s+", lines[index].strip()):
                index += 1
            continue
        kept.append(lines[index])
        index += 1
    return "\n".join(kept)


def normalize_inline_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text).replace("&amp;", "&")).strip()


def image_url(markdown_image: str) -> str | None:
    match = re.search(r"!\[[^\]]*]\(([^)]+)\)", markdown_image)
    return match.group(1) if match else None


def link_url(markdown_link: str) -> str | None:
    match = re.search(r"(?<!!)\[[^\]]+]\(([^)]+)\)", markdown_link)
    return match.group(1) if match else None


def clean_url(url: str) -> str:
    if not url:
        return ""
    cleaned = html.unescape(url.strip())
    if cleaned.startswith("https://www.google.com/s2/favicons?domain="):
        domain = cleaned.split("domain=", 1)[1].split("&", 1)[0]
        return unquote(domain)
    return cleaned


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_markdown(markdown: str) -> str:
    lines = [line.rstrip() for line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    compact = "\n".join(lines)
    compact = re.sub(r"(?m)^#{1,6}\s*$\n?", "", compact)
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    compact = re.sub(r"[ \t]{2,}", " ", compact)
    return compact.strip() + ("\n" if compact.strip() else "")
