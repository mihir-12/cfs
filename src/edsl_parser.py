"""Lightweight parser for the `.edsl` files in this project.

Extracts the higher-order data-model elements that the CF functional
requirements describe -- `container`s, `EnumeratedDataType`s, and
`application`/`functional` `interface`s -- from each file's `dataTypeSet` and
`interfaceSet` regions.

The parser is intentionally dependency-free: it works by scanning lines and
tracking brace depth, which is enough for the well-formed EDSL files here.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, NamedTuple


class Field(NamedTuple):
    """A typed, named member. Used for container fields AND interface parameters —
    both are just `<Type> <name>` in the source, so they share this shape.
    Still unpacks like the old `(type, name)` tuples did (`for t, n in fields`).
    """

    type: str
    name: str


@dataclass(kw_only=True)
class EdslElement(ABC):
    """Common shape every higher-order construct has, regardless of kind."""

    name: str
    # Declared identifier in the EDSL source (e.g. "EotPacket_Payload", "CFDP", "CfdpCommands").
    package: str
    # Package the element belongs to — taken from the file's `package X` line
    # (usually the same as the .edsl filename stem, e.g. "CF", "CFE_SB").
    comment: str = ""
    # Leading // comment(s) immediately above the declaration, joined into one string.
    # Falls back to an inline "..." description on the declaration line when present.
    raw_element_text: str = ""
    # Verbatim source snippet from those comments through the element's closing brace;
    # used as LLM context and as the hash input for description/reference caches.
    ai_generated_desc: str = ""
    # Plain-English description filled in later by llm_descriptions (empty until then).

    kind: ClassVar[str]
    # Subclass tag: "container" / "enum" / "interface". Class-level, not set per instance.
    names_label: ClassVar[str]
    # Specifies what the contained names are for this element kind — used as the
    # English label in the LLM prompt: "fields" / "members" / "commands and parameters".

    @abstractmethod
    def get_contained_names(self) -> set[str]:
        """All names inside this element (field / member / command+parameter names).

        Used as the allow-list for reference_selection and to decide whether
        a matched element has anything worth asking the LLM about.
        """

    @abstractmethod
    def get_typed_fields(self) -> list[Field]:
        """Members that have a type to look up for one-hop LLM context enrichment.

        Empty for Enum (members have no type).
        """

    @abstractmethod
    def get_descriptor_lines(self) -> list[str]:
        """Kind-specific text lines appended into the embedding descriptor."""


@dataclass(kw_only=True)
class Container(EdslElement):
    kind: ClassVar[str] = "container"
    names_label: ClassVar[str] = "fields"
    extends: str | None = None
    # Parent container name from `extends X`, if any (e.g. "CommandHeader"); else None.
    fields: list[Field] = field(default_factory=list)
    # Direct fields declared in this container body, each Field(type, name).

    def get_contained_names(self) -> set[str]:
        return {name for _, name in self.fields}

    def get_typed_fields(self) -> list[Field]:
        return list(self.fields)

    def get_descriptor_lines(self) -> list[str]:
        lines: list[str] = []
        if self.extends:
            lines.append(f"extends {humanize(self.extends)}")
        if self.fields:
            field_words = ", ".join(humanize(name) for _, name in self.fields)
            lines.append(f"fields: {field_words}")
        return lines


@dataclass(kw_only=True)
class Enum(EdslElement):
    kind: ClassVar[str] = "enum"
    names_label: ClassVar[str] = "members"
    members: list[str] = field(default_factory=list)
    # Enum constant names only (e.g. ["CLASS_1", "CLASS_2"]); numeric values are not stored.

    def get_contained_names(self) -> set[str]:
        return set(self.members)

    def get_typed_fields(self) -> list[Field]:
        return []

    def get_descriptor_lines(self) -> list[str]:
        if not self.members:
            return []
        member_words = ", ".join(humanize(m) for m in self.members)
        return [f"members: {member_words}"]


@dataclass(kw_only=True)
class Interface(EdslElement):
    kind: ClassVar[str] = "interface"
    names_label: ClassVar[str] = "commands and parameters"
    commands: list[str] = field(default_factory=list)
    # Command names from the commands { } block (qualifiers/args discarded).
    parameters: list[Field] = field(default_factory=list)
    # Parameters from the parameters { } block as Field(type, name).

    def get_contained_names(self) -> set[str]:
        return set(self.commands) | {name for _, name in self.parameters}

    def get_typed_fields(self) -> list[Field]:
        return list(self.parameters)

    def get_descriptor_lines(self) -> list[str]:
        lines: list[str] = []
        if self.commands:
            cmd_words = ", ".join(humanize(c) for c in self.commands)
            lines.append(f"commands: {cmd_words}")
        if self.parameters:
            param_words = ", ".join(humanize(name) for _, name in self.parameters)
            lines.append(f"parameters: {param_words}")
        return lines


_PACKAGE_RE = re.compile(r"^\s*package\s+(\w+)")
_CONTAINER_RE = re.compile(
    r'^\s*container\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+"([^"]*)")?'
)
_ENUM_RE = re.compile(r'^\s*EnumeratedDataType\s+(\w+)(?:\s+"([^"]*)")?')
_INTERFACE_RE = re.compile(
    r'^\s*(?:application|functional)\s+interface\s+(\w+)(?:\s+"([^"]*)")?'
)
_FIELD_RE = re.compile(r"^\s*([\w$]+)\s+(\w+)\s*$")
_MEMBER_RE = re.compile(r"(\w+)\s*=\s*[-\w]+")
_COMMAND_RE = re.compile(r"^\s*(?:sync|async)\s+(\w+)")
_PARAMETER_RE = re.compile(
    r"^\s*(?:readOnly\s+|async\s+)*([\w$]+)\s+(\w+)(?:\s+\"[^\"]*\")?\s*$"
)
_SECTION_RE = re.compile(r"^\s*(dataTypeSet|interfaceSet)\b")


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


def _extract_fields(inner: list[str]) -> list[Field]:
    fields: list[Field] = []
    for raw in inner:
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if "{" in stripped or "}" in stripped:
            continue
        m = _FIELD_RE.match(stripped)
        if m:
            fields.append(Field(type=m.group(1), name=m.group(2)))
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


def _extract_commands(inner: list[str]) -> list[str]:
    """Extract command names from a commands { } block, skipping nested argument blocks."""
    commands: list[str] = []
    i = 0
    while i < len(inner):
        stripped = inner[i].strip()
        if not stripped or stripped.startswith("//"):
            i += 1
            continue
        m = _COMMAND_RE.match(stripped)
        if m:
            commands.append(m.group(1))
            if "{" in stripped:
                i = _block_end(inner, i) + 1
            else:
                i += 1
            continue
        if "{" in stripped:
            i = _block_end(inner, i) + 1
        else:
            i += 1
    return commands


def _extract_parameters(inner: list[str]) -> list[Field]:
    """Extract parameter Field(type, name) pairs, discarding qualifiers and quotes."""
    parameters: list[Field] = []
    for raw in inner:
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if "{" in stripped or "}" in stripped:
            continue
        m = _PARAMETER_RE.match(stripped)
        if m:
            parameters.append(Field(type=m.group(1), name=m.group(2)))
    return parameters


def _find_named_subblock(inner: list[str], name: str) -> list[str]:
    """Return the body lines of the first `name { ... }` sub-block in `inner`."""
    pattern = re.compile(rf"^\s*{re.escape(name)}\b")
    for i, line in enumerate(inner):
        if pattern.match(line) and "{" in line:
            end = _block_end(inner, i)
            return inner[i + 1 : end]
    return []


def _is_decorative_comment(text: str) -> bool:
    """True for separator-banner comments made mostly of dashes/punctuation."""
    stripped = text.strip()
    if not stripped:
        return True
    meaningful = sum(1 for ch in stripped if ch.isalnum())
    return meaningful < 3


def _resolve_comment(leading_parts: list[str], inline_description: str | None) -> str:
    """Prefer leading // comments; fall back to an inline \"...\" description.

    Decorative separator lines (e.g. `// ----`) are dropped so a useful inline
    description can still be used when that's all that remains.
    """
    kept = [part for part in leading_parts if not _is_decorative_comment(part)]
    leading = " ".join(kept).strip()
    if leading:
        return leading
    return (inline_description or "").strip()


def _scan_section_body(body: list[str], package: str) -> list[EdslElement]:
    """Scan one dataTypeSet/interfaceSet body for containers, enums, and interfaces."""
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
        interface_m = _INTERFACE_RE.match(body[i])

        if container_m or enum_m or interface_m:
            end = _block_end(body, i)
            inner = body[i + 1 : end]
            raw_start = comment_start if comment_start is not None else i
            raw = "\n".join(body[raw_start : end + 1]).strip()
            leading_parts = list(pending_comment)

            if container_m:
                element: EdslElement = Container(
                    name=container_m.group(1),
                    package=package,
                    comment=_resolve_comment(leading_parts, container_m.group(3)),
                    extends=container_m.group(2),
                    fields=_extract_fields(inner),
                    raw_element_text=raw,
                )
            elif enum_m:
                element = Enum(
                    name=enum_m.group(1),
                    package=package,
                    comment=_resolve_comment(leading_parts, enum_m.group(2)),
                    members=_extract_members(inner),
                    raw_element_text=raw,
                )
            else:
                assert interface_m is not None
                element = Interface(
                    name=interface_m.group(1),
                    package=package,
                    comment=_resolve_comment(leading_parts, interface_m.group(2)),
                    commands=_extract_commands(_find_named_subblock(inner, "commands")),
                    parameters=_extract_parameters(
                        _find_named_subblock(inner, "parameters")
                    ),
                    raw_element_text=raw,
                )
            elements.append(element)
            pending_comment = []
            comment_start = None
            i = end + 1
            continue

        # Any other declaration (IntegerDataType, component, stateMachine, ...):
        # skip its block so inner lines are not mistaken for top-level declarations.
        if "{" in body[i]:
            i = _block_end(body, i) + 1
        else:
            i += 1
        pending_comment = []
        comment_start = None

    return elements


def parse_file(path: str | Path) -> list[EdslElement]:
    """Parse a single `.edsl` file into containers, enums, and interfaces."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    package_match = _PACKAGE_RE.search(text)
    package = package_match.group(1) if package_match else path.stem

    lines = text.splitlines()
    elements: list[EdslElement] = []

    # Scan every top-level dataTypeSet / interfaceSet region in the file.
    i = 0
    while i < len(lines):
        if _SECTION_RE.match(lines[i]) and "{" in lines[i]:
            end = _block_end(lines, i)
            body = lines[i + 1 : end]
            elements.extend(_scan_section_body(body, package))
            i = end + 1
            continue
        i += 1

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
    """
    - The descriptor of each Edsl element is what's turned into an embedding and then compared to the requirement embedding via cosine similarity
    - The descriptor contains the ai generated description, element comment, the parents of the element, and the fields/members of the element (humanized)
    """
    parts: list[str] = []
    if element.ai_generated_desc:
        parts.append(element.ai_generated_desc)
    if element.comment:
        parts.append(element.comment)
    parts.append(humanize(element.name))
    parts.extend(element.get_descriptor_lines())
    return ". ".join(p for p in parts if p)


def parse_directory(directory: str | Path = ".") -> list[EdslElement]:
    """Parse every `.edsl` file in `directory` (non-recursive)."""
    directory = Path(directory)
    elements: list[EdslElement] = []
    for path in sorted(directory.glob("*.edsl")):
        elements.extend(parse_file(path))
    return elements


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    parsed = parse_directory(root / "data")
    print(f"Parsed {len(parsed)} elements\n")
    for el in parsed:
        if el.name in {
            "TxFileCmd",
            "EotPacket_Payload",
            "CFDP",
            "FreezeCmd",
            "CfdpCommands",
            "TxnState",
            "SoftwareBusAccess",
        }:
            print(f"[{el.package}] {el.kind} {el.name}")
            print(f"  comment: {el.comment!r}")
            print(f"  contained: {sorted(el.get_contained_names())}")
            print(f"  -> {build_descriptor(el)}\n")
