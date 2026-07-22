"""Requirements-to-design traceability via semantic similarity.

Matches each CF functional requirement against the elements of the EDSL model
(containers, enums, and interfaces) using sentence-transformer embeddings and
cosine similarity, then LLM-filters candidates and reports fields / extracted
variables for the kept matches.

- cFS: NASA Core Flight System (open-source flight software framework)
- CF: the CFDP file-transfer application within cFS
- .edsl files: the formal model of CF and the cFE services it uses
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from edsl_parser import Container, EdslElement, Enum, Interface, build_descriptor, parse_directory
from llm_descriptions import create_ai_generated_descriptions
from reference_selection import reference_key, select_references

# Minimum cosine similarity for a candidate; always keep at least the single best.
THRESHOLD = 0.4
# Cosine candidate pool size before the LLM relevance filter.
CANDIDATE_N = 10

SRC_DIR = Path(__file__).resolve().parent
ROOT = SRC_DIR.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / "cache"


def load_requirements() -> list[dict]:
    """Load requirement IDs + descriptions from the CSV, dropping empty rows."""
    df = pd.read_csv(DATA_DIR / "cf_FunctionalRequirements.csv")
    df = df.rename(
        columns={
            "Custom field (Requirement ID)": "id",
            "Description": "text",
        }
    )
    df = df[["id", "text"]].dropna(subset=["text"])
    df["text"] = df["text"].astype(str).str.strip()
    df = df[df["text"] != ""]
    return df.to_dict("records")


def _match_base(element: EdslElement, score: float) -> dict[str, Any]:
    """Shared match object fields (no *_referenced keys)."""
    return {
        "name": element.name,
        "package": element.package,
        "kind": element.kind,
        "ai_generated_desc": element.ai_generated_desc,
        "cosine_similarity_score": round(float(score), 4),
    }


def _attach_referenced(
    match_dict: dict[str, Any], element: EdslElement, referenced: list[str]
) -> dict[str, Any]:
    """Attach kind-appropriate *_referenced keys for the given names."""
    if not referenced:
        return match_dict
    if isinstance(element, Container):
        match_dict["fields_referenced"] = list(referenced)
    elif isinstance(element, Enum):
        match_dict["members_referenced"] = list(referenced)
    elif isinstance(element, Interface):
        param_names = {p.name for p in element.parameters}
        commands_referenced = [n for n in referenced if n in element.commands]
        parameters_referenced = [n for n in referenced if n in param_names]
        if commands_referenced:
            match_dict["commands_referenced"] = commands_referenced
        if parameters_referenced:
            match_dict["parameters_referenced"] = parameters_referenced
    return match_dict


def _expand_edsl_mapping(
    paths: list[Any],
    match_by_name: dict[str, dict[str, Any]],
    elements_by_name: dict[str, EdslElement],
) -> list[dict[str, Any]]:
    """Turn Element / Element.member path strings into match-shaped objects.

    Element.member -> base clone with only that member in *_referenced.
    Element alone  -> base clone with no *_referenced keys.
    Unknown paths are dropped.
    """
    expanded: list[dict[str, Any]] = []
    for path in paths:
        if not isinstance(path, str) or not path.strip():
            continue
        if "." in path:
            el_name, member = path.split(".", 1)
        else:
            el_name, member = path, None

        base = match_by_name.get(el_name)
        element = elements_by_name.get(el_name)
        if base is None or element is None:
            continue

        obj = {
            "name": base["name"],
            "package": base["package"],
            "kind": base["kind"],
            "ai_generated_desc": base["ai_generated_desc"],
            "cosine_similarity_score": base["cosine_similarity_score"],
        }
        if member is not None:
            if member not in element.get_contained_names():
                continue
            _attach_referenced(obj, element, [member])
        expanded.append(obj)
    expanded.sort(key=lambda o: o["cosine_similarity_score"], reverse=True)
    return expanded


def main() -> None:
    load_dotenv(ROOT / ".env")
    model = SentenceTransformer("all-mpnet-base-v2", cache_folder=CACHE_DIR)

    requirements = load_requirements()
    requirement_texts = [r["text"] for r in requirements]

    elements = parse_directory(DATA_DIR)
    create_ai_generated_descriptions(elements, cache_path=CACHE_DIR / "descriptions_cache.json")
    element_descriptors = [build_descriptor(el) for el in elements]

    requirement_embeddings = model.encode(requirement_texts)
    descriptor_embeddings = model.encode(element_descriptors)

    # rows = requirements, columns = EDSL elements
    scores = cosine_similarity(requirement_embeddings, descriptor_embeddings)

    # Cosine candidates (wider pool); LLM relevance filter decides final matches.
    req_candidates: list[tuple[dict, list[float], list[int]]] = []
    for req, row in zip(requirements, scores):
        ranked = sorted(range(len(elements)), key=lambda j: row[j], reverse=True)
        ranked_deduplicated = []
        seen_edsl_elements = set()
        # Deduplicate by name: keep highest-scoring package when names collide.
        for j in ranked:
            if elements[j].name not in seen_edsl_elements:
                seen_edsl_elements.add(elements[j].name)
                ranked_deduplicated.append(j)

        candidates = [j for j in ranked_deduplicated if row[j] >= THRESHOLD][:CANDIDATE_N]
        if not candidates:  # always retain the single best match
            candidates = ranked_deduplicated[:1]
        req_candidates.append((req, row, candidates))

    req_match_payloads: list[tuple[str, str, list[EdslElement]]] = []
    for req, _row, candidates in req_candidates:
        candidate_elements = [elements[j] for j in candidates]
        req_match_payloads.append((req["id"], req["text"], candidate_elements))
    selection = select_references(
        req_match_payloads, cache_path=CACHE_DIR / "reference_selection_cache.json"
    )

    traceability: dict[str, dict] = {}
    for req, row, candidates in req_candidates:
        candidate_elements = [elements[j] for j in candidates]
        name_to_idx = {elements[j].name: j for j in candidates}
        selected = selection.get(
            reference_key(req["id"], req["text"], candidate_elements),
            {"relevant_matches": [], "references": {}, "extracted_variables": []},
        )
        relevant_names = selected.get("relevant_matches") or []
        # Fall back to top cosine candidate if the LLM kept nothing valid.
        if not relevant_names:
            relevant_names = [elements[candidates[0]].name]

        # Final matches: relevant subset, ordered by cosine score descending.
        kept_indices = [
            name_to_idx[name] for name in relevant_names if name in name_to_idx
        ]
        kept_indices.sort(key=lambda j: row[j], reverse=True)

        references_by_element = selected.get("references") or {}
        raw_extracted = selected.get("extracted_variables") or []

        match_dicts: list[dict[str, Any]] = []
        match_by_name: dict[str, dict[str, Any]] = {}
        elements_by_name: dict[str, EdslElement] = {}
        for j in kept_indices:
            element = elements[j]
            match_dict = _match_base(element, row[j])
            referenced = references_by_element.get(element.name, [])
            _attach_referenced(match_dict, element, referenced)
            match_dicts.append(match_dict)
            match_by_name[element.name] = match_dict
            elements_by_name[element.name] = element

        extracted_variables = []
        for var in raw_extracted:
            if not isinstance(var, dict):
                continue
            name = var.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            extracted_variables.append(
                {
                    "name": name.strip(),
                    "edsl_mapping": _expand_edsl_mapping(
                        var.get("edsl_mapping") or [],
                        match_by_name,
                        elements_by_name,
                    ),
                }
            )

        traceability[req["id"]] = {
            "requirement": req["text"],
            "matches": match_dicts,
            "extracted_variables": extracted_variables,
        }

    with (ROOT / "traceability.json").open("w", encoding="utf-8") as f:
        json.dump(traceability, f, indent=4)

    print(
        f"Matched {len(requirements)} requirements against {len(elements)} "
        f"EDSL elements -> traceability.json"
    )


if __name__ == "__main__":
    main()
