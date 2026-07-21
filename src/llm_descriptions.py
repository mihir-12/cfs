"""Generate plain-English descriptions for EDSL elements using Google Gemini.

Each description states an element's purpose and expands cFS/CFDP domain
abbreviations (e.g. "eot" -> "end of transaction"), giving the embedding model
far richer text than the raw identifiers. Results are cached to disk keyed by a
hash of the element's source, so reruns are free and deterministic and only new
or changed elements trigger an API call.
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

from edsl_parser import EdslElement, parse_directory

DEFAULT_CACHE = "descriptions_cache.json"
DEFAULT_MODEL = "gemini-flash-latest"
BATCH_SIZE = 20
MAX_RETRIES = 5

_PROMPT_HEADER = """\
You are documenting the data model of NASA's Core Flight System (cFS), \
specifically the CF application which implements the CCSDS File Delivery \
Protocol (CFDP) for transferring files between a spacecraft and the ground.

For each item below, write a concise 1-2 sentence, plain-English description of \
what it represents and its purpose. Expand domain abbreviations so the text is \
self-explanatory. Common abbreviations:
- eot = end of transaction
- eid = entity id
- txn = transaction
- seq = sequence
- crc = cyclic redundancy check
- pdu = protocol data unit
- cfdp = CCSDS file delivery protocol
- hk = housekeeping
- tlm = telemetry
- cmd = command
- nak = negative acknowledgment, ack = acknowledgment
- msg = message, mid = message id
- sb = software bus, tbl = table, hdr = header
- src = source, dst/dest = destination, dir = directory

Do not mention EDSL, containers, structs, or code constructs. Describe the \
meaning and behavior. Return ONLY a JSON object mapping each item id (e.g. \
"item_0") to its description string.

Items:
"""


def _cache_key(element: EdslElement) -> str:
    #Builds the hash key for an EDSL element
    #hash key gets mapped to the corresponding EDSL element description
    digest = hashlib.sha1(element.raw_element_text.encode("utf-8")).hexdigest()[:10]
    return f"{element.package}:{element.name}:{digest}"


def _referenced_context(element: EdslElement, name_to_element_map: dict[str, EdslElement]) -> str:
    """Include one-hop field/member names of composite types this element references."""
    lines: list[str] = []
    for type_name, _field_name in element.get_typed_fields():
        ref = name_to_element_map.get(type_name)
        if ref is None or ref.name == element.name:
            continue
        names = sorted(ref.get_contained_names())
        if not names:
            continue
        lines.append(f"  {ref.name} {ref.names_label}: {', '.join(names)}")
        # ex. "TxnFilenamess fields: src_filename, dst_filename"
    return "\n".join(lines)


def _item_block(item_id: str, element: EdslElement, name_to_element_map: dict) -> str:
    """formats one EDSL element into the chunk of prompt text that gets sent to Gemini for that element."""
    # to create an ai generated description for an edsl element, we use the local item_id, element kind, raw element text/code, and the direct fields/members it references
    block = [f"[{item_id}] kind={element.kind} name={element.name}", element.raw_element_text]
    context = _referenced_context(element, name_to_element_map)
    if context:
        block.append("Referenced types:")
        block.append(context)
    return "\n".join(block)


def _retry_delay(exc: Exception, attempt: int) -> float:
    """Extract the server-suggested retry delay, else use exponential backoff."""
    text = str(exc)
    match = re.search(r"retry(?:\s+in|Delay'?:?\s*'?)\s*([\d.]+)s", text)
    if match:
        return float(match.group(1)) + 1.0
    return min(2**attempt, 30)


def _call_gemini(client, model: str, batch: list, name_to_element_map: dict) -> dict[str, str]:
    item_ids = [f"item_{i}" for i in range(len(batch))]
    prompt = _PROMPT_HEADER + "\n\n".join(
        _item_block(item_id, el, name_to_element_map) for item_id, el in zip(item_ids, batch)
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
            return {
                el.name: data[item_id]
                for item_id, el in zip(item_ids, batch)
                if item_id in data
            }
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


def create_ai_generated_descriptions(
    elements: list[EdslElement],
    cache_path: str | Path = DEFAULT_CACHE,
    model: str = DEFAULT_MODEL,
) -> list[EdslElement]:
    """Populate `element.ai_generated_desc` for every element, using a disk cache."""
    root = Path(__file__).resolve().parent.parent
    cache_path = Path(cache_path)
    load_dotenv(root / ".env")

    cache: dict[str, str] = {}
    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as f:
            cache = json.load(f)

    uncached = [el for el in elements if _cache_key(el) not in cache]

    if uncached:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Warning: GEMINI_API_KEY not set; skipping LLM descriptions.")
        else:
            client = genai.Client(api_key=api_key)
            name_to_element_map = {el.name: el for el in elements}
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            for start in range(0, len(uncached), BATCH_SIZE):
                batch = uncached[start : start + BATCH_SIZE]
                try:
                    results = _call_gemini(client, model, batch, name_to_element_map)
                except Exception as exc:  # noqa: BLE001 - keep pipeline resilient
                    print(f"Warning: Gemini call failed for a batch: {exc}")
                    continue
                for el in batch:
                    if el.name in results:
                        cache[_cache_key(el)] = results[el.name]
                # Persist after each batch so rate-limit interruptions don't lose work.
                with cache_path.open("w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=2)
                print(
                    f"  described {min(start + BATCH_SIZE, len(uncached))}"
                    f"/{len(uncached)} new elements"
                )

    for el in elements:
        el.ai_generated_desc = cache.get(_cache_key(el), "")

    return elements


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    els = create_ai_generated_descriptions(
        parse_directory(root / "data"),
        cache_path=root / "cache" / DEFAULT_CACHE,
    )
    for el in els:
        if el.name in {"EotPacket", "EotPacket_Payload", "TxFileCmd", "CFDP"}:
            print(f"[{el.package}] {el.name}: {el.ai_generated_desc}")
