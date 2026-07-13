"""Select which fields/members of a matched EDSL element a requirement is about.

Given a requirement and a container/enum element that semantic similarity has
already matched to it, asks Gemini to point out the specific fields (for a
container) or members (for an enum) that the requirement is actually about --
mirroring the `fields_referenced` / `members_referenced` shape used in
`manual_mapping.json`. Results are cached to disk keyed by a hash of the
requirement id and the element's source, so reruns are free and only new or
changed requirement-element pairs trigger an API call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from edsl_parser import EdslElement

DEFAULT_CACHE = "reference_selection_cache.json"
DEFAULT_MODEL = "gemini-flash-latest"
BATCH_SIZE = 20
MAX_RETRIES = 5

_PROMPT_HEADER = """\
You are tracing NASA cFS/CF (CCSDS File Delivery Protocol) requirements to the \
specific fields or enum members of the design elements they have already been \
matched to.

For each item below, a requirement is paired with a container or enum it was \
matched to. Identify which of that element's fields (for a container) or \
members (for an enum) the requirement is SPECIFICALLY about -- not every \
field/member present, only the ones the requirement text calls out or clearly \
implies. If none of the fields/members stand out individually (the \
requirement matches the element as a whole), return an empty list for that \
item.

Return ONLY a JSON object mapping each item id (e.g. "item_0") to a list of \
field/member name strings, using the exact names as they appear in the source.

Items:
"""


def _pair_key(req_id: str, element: EdslElement) -> str:
    #Builds the hash key for a (requirement, element) pair
    #hash key gets mapped to the corresponding list of relevant field/member names in the cache
    digest = hashlib.sha1(f"{req_id}:{element.raw_element_text}".encode("utf-8")).hexdigest()[:10]
    return f"{req_id}:{element.package}:{element.name}:{digest}"


def _valid_names(element: EdslElement) -> set[str]:
    #returns the set of valid field/member names for a container/enum element
    if element.kind == "container":
        return {name for _, name in element.container_fields}
    elif element.kind == "enum":
        return set(element.enum_members)


def _item_block(item_id: str, req_text: str, element: EdslElement) -> str:
    #returns the block of text for a single item in the prompt
    label = "fields" if element.kind == "container" else "members"
    return (
        f"[{item_id}] requirement: {req_text}\n"
        f"matched {element.kind} (choose only from its {label}):\n"
        f"{element.raw_element_text}"
    )


def _retry_delay(exc: Exception, attempt: int) -> float:
    """Extract the server-suggested retry delay, else use exponential backoff."""
    text = str(exc)
    match = re.search(r"retry(?:\s+in|Delay'?:?\s*'?)\s*([\d.]+)s", text)
    if match:
        return float(match.group(1)) + 1.0
    return min(2**attempt, 30)


def _call_gemini(client, model: str, pairs_batch: list[tuple[str, str, EdslElement]]) -> dict[str, list[str]]:
    #pairs_batch is a list of (requirement_id, requirement_text, element) tuples - a single batch
    item_ids = [f"item_{i}" for i in range(len(pairs_batch))]
    prompt = _PROMPT_HEADER + "\n\n".join(
        _item_block(item_id, req_text, element)
        for item_id, (_req_id, req_text, element) in zip(item_ids, pairs_batch)
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
            data = json.loads(response.text) #local item_id is mapped to the list of relevant field/member names for the EdslElement corresponding to the local item_id
            results: dict[str, list[str]] = {}
            for item_id, (req_id, _req_text, element) in zip(item_ids, pairs_batch):
                names = data.get(item_id) #gets the list of relevant field/member names (produced by the LLM) for the container/enum corresponding to the local item_id
                if not isinstance(names, list):
                    continue
                valid = _valid_names(element) #gets the set of valid field/member names for the container/enum corresponding to the local item_id
                results[_pair_key(req_id, element)] = [n for n in names if n in valid] #extracts the list of relevant field/member names that are also valid and adds them to the results dictionary
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


def reference_key(req_id: str, element: EdslElement) -> str:
    """Public lookup key so callers can read results without reimplementing hashing."""
    return _pair_key(req_id, element)


def select_references(
    all_pairs: list[tuple[str, str, EdslElement]],
    cache_path: str | Path = DEFAULT_CACHE,
    model: str = DEFAULT_MODEL,
) -> dict[str, list[str]]:
    """Return {reference_key(req_id, element) -> [relevant field/member names]}.

    `all_pairs` is a list of (requirement_id, requirement_text, element) tuples and
    should already be filtered to elements that actually have fields/members to
    choose from.
    """
    root = Path(__file__).resolve().parent.parent
    cache_path = Path(cache_path)
    load_dotenv(root / ".env")

    cache: dict[str, list[str]] = {}
    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as f:
            cache = json.load(f)

    # we only need to use the llm to find relevant field/member names for pairs that are not already in the cache
    uncached = [p for p in all_pairs if _pair_key(p[0], p[2]) not in cache]

    if uncached:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Warning: GEMINI_API_KEY not set; skipping reference selection.")
        else:
            client = genai.Client(api_key=api_key)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            for start in range(0, len(uncached), BATCH_SIZE):
                pairs_batch = uncached[start : start + BATCH_SIZE]
                try:
                    results = _call_gemini(client, model, pairs_batch)
                except Exception as exc:  # noqa: BLE001 - keep pipeline resilient
                    print(f"Warning: Gemini call failed for a batch: {exc}")
                    continue
                cache.update(results) #adds the results to the cache
                # Persist after each batch so rate-limit interruptions don't lose work.
                with cache_path.open("w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=2)
                print(
                    f"  selected references for {min(start + BATCH_SIZE, len(uncached))}"
                    f"/{len(uncached)} new pairs"
                )

    return {
        reference_key(req_id, element): cache.get(reference_key(req_id, element), [])
        for req_id, _req_text, element in all_pairs
    }
