from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from edsl_parser import build_descriptor, parse_directory
from llm_descriptions import create_ai_generated_descriptions
from main import CACHE_DIR, DATA_DIR, load_requirements

"""
#Scratch script: check HkCounters similarity score against CF1001. Not part of the pipeline.
"""

def check_score(element_name, requirement_id, package=None):
    model = SentenceTransformer("all-MiniLM-L6-v2", cache_folder = CACHE_DIR)
    requirements = load_requirements()
    target_req = next((r for r in requirements if r["id"] == requirement_id), None)
    if not target_req:
        raise NameError(f'{requirement_id} was not found')
    elements = parse_directory(DATA_DIR)
    create_ai_generated_descriptions(elements, cache_path=CACHE_DIR / "descriptions_cache.json")
    element_descriptors = [build_descriptor(el) for el in elements]
    target_req_embedding = model.encode([target_req["text"]])
    element_descriptor_embeddings = model.encode(element_descriptors)
    scores = cosine_similarity(target_req_embedding, element_descriptor_embeddings)[0]

    target_idx = next(
        j
        for j, el in enumerate(elements)
        if el.name == element_name and (package is None or el.package == package)
    )
    ranked = sorted(range(len(elements)), key=lambda j: scores[j], reverse=True)
    #where in the list of EdslElements (sorted by similarity score), the target element appears
    target_rank = ranked.index(target_idx) + 1

    print(f"Requirement {requirement_id}: {target_req['text']}\n")
    print(
        f"{element_name} (package={elements[target_idx].package}): "
        f"score={scores[target_idx]:.4f}, rank={target_rank}/{len(elements)}\n"
    )

    print(f"{'rank':<5}{'score':<8}{'package':<16}{'kind':<10}name")
    for rank, j in enumerate(ranked[:15], start=1):
        el = elements[j]
        marker = f"  <-- {element_name}" if el.name == element_name else ""
        print(f"{rank:<5}{scores[j]:<8.4f}{el.package:<16}{el.kind:<10}{el.name}{marker}")

    return scores[target_idx]

if __name__ == "__main__":
    check_score("HKCommandCounters", "CF1000")
