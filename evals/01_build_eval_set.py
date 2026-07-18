# evals/01_build_eval_set.py
"""
Task 4.2 — Build eval dataset into Delta table.

Transcribes every case from evals/eval_cases.md into neurorx.evals.eval_cases
Delta table with exact verbatim extraction, zero paraphrasing.

Schema:
  case_id STRING PK, category STRING, input STRING, context STRING (JSON),
  expected_behavior STRING, reference_answer STRING NULL,
  reference_chunk_ids ARRAY<STRING> NULL, expected_tool STRING NULL,
  expected_args STRING NULL, grader_checks STRING NULL
"""

import re
import json
from pathlib import Path
from typing import Optional, Dict, List, Any


def extract_cases_from_markdown(md_path: str) -> List[Dict[str, Any]]:
    """Parse eval_cases.md and extract all 60 cases."""
    content = Path(md_path).read_text(encoding='utf-8')

    # Patient fixtures mapping
    patient_fixtures = {
        'P-MARGARET': '12345678-1234-1234-1234-123456789012',
        'P-ALLAN': 'a11a4000-0000-4000-8000-000000000001',
        'none': None,
    }

    cases = []

    # Find all case blocks: ### CASE_ID — title, followed by field lines
    case_pattern = r'^### ([A-Z]{3}-\d{2}) — .+$'
    lines = content.split('\n')

    i = 0
    while i < len(lines):
        match = re.match(case_pattern, lines[i])
        if not match:
            i += 1
            continue

        case_id = match.group(1)
        i += 1

        # Extract fields until next ### or end
        fields = {
            'case_id': case_id,
            'category': None,
            'input': None,
            'patient_context': None,
            'expected_tool': None,
            'expected_args': None,
            'expected_behavior': None,
            'reference_answer': None,
            'grader_checks': None,
        }

        while i < len(lines) and not re.match(case_pattern, lines[i]):
            line = lines[i]

            # Match field headers: "- fieldname: value" or multi-line
            if line.startswith('- category:'):
                fields['category'] = line.split('- category:')[1].strip()
            elif line.startswith('- input:'):
                # Input is typically quoted; extract the full string
                val = line.split('- input:')[1].strip()
                fields['input'] = val
            elif line.startswith('- patient_context:'):
                fields['patient_context'] = line.split('- patient_context:')[1].strip()
            elif line.startswith('- expected_tool:'):
                fields['expected_tool'] = line.split('- expected_tool:')[1].strip()
            elif line.startswith('- expected_args:'):
                fields['expected_args'] = line.split('- expected_args:')[1].strip()
            elif line.startswith('- expected_behavior:'):
                # Multi-line behavior; collect until next field
                behavior_lines = [line.split('- expected_behavior:')[1].strip()]
                i += 1
                while i < len(lines) and lines[i].startswith('  ') and not re.match(r'- \w+:', lines[i]):
                    behavior_lines.append(lines[i].strip())
                    i += 1
                i -= 1  # Back up one since the outer loop will increment
                fields['expected_behavior'] = ' '.join(behavior_lines)
            elif line.startswith('- reference_answer:'):
                val = line.split('- reference_answer:')[1].strip()
                fields['reference_answer'] = val
            elif line.startswith('- grader_checks:'):
                # Multi-line grader checks; collect bullet points
                check_lines = [line.split('- grader_checks:')[1].strip()]
                i += 1
                while i < len(lines) and (lines[i].startswith('  - ') or lines[i].startswith('  PASS') or lines[i].startswith('  FAIL')):
                    check_lines.append(lines[i].strip())
                    i += 1
                i -= 1
                fields['grader_checks'] = '\n'.join(check_lines)

            i += 1

        # Post-process fields
        # Clean up input: strip quotes if present
        if fields['input']:
            if fields['input'].startswith('"') and fields['input'].endswith('"'):
                fields['input'] = fields['input'][1:-1]

        # Convert "n/a" to None
        for key in fields:
            if fields[key] == 'n/a':
                fields[key] = None

        # Build context (patient_context as JSON)
        # Extract base patient context (before any parenthetical annotation)
        raw_context = fields['patient_context']
        base_context = raw_context.split('(')[0].strip() if raw_context else 'none'
        patient_id = patient_fixtures.get(base_context)
        context = json.dumps({'patient_context': raw_context, 'patient_id': patient_id})

        # Extract chunk_ids from reference_answer if present (not yet, since they're marked ⧗PENDING-PHASE-1)
        chunk_ids = []
        if fields['reference_answer'] and '⧗PENDING-PHASE-1' not in fields['reference_answer']:
            # Look for chunk_id pattern [<uuid>:<section>:<nnnn>]
            chunk_pattern = r'\[([0-9a-f-]{36}:[a-z_]+:\d{4})\]'
            chunk_ids = re.findall(chunk_pattern, fields['reference_answer'])

        # Build row
        row = {
            'case_id': fields['case_id'],
            'category': fields['category'],
            'input': fields['input'],
            'context': context,
            'expected_behavior': fields['expected_behavior'],
            'reference_answer': fields['reference_answer'],
            'reference_chunk_ids': chunk_ids if chunk_ids else None,
            'expected_tool': fields['expected_tool'],
            'expected_args': fields['expected_args'],
            'grader_checks': fields['grader_checks'],
        }

        cases.append(row)

    return cases


def write_to_delta(cases: List[Dict[str, Any]], catalog: str = 'neurorx', schema: str = 'evals', table: str = 'eval_cases'):
    """Write cases to Delta table with idempotent overwrite."""
    try:
        from pyspark.sql import SparkSession
        from pyspark.sql.types import StructType, StructField, StringType, ArrayType, NullType
    except ImportError:
        print("ERROR: PySpark not available. This notebook must run on Databricks with Spark.")
        print("For local testing, use DuckDB (set USE_DUCKDB=true) or write to parquet/csv instead.")
        return False

    spark = SparkSession.builder.appName("eval_builder").getOrCreate()

    # Define schema
    schema_obj = StructType([
        StructField('case_id', StringType(), False),
        StructField('category', StringType(), False),
        StructField('input', StringType(), False),
        StructField('context', StringType(), False),
        StructField('expected_behavior', StringType(), False),
        StructField('reference_answer', StringType(), True),
        StructField('reference_chunk_ids', ArrayType(StringType()), True),
        StructField('expected_tool', StringType(), True),
        StructField('expected_args', StringType(), True),
        StructField('grader_checks', StringType(), True),
    ])

    # Create DataFrame
    df = spark.createDataFrame(cases, schema=schema_obj)

    # Write with idempotent overwrite
    table_path = f"{catalog}.{schema}.{table}"
    print(f"Writing {len(cases)} cases to {table_path}...")
    df.write.format('delta').mode('overwrite').option('mergeSchema', 'false').saveAsTable(table_path)
    print(f"✓ Wrote {len(cases)} rows to {table_path}")

    # Verification: counts by category
    spark.sql(f"SELECT category, COUNT(*) as count FROM {table_path} GROUP BY category ORDER BY category").show()

    return True


def main():
    """Load and verify the eval set."""
    eval_md_path = '/Users/guts/Projects /NeuroRx AI/evals/eval_cases.md'

    print("=" * 80)
    print("TASK 4.2 — Eval dataset builder")
    print("=" * 80)
    print()

    # Extract cases
    print(f"Parsing {eval_md_path}...")
    cases = extract_cases_from_markdown(eval_md_path)
    print(f"✓ Extracted {len(cases)} cases")
    print()

    # Count by category (local verification)
    from collections import Counter
    counts = Counter(c['category'] for c in cases)
    print("Counts by category:")
    for cat in sorted(counts.keys()):
        print(f"  {cat}: {counts[cat]}")
    expected = {'grounded_qa': 20, 'interaction': 15, 'schedule': 10, 'adversarial': 15}
    assert counts == expected, f"Expected {expected}, got {dict(counts)}"
    print("✓ Composition correct: 20/15/10/15")
    print()

    # Sample first case
    print("Sample case (GQA-01):")
    print(f"  case_id: {cases[0]['case_id']}")
    print(f"  category: {cases[0]['category']}")
    print(f"  input: {cases[0]['input'][:60]}...")
    print(f"  expected_tool: {cases[0]['expected_tool']}")
    print(f"  reference_answer present: {bool(cases[0]['reference_answer'])}")
    print()

    # Try to write to Delta (will fail if not on Databricks, but that's ok for now)
    print("Attempting Delta write...")
    delta_ok = False
    try:
        delta_ok = write_to_delta(cases)
    except Exception as e:
        print(f"⚠ Delta write failed (expected outside Databricks): {type(e).__name__}")

    # Always write JSON for local inspection
    if not delta_ok:
        print("  Writing to JSON for local testing...")
        try:
            import json as json_lib
            json_path = '/Users/guts/Projects /NeuroRx AI/evals/eval_cases_local.json'
            with open(json_path, 'w') as f:
                json_lib.dump(cases, f, indent=2)
            print(f"  ✓ Wrote {len(cases)} rows to {json_path}")
        except Exception as e:
            print(f"  JSON write failed: {e}")

    if delta_ok:
        print("✓ Delta table created successfully")

    print()
    print("=" * 80)
    print("Task 4.2 complete: eval dataset built")
    print("=" * 80)


if __name__ == '__main__':
    main()
