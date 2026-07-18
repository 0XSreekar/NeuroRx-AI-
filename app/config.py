"""Configuration module for NeuroRx AI.

Loads settings from environment variables at import time. Requires nine variables
with no defaults (Databricks token, Vector Search endpoint, FM API endpoints,
Lakebase credentials). Fails loudly at import if any are missing, listing all
names in one error message.

See `.env.example` for a template. Load .env with `load_dotenv()` before importing
if using a local file.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

# Load .env file if it exists (safe to call if no .env file is present)
load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Immutable configuration container for NeuroRx AI.

    Attributes:
        databricks_host: Databricks workspace URL (e.g., https://dbc-xxxx.cloud.databricks.com)
        databricks_token: Personal access token for Databricks API
        catalog: Unity Catalog name (defaults to "neurorx")
        schema_*: Schema names (auto-derived from catalog, can be overridden)
        volume_raw: Managed volume for raw file ingestion
        vector_search_endpoint: AI Search endpoint name
        vector_index_fullname: Full name of Vector Search index
        fm_chat_endpoint: Foundation Model API endpoint for supervisor agent (Claude)
        fm_guardrail_endpoint: Foundation Model API endpoint for guardrail (Haiku)
        lakebase_*: Lakebase (OLTP) connection details
    """

    # Databricks workspace
    databricks_host: str
    databricks_token: str

    # Unity Catalog
    catalog: str
    schema_bronze: str
    schema_silver: str
    schema_gold: str
    schema_app: str
    schema_evals: str

    # Data volumes
    volume_raw: str

    # Vector Search
    vector_search_endpoint: str
    vector_index_fullname: str

    # Foundation Model APIs
    fm_chat_endpoint: str
    fm_guardrail_endpoint: str

    # Lakebase (OLTP)
    lakebase_host: str
    lakebase_db: str
    lakebase_user: str
    lakebase_password: str


def _load_settings() -> Settings:
    """Load and validate settings from environment variables.

    Returns:
        Settings: Immutable configuration object.

    Raises:
        ValueError: If any required variable is missing. The error message lists
                   all missing variables, not just the first one.
    """

    # Variables with default values
    catalog = os.getenv("CATALOG", "neurorx")
    schema_bronze = os.getenv("SCHEMA_BRONZE", f"{catalog}.bronze")
    schema_silver = os.getenv("SCHEMA_SILVER", f"{catalog}.silver")
    schema_gold = os.getenv("SCHEMA_GOLD", f"{catalog}.gold")
    schema_app = os.getenv("SCHEMA_APP", f"{catalog}.app")
    schema_evals = os.getenv("SCHEMA_EVALS", f"{catalog}.evals")
    volume_raw = os.getenv("VOLUME_RAW", "neurorx.bronze.raw_files")
    vector_index_fullname = os.getenv(
        "VECTOR_INDEX_FULLNAME", "neurorx.gold.drug_knowledge_index"
    )

    # Required variables (no defaults)
    required_vars = {
        "DATABRICKS_HOST": os.getenv("DATABRICKS_HOST"),
        "DATABRICKS_TOKEN": os.getenv("DATABRICKS_TOKEN"),
        "VECTOR_SEARCH_ENDPOINT": os.getenv("VECTOR_SEARCH_ENDPOINT"),
        "FM_CHAT_ENDPOINT": os.getenv("FM_CHAT_ENDPOINT"),
        "FM_GUARDRAIL_ENDPOINT": os.getenv("FM_GUARDRAIL_ENDPOINT"),
        "LAKEBASE_HOST": os.getenv("LAKEBASE_HOST"),
        "LAKEBASE_DB": os.getenv("LAKEBASE_DB"),
        "LAKEBASE_USER": os.getenv("LAKEBASE_USER"),
        "LAKEBASE_PASSWORD": os.getenv("LAKEBASE_PASSWORD"),
    }

    # Collect all missing required variables
    missing = sorted([name for name, value in required_vars.items() if value is None])
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return Settings(
        databricks_host=required_vars["DATABRICKS_HOST"],
        databricks_token=required_vars["DATABRICKS_TOKEN"],
        catalog=catalog,
        schema_bronze=schema_bronze,
        schema_silver=schema_silver,
        schema_gold=schema_gold,
        schema_app=schema_app,
        schema_evals=schema_evals,
        volume_raw=volume_raw,
        vector_search_endpoint=required_vars["VECTOR_SEARCH_ENDPOINT"],
        vector_index_fullname=vector_index_fullname,
        fm_chat_endpoint=required_vars["FM_CHAT_ENDPOINT"],
        fm_guardrail_endpoint=required_vars["FM_GUARDRAIL_ENDPOINT"],
        lakebase_host=required_vars["LAKEBASE_HOST"],
        lakebase_db=required_vars["LAKEBASE_DB"],
        lakebase_user=required_vars["LAKEBASE_USER"],
        lakebase_password=required_vars["LAKEBASE_PASSWORD"],
    )


# Load settings at module import time; fail loudly if config is incomplete
try:
    settings = _load_settings()
except ValueError as e:
    raise ImportError(f"Configuration error: {e}") from e
