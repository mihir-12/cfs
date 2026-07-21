"""Select relevant matches, per-element references, and extracted variables.

For each requirement and its cosine candidate EDSL elements, asks Gemini to
(0) filter to relevant matches, (1) extract phrases from the requirement,
(2) pick contained names of kept elements, and (3) map phrases to
Element / Element.member paths. Results are cached per requirement.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

from edsl_parser import EdslElement

DEFAULT_CACHE = "reference_selection_cache.json"
DEFAULT_MODEL = "gemini-flash-latest"
BATCH_SIZE = 10
MAX_RETRIES = 5

# One requirement + all its candidate elements per prompt item.
ReqMatch = tuple[str, str, list[EdslElement]]  # (req_id, req_text, candidates)

_PROMPT_HEADER = """\
You are tracing NASA cFS/CF (CCSDS File Delivery Protocol) requirements to \
the EDSL design model.

For each item below you are given a requirement and candidate EDSL elements \
from embedding similarity. Work in this order:

0) RELEVANCE — From the candidate list only, choose relevant_matches (exact \
element names). Keep an element only if the requirement is actually about \
that construct (command, counter, event, structure, etc.). Discard neighbors \
that merely share vague domain wording (e.g. a scheduling interface for a \
No-Op / command-counter requirement).

1) EXTRACT — From the requirement text alone, list notable entities and \
actions as short phrases (commands, counters, messages, behaviors, etc.). \
Do not invent EDSL names here; stay close to the requirement wording.

2) REFERENCES — For each element in relevant_matches only, list which of its \
contained names (fields / members / commands / parameters) the requirement \
is specifically about. Use exact names from the source / contained-names \
list. Empty list if none stand out. Do not include references for elements \
you dropped in step 0.

3) MAP — For each phrase from step 1, set edsl_mapping to zero or more paths \
drawn only from relevant_matches (and the names from step 2 where \
relevant). Path format: "Element" or "Element.member". If nothing among the \
kept matches fits a phrase, keep the phrase and use an empty edsl_mapping list.

Return ONLY a JSON object mapping each item id (e.g. "item_0") to an object:
{
  "relevant_matches": ["ElementName", ...],
  "references": { "<ElementName>": ["containedName", ...], ... },
  "extracted_variables": [
    { "name": "<phrase from step 1>", "edsl_mapping": ["Element.member", ...] }
  ]
}

Items:
"""


def _req_key(req_id: str, req_text: str, elements: list[EdslElement]) -> str:
    """Cache key for one requirement and its candidate element set."""
    parts = [f"{req_id}:{req_text}"]
    for el in sorted(elements, key=lambda e: (e.package, e.name)):
        parts.append(f"{el.package}:{el.name}:{el.raw_element_text}")
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:10]
    return f"{req_id}:{digest}"


def reference_key(req_id: str, req_text: str, elements: list[EdslElement]) -> str:
    """Public lookup key so callers can read results without reimplementing hashing."""
    return _req_key(req_id, req_text, elements)


def _path_allow_list(elements: list[EdslElement]) -> set[str]:
    """Valid edsl_mapping paths: Element and Element.member for each element."""
    allowed: set[str] = set()
    for el in elements:
        allowed.add(el.name)
        for name in el.get_contained_names():
            allowed.add(f"{el.name}.{name}")
    return allowed


def _element_by_name(elements: list[EdslElement]) -> dict[str, EdslElement]:
    return {el.name: el for el in elements}


def _validate_result(
    raw: Any, elements: list[EdslElement]
) -> dict[str, Any]:
    """Filter LLM output to valid relevant_matches, references, and paths."""
    name_to_el = _element_by_name(elements)
    candidate_names = set(name_to_el)

    relevant_matches: list[str] = []
    if isinstance(raw, dict) and isinstance(raw.get("relevant_matches"), list):
        seen: set[str] = set()
        for name in raw["relevant_matches"]:
            if (
                isinstance(name, str)
                and name in candidate_names
                and name not in seen
            ):
                relevant_matches.append(name)
                seen.add(name)

    if not relevant_matches and elements:
        # Safety net: keep the first candidate (highest cosine from caller order).
        relevant_matches = [elements[0].name]

    kept_elements = [name_to_el[n] for n in relevant_matches if n in name_to_el]
    kept_names = set(relevant_matches)
    allowed_paths = _path_allow_list(kept_elements)

    references: dict[str, list[str]] = {n: [] for n in relevant_matches}
    if isinstance(raw, dict) and isinstance(raw.get("references"), dict):
        for el_name, names in raw["references"].items():
            if el_name not in kept_names:
                continue
            el = name_to_el.get(el_name)
            if el is None or not isinstance(names, list):
                continue
            valid = el.get_contained_names()
            references[el_name] = [
                n for n in names if isinstance(n, str) and n in valid
            ]

    extracted: list[dict[str, Any]] = []
    raw_extracted = raw.get("extracted_variables") if isinstance(raw, dict) else None
    if isinstance(raw_extracted, list):
        for item in raw_extracted:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            mapping = item.get("edsl_mapping", [])
            if not isinstance(mapping, list):
                mapping = []
            cleaned = [
                p for p in mapping if isinstance(p, str) and p in allowed_paths
            ]
            extracted.append({"name": name.strip(), "edsl_mapping": cleaned})

    return {
        "relevant_matches": relevant_matches,
        "references": references,
        "extracted_variables": extracted,
    }


def _item_block(item_id: str, req_text: str, elements: list[EdslElement]) -> str:
    sections = [
        f"[{item_id}] requirement: {req_text}",
        "",
        "Candidate elements:",
    ]
    for el in elements:
        contained = ", ".join(sorted(el.get_contained_names())) or "(none)"
        sections.append(f"--- {el.name} ({el.kind}) ---")
        sections.append(f"contained names: {contained}")
        sections.append(el.raw_element_text)
        sections.append("")
    return "\n".join(sections).rstrip()


def _retry_delay(exc: Exception, attempt: int) -> float:
    """Extract the server-suggested retry delay, else use exponential backoff."""
    text = str(exc)
    match = re.search(r"retry(?:\s+in|Delay'?:?\s*'?)\s*([\d.]+)s", text)
    if match:
        return float(match.group(1)) + 1.0
    return min(2**attempt, 30)


def _call_gemini(
    client, model: str, batch: list[ReqMatch]
) -> dict[str, dict[str, Any]]:
    item_ids = [f"item_{i}" for i in range(len(batch))]
    prompt = _PROMPT_HEADER + "\n\n".join(
        _item_block(item_id, req_text, elements)
        for item_id, (_req_id, req_text, elements) in zip(item_ids, batch)
    )

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
            data = json.loads(response.text)
            results: dict[str, dict[str, Any]] = {}
            for item_id, (req_id, req_text, elements) in zip(item_ids, batch):
                raw = data.get(item_id)
                validated = _validate_result(raw, elements)
                results[_req_key(req_id, req_text, elements)] = validated
            return results
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            text = str(exc)
            retryable = (
                isinstance(exc, json.JSONDecodeError)
                or "RESOURCE_EXHAUSTED" in text
                or "UNAVAILABLE" in text
                or "429" in text
                or "503" in text
            )
            if retryable and attempt < MAX_RETRIES - 1:
                delay = _retry_delay(exc, attempt)
                print(f"  transient error ({type(exc).__name__}); retrying in {delay:.0f}s...")
                time.sleep(delay)
                continue
            raise
    raise last_exc  # type: ignore[misc]


def _empty_result(elements: list[EdslElement]) -> dict[str, Any]:
    # Fall back to the top cosine candidate (first in caller-ordered list).
    relevant = [elements[0].name] if elements else []
    return {
        "relevant_matches": relevant,
        "references": {n: [] for n in relevant},
        "extracted_variables": [],
    }


def select_references(
    req_matches: list[ReqMatch],
    cache_path: str | Path = DEFAULT_CACHE,
    model: str = DEFAULT_MODEL,
) -> dict[str, dict[str, Any]]:
    """Return {reference_key(...) -> {relevant_matches, references, extracted_variables}}.

    `req_matches` is a list of (requirement_id, requirement_text, [candidate elements]).
    """
    root = Path(__file__).resolve().parent.parent
    cache_path = Path(cache_path)
    load_dotenv(root / ".env")

    cache: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        try:
            with cache_path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except json.JSONDecodeError:
            print(f"Warning: corrupt cache at {cache_path}; starting fresh.")
            loaded = {}
        # Require the new relevant_matches key so older cache shapes are ignored.
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                if (
                    isinstance(value, dict)
                    and "relevant_matches" in value
                    and "references" in value
                ):
                    cache[key] = value

    uncached = [
        item
        for item in req_matches
        if _req_key(item[0], item[1], item[2]) not in cache
    ]

    if uncached:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Warning: GEMINI_API_KEY not set; skipping reference selection.")
        else:
            client = genai.Client(api_key=api_key)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            for start in range(0, len(uncached), BATCH_SIZE):
                batch = uncached[start : start + BATCH_SIZE]
                try:
                    results = _call_gemini(client, model, batch)
                except Exception as exc:  # noqa: BLE001
                    print(f"Warning: Gemini call failed for a batch: {exc}")
                    continue
                cache.update(results)
                with cache_path.open("w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=2)
                print(
                    f"  selected references for {min(start + BATCH_SIZE, len(uncached))}"
                    f"/{len(uncached)} new requirements"
                )

    out: dict[str, dict[str, Any]] = {}
    for req_id, req_text, elements in req_matches:
        key = _req_key(req_id, req_text, elements)
        out[key] = cache.get(key) or _empty_result(elements)
    return out
