# How this works

Traces CF (CFDP file-transfer application) functional requirements to the parts of the `.edsl` data
model that implement them, producing `traceability.json`. The approach combines sentence-embedding
similarity (to find candidate design elements) with two targeted LLM calls (to enrich those elements
with plain-English descriptions, and to pinpoint which of their fields/members a requirement is
actually about).

## Pipeline (`main.py`)

1. **Load requirements** – `load_requirements()` reads `data/cf_FunctionalRequirements.csv` into a
   list of `{id, text}` dicts.
2. **Parse the design model** – `edsl_parser.parse_directory()` scans every `.edsl` file in `data/`
   and extracts each `container` and `EnumeratedDataType` as an `EdslElement` (name, package, fields
   or enum members, leading comment, raw source text).
3. **Enrich with AI descriptions** – `llm_descriptions.create_ai_generated_descriptions()` sends each
   element (plus the elements it references) to Gemini, asking for a 1-2 sentence plain-English
   description that expands cFS/CFDP jargon (e.g. "eot" -> "end of transaction"). This gives the
   embedding model much richer text to work with than raw identifiers. Results are cached to disk
   (`cache/descriptions_cache.json`), keyed by a hash of the element's source text, so reruns only
   call the API for new or changed elements.
4. **Build descriptors and embed** – `edsl_parser.build_descriptor()` turns each element into a single
   string (AI description + comment + humanized name + parent/fields/members), which
   `all-mpnet-base-v2` (via `sentence-transformers`) embeds alongside the requirement texts.
5. **Rank by cosine similarity** – for each requirement, every element is ranked by cosine similarity
   between its embedding and the requirement's. Duplicate element names (an element can appear in
   more than one `.edsl` file) are collapsed to their first/highest-scoring occurrence, then the
   top `TOP_N` elements at or above `THRESHOLD` are kept as matches (falling back to the single best
   match if none clear the threshold).
6. **Select specific fields/members** – cosine similarity only tells you *which element* is relevant,
   not *which part* of it. So for every matched container/enum that has fields/members,
   `reference_selection.select_references()` sends the requirement text plus that element's raw
   definition to Gemini and asks it to pick out only the fields (or enum members) the requirement is
   specifically calling out. This is also cached to disk (`cache/reference_selection_cache.json`).
7. **Write `traceability.json`** – for each requirement, records its matched elements with name,
   package, kind, AI-generated description, cosine similarity score, and (if applicable)
   `fields_referenced` / `members_referenced`.

## Why two LLM steps instead of one?

Embeddings are good at "is this element roughly about the same topic as this requirement?" but bad
at fine-grained relevance inside jargon-dense, short identifiers — a general-purpose model can rank a
loosely-related element above the element that actually contains the one field the requirement
mentions. Splitting the work keeps each LLM call narrow and cheap: one pass makes elements more
embeddable (better recall), the other pass reasons about a single already-matched element to name
the specific fields/members involved (better precision), instead of asking one model to do both
retrieval and fine-grained reasoning over the whole model at once.

## Files

| File | Role |
|---|---|
| `main.py` | Orchestrates the full pipeline described above; entry point (`python main.py`). |
| `edsl_parser.py` | Parses `.edsl` files into `EdslElement`s; builds the text descriptor used for embedding. |
| `llm_descriptions.py` | Gemini call that fills in `EdslElement.ai_generated_desc`, with disk caching. |
| `reference_selection.py` | Gemini call that picks specific `fields_referenced`/`members_referenced` per requirement-element match, with disk caching. |
| `check_score.py` | Scratch/debugging script to inspect one element's similarity score and rank for a given requirement. Not part of the pipeline. |

## Running it

Requires a `.env` file (in the project root) with `GEMINI_API_KEY` set. Without it, the LLM steps are
skipped (elements simply get no AI description / no field selection) and the rest of the pipeline
still runs on cosine similarity alone.

```bash
python src/main.py
```

Outputs `traceability.json` in the project root. The `data/`, `cache/` (LLM response caches + the
downloaded sentence-transformer model), and requirement CSV paths are all resolved relative to the
project root via `pathlib`, so the script can be run from anywhere.
