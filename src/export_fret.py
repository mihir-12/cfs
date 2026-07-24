"""Export traceability.json + EDSL model into a FRET-style requirements/variables JSON.

Assumptions (from the export design):
- Output mirrors sample_fret_req_vars.json: top-level {requirements, variables}.
- Every EDSL container / enum / interface is flattened into FRET variables:
  Element.member (e.g. Queue.pend, Queue.active, Queue.history, Queue.all).
  Elements with no contained names become a bare ElementName.
- component_name on variables is the EDSL package / source file stem (CF, CFE_TBL, …),
  so the same Element.member in two packages is not a name collision.
- Requirements are freeForm and do not set a component (freeForm semantics has no
  component / component_name fields).
- project is "test_project" for both requirements and variables.
- variable _id and requirement _id are new UUIDs; reqid is the existing
  traceability key (CF1000, CF1001, ...), not invented.
- dataType is always "boolean" for every variable.
- Each requirement is freeForm only (not FRETish / type "nasa").
- fulltext = raw requirement text + matched EDSL paths in braces, e.g.
  '...reject the command. {CfdpCommands.noop} {HKCommandCounters.cmd}'
- semantics.variables is always empty; matched paths appear only in fulltext braces.
- Matched brace paths come from extracted_variables[].edsl_mapping[]:
  name.member when *_referenced is set, else bare name (no package prefix).
- variables[].reqs lists requirement _ids whose brace-set includes that
  variable (matched by component/package + variable_name).
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from edsl_parser import EdslElement, parse_directory

SRC_DIR = Path(__file__).resolve().parent
ROOT = SRC_DIR.parent
DATA_DIR = ROOT / "data"

PROJECT = "test_project"
REF_KEYS = (
    "fields_referenced",
    "members_referenced",
    "commands_referenced",
    "parameters_referenced",
)

# (component/package, variable_name)
VarKey = tuple[str, str]


def _flatten_element_paths(element: EdslElement) -> list[str]:
    """Return Element.member paths (or bare Element if no contained names)."""
    names = sorted(element.get_contained_names())
    if not names:
        return [element.name]
    return [f"{element.name}.{n}" for n in names]


def flatten_edsl_variables(elements: list[EdslElement]) -> list[VarKey]:
    """All (package, Element.member) keys from the EDSL corpus, first-seen order."""
    out: list[VarKey] = []
    seen: set[VarKey] = set()
    for el in elements:
        for path in _flatten_element_paths(el):
            key = (el.package, path)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def paths_from_mapping_object(obj: dict[str, Any]) -> list[VarKey]:
    """Flatten one edsl_mapping object into (package, Element.member) keys."""
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        return []
    package = obj.get("package") if isinstance(obj.get("package"), str) else ""
    referenced: list[str] = []
    for key in REF_KEYS:
        vals = obj.get(key)
        if isinstance(vals, list):
            referenced.extend(v for v in vals if isinstance(v, str))
    if referenced:
        return [(package, f"{name}.{m}") for m in referenced]
    return [(package, name)]


def matched_vars_for_requirement(entry: dict[str, Any]) -> list[VarKey]:
    """Unique (package, path) matches from edsl_mapping, first-seen order."""
    seen: set[VarKey] = set()
    keys: list[VarKey] = []
    for var in entry.get("extracted_variables") or []:
        if not isinstance(var, dict):
            continue
        for obj in var.get("edsl_mapping") or []:
            if not isinstance(obj, dict):
                continue
            for key in paths_from_mapping_object(obj):
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
    return keys


def _freeform_semantics() -> dict[str, Any]:
    return {
        "type": "freeForm",
        "scope": {"type": "null"},
        "condition": "null",
        "probability": "null",
        "timing": "null",
        "response": "action",
        "variables": [],
        "ft": "Unhandled.",
        "pctl": "Unhandled.",
        "description": (
            "FRET only speaks FRETish but as a courtesy will save this requirement. "
        ),
    }


def _variable_stub(
    component: str, variable_name: str, req_ids: list[str]
) -> dict[str, Any]:
    return {
        "project": PROJECT,
        "component_name": component,
        "variable_name": variable_name,
        "reqs": list(req_ids),
        "dataType": "boolean",
        "idType": "",
        "moduleName": "",
        "description": "",
        "assignment": "",
        "copilotAssignment": "",
        "modeRequirement": "",
        "modeldoc": False,
        "modelComponent": "",
        "modeldoc_id": "",
        "completed": False,
        "_id": str(uuid.uuid4()),
    }


def export_fret(
    traceability: dict[str, Any], elements: list[EdslElement]
) -> dict[str, Any]:
    """Build FRET {requirements, variables} from traceability + EDSL elements."""
    all_keys = flatten_edsl_variables(elements)
    var_to_reqs: dict[VarKey, list[str]] = defaultdict(list)

    requirements: list[dict[str, Any]] = []
    for reqid, entry in traceability.items():
        if not isinstance(entry, dict):
            continue
        req_text = entry.get("requirement") or ""
        matched = matched_vars_for_requirement(entry)
        # Braces show Element.member only (component is on the variable record).
        brace_paths: list[str] = []
        seen_paths: set[str] = set()
        for _pkg, path in matched:
            if path not in seen_paths:
                seen_paths.add(path)
                brace_paths.append(path)
        braces = " ".join(f"{{{p}}}" for p in brace_paths)
        fulltext = f"{req_text} {braces}".strip() if braces else req_text
        req_uuid = str(uuid.uuid4())
        for key in matched:
            var_to_reqs[key].append(req_uuid)
        requirements.append(
            {
                "reqid": reqid,
                "project": PROJECT,
                "rationale": req_text,
                "fulltext": fulltext,
                "status": "",
                "semantics": _freeform_semantics(),
                "_id": req_uuid,
            }
        )

    variables = [
        _variable_stub(component, name, var_to_reqs.get((component, name), []))
        for component, name in all_keys
    ]
    return {"requirements": requirements, "variables": variables}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export traceability.json to a FRET requirements/variables JSON."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=ROOT / "traceability.json",
        help="Path to traceability.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "fret_req_vars.json",
        help="Path to write FRET JSON",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="Directory of .edsl files",
    )
    args = parser.parse_args()

    with args.input.open("r", encoding="utf-8") as f:
        traceability = json.load(f)

    elements = parse_directory(args.data_dir)
    payload = export_fret(traceability, elements)

    with args.output.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4)

    print(
        f"Wrote {len(payload['requirements'])} requirements and "
        f"{len(payload['variables'])} variables -> {args.output}"
    )


if __name__ == "__main__":
    main()
