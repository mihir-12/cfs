"""Requirements-to-design traceability via semantic similarity.

Matches each CF functional requirement against the elements of the EDSL model
(containers, enums, and interfaces) using sentence-transformer embeddings and
cosine similarity, then LLM-filters candidates and reports fields / extracted
variables for the kept matches.

- cFS: NASA Core Flight System (open-source flight software framework)
- CF: the CFDP file-transfer application within cFS
- .edsl files: the formal model of CF and the cFE services it uses
"""

import json
from pathlib import Path

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
        # deduplicates the ranked list of EDSL elements
        # if the same named element appears in multiple EDSL files, only the first occurent (with the highest similarity score) is kept
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
        extracted_variables = selected.get("extracted_variables") or []

        match_dicts = []
        for j in kept_indices:
            element = elements[j]
            match_dict = {
                "name": element.name,
                "package": element.package,
                "kind": element.kind,
                "ai_generated_desc": element.ai_generated_desc,
                "cosine_similarity_score": round(float(row[j]), 4),
            }
            referenced = references_by_element.get(element.name, [])
            if referenced:
                if isinstance(element, Container):
                    match_dict["fields_referenced"] = referenced
                elif isinstance(element, Enum):
                    match_dict["members_referenced"] = referenced
                elif isinstance(element, Interface):
                    commands_referenced = [n for n in referenced if n in element.commands]
                    parameters_referenced = [
                        n for n in referenced if n in {p.name for p in element.parameters}
                    ]
                    if commands_referenced:
                        match_dict["commands_referenced"] = commands_referenced
                    if parameters_referenced:
                        match_dict["parameters_referenced"] = parameters_referenced
            match_dicts.append(match_dict)

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
