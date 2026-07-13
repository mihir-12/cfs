"""Lightweight parser for the `.edsl` files in this project.

Extracts the data-model elements that the CF functional requirements actually
describe -- `container`s (with their fields) and `EnumeratedDataType`s (with
their members) -- from the `dataTypeSet { ... }` region of each file.

The parser is intentionally dependency-free: it works by scanning lines and
tracking brace depth, which is enough for the well-formed EDSL files here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from glob import glob


@dataclass
class EdslElement:
    """A single parsed element (a container or an enum)."""

    name: str
    kind: str  # "container" or "enum"
    package: str #the package that the element belongs to (aka the name of the edsl file)
    source: str
    comment: str = ""
    extends: str | None = None #for a container that extends another container
    container_fields: list[tuple[str, str]] = field(default_factory=list)  # (type, name) for  each field in a container
    enum_members: list[str] = field(default_factory=list) #for each enum
    raw_element_text: str = ""  # source snippet (from top comment through closing brace)
    ai_generated_desc: str = ""  # plain-English description, filled by the LLM step

#     container EotPacket_Payload {
#         uint32 seq_num
#         uint32 channel
#         uint32 direction
#         ...
#         TxnFilenames fnames
#     }
#
# -> container_fields = [("uint32", "seq_num"), ("uint32", "channel"), ...,
#              ("TxnFilenames", "fnames")], and enum_members = [].
# ------------------------------------------------------------------------------------
#     EnumeratedDataType CFDP {
#         list {
#             CLASS_1 = 0
#             CLASS_2 = 1
#         }
#     }
#
# -> enum_members = ["CLASS_1", "CLASS_2"], and container_fields = [].


_PACKAGE_RE = re.compile(r"^\s*package\s+(\w+)")
_CONTAINER_RE = re.compile(r"^\s*container\s+(\w+)(?:\s+extends\s+(\w+))?")
_ENUM_RE = re.compile(r"^\s*EnumeratedDataType\s+(\w+)")
_FIELD_RE = re.compile(r"^\s*([\w$]+)\s+(\w+)\s*$")
_MEMBER_RE = re.compile(r"(\w+)\s*=\s*[-\w]+")


def _block_end(lines: list[str], start: int) -> int:
    """Return the index of the line where the brace-block opened at `start` closes."""
    depth = 0
    seen_open = False
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if "{" in lines[i]:
            seen_open = True
        if seen_open and depth <= 0:
            return i
    return len(lines) - 1


def _extract_fields(inner: list[str]) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for raw in inner:
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if "{" in stripped or "}" in stripped:
            continue
        m = _FIELD_RE.match(stripped)
        if m:
            fields.append((m.group(1), m.group(2)))
    return fields


def _extract_members(inner: list[str]) -> list[str]:
    members: list[str] = []
    for raw in inner:
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            continue
        m = _MEMBER_RE.match(stripped)
        if m:
            members.append(m.group(1))
    return members


def parse_file(path: str) -> list[EdslElement]:
    """Parse a single `.edsl` file into a list of container/enum elements."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    source = os.path.basename(path)
    package_match = _PACKAGE_RE.search(text)
    package = package_match.group(1) if package_match else source

    lines = text.splitlines()

    # Locate the dataTypeSet region; files without one (e.g. CONFIG) yield nothing.
    dts_start = next(
        (i for i, ln in enumerate(lines) if re.match(r"\s*dataTypeSet\b", ln)),
        None,
    )
    if dts_start is None:
        return []
    dts_end = _block_end(lines, dts_start)
    body = lines[dts_start + 1 : dts_end]

    elements: list[EdslElement] = []
    pending_comment: list[str] = []
    comment_start: int | None = None
    i = 0
    while i < len(body):
        stripped = body[i].strip()

        if stripped.startswith("//"):
            if not pending_comment:
                comment_start = i
            pending_comment.append(stripped[2:].strip())
            i += 1
            continue
        if not stripped:
            pending_comment = []
            comment_start = None
            i += 1
            continue

        container_m = _CONTAINER_RE.match(body[i])
        enum_m = _ENUM_RE.match(body[i])

        if container_m or enum_m:
            end = _block_end(body, i)
            inner = body[i + 1 : end]
            raw_start = comment_start if comment_start is not None else i
            raw = "\n".join(body[raw_start : end + 1]).strip()
            if container_m:
                element = EdslElement(
                    name=container_m.group(1),
                    kind="container",
                    package=package,
                    source=source,
                    comment=" ".join(pending_comment).strip(),
                    extends=container_m.group(2),
                    container_fields=_extract_fields(inner),
                    raw_element_text=raw,
                )
            else:
                element = EdslElement(
                    name=enum_m.group(1),
                    kind="enum",
                    package=package,
                    source=source,
                    comment=" ".join(pending_comment).strip(),
                    enum_members=_extract_members(inner),
                    raw_element_text=raw,
                )
            elements.append(element)
            pending_comment = []
            comment_start = None
            i = end + 1
            continue

        # Any other declaration: skip its block (if any) so its inner lines are
        # not mistaken for top-level declarations.
        if "{" in body[i]:
            i = _block_end(body, i) + 1
        else:
            i += 1
        pending_comment = []
        comment_start = None

    return elements


def humanize(identifier: str) -> str:
    """Split an identifier into lowercase space-separated words.

    Handles snake_case, camelCase, PascalCase, acronym runs, and digit
    boundaries, e.g. `EotPacket` -> "eot packet", `HKCommandCounters` ->
    "hk command counters", `dst_filename` -> "dst filename".
    """
    s = identifier.replace("_", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)  # camelCase boundary
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)  # acronym -> Word boundary
    s = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", s)  # letter -> digit
    s = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", s)  # digit -> letter
    return " ".join(s.split()).lower()


def build_descriptor(element: EdslElement) -> str:
    """Compose an enriched natural-language string describing an element.

    The goal is to give the embedding model real language (comments + word-split
    identifiers) instead of opaque tokens.
    """
    parts: list[str] = []
    if element.ai_generated_desc:
        parts.append(element.ai_generated_desc)
    if element.comment:
        parts.append(element.comment)
    parts.append(humanize(element.name))

    if element.kind == "container":
        if element.extends:
            parts.append(f"extends {humanize(element.extends)}")
        if element.container_fields:
            field_words = ", ".join(humanize(name) for _, name in element.container_fields)
            parts.append(f"fields: {field_words}")
    else:  # enum
        if element.enum_members:
            member_words = ", ".join(humanize(m) for m in element.enum_members)
            parts.append(f"values: {member_words}")

    return ". ".join(p for p in parts if p)


def parse_directory(directory: str = ".") -> list[EdslElement]:
    """Parse every `.edsl` file in `directory` (non-recursive)."""
    elements: list[EdslElement] = []
    for path in sorted(glob(os.path.join(directory, "*.edsl"))):
        elements.extend(parse_file(path))
    return elements


if __name__ == "__main__":
    parsed = parse_directory(os.path.dirname(os.path.abspath(__file__)))
    print(f"Parsed {len(parsed)} elements\n")
    for el in parsed:
        if el.name in {"TxFileCmd", "EotPacket_Payload", "CFDP", "FreezeCmd"}:
            print(f"[{el.package}] {el.kind} {el.name}")
            print(f"  -> {build_descriptor(el)}\n")
