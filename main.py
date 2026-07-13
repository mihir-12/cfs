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
import os

import pandas as pd
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from edsl_parser import build_descriptor, parse_directory
from llm_descriptions import enrich

# Minimum cosine similarity for a match; always keep at least the single best.
THRESHOLD = 0.5
# Maximum number of design elements reported per requirement.
TOP_N = 5

HERE = os.path.dirname(os.path.abspath(__file__))


def load_requirements() -> list[dict]:
    """Load requirement IDs + descriptions from the CSV, dropping empty rows."""
    df = pd.read_csv(os.path.join(HERE, "cf_FunctionalRequirements.csv"))
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
    load_dotenv()
    model = SentenceTransformer("all-MiniLM-L6-v2")

    requirements = load_requirements()
    requirement_texts = [r["text"] for r in requirements]

    elements = parse_directory(HERE)
    enrich(elements, cache_path=os.path.join(HERE, "descriptions_cache.json"))
    descriptors = [build_descriptor(el) for el in elements]

    requirement_embeddings = model.encode(requirement_texts)
    descriptor_embeddings = model.encode(descriptors)

    # rows = requirements, columns = EDSL elements
    scores = cosine_similarity(requirement_embeddings, descriptor_embeddings)

    traceability: dict[str, dict] = {}
    for req, row in zip(requirements, scores):
        ranked = sorted(range(len(elements)), key=lambda j: row[j], reverse=True)
        matches = [j for j in ranked if row[j] >= THRESHOLD][:TOP_N]
        if not matches:  # always retain the single best match
            matches = ranked[:1]

        traceability[req["id"]] = {
            "requirement": req["text"],
            "matches": [
                {
                    "name": elements[j].name,
                    "package": elements[j].package,
                    "kind": elements[j].kind,
                    "score": round(float(row[j]), 4),
                }
                for j in matches
            ],
        }

    with open(os.path.join(HERE, "traceability.json"), "w", encoding="utf-8") as f:
        json.dump(traceability, f, indent=4)

    print(
        f"Matched {len(requirements)} requirements against {len(elements)} "
        f"EDSL elements -> traceability.json"
    )


if __name__ == "__main__":
    main()
