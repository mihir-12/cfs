"""Requirements-to-design traceability via semantic similarity.

Matches each CF functional requirement against the elements of the EDSL model
(containers, their fields, and enums) using sentence-transformer embeddings and
cosine similarity. For every requirement it reports the design elements that are
most likely to implement it.

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

from edsl_parser import EdslElement, build_descriptor, parse_directory
from llm_descriptions import create_ai_generated_descriptions
from reference_selection import reference_key, select_references

# Minimum cosine similarity for a match; always keep at least the single best.
THRESHOLD = 0.4
# Maximum number of design elements reported per requirement.
TOP_N = 5

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
    model = SentenceTransformer("all-mpnet-base-v2", cache_folder = CACHE_DIR)

    requirements = load_requirements()
    requirement_texts = [r["text"] for r in requirements]

    elements = parse_directory(DATA_DIR)
    create_ai_generated_descriptions(elements, cache_path=CACHE_DIR / "descriptions_cache.json")
    element_descriptors = [build_descriptor(el) for el in elements]

    requirement_embeddings = model.encode(requirement_texts)
    descriptor_embeddings = model.encode(element_descriptors)

    # rows = requirements, columns = EDSL elements
    scores = cosine_similarity(requirement_embeddings, descriptor_embeddings)

    req_matches: list[tuple[dict, list[float], list[int]]] = []
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

        matches = [j for j in ranked_deduplicated if row[j] >= THRESHOLD][:TOP_N]
        if not matches:  # always retain the single best match
            matches = ranked_deduplicated[:1]
        req_matches.append((req, row, matches))

    # Ask the LLM which specific fields/members each matched container/enum a
    # requirement is actually about, so it can be reported the same way
    # manual_mapping.json is (fields_referenced / members_referenced).
    pairs: list[tuple[str, str, EdslElement]] = []
    for req, _row, matches in req_matches:
        for j in matches:
            element = elements[j]
            has_fields = element.kind == "container" and element.container_fields
            has_members = element.kind == "enum" and element.enum_members
            if has_fields or has_members:
                pairs.append((req["id"], req["text"], element))
    references = select_references(
        pairs, cache_path=CACHE_DIR / "reference_selection_cache.json"
    )

    traceability: dict[str, dict] = {}
    for req, row, matches in req_matches:
        match_dicts = []
        for j in matches:
            element = elements[j]
            match_dict = {
                "name": element.name,
                "package": element.package,
                "kind": element.kind,
                "ai_generated_desc": element.ai_generated_desc,
                "cosine_similarity_score": round(float(row[j]), 4),
            }
            referenced = references.get(reference_key(req["id"], element), [])
            if referenced:
                key = "fields_referenced" if element.kind == "container" else "members_referenced"
                match_dict[key] = referenced
            match_dicts.append(match_dict)

        traceability[req["id"]] = {
            "requirement": req["text"],
            "matches": match_dicts,
        }

    with (ROOT / "traceability.json").open("w", encoding="utf-8") as f:
        json.dump(traceability, f, indent=4)

    print(
        f"Matched {len(requirements)} requirements against {len(elements)} "
        f"EDSL elements -> traceability.json"
    )


if __name__ == "__main__":
    main()
