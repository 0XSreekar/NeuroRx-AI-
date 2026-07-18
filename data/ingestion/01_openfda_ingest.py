# Databricks notebook source
# MAGIC %md
# MAGIC # openFDA label ingestion
# MAGIC
# MAGIC Ingests structured product labels (SPL) for ~200 common chronic-care drugs into
# MAGIC `neurorx.bronze.fda_labels_raw`, per `DATA_CONTRACTS.md` §3.1.
# MAGIC
# MAGIC **Every field path and API behavior below was verified against the live
# MAGIC `api.fda.gov/drug/label.json` endpoint before this notebook was written — see
# MAGIC the "Verified API behavior" cell for what was checked and what it found.**
# MAGIC
# MAGIC Idempotent: re-running overwrites each drug's per-file JSON in the volume, then
# MAGIC MERGEs into the bronze table on `(set_id, spl_version)`.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verified API behavior
# MAGIC
# MAGIC This notebook does not guess field names. Before writing it, the live API was
# MAGIC queried directly and the following was confirmed:
# MAGIC
# MAGIC 1. **`set_id` is a top-level response field**, not nested under `openfda`. It is
# MAGIC    identical to `openfda.spl_set_id[0]`. Top-level is used here — one fewer
# MAGIC    layer of list-unwrapping, and it's present even on labels with a sparse
# MAGIC    `openfda` block.
# MAGIC 2. **The version field is literally named `version`**, not `spl_version`. The
# MAGIC    contract's column is named `spl_version` (to disambiguate from other
# MAGIC    version-like fields in the wider pipeline) but its value comes from this
# MAGIC    `version` field.
# MAGIC 3. **`effective_time` is a top-level string in `YYYYMMDD` format** (e.g.
# MAGIC    `"20250617"`), not pre-formatted as a date. Parsed explicitly below.
# MAGIC 4. **The four target sections are top-level keys, each a list of one string**
# MAGIC    (`dosage_and_administration`, `drug_interactions`, `warnings`,
# MAGIC    `information_for_patients`). A key is **absent entirely** (not `null`) when
# MAGIC    the label doesn't carry that section — checked with `in r`, not `r.get(...)`.
# MAGIC 5. **The `information_for_patients` fallback field name in the task spec was
# MAGIC    wrong and has been corrected.** Checked against openFDA's own searchable-fields
# MAGIC    reference: the real field is **`patient_medication_information`**, not
# MAGIC    `patient_information`. `spl_patient_package_insert` (the second fallback) was
# MAGIC    confirmed correct and was seen populated on real labels (isotretinoin,
# MAGIC    combined oral contraceptives). Fallback order used below:
# MAGIC    `information_for_patients` → `patient_medication_information` →
# MAGIC    `spl_patient_package_insert`.
# MAGIC 6. **A query with zero matches returns HTTP 404**, not an empty result list —
# MAGIC    `{"error": {"code": "NOT_FOUND", "message": "No matches found!"}}`. Handled
# MAGIC    as "no label found," not as a transient failure to retry.
# MAGIC 7. **`limit=1` on a bare generic-name search frequently returns a combination
# MAGIC    product, not the single-ingredient label** — confirmed for `metformin`
# MAGIC    (top hit: `SITAGLIPTIN AND METFORMIN HYDROCHLORIDE`) and `lisinopril` (top
# MAGIC    hit: `LISINOPRIL AND HYDROCHLOROTHIAZIDE TABLETS`). Fetching a wider
# MAGIC    candidate pool (`limit=25`) and scoring for single-ingredient + section
# MAGIC    completeness reliably surfaces the plain drug instead — confirmed this
# MAGIC    picks `METFORMIN HYDROCHLORIDE` correctly. See `pick_best_result()`.
# MAGIC 8. **Rate limits, confirmed against `open.fda.gov/apis/authentication`:** without
# MAGIC    a key, 240 requests/minute and 1,000 requests/day per IP. With a free key,
# MAGIC    240/minute and 120,000/day. Registration:
# MAGIC    [`https://api.data.gov/signup/`](https://api.data.gov/signup/). At ~200 drugs
# MAGIC    and one call each (plus rare retries), this run stays far under the no-key
# MAGIC    daily cap — a key is not required, but the notebook prints the signup URL if
# MAGIC    429s persist.

# COMMAND ----------

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

CATALOG = "neurorx"
TARGET_TABLE = f"{CATALOG}.bronze.fda_labels_raw"
VOLUME_DIR = f"/Volumes/{CATALOG}/bronze/raw_files/openfda"
SOURCE_API = "api.fda.gov/drug/label"
API_BASE_URL = "https://api.fda.gov/drug/label.json"

# Sections this product needs. Absence of a key means the section is missing —
# checked via `in`, never `.get(..., default)`, since a present-but-empty list
# and an absent key are different facts worth keeping distinct in bronze.
TARGET_SECTIONS = [
    "dosage_and_administration",
    "drug_interactions",
    "warnings",
    "information_for_patients",
]

# Verified fallback order for information_for_patients (see cell above).
PATIENT_INFO_FALLBACKS = ["patient_medication_information", "spl_patient_package_insert"]

RATE_LIMIT_SLEEP_SECONDS = 1.5
MAX_RETRIES = 3
CANDIDATE_POOL_SIZE = 25  # results fetched per query before scoring; see F-combo note above

dbutils.widgets.text("api_key", "", "openFDA API key (optional, blank = unauthenticated)")
API_KEY = dbutils.widgets.get("api_key").strip()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Curated drug list (~200 generic names)
# MAGIC
# MAGIC Covers the major chronic-care classes named in the task. `warfarin`,
# MAGIC `ibuprofen`, `metformin`, and `lisinopril` are present — the Phase 1 exit
# MAGIC checkpoint and the eval set's warfarin+ibuprofen case depend on them.

# COMMAND ----------

DRUG_LIST = [
    # Statins
    "atorvastatin", "rosuvastatin", "simvastatin", "pravastatin", "lovastatin",
    "fluvastatin", "pitavastatin",
    # ACE inhibitors
    "lisinopril", "enalapril", "ramipril", "benazepril", "quinapril",
    "fosinopril", "trandolapril", "captopril", "moexipril",
    # ARBs
    "losartan", "valsartan", "olmesartan", "irbesartan", "candesartan",
    "telmisartan", "azilsartan",
    # Beta blockers
    "metoprolol", "atenolol", "carvedilol", "bisoprolol", "propranolol",
    "nebivolol", "labetalol", "acebutolol", "nadolol",
    # Diabetes: metformin/sulfonylureas/insulin
    "metformin", "glipizide", "glyburide", "glimepiride", "sitagliptin",
    "empagliflozin", "dapagliflozin", "canagliflozin", "pioglitazone",
    "insulin glargine", "insulin lispro", "insulin aspart", "insulin detemir",
    "insulin degludec", "liraglutide", "semaglutide", "dulaglutide",
    "repaglinide", "acarbose",
    # Anticoagulants incl. warfarin
    "warfarin", "apixaban", "rivaroxaban", "dabigatran", "edoxaban",
    "enoxaparin", "heparin",
    # Antiplatelets
    "aspirin", "clopidogrel", "ticagrelor", "prasugrel", "dipyridamole",
    # PPIs
    "omeprazole", "esomeprazole", "pantoprazole", "lansoprazole",
    "rabeprazole", "dexlansoprazole",
    # Levothyroxine / thyroid
    "levothyroxine", "liothyronine", "methimazole", "propylthiouracil",
    # NSAIDs incl. ibuprofen
    "ibuprofen", "naproxen", "celecoxib", "meloxicam", "diclofenac",
    "indomethacin", "ketorolac", "piroxicam", "nabumetone",
    # SSRIs
    "sertraline", "escitalopram", "fluoxetine", "citalopram", "paroxetine",
    "fluvoxamine", "vilazodone",
    # SNRIs (common adjunct class for chronic-care patients)
    "venlafaxine", "duloxetine", "desvenlafaxine",
    # Common antibiotics
    "amoxicillin", "azithromycin", "ciprofloxacin", "levofloxacin",
    "doxycycline", "cephalexin", "clindamycin", "trimethoprim",
    "sulfamethoxazole", "metronidazole", "nitrofurantoin",
    # Inhalers / respiratory
    "albuterol", "fluticasone", "budesonide", "tiotropium", "montelukast",
    "salmeterol", "formoterol", "ipratropium", "beclomethasone",
    # Diuretics (chronic-care common companion class)
    "hydrochlorothiazide", "furosemide", "spironolactone", "chlorthalidone",
    "torsemide", "amiloride", "metolazone",
    # Calcium channel blockers
    "amlodipine", "diltiazem", "verapamil", "nifedipine", "felodipine",
    # Antiarrhythmics / cardiac
    "digoxin", "amiodarone", "sotalol", "flecainide",
    # Other chronic-care commons
    "allopurinol", "febuxostat", "colchicine", "gabapentin", "pregabalin",
    "tramadol", "hydrocodone", "oxycodone", "prednisone", "prednisolone",
    "methylprednisolone", "cyclobenzaprine", "baclofen", "tizanidine",
    "sildenafil", "tadalafil", "finasteride", "tamsulosin", "doxazosin",
    "oxybutynin", "solifenacin", "mirabegron", "donepezil", "memantine",
    "rivastigmine", "galantamine", "quetiapine", "aripiprazole",
    "risperidone", "olanzapine", "lamotrigine", "valproic acid",
    "levetiracetam", "topiramate", "carbamazepine", "phenytoin",
    "buspirone", "trazodone", "mirtazapine", "bupropion", "hydroxyzine",
    "diphenhydramine", "loratadine", "cetirizine", "fexofenadine",
    "famotidine", "ranitidine", "sucralfate", "ondansetron", "metoclopramide",
    "promethazine", "polyethylene glycol", "docusate", "senna", "lactulose",
    "calcium carbonate", "vitamin d3", "cholecalciferol", "cyanocobalamin",
    "ferrous sulfate", "potassium chloride", "magnesium oxide",
    "alendronate", "risedronate", "ibandronate", "zoledronic acid",
    "denosumab", "raloxifene", "estradiol", "progesterone", "testosterone",
    "methotrexate", "hydroxychloroquine", "sulfasalazine", "leflunomide",
    "azathioprine", "mycophenolate", "cyclosporine", "tacrolimus",
    "isotretinoin", "tretinoin", "clindamycin phosphate", "benzoyl peroxide",
    "metronidazole gel", "hydrocortisone", "triamcinolone", "clobetasol",
    "mupirocin", "permethrin", "fluconazole", "terbinafine", "nystatin",
    "acyclovir", "valacyclovir", "oseltamivir",
]

assert len(DRUG_LIST) >= 195, f"expected ~200 drugs, got {len(DRUG_LIST)}"
for required in ["warfarin", "ibuprofen", "metformin", "lisinopril"]:
    assert required in DRUG_LIST, f"required demo drug '{required}' missing from DRUG_LIST"

print(f"DRUG_LIST size: {len(DRUG_LIST)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Query + candidate scoring
# MAGIC
# MAGIC `pick_best_result()` implements the fix for the combo-product problem
# MAGIC confirmed in the "Verified API behavior" cell: score every candidate in the
# MAGIC fetched pool and take the best, rather than trusting openFDA's own result
# MAGIC ordering.

# COMMAND ----------


def _build_url(field, term, limit):
    query = f'openfda.{field}:"{term}"'
    params = {"search": query, "limit": limit}
    if API_KEY:
        params["api_key"] = API_KEY
    return f"{API_BASE_URL}?{urllib.parse.urlencode(params)}"


def _fetch_json(url):
    """GET url, returning parsed JSON on 200 and None on a 404 no-match response.

    Raises after exhausting MAX_RETRIES on HTTP 429, and re-raises any other
    HTTP or network error immediately (not retried — those indicate a real
    problem, not rate limiting).
    """
    backoff = 1.0
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # confirmed: openFDA's no-match response, not a failure
            if e.code == 429:
                if attempt == MAX_RETRIES:
                    print(
                        f"  429 persisted after {MAX_RETRIES} retries. "
                        f"Register a free API key to raise your rate limit: "
                        f"https://api.data.gov/signup/ "
                        f"(see https://open.fda.gov/apis/authentication/ for usage)."
                    )
                    return "RATE_LIMITED"
                print(f"  429 received, backing off {backoff:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(backoff)
                backoff *= 2
                continue
            raise
    return "RATE_LIMITED"


def _is_combo_product(generic_name):
    """Heuristic confirmed against live data: combo products join ingredient
    names with ' AND ', ',', or '/'. Single-ingredient labels (including salt
    forms like 'METFORMIN HYDROCHLORIDE') do not contain these separators.
    """
    gn = generic_name.upper()
    return " AND " in gn or "," in gn or "/" in gn


def _score_candidate(result, query_term):
    """Higher is better. Verified against live metformin/ibuprofen data:
    penalizing combo products and rewarding section completeness reliably
    selects the clean single-ingredient, most-complete label.
    """
    openfda = result.get("openfda", {})
    generic_names = openfda.get("generic_name", [])
    generic_name = generic_names[0] if generic_names else ""

    score = 0
    if generic_name and not _is_combo_product(generic_name):
        score += 100
    # Prefer a generic_name closer in length to the bare query term (proxy for
    # "plain drug" over "drug plus qualifiers/salt verbosity").
    score -= abs(len(generic_name) - len(query_term))
    # Section completeness: every target section present is worth more than
    # any tie-breaker above.
    for section in TARGET_SECTIONS:
        if section in result:
            score += 10
    for fallback in PATIENT_INFO_FALLBACKS:
        if fallback in result:
            score += 3
    return score


def find_best_label(drug_name):
    """Query openFDA for drug_name, generic_name first then brand_name fallback.

    Returns (result_dict, field_used, query_used) or (None, None, None) if no
    label was found on either field.
    """
    for field in ("generic_name", "brand_name"):
        url = _build_url(field, drug_name, CANDIDATE_POOL_SIZE)
        payload = _fetch_json(url)
        if payload == "RATE_LIMITED":
            return "RATE_LIMITED", field, url
        if payload is None:
            continue  # no match on this field; try the fallback field
        results = payload.get("results", [])
        if not results:
            continue
        best = max(results, key=lambda r: _score_candidate(r, drug_name))
        return best, field, url
    return None, None, None

# COMMAND ----------

# MAGIC %md
# MAGIC ## Extraction
# MAGIC
# MAGIC **Design note — a tension between this task and `DATA_CONTRACTS.md`, resolved
# MAGIC explicitly rather than silently:** `DATA_CONTRACTS.md` §3.1 defines `payload`
# MAGIC as *"the complete label JSON as returned by openFDA,"* and its own
# MAGIC `has_any_target_section` expectation is written as
# MAGIC `payload:dosage_and_administration IS NOT NULL OR ...` — a **top-level**
# MAGIC VARIANT path lookup. That only works if `payload` mirrors openFDA's own
# MAGIC top-level layout. This task's requirement #2, read literally, asks for a
# MAGIC trimmed extraction (`openfda` block + exactly four sections). Trimming would
# MAGIC silently break the frozen expectation expression, since the sections would
# MAGIC no longer be at the path it checks.
# MAGIC
# MAGIC **Resolution used here:** `payload` is the chosen candidate's **complete,
# MAGIC untouched** openFDA result object — every field openFDA returned, not just
# MAGIC the four target sections — with one small sidecar object,
# MAGIC `_neurorx_ingestion_meta`, added alongside it to carry the two facts this
# MAGIC task explicitly requires tracking (which field matched, and which
# MAGIC `information_for_patients` fallback key was used). Nothing from the original
# MAGIC response is removed or restructured, so `payload:dosage_and_administration`
# MAGIC etc. resolve exactly as the contract's expectation expects.
# MAGIC
# MAGIC **Fixed in `DATA_CONTRACTS.md` §3.1:** the `has_any_target_section`
# MAGIC expression originally checked only the four canonical section names, not
# MAGIC the two fallback field names — a label carrying only
# MAGIC `spl_patient_package_insert` (no `information_for_patients`) would have
# MAGIC shown as missing that section even though this notebook found usable
# MAGIC patient-guidance text via the fallback. The contract's expression now also
# MAGIC checks `payload:patient_medication_information IS NOT NULL` and
# MAGIC `payload:spl_patient_package_insert IS NOT NULL`, so it correctly counts a
# MAGIC label as having patient-guidance content regardless of which of the three
# MAGIC keys carried it.

# COMMAND ----------


def extract_label_record(result, field_used, query_used, drug_name):
    info_for_patients_source = None
    if "information_for_patients" in result:
        info_for_patients_source = "information_for_patients"
    else:
        for fallback in PATIENT_INFO_FALLBACKS:
            if fallback in result:
                info_for_patients_source = fallback
                break

    effective_time_raw = result.get("effective_time")  # "YYYYMMDD" string, confirmed
    effective_time_iso = None
    if effective_time_raw:
        try:
            effective_time_iso = datetime.strptime(effective_time_raw, "%Y%m%d").date().isoformat()
        except ValueError:
            effective_time_iso = None

    # Complete, untouched openFDA payload plus the sidecar metadata object.
    payload = dict(result)
    payload["_neurorx_ingestion_meta"] = {
        "queried_drug_name": drug_name,
        "matched_via_field": field_used,
        "information_for_patients_source": info_for_patients_source,
    }

    # Same fix applied to DATA_CONTRACTS.md's has_any_target_section: a section
    # only counts as missing if none of its valid keys were found.
    # information_for_patients has two fallbacks; the other three sections don't.
    missing_sections = [
        s for s in TARGET_SECTIONS
        if not (s == "information_for_patients" and info_for_patients_source is not None)
        and s not in result
    ]

    return {
        "set_id": result.get("set_id"),
        "spl_version": result.get("version"),  # verified: API field is "version"
        "effective_time": effective_time_iso,
        "payload": payload,
        "pull_query": query_used,
        # kept only for the Python-side summary cell below, not written to the table
        "_summary_missing_sections": missing_sections,
        "_summary_info_source": info_for_patients_source,
    }

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run: fetch every drug, write per-drug JSON to the volume
# MAGIC
# MAGIC Idempotent — each file is overwritten on rerun, named by generic drug name.

# COMMAND ----------

dbutils.fs.mkdirs(VOLUME_DIR)

summary_rows = []

for i, drug_name in enumerate(DRUG_LIST):
    safe_name = drug_name.replace(" ", "_").replace("/", "_")
    file_path = f"{VOLUME_DIR}/{safe_name}.json"

    result, field_used, query_used = find_best_label(drug_name)

    if result == "RATE_LIMITED":
        summary_rows.append(
            {"drug": drug_name, "status": "rate_limited", "set_id": None, "missing_sections": None}
        )
        print(f"[{i + 1}/{len(DRUG_LIST)}] {drug_name}: RATE LIMITED, skipping")
    elif result is None:
        summary_rows.append(
            {"drug": drug_name, "status": "not_found", "set_id": None, "missing_sections": None}
        )
        print(f"[{i + 1}/{len(DRUG_LIST)}] {drug_name}: no label found (generic or brand name)")
    else:
        record = extract_label_record(result, field_used, query_used, drug_name)
        record["_ingested_at"] = datetime.now(timezone.utc).isoformat()
        record["_source_file"] = query_used  # request URL, per DATA_CONTRACTS.md F12

        # Row-shaped JSON: top-level keys match the target table's columns exactly,
        # so Spark's schema inference on read lines up with no reshaping needed.
        row_json = {
            "set_id": record["set_id"],
            "spl_version": record["spl_version"],
            "effective_time": record["effective_time"],
            "payload": record["payload"],
            "pull_query": record["pull_query"],
            "_ingested_at": record["_ingested_at"],
            "_source_file": record["_source_file"],
        }

        with open(f"/dbfs{file_path}" if not file_path.startswith("/Volumes") else file_path, "w") as f:
            json.dump(row_json, f)

        missing = record["_summary_missing_sections"]
        summary_rows.append(
            {
                "drug": drug_name,
                "status": "found",
                "set_id": record["set_id"],
                "missing_sections": ", ".join(missing) if missing else "(none)",
            }
        )
        print(f"[{i + 1}/{len(DRUG_LIST)}] {drug_name}: OK (set_id={record['set_id']}, via={field_used})")

    time.sleep(RATE_LIMIT_SLEEP_SECONDS)

print(f"\nDone. {len(summary_rows)} drugs processed.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Load into `neurorx.bronze.fda_labels_raw`
# MAGIC
# MAGIC Reads every per-drug JSON file back from the volume and MERGEs into the
# MAGIC bronze table on `(set_id, spl_version)` — the natural key from
# MAGIC `DATA_CONTRACTS.md` §3.1. `payload` carries the complete label document as
# MAGIC `VARIANT`, per that contract's F15 resolution.

# COMMAND ----------

from pyspark.sql import functions as F

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
        set_id STRING,
        spl_version STRING,
        effective_time DATE,
        payload VARIANT,
        source_api STRING,
        pull_query STRING,
        _ingested_at TIMESTAMP,
        _source_file STRING
    )
""")

# Each file is already row-shaped (see the write loop above): top-level keys
# match the target table's columns, except `payload` arrives as a nested
# struct (Spark's JSON inference) rather than VARIANT, and `source_api` isn't
# in the file at all (constant, added here instead of repeated 200 times on disk).
raw_df = spark.read.json(f"{VOLUME_DIR}/*.json", multiLine=False)

if raw_df.rdd.isEmpty():
    print("No JSON files found in volume — nothing to load.")
else:
    staged_df = raw_df.select(
        F.col("set_id"),
        F.col("spl_version"),
        F.to_date(F.col("effective_time")).alias("effective_time"),
        F.parse_json(F.to_json(F.col("payload"))).alias("payload"),
        F.lit(SOURCE_API).alias("source_api"),
        F.col("pull_query"),
        F.to_timestamp(F.col("_ingested_at")).alias("_ingested_at"),
        F.col("_source_file"),
    ).filter(F.col("set_id").isNotNull())

    staged_df.createOrReplaceTempView("staged_fda_labels")

    spark.sql(f"""
        MERGE INTO {TARGET_TABLE} AS target
        USING staged_fda_labels AS source
        ON target.set_id = source.set_id AND target.spl_version <=> source.spl_version
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)

    print(f"MERGE complete into {TARGET_TABLE}.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

found = [r for r in summary_rows if r["status"] == "found"]
not_found = [r for r in summary_rows if r["status"] == "not_found"]
rate_limited = [r for r in summary_rows if r["status"] == "rate_limited"]

print("=" * 70)
print("INGESTION SUMMARY")
print("=" * 70)
print(f"Drugs requested:     {len(summary_rows)}")
print(f"Labels found:        {len(found)}")
print(f"Not found:           {len(not_found)}")
print(f"Rate-limited/skipped: {len(rate_limited)}")
print()

section_miss_counts = {s: 0 for s in TARGET_SECTIONS}
for row in found:
    if row["missing_sections"] != "(none)":
        for s in row["missing_sections"].split(", "):
            section_miss_counts[s] += 1

print("Missing-section counts (among labels found):")
for section, count in section_miss_counts.items():
    print(f"  {section:35s} {count}")

if not_found:
    print("\nDrugs with no label found (generic or brand name):")
    for row in not_found:
        print(f"  - {row['drug']}")

if rate_limited:
    print("\nDrugs skipped due to persistent 429s (rerun the notebook, or supply an api_key widget value):")
    for row in rate_limited:
        print(f"  - {row['drug']}")

display(spark.createDataFrame(summary_rows))
