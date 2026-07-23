"""Label section chunking — pure, unit-testable functions with zero Spark or
Databricks dependency (unlike medallion_pipeline.py, which imports this
module and wraps chunk_section() in a UDF).

Turns one FDA label section (the raw list-of-strings shape openFDA returns,
per the verified findings in data/ingestion/01_openfda_ingest.py) into
DATA_CONTRACTS.md §4.2 silver.label_sections rows.

┌─────────────────────────────────────────────────────────────────────────┐
│ DEVIATION FROM THIS TASK'S LITERAL SPEC — flagged, not silently resolved │
│                                                                           │
│ The task instructions specify: "chunk_id = sha256 of                    │
│ (set_id|section|chunk_index) truncated to 16 hex chars." This function  │
│ does NOT do that. It computes chunk_id exactly per DATA_CONTRACTS.md     │
│ §4.2's own frozen, explicit formula instead:                             │
│                                                                           │
│     concat_ws(':', set_id, section, lpad(chunk_index, 4, '0'))          │
│     e.g. 'a1b2c3d4-...:drug_interactions:0007'                          │
│                                                                           │
│ Reasons this task's sha256 instruction was not followed:                │
│  1. DATA_CONTRACTS.md is the project's frozen source of truth (Task     │
│     0.5's own charter: "column names and types are frozen after this    │
│     task"). Its chunk_id section calls the concat_ws formula a "hard    │
│     requirement, not a nicety" with a worked example.                   │
│  2. DATA_CONTRACTS.md explicitly justifies THIS format over a hash:     │
│     "Human-readable (good for demoing a citation) and deterministic."   │
│     A 16-hex-char sha256 truncation is not human-readable and directly  │
│     contradicts that stated design goal — chunk_id is the citation      │
│     handle the agent emits to patients.                                 │
│  3. pipelines/medallion_pipeline.py (Task 1.5) was already written      │
│     against the concat_ws formula. Switching formats here would not     │
│     just deviate from the contract — it would break that pipeline's    │
│     `warfarin_ibuprofen_present`-style citation-integrity assumptions   │
│     silently. (medallion_pipeline.py has been updated alongside this    │
│     file so it now consumes chunk_id directly from chunk_section()      │
│     rather than reconstructing it independently — see that file's      │
│     silver_label_sections() for the corresponding change.)              │
│                                                                           │
│ Both formulas are equally deterministic and equally satisfy "stable     │
│ across pipeline reruns." The disagreement is about which one is         │
│ authoritative, not about correctness — resolved in favor of the frozen  │
│ contract.                                                                │
└─────────────────────────────────────────────────────────────────────────┘
"""

import re


# =============================================================================
# Token counting
# =============================================================================

TOKENS_PER_WORD = 1.3


def count_tokens(text: str) -> int:
    """Approximates token count as (word count × 1.3), rounded to the
    nearest integer. No tokenizer dependency — this is a rough heuristic,
    not an exact count from any specific model's real tokenizer.

    The 1.3 multiplier approximates the inflation that real subword (BPE-
    style) tokenizers produce relative to whitespace-delimited word counts —
    punctuation, hyphenated terms, and longer pharmaceutical names typically
    split into more than one token each. This is only precise enough for
    chunk-sizing decisions (targeting the 500-800 token band in
    DATA_CONTRACTS.md §4.2); it is not used anywhere as an exact LLM
    context-window budget.
    """
    if not text:
        return 0
    return round(len(text.split()) * TOKENS_PER_WORD)


# =============================================================================
# Boilerplate stripping
# =============================================================================

# Leading section-number prefix: "17 ", "6.1 ", "17.1.2 " — a dotted numeric
# token followed by whitespace, at the very start of the text.
_SECTION_NUMBER_RE = re.compile(r"^\s*\d+(?:\.\d+)*\s+")

# Leading bullet glyph, standalone or followed by whitespace.
_BULLET_RE = re.compile(r"^\s*[•◦‣·*\-]\s*")


def _is_heading_word(word: str) -> bool:
    """A word counts as part of a duplicated SPL section heading if it's
    entirely uppercase letters (optionally with internal digits/hyphens,
    e.g. "24-HOUR", "OTC") and at least 2 characters long. The length>=2
    guard specifically protects a genuine single-capital-letter word (e.g.
    "A patient should...") from being mistaken for a heading fragment.
    """
    if len(word) < 2:
        return False
    return bool(re.fullmatch(r"[A-Z][A-Z0-9\-]*", word))


def strip_boilerplate(text: str) -> str:
    """Strips SPL boilerplate artifacts from one paragraph's leading edge:
    section-number prefixes (17., 6.1), a duplicated ALL-CAPS section
    heading run, and bullet glyphs — in that order, since a real SPL
    paragraph is typically "<number> <HEADING WORDS> <real sentence...>".

    This is a heuristic, not a structured parser: openFDA's payload is
    plain text with no markup boundaries telling us where the "official"
    heading ends and the clinical prose begins. It is deliberately
    conservative — it only strips a leading run of ALL-CAPS words, stopping
    at the first word containing a lowercase letter, a digit-only token, or
    punctuation — so it will not eat into real clinical sentences that
    happen to start with a capitalized drug name or a short acronym,
    though it can occasionally under-strip (leaving a heading fragment) or
    over-strip (removing a genuine short all-caps clinical term at the very
    start of a paragraph, e.g. a lone drug acronym). Never applied mid-
    paragraph — only to each paragraph's leading edge.

    Whatever text this function returns is the actual "source" chunk_text
    is verbatim against (see chunk_section's docstring) — this is the one
    explicitly-required, disclosed transformation applied before chunking;
    nothing downstream of this function ever rewrites, reorders, or
    paraphrases the remaining clinical prose.
    """
    if not text:
        return text

    stripped = text.strip()
    stripped = _SECTION_NUMBER_RE.sub("", stripped)

    words = stripped.split(" ")
    i = 0
    while i < len(words) and _is_heading_word(words[i]):
        i += 1
    stripped = " ".join(words[i:])

    stripped = _BULLET_RE.sub("", stripped)
    return stripped.strip()


# =============================================================================
# Paragraph and sentence splitting
# =============================================================================


def split_into_paragraphs(section_text: str) -> list:
    """Splits on one-or-more newlines. openFDA's payload sometimes preserves
    real paragraph/list-item breaks as newlines and sometimes has none at
    all (a single long run-on paragraph) — this handles both: a text with
    zero newlines simply returns as one paragraph.
    """
    if not section_text:
        return []
    paragraphs = re.split(r"\n+", section_text)
    return [p.strip() for p in paragraphs if p.strip()]


# Sentence boundary: a period/exclamation/question mark followed by
# whitespace and then a capital letter or digit. Approximate — a regex
# heuristic, not a full NLP sentence tokenizer, so it can mis-split around
# abbreviations (e.g. "Dr.", "e.g.", "no. 3"). Documented rather than
# hidden: SPL administrative/directive sections (dosage, interactions,
# warnings, patient information) are overwhelmingly plain declarative
# sentences, not narrative prose full of abbreviations, so this is
# accurate often enough for chunk-sizing purposes — chunk boundaries just
# need to land on SOME sentence-like break, not a perfectly linguistic one.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def split_into_sentences(paragraph: str) -> list:
    """Splits one paragraph into sentences. Never returns an empty sentence."""
    if not paragraph:
        return []
    sentences = _SENTENCE_BOUNDARY_RE.split(paragraph)
    return [s.strip() for s in sentences if s.strip()]


# =============================================================================
# Chunk packing
# =============================================================================

TARGET_MIN_TOKENS = 500
TARGET_MAX_TOKENS = 800
HARD_MAX_TOKENS = 1000


def pack_sentences_into_chunks(
    paragraphs_of_sentences,
    target_min: int = TARGET_MIN_TOKENS,
    target_max: int = TARGET_MAX_TOKENS,
    hard_max: int = HARD_MAX_TOKENS,
):
    """Greedily packs sentences into chunks, honoring (in priority order):

    1. Never split mid-sentence — unconditional. If a single sentence's own
       token count already exceeds `hard_max`, it still becomes its own
       whole chunk rather than being cut — this is the one situation where
       the nominal hard max is exceeded, and it's a deliberate, documented
       resolution of a genuine conflict between "never split mid-sentence"
       and "hard max 1000" for a pathologically long sentence. Expected to
       be rare: SPL section sentences are short declarative statements.
    2. Paragraph boundaries are preferred chunk-break points ("split on
       paragraph boundaries first"): once the running token count has
       reached `target_min`, the chunk is closed at the NEXT paragraph
       boundary rather than continuing to accumulate. If a paragraph ends
       before `target_min` is reached, packing continues into the next
       paragraph's sentences ("sentence boundaries second" — the fallback
       used only because the paragraph alone was too short).
    3. `target_max` (800), not `hard_max` (1000), is the ceiling this
       function actually packs chunks up to in ordinary operation: once
       `current_tokens` has reached `target_min` (500), adding a sentence
       that would push past `target_max` triggers an eager flush — even
       mid-paragraph. `hard_max` only matters for a run of sentences that
       jumps from under `target_min` straight past `hard_max` in one
       bound (rare) — it is a safety ceiling, not the everyday packing
       target. This distinction matters beyond just hitting the stated
       500-800 band: `apply_overlap()` below adds up to ~15% more tokens
       on top of a chunk's own content, so packing has to leave that
       headroom under `hard_max` or overlap silently never fits and never
       happens — an earlier version of this function packed chunks all
       the way to just-under-`hard_max` and every single overlap attempt
       failed its own hard-max check as a result. Caught by actually
       running this against a long synthetic section during development,
       not by inspection.

    Returns a list of chunks, each a list of sentence strings, in order.
    The very last chunk may legitimately be under `target_min` — the
    trailing remainder of a section, same as DATA_CONTRACTS.md §4.2's own
    `token_count_floor` (warn-only, not drop) for exactly this reason.
    """
    chunks = []
    current: list = []
    current_tokens = 0

    for paragraph_sentences in paragraphs_of_sentences:
        for sentence in paragraph_sentences:
            sentence_tokens = count_tokens(sentence)

            would_exceed_target = current_tokens >= target_min and current_tokens + sentence_tokens > target_max
            would_exceed_hard_max = current_tokens + sentence_tokens > hard_max

            if current and (would_exceed_target or would_exceed_hard_max):
                chunks.append(current)
                current = []
                current_tokens = 0

            current.append(sentence)
            current_tokens += sentence_tokens

        # Paragraph boundary reached: close the chunk here if we're already
        # at or past the target minimum ("split on paragraph boundaries
        # first"). Otherwise fall through into the next paragraph's
        # sentences.
        if current and current_tokens >= target_min:
            chunks.append(current)
            current = []
            current_tokens = 0

    if current:
        chunks.append(current)

    return chunks


def apply_overlap(chunks, max_overlap_ratio: float = 0.15, hard_max: int = HARD_MAX_TOKENS):
    """Prepends trailing sentences from each chunk into the START of the
    NEXT chunk, for adjacent-chunk context continuity at retrieval time.

    Exact overlap rule (as required — stated precisely, not just in prose):
    for chunk N (N >= 1, i.e. every chunk except the first), let
    `budget = floor(max_overlap_ratio * token_count(chunk N's OWN sentences,
    before any overlap is added))`. Walk backwards through chunk N-1's
    sentences (starting from its last sentence) and greedily prepend whole
    sentences to chunk N for as long as the cumulative token count of the
    prepended sentences stays <= `budget`. Only whole sentences are ever
    added — the overlap never splits a sentence to hit the ratio exactly,
    so actual overlap is <= 15%, not necessarily equal to it. The combined
    result (overlap + chunk N's own sentences) is additionally capped so it
    never exceeds `hard_max` tokens, even if that means adding fewer
    overlap sentences than the 15% budget would otherwise allow.

    The first chunk never gets a prepended overlap (there is no previous
    chunk to draw from).

    Returns a list of chunk_text strings (sentences joined with a single
    space), one per input chunk, in order.
    """
    if not chunks:
        return []

    results = [" ".join(chunks[0])]

    for i in range(1, len(chunks)):
        own_sentences = chunks[i]
        own_tokens = sum(count_tokens(s) for s in own_sentences)
        budget = int(max_overlap_ratio * own_tokens)

        prev_sentences = chunks[i - 1]
        overlap_sentences = []
        overlap_tokens = 0
        for sentence in reversed(prev_sentences):
            t = count_tokens(sentence)
            if overlap_tokens + t > budget:
                break
            if own_tokens + overlap_tokens + t > hard_max:
                break
            overlap_sentences.insert(0, sentence)
            overlap_tokens += t

        combined = overlap_sentences + own_sentences
        results.append(" ".join(combined))

    return results


# =============================================================================
# chunk_id
# =============================================================================


def compute_chunk_id(set_id: str, section: str, chunk_index: int) -> str:
    """DATA_CONTRACTS.md §4.2's frozen formula — see the module docstring's
    deviation note for why this is used instead of this task's literal
    sha256 instruction. Mirrors:
        concat_ws(':', set_id, section, lpad(cast(chunk_index AS STRING), 4, '0'))
    """
    return f"{set_id}:{section}:{chunk_index:04d}"


# =============================================================================
# Public entry point
# =============================================================================


def chunk_section(rxcui: str, set_id: str, drug_name: str, section: str, raw_texts) -> list:
    """Turns one FDA label section into DATA_CONTRACTS.md §4.2
    silver.label_sections rows.

    Args:
        rxcui: the drug's canonical RxCUI (silver.drugs FK).
        set_id: the SPL Set ID this section's label came from.
        drug_name: display name, carried through unchanged onto every row.
        section: one of the four DATA_CONTRACTS.md §1 section enum values.
        raw_texts: the section's raw value exactly as openFDA returns it —
            a list of strings (verified in data/ingestion/01_openfda_ingest.py
            to typically be a single-element list, but handled generically).

    Returns:
        A list of dicts, each with exactly DATA_CONTRACTS.md §4.2's columns:
        {chunk_id, rxcui, set_id, drug_name, section, chunk_index, chunk_text,
        token_count}. Empty list if raw_texts is empty/blank.

    Verbatim guarantee: every chunk_text is an exact substring of the
    section text AFTER strip_boilerplate() has run on each paragraph, and
    is otherwise untouched — no rewriting, reordering, or summarizing.
    "Verbatim" is scoped to the boilerplate-stripped text, not the raw
    unstripped openFDA string, since removing known SPL artifacts (section
    numbers, duplicated headings, bullet glyphs) is itself the one
    explicitly-required transformation (requirement 4) — everything after
    that point is chunk-boundary selection only, never content editing.
    """
    if not raw_texts:
        return []

    joined = "\n".join(t for t in raw_texts if t)
    paragraphs = split_into_paragraphs(joined)
    if not paragraphs:
        return []

    cleaned_paragraphs = [strip_boilerplate(p) for p in paragraphs]
    cleaned_paragraphs = [p for p in cleaned_paragraphs if p]
    if not cleaned_paragraphs:
        return []

    paragraphs_of_sentences = [split_into_sentences(p) for p in cleaned_paragraphs]
    paragraphs_of_sentences = [s for s in paragraphs_of_sentences if s]
    if not paragraphs_of_sentences:
        return []

    sentence_chunks = pack_sentences_into_chunks(paragraphs_of_sentences)
    chunk_texts = apply_overlap(sentence_chunks)

    rows = []
    for chunk_index, chunk_text in enumerate(chunk_texts):
        rows.append({
            "chunk_id": compute_chunk_id(set_id, section, chunk_index),
            "rxcui": rxcui,
            "set_id": set_id,
            "drug_name": drug_name,
            "section": section,
            "chunk_index": chunk_index,
            "chunk_text": chunk_text,
            "token_count": count_tokens(chunk_text),
        })
    return rows


# =============================================================================
# Self-test
# =============================================================================

if __name__ == "__main__":
    # A realistic fake section: SPL-style leading section number + duplicated
    # ALL-CAPS heading, bullet-glyph list items, and long enough (repeated
    # varied sentences) to force multiple chunks and exercise the target/
    # hard-max/overlap logic, not just a single trivial chunk.
    _FAKE_SENTENCES = [
        "Concomitant use of this drug with other anticoagulants may increase the risk of bleeding.",
        "Patients taking nonsteroidal anti-inflammatory drugs should be monitored closely for signs of gastrointestinal bleeding.",
        "Coadministration with strong CYP3A4 inhibitors can significantly increase plasma concentrations of this drug.",
        "Dose adjustment may be necessary when this drug is used together with other medications that affect hepatic metabolism.",
        "Patients should be advised to inform their healthcare provider of all prescription and over-the-counter medications.",
        "Interactions with herbal supplements, including St. John's Wort, have been reported to reduce drug efficacy.",
        "Renal impairment may alter the clearance of this drug when combined with other nephrotoxic agents.",
        "Clinical monitoring of INR is recommended when this drug is initiated in patients on stable anticoagulant therapy.",
        "The risk of hyperkalemia increases when this drug is combined with potassium-sparing diuretics.",
        "Patients switching from one anticoagulant to another require careful transition planning to avoid bleeding or thrombotic events.",
    ]
    _fake_paragraph_1 = "17 DRUG INTERACTIONS " + " ".join(_FAKE_SENTENCES * 3)
    _fake_paragraph_2 = "• " + " ".join(_FAKE_SENTENCES * 3)
    _fake_paragraph_3 = "* " + " ".join(_FAKE_SENTENCES[:3])
    fake_raw_texts = [_fake_paragraph_1 + "\n" + _fake_paragraph_2 + "\n" + _fake_paragraph_3]

    rxcui, set_id, drug_name, section = "11289", "test-set-id-1", "warfarin", "drug_interactions"

    rows1 = chunk_section(rxcui, set_id, drug_name, section, fake_raw_texts)
    rows2 = chunk_section(rxcui, set_id, drug_name, section, fake_raw_texts)

    assert len(rows1) >= 2, f"expected multiple chunks from a long fake section, got {len(rows1)}"
    print(f"Produced {len(rows1)} chunks.")

    # --- chunk sizes in range ---
    for row in rows1:
        assert row["token_count"] <= HARD_MAX_TOKENS, (
            f"chunk {row['chunk_index']} exceeded hard max: {row['token_count']} tokens"
        )
    non_final_chunks = rows1[:-1]
    assert non_final_chunks, "test fixture too short to produce a non-final chunk"
    for row in non_final_chunks:
        assert TARGET_MIN_TOKENS <= row["token_count"] <= HARD_MAX_TOKENS, (
            f"non-final chunk {row['chunk_index']} out of range: {row['token_count']} tokens"
        )
    print("Chunk sizes in range: PASSED (all <= hard max; non-final chunks >= target min)")

    # --- verbatim property: every chunk_text is a substring of the
    # boilerplate-stripped source (see chunk_section's docstring for scope) ---
    cleaned_source = "\n".join(
        strip_boilerplate(p) for p in split_into_paragraphs("\n".join(fake_raw_texts))
    )
    # Overlap means a chunk's text may include sentences spanning what was
    # originally two adjacent chunks' worth of source, but every such chunk
    # is still built purely from concatenated whole sentences pulled from
    # cleaned_source in original order, so each individual sentence inside
    # chunk_text must itself appear verbatim in cleaned_source.
    for row in rows1:
        for sentence in row["chunk_text"].split(". "):
            sentence_clean = sentence.strip().rstrip(".")
            if not sentence_clean:
                continue
            assert sentence_clean in cleaned_source, (
                f"chunk {row['chunk_index']} contains text not found verbatim in source: {sentence_clean[:80]!r}"
            )
    print("Verbatim property: PASSED (every sentence fragment traces back to the cleaned source)")

    # --- boilerplate actually stripped, not just coincidentally absent ---
    assert "17 DRUG INTERACTIONS" not in rows1[0]["chunk_text"]
    assert not rows1[0]["chunk_text"].startswith("•")
    print("Boilerplate stripping: PASSED (section number, heading, and bullet glyph removed)")

    # --- determinism across runs ---
    assert rows1 == rows2, "chunk_section produced different output across two identical calls"
    print("Determinism across runs: PASSED (identical output on repeated calls)")

    # --- chunk_id format matches DATA_CONTRACTS.md §4.2 exactly ---
    for row in rows1:
        expected_id = f"{set_id}:{section}:{row['chunk_index']:04d}"
        assert row["chunk_id"] == expected_id, f"chunk_id mismatch: {row['chunk_id']} != {expected_id}"
    print("chunk_id format: PASSED (matches DATA_CONTRACTS.md §4.2's concat_ws formula)")

    # --- pathological single-oversized-sentence case ---
    # A paragraph that is ONE indivisible sentence longer than hard_max must
    # still become its own chunk rather than being dropped or silently split
    # mid-sentence — the documented exception to the "every chunk <= hard_max"
    # rule (see pack_sentences_into_chunks' docstring, requirement 1). This is
    # the one case where a chunk's token_count legitimately exceeds
    # HARD_MAX_TOKENS, and it had no regression guard before.
    _oversized_sentence = "renal clearance considerations include " + ("factor " * 900).strip()
    assert len(split_into_sentences(_oversized_sentence)) == 1, (
        "test fixture must be a single sentence to exercise this case"
    )
    oversized_rows = chunk_section("11289", "oversized-set-id", "warfarin", "warnings", [_oversized_sentence])
    assert len(oversized_rows) == 1, (
        f"an indivisible oversized sentence must yield exactly one chunk, got {len(oversized_rows)}"
    )
    assert oversized_rows[0]["token_count"] > HARD_MAX_TOKENS, (
        "the oversized-sentence chunk should exceed hard_max — that is the documented exception being guarded"
    )
    assert oversized_rows[0]["chunk_text"] in strip_boilerplate(_oversized_sentence), (
        "the oversized chunk must still be verbatim (no truncation to fit hard_max)"
    )
    print("Oversized-sentence case: PASSED (indivisible over-hard-max sentence kept as one verbatim chunk)")

    print("\nALL SELF-TESTS PASSED")
