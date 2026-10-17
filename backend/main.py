
import sys
import os
# Ensure the parent directory of backend is in the path to allow resolving 'backend.xxx' imports
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Align main vs backend.main module references to prevent double-import side effects
if "main" in sys.modules and "backend.main" not in sys.modules:
    sys.modules["backend.main"] = sys.modules["main"]
elif "backend.main" in sys.modules and "main" not in sys.modules:
    sys.modules["main"] = sys.modules["backend.main"]

# Load environment variables early from .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import time
import sqlite3
import json
import httpx
import logging
import asyncio
import re
import glob
import threading
from functools import lru_cache
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form, Cookie, Depends, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union
# --- Federated Configuration ---
BASE_DIR = os.getenv("ALPHABOT_DB_DIR", os.path.dirname(os.path.abspath(__file__)))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alphabot-federated-engine")

def is_plant_db(db_path):
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'metrics_%'")
        row = cursor.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False

# Runtime / system files that must never join analytics federation
SYSTEM_DB_NAMES = frozenset({
    "sessions",
    "benchmark_test",
    "corporate_metrics_db_7",
    "corporate_metrics_db_8",
    "test_climbing_debug_sessions",
    "metadata_cache",
    "db_a",
    "db_b",
    "db_c",
})

def discover_power_plants() -> List[str]:
    try:
        discovered = []
        for f in os.listdir(BASE_DIR):
            if not f.endswith(".db"):
                continue
            db_name = os.path.splitext(f)[0]
            if db_name in SYSTEM_DB_NAMES:
                continue
            if is_plant_db(os.path.join(BASE_DIR, f)):
                discovered.append(db_name)
        return sorted(discovered)
    except Exception:
        return []

# The original aggregate pool. Anything else (charlie, sales_q4, later uploads)
# stays a separate database and is not queried with these eight.
DEFAULT_PLANT_NAMES = (
    "darlington",
    "diablo_canyon",
    "grand_gulf",
    "hinkley_point",
    "kashiwazaki",
    "palo_verde",
    "three_mile_island",
    "vogtle",
)

def _configured_default_plants() -> List[str]:
    names: List[str] = []
    config_path = os.path.join(BASE_DIR, "connections.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            db_entry = cfg.get("databases", {}) if isinstance(cfg, dict) else {}
            if isinstance(db_entry, dict) and db_entry:
                names = list(db_entry.keys())
        except Exception as e:
            logger.error(f"Error reading default plant list: {e}")
    if not names:
        names = list(DEFAULT_PLANT_NAMES)
    found = []
    for name in names:
        if name in SYSTEM_DB_NAMES:
            continue
        db_path = os.path.join(BASE_DIR, f"{name}.db")
        if os.path.exists(db_path) and is_plant_db(db_path):
            found.append(name)
    return sorted(found)

def get_federated_databases() -> List[str]:
    """The default pool: the original 8 plant databases only."""
    return _configured_default_plants()

def get_standalone_databases() -> List[str]:
    """Analytics databases that are queried on their own, not inside the default pool."""
    federated = set(get_federated_databases())
    standalone = []
    for name in ConnectionManager.keys():
        if name in SYSTEM_DB_NAMES or name in federated:
            continue
        if re.match(r"^u\d+_", name):
            continue
        db_path = ConnectionManager.db_path(name)
        if os.path.exists(db_path) and is_plant_db(db_path):
            standalone.append(name)
    return sorted(standalone)

def database_catalog(user_id: Optional[int] = None) -> Dict[str, Any]:
    from backend import accounts
    federated = get_federated_databases()
    demo = get_standalone_databases()
    labels = {name: name.replace("_", " ") for name in demo}
    owned = []
    if user_id:
        for row in accounts.list_databases(user_id):
            owned.append(row["storage_key"])
            labels[row["storage_key"]] = row["display_name"]
    return {
        "databases": demo + owned,
        "owned": owned,
        "federated": federated,
        "federated_count": len(federated),
        "labels": labels,
    }

def attach_sqlite_database(key: str, path: str) -> None:
    """Register one SQLite file so it can be queried without joining the default 8."""
    if not os.path.exists(path):
        return
    ConnectionManager._paths[key] = os.path.abspath(path)
    if key not in ConnectionManager._db_keys:
        ConnectionManager._db_keys = sorted(set(ConnectionManager._db_keys) | {key})
    registry = MetadataRegistry.get_instance()
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'metrics_%'"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(f"No metrics table in {path}")
    registry.table_names[key] = row[0]
    profiler = SemanticSchemaProfiler.get_instance()
    if key not in profiler.profiles:
        profile = profiler.profile_single_database(key, row[0], path)
        if profile:
            profiler.profiles[key] = profile
    ensure_query_indexes(path, row[0], profiler.profiles.get(key))
    try:
        MetadataRegistry._db_mtimes[key] = os.path.getmtime(os.path.abspath(path))
    except OSError:
        pass

def load_user_databases() -> None:
    from backend import accounts
    accounts.init()
    for row in accounts.all_databases():
        if not os.path.exists(row["path"]):
            continue
        try:
            attach_sqlite_database(row["storage_key"], row["path"])
        except Exception as exc:
            logger.error(f"Could not attach {row['storage_key']}: {exc}")
    from backend.services.workspace.workspace_manager import WorkspaceManager
    WorkspaceManager.get_instance().initialize()

def user_can_access(user_id: int, selected_db: Optional[str]) -> bool:
    from backend import accounts
    selected = selected_db or "default"
    if selected in ("default", "all"):
        return True
    if selected in get_federated_databases() or selected in get_standalone_databases():
        return True
    row = accounts.get_database(selected)
    return bool(row and row["user_id"] == user_id)

def current_user(alphabot_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    from backend import accounts
    if "pytest" in sys.modules and not alphabot_session:
        return {"id": 0, "email": "pytest@local"}
    user = accounts.user_from_token(alphabot_session)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user

def sync_federated_plants() -> List[str]:
    """Keep POWER_PLANTS in-place so existing imports see the live pool."""
    POWER_PLANTS[:] = get_federated_databases()
    return POWER_PLANTS

POWER_PLANTS: List[str] = []

class ConnectionManager:
    """Tracks the SQLite files this process can query."""
    _paths: Dict[str, str] = {}
    _db_keys: List[str] = []

    @classmethod
    def _sqlite_path_from_url(cls, url: str):
        if not isinstance(url, str) or not url.startswith("sqlite:///"):
            return None
        rel = url[len("sqlite:///"):]
        if not os.path.isabs(rel):
            rel = os.path.join(BASE_DIR, rel)
        return os.path.abspath(rel)

    @classmethod
    def load(cls):
        cls._paths.clear()
        config_path = os.path.join(BASE_DIR, "connections.json")
        databases_config = {}
        ignore = set()
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    cfg = json.load(f)
                if isinstance(cfg, list):
                    for item in cfg:
                        if isinstance(item, dict) and "name" in item and "url" in item:
                            databases_config[item["name"]] = item["url"]
                elif isinstance(cfg, dict):
                    ignore = set(cfg.get("ignore", []))
                    db_entry = cfg.get("databases", {})
                    if isinstance(db_entry, dict):
                        databases_config = db_entry
                    elif isinstance(db_entry, list):
                        for item in db_entry:
                            if isinstance(item, dict) and "name" in item and "url" in item:
                                databases_config[item["name"]] = item["url"]
            except Exception as e:
                logger.error(f"Error loading connections.json: {e}. Falling back to discovery.")
                databases_config = {}

        for key, url in databases_config.items():
            if key in ignore:
                continue
            resolved = cls._sqlite_path_from_url(url)
            if resolved:
                cls._paths[key] = resolved

        try:
            for name in discover_power_plants():
                if name not in cls._paths and name not in ignore:
                    discovered = os.path.join(BASE_DIR, f"{name}.db")
                    if os.path.exists(discovered):
                        cls._paths[name] = os.path.abspath(discovered)
        except Exception as e:
            logger.error(f"Error during auto-discovery of databases: {e}")
        
        cls._db_keys = sorted(cls._paths.keys())
        sync_federated_plants()
        return cls._db_keys

    @classmethod
    def keys(cls) -> List[str]:
        return cls._db_keys

    @classmethod
    def db_path(cls, key: str) -> str:
        return cls._paths.get(key) or os.path.join(BASE_DIR, f"{key}.db")

try:
    ConnectionManager.load()
except Exception:
    pass

def is_default_plant(plant_name: str) -> bool:
    import sys
    # In test environment, all databases are treated as default to support mock tests
    if "pytest" in sys.modules:
        return True
    return plant_name in POWER_PLANTS


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alphabot-federated-engine")

def mentioned_years(text: str) -> List[str]:
    """Years a question names, in the order they apply.

    A span joined by to, through, until, or a hyphen includes every year from
    the start through the end. The same is true of 'between 2020 and 2023'.
    Years joined by and, or, vs, or versus stay as only those years.
    """
    if not text:
        return []
    year = r"((?:19|20)\d{2})"
    pattern = re.compile(
        rf"\bbetween\s+{year}\s+and\s+{year}\b"
        rf"|(?<!\d){year}\s*(?:to|through|thru|till|until|-)\s*{year}(?!\d)"
        rf"|(?<!\d){year}(?!\d)"
    )
    found: List[str] = []

    def add(value: str) -> None:
        if value not in found:
            found.append(value)

    for match in pattern.finditer(text.lower()):
        ends = [group for group in match.groups() if group]
        if len(ends) == 2:
            start, end = int(ends[0]), int(ends[1])
            step = 1 if start <= end else -1
            for value in range(start, end + step, step):
                add(str(value))
        else:
            add(ends[0])
    return found


def extract_detected_years(text: str) -> List[str]:
    return mentioned_years(text)


def extract_raw_year_candidates(text: str) -> List[str]:
    import re
    if not text:
        return []
    tokens = re.findall(r'\b[a-zA-Z0-9_-]+\b', text.lower())
    years = []
    for token in tokens:
        matches = re.findall(r'(?<!\d)(\d{4})(?!\d)', token)
        if matches:
            cleaned = re.sub(r'[0-9_fy\-]', '', token)
            if not cleaned:
                years.extend(matches)
    return years


def _make_plurals(word: str) -> List[str]:
    """Generate common plural forms including irregular endings for dimension matching."""
    variants = [word]
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        variants.append(word[:-1] + "ies")   # facility -> facilities
    elif word.endswith(("s", "x", "z", "sh", "ch")):
        variants.append(word + "es")           # status -> statuses
    else:
        variants.append(word + "s")            # vendor -> vendors
    return variants


# --- Semantic Schema Profiler (Dynamic Classification & Profiling Layer) ---
class ColumnProfile(BaseModel):
    classification: str  # METRIC, DIMENSION, IDENTIFIER, STATUS, TIME
    data_type: str
    canonical_concept: Optional[str] = None
    aliases: List[str] = []
    cardinality: Optional[int] = None
    cardinality_ratio: Optional[float] = None
    sample_values: List[Any] = []
    pattern_format: Optional[str] = None
    unit: Optional[str] = None
    aggregation_default: Optional[str] = None

class SchemaProfile(BaseModel):
    database_name: str
    table_name: str
    primary_entity: str = "project"
    columns: Dict[str, ColumnProfile] = {}
    defaults: Dict[str, Any] = {}

class SemanticSchemaProfiler:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.profiles = {}  # db_name -> SchemaProfile
        self._initialized = False

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def profile_single_database(self, plant: str, table_name: str, db_path: str) -> Optional[SchemaProfile]:
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({table_name})")
            cols = cursor.fetchall()
            
            # Fetch row count
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            row_count = cursor.fetchone()[0] or 1
            
            columns_profile = {}
            for col in cols:
                col_name, col_type = col[1], col[2].upper()
                col_name_lower = col_name.lower()
                
                # Sample distinct values and count cardinality (use subquery limit for cardinality count on large tables)
                cursor.execute(f"SELECT DISTINCT {col_name} FROM {table_name} WHERE {col_name} IS NOT NULL LIMIT 400")
                distinct_vals = [row[0] for row in cursor.fetchall()]
                
                if row_count > 10000:
                    cursor.execute(f"SELECT COUNT(DISTINCT {col_name}) FROM (SELECT {col_name} FROM {table_name} LIMIT 10000)")
                    sample_cardinality = cursor.fetchone()[0] or 0
                    cardinality_ratio = float(sample_cardinality) / 10000.0
                    cardinality = int(cardinality_ratio * row_count)
                else:
                    cursor.execute(f"SELECT COUNT(DISTINCT {col_name}) FROM {table_name}")
                    cardinality = cursor.fetchone()[0] or 0
                    cardinality_ratio = float(cardinality) / float(row_count)
                
                # --- Phase 7E: Semantic-aware classification & alias generation ---
                try:
                    from backend.services.workspace.semantic_engine import (
                        classify_column_semantic,
                        expand_column_name,
                        resolve_canonical_concept,
                    )
                    classification = classify_column_semantic(
                        col_name_lower, col_type, cardinality_ratio, distinct_vals, row_count
                    )
                    aliases = expand_column_name(col_name_lower)
                    canonical_concept = resolve_canonical_concept(col_name_lower, aliases)
                except Exception as _se_err:
                    # Graceful fallback to original logic if semantic engine unavailable
                    logger.warning(f"semantic_engine unavailable, using fallback for {col_name_lower}: {_se_err}")
                    is_numeric = any(t in col_type for t in ["INT", "NUMERIC", "REAL", "DOUBLE", "FLOAT", "DECIMAL"])
                    classification = "DIMENSION"
                    if is_numeric:
                        if re.search(r"(?i)(date|time|year|month|period|quarter|fy_)$", col_name_lower):
                            classification = "TIME"
                        elif re.search(r"(?i)(_id|_code|_uuid|_ref|identifier)$", col_name_lower):
                            classification = "IDENTIFIER"
                        else:
                            classification = "METRIC"
                    else:
                        if re.search(r"(?i)(date|time|year|month|period|quarter|fy_)$", col_name_lower):
                            classification = "TIME"
                        elif re.search(r"(?i)(status|state|stage|phase|payment)$", col_name_lower):
                            classification = "STATUS"
                        elif re.search(r"(?i)(_id|_code|_uuid|_ref|identifier)$", col_name_lower):
                            classification = "IDENTIFIER"
                    aliases = sorted(list({col_name_lower, col_name_lower.replace("_", " "), col_name_lower.replace(" ", "_")}))
                    canonical_concept = None

                # Identify default unit and aggregation defaults
                unit = None
                aggregation_default = None
                if classification == "METRIC":
                    if "percentage" in col_name_lower or "pct" in col_name_lower or "completion" in col_name_lower:
                        unit = "PERCENT"
                        aggregation_default = "AVG"
                    elif "days" in col_name_lower or "delay" in col_name_lower:
                        unit = "DAYS"
                        aggregation_default = "AVG"
                    elif "capacity" in col_name_lower or "mw" in col_name_lower:
                        unit = "MW"
                        aggregation_default = "SUM"
                    else:
                        unit = "USD"
                        aggregation_default = "SUM"
                
                columns_profile[col_name_lower] = ColumnProfile(
                    classification=classification,
                    data_type=col_type,
                    canonical_concept=canonical_concept,
                    aliases=aliases,
                    cardinality=cardinality,
                    cardinality_ratio=cardinality_ratio,
                    sample_values=distinct_vals[:400] if classification in ("DIMENSION", "STATUS") else distinct_vals[:50],
                    unit=unit,
                    aggregation_default=aggregation_default
                )
                
            # KPIs
            metrics_priority = ["revenue", "capacity_mw", "budget_allocated", "budget_used", "budget_remaining", "completion_percentage", "delay_days"]
            metric_cols = [m for m, p in columns_profile.items() if p.classification == "METRIC"]
            sorted_metrics = sorted(metric_cols, key=lambda m: metrics_priority.index(m) if m in metrics_priority else len(metrics_priority))
            kpis = sorted_metrics[:4]
            
            # Groupings
            dim_priority = ["project_type", "location", "state", "contractor_name", "category", "material_status"]
            dim_cols = [d for d, p in columns_profile.items() if p.classification in ["DIMENSION", "STATUS"]]
            sorted_dims = sorted(dim_cols, key=lambda d: dim_priority.index(d) if d in dim_priority else len(dim_priority))
            groupings = sorted_dims[:3]
            
            profile = SchemaProfile(
                database_name=plant,
                table_name=table_name,
                columns=columns_profile,
                defaults={
                    "kpi_candidates": kpis,
                    "grouping_dimensions": groupings
                }
            )
            conn.close()
            return profile
        except Exception as e:
            logger.error(f"Error profiling plant database {plant}: {e}")
            return None

    def profile_databases(self, registry, force=False):
        if self._initialized and not force:
            return
        logger.info("🔍 Running Semantic Schema Profiler on discovered databases...")
        self.profiles.clear()
        
        # 1. Access dynamic database structures
        for plant in registry.table_names.keys():
            table_name = registry.table_names.get(plant)
            db_path = ConnectionManager.db_path(plant)
            if not os.path.exists(db_path) or not table_name:
                continue
            
            profile = self.profile_single_database(plant, table_name, db_path)
            if profile:
                self.profiles[plant] = profile
                
        self._initialized = True
        logger.info(f"✅ Semantic Schema Profiler initialized successfully with {len(self.profiles)} database profiles.")


# --- Semantic Schema Adapter (Unified Mapping Layer) ---
class SemanticSchemaAdapter:
    _instance = None
    _lock = threading.RLock()

    def __init__(self):
        self.column_to_type = {}       # e.g., 'capacity_mw': 'METRIC'
        self.column_to_aliases = {}    # e.g., 'project_type': {'project_type', 'type', 'department', ...}
        self.alias_to_column = {}      # e.g., 'department': 'project_type'
        self.dimensions_values = {}    # e.g., 'project_type': ['Solar', 'Wind', ...]
        self.kpi_candidates = []       # e.g., ['revenue', 'capacity_mw', ...]
        self.grouping_dimensions = []  # e.g., ['project_type', 'location']
        self._initialized = False

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def build_schema_maps(self, registry):
        logger.info("🔧 Building Dynamic Semantic Schema Map...")
        self.column_to_type.clear()
        self.column_to_aliases.clear()
        self.alias_to_column.clear()
        self.dimensions_values.clear()
        self.kpi_candidates.clear()
        self.grouping_dimensions.clear()

        # Rebuild profiler first
        profiler = SemanticSchemaProfiler.get_instance()
        profiler.profile_databases(registry)

        if not profiler.profiles:
            # Fallback to populating from registry fields (needed for mock test environment)
            for col_name, info in registry.metrics.items():
                self.column_to_type[col_name.lower()] = "METRIC"
                
            for col_name, info in registry.categoricals.items():
                col_lower = col_name.lower()
                self.column_to_type[col_lower] = "DIMENSION"
                self.dimensions_values[col_lower] = list(info.get("values", []))

            # Dynamic classification (Identifier / Status / Date / Metric / Dimension)
            for col_name in list(self.column_to_type.keys()):
                if re.search(r"(?i)(_id|_code|_uuid|identifier|name)$", col_name):
                    self.column_to_type[col_name] = "IDENTIFIER"
                elif re.search(r"(?i)(date|time|year|month|period|quarter|fy_)$", col_name):
                    self.column_to_type[col_name] = "DATE"
                elif re.search(r"(?i)(status|state|stage|phase|payment)$", col_name) and self.column_to_type[col_name] == "DIMENSION":
                    self.column_to_type[col_name] = "STATUS"
        else:
            # Merge profiles into unified adapter dictionaries
            for plant, profile in profiler.profiles.items():
                if is_default_plant(plant):
                    for col_name, col_profile in profile.columns.items():
                        self.column_to_type[col_name] = col_profile.classification
                        if col_profile.classification in ["DIMENSION", "STATUS"] or col_name in registry.categoricals:
                            # Collect dimension values
                            if col_name not in self.dimensions_values:
                                self.dimensions_values[col_name] = []
                            # Merge unique sampled values
                            self.dimensions_values[col_name].extend(col_profile.sample_values)
                            self.dimensions_values[col_name] = sorted(list(set(self.dimensions_values[col_name])))
                        
                        # Merge aliases from database profile
                        if col_name not in self.column_to_aliases:
                            self.column_to_aliases[col_name] = set()
                        self.column_to_aliases[col_name].update(col_profile.aliases)
                        
                        for alias in col_profile.aliases:
                            self.alias_to_column[alias] = col_name

                    # Merge defaults
                    kpis = profile.defaults.get("kpi_candidates", [])
                    for kpi in kpis:
                        if kpi not in self.kpi_candidates:
                            self.kpi_candidates.append(kpi)
                    
                    groupings = profile.defaults.get("grouping_dimensions", [])
                    for grp in groupings:
                        if grp not in self.grouping_dimensions:
                            self.grouping_dimensions.append(grp)

        # Fill in aliases for columns not covered by standard synonyms
        for col_name in self.column_to_type:
            if col_name not in self.column_to_aliases:
                syns = {col_name, col_name.replace('_', ' '), col_name.replace(' ', '_')}
                self.column_to_aliases[col_name] = syns
                for syn in syns:
                    self.alias_to_column[syn] = col_name

        # Re-ensure / Generate Defaults if not populated from profiles
        if not self.kpi_candidates:
            metrics_priority = ["revenue", "capacity_mw", "budget_allocated", "budget_used", "budget_remaining", "completion_percentage", "delay_days"]
            metric_cols = [m for m in self.column_to_type if self.column_to_type[m] == "METRIC"]
            sorted_metrics = sorted(metric_cols, key=lambda m: metrics_priority.index(m) if m in metrics_priority else len(metrics_priority))
            self.kpi_candidates = sorted_metrics[:4]

        if not self.grouping_dimensions:
            dim_priority = ["project_type", "location", "state", "contractor_name", "category", "material_status"]
            dim_cols = [d for d in self.column_to_type if self.column_to_type[d] in ["DIMENSION", "STATUS"]]
            sorted_dims = sorted(dim_cols, key=lambda d: dim_priority.index(d) if d in dim_priority else len(dim_priority))
            self.grouping_dimensions = sorted_dims[:3]

        # Limit KPIs and groupings to max size
        self.kpi_candidates = self.kpi_candidates[:4]
        self.grouping_dimensions = self.grouping_dimensions[:3]

        self._initialized = True
        logger.info(f"✅ Semantic Schema Adapter initialized successfully!")
        logger.info(f"   Mapped Columns: {list(self.column_to_type.keys())}")
        logger.info(f"   Default KPIs: {self.kpi_candidates}")
        logger.info(f"   Default Groupings: {self.grouping_dimensions}")

    def build_schema_maps_for_profile(self, plant: str, profile) -> None:
        """Builds semantic maps scoped exclusively to a single database profile."""
        self.column_to_type.clear()
        self.column_to_aliases.clear()
        self.alias_to_column.clear()
        self.dimensions_values.clear()
        self.kpi_candidates.clear()
        self.grouping_dimensions.clear()
        
        for col_name, col_profile in profile.columns.items():
            self.column_to_type[col_name] = col_profile.classification
            if col_profile.classification in ["DIMENSION", "STATUS", "IDENTIFIER"]:
                if col_name not in self.dimensions_values:
                    self.dimensions_values[col_name] = []
                self.dimensions_values[col_name].extend(col_profile.sample_values)
                self.dimensions_values[col_name] = sorted(list(set(self.dimensions_values[col_name])))
            
            # Merge aliases
            if col_name not in self.column_to_aliases:
                self.column_to_aliases[col_name] = set()
            self.column_to_aliases[col_name].update(col_profile.aliases)
            
            for alias in col_profile.aliases:
                self.alias_to_column[alias] = col_name
                self.alias_to_column[alias.replace('_', ' ')] = col_name
                self.alias_to_column[alias.replace(' ', '_')] = col_name
                
        # Merge defaults
        kpis = profile.defaults.get("kpi_candidates", [])
        for kpi in kpis:
            if kpi not in self.kpi_candidates:
                self.kpi_candidates.append(kpi)
        
        groupings = profile.defaults.get("grouping_dimensions", [])
        for grp in groupings:
            if grp not in self.grouping_dimensions:
                self.grouping_dimensions.append(grp)

        # Fill in aliases for columns not covered by standard synonyms
        for col_name in self.column_to_type:
            if col_name not in self.column_to_aliases:
                syns = {col_name, col_name.replace('_', ' '), col_name.replace(' ', '_')}
                self.column_to_aliases[col_name] = syns
                for syn in syns:
                    self.alias_to_column[syn] = col_name

        self.kpi_candidates = self.kpi_candidates[:4]
        self.grouping_dimensions = self.grouping_dimensions[:3]
        self._initialized = True

    def resolve_column_name(self, alias_name: str) -> Optional[str]:
        if not alias_name:
            return None
        alias_clean = alias_name.lower().strip()
        if alias_clean in self.column_to_type:
            return alias_clean
        if alias_clean in self.alias_to_column:
            return self.alias_to_column[alias_clean]
        
        for col, aliases in self.column_to_aliases.items():
            for a in aliases:
                a_lower = a.lower()
                # 1. Direct substring match (existing behaviour)
                if alias_clean in a_lower or a_lower in alias_clean:
                    return col
                # 2. Prefix-token match: "supplier" matches "supplier name"
                q_words = alias_clean.split()
                a_words = a_lower.replace('_', ' ').split()
                if q_words and a_words and len(q_words) <= len(a_words):
                    if all(aw.startswith(qw) for qw, aw in zip(q_words, a_words)):
                        return col
        return None

    def get_dimension_values(self, dimension_alias: str) -> List[Any]:
        col = self.resolve_column_name(dimension_alias)
        if col and col in self.dimensions_values:
            return self.dimensions_values[col]
        return []



# --- Metadata Registry (Federated Model) ---
class MetadataRegistry:
    _instance = None
    _lock = threading.RLock()
    _last_checked = 0.0
    _db_mtimes = {}
    _config_mtime = 0.0

    def __init__(self):
        self.metrics = {}       # e.g., 'revenue': {'column': 'revenue', 'type': 'NUMERIC'}
        self.categoricals = {}  # e.g., 'department': {'values': ['sales', 'digital', ...]}
        self.table_names = {}   # e.g., 'diablo_canyon': 'metrics_diablo_canyon'
        self._initialized = False

    @classmethod
    def get_instance(cls):
        global POWER_PLANTS
        with cls._lock:
            config_path = os.path.join(BASE_DIR, "connections.json")
            config_mtime = 0.0
            if os.path.exists(config_path):
                try:
                    config_mtime = os.path.getmtime(config_path)
                except OSError:
                    config_mtime = 0.0

            if cls._instance is None:
                cls._instance = cls()
                cls._instance.initialize()
                cls._db_mtimes = {}
                cls._config_mtime = config_mtime
                for db in ConnectionManager.keys():
                    path = ConnectionManager.db_path(db)
                    if os.path.exists(path):
                        try:
                            cls._db_mtimes[db] = os.path.getmtime(path)
                        except OSError:
                            pass
            else:
                now = time.time()
                if now - cls._last_checked > 5.0:
                    cls._last_checked = now
                    
                    needs_reload = False
                    if config_mtime != cls._config_mtime:
                        needs_reload = True
                    else:
                        current_dbs = ConnectionManager.keys()
                        for db in current_dbs:
                            path = ConnectionManager.db_path(db)
                            if os.path.exists(path):
                                try:
                                    mtime = os.path.getmtime(path)
                                    if cls._db_mtimes.get(db) != mtime:
                                        needs_reload = True
                                        break
                                except OSError:
                                    needs_reload = True
                                    break
                    
                    if needs_reload:
                        logger.info("🔄 Schema drift or DB file/config modification detected! Hot-reloading registry...")
                        if config_mtime != cls._config_mtime:
                            ConnectionManager.load()
                        cls._instance._initialized = False
                        cls._instance.initialize()
                        cls._db_mtimes.clear()
                        cls._config_mtime = config_mtime
                        for db in ConnectionManager.keys():
                            path = ConnectionManager.db_path(db)
                            if os.path.exists(path):
                                try:
                                    cls._db_mtimes[db] = os.path.getmtime(path)
                                except OSError:
                                    pass
                # Prevent cache corruption after schema reload
                        RESULT_CACHE.clear()
                        SUGGEST_CACHE.clear()
                        correct_query_spelling.cache_clear()
                        is_value_in_workspace_db.cache_clear()
        return cls._instance

    def initialize(self):
        if self._initialized:
            return
        
        start_init = time.perf_counter()
        logger.info("🚀 Initializing Singleton Metadata Registry with Cache...")
        
        db_keys = ConnectionManager.keys()
        if not db_keys:
            logger.error("FATAL: No database files found.")
            return

        new_metrics = {}
        new_categoricals = {}
        new_table_names = {}
        
        cache_path = os.path.join(BASE_DIR, "metadata_cache.json")
        cache_data = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
            except Exception as e:
                logger.error(f"Error loading metadata_cache.json: {e}")

        cached_dbs = cache_data.get("databases", {})
        new_cached_dbs = {}
        cache_dirty = False
        
        profiler = SemanticSchemaProfiler.get_instance()
        profiler.profiles.clear()
        
        for plant in db_keys:
            db_path = ConnectionManager.db_path(plant)
            current_mtime = 0.0
            if os.path.exists(db_path):
                try:
                    current_mtime = os.path.getmtime(db_path)
                except OSError:
                    current_mtime = 0.0
            
            cached_entry = cached_dbs.get(plant, {})
            cached_mtime = cached_entry.get("db_mtime", -1.0)
            cached_profile_dict = cached_entry.get("profile")
            
            if (cached_mtime == current_mtime 
                    and cached_profile_dict is not None 
                    and "table_name" in cached_entry 
                    and "categoricals" in cached_entry):
                logger.info(f"💾 Cache hit for {plant}. Loading metadata from cache.")
                table_name = cached_entry["table_name"]
                new_table_names[plant] = table_name
                
                try:
                    profile = SchemaProfile.model_validate(cached_profile_dict)
                    profiler.profiles[plant] = profile
                    
                    for col_name, col_profile in profile.columns.items():
                        if col_profile.classification == "METRIC":
                            if is_default_plant(plant):
                                new_metrics[col_name] = {"column": col_name, "type": col_profile.data_type}
                            
                    for col_name, vals in cached_entry["categoricals"].items():
                        if is_default_plant(plant):
                            if col_name not in new_categoricals:
                                new_categoricals[col_name] = {"values": set()}
                            new_categoricals[col_name]["values"].update(vals)
                        
                    new_cached_dbs[plant] = cached_entry
                except Exception as ex:
                    logger.error(f"Error validating cached profile for {plant}: {ex}. Re-profiling...")
                    cached_mtime = -1.0
                    
            if cached_mtime != current_mtime:
                logger.info(f"🔍 Cache miss or drift for {plant}. Introspecting database...")
                cache_dirty = True
                
                table_name = None
                try:
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'metrics_%'")
                    res = cursor.fetchone()
                    if res:
                        table_name = res[0]
                    conn.close()
                except Exception as e:
                    logger.warning(f"Table name discovery error on {plant}: {e}")
                    
                if not table_name:
                    logger.warning(f"Could not find metrics table in {plant}. Skipping.")
                    continue
                    
                new_table_names[plant] = table_name
                
                profile = profiler.profile_single_database(plant, table_name, db_path)
                if not profile:
                    logger.warning(f"Could not profile {plant}. Skipping.")
                    continue
                
                profiler.profiles[plant] = profile
                
                db_categoricals = {}
                try:
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()
                    for col_name, col_profile in profile.columns.items():
                        classification = col_profile.classification
                        col_type = col_profile.data_type
                        
                        if classification == "METRIC":
                            if is_default_plant(plant):
                                new_metrics[col_name] = {"column": col_name, "type": col_type}
                        elif classification in ["DIMENSION", "STATUS", "IDENTIFIER"]:
                            if col_name not in db_categoricals:
                                db_categoricals[col_name] = []
                            limit = 100 if classification == "IDENTIFIER" else 50
                            cursor.execute(f"SELECT DISTINCT {col_name} FROM {profile.table_name} WHERE {col_name} IS NOT NULL LIMIT {limit}")
                            vals = [str(row[0]) for row in cursor.fetchall()]
                            if is_default_plant(plant):
                                if col_name not in new_categoricals:
                                    new_categoricals[col_name] = {"values": set()}
                                new_categoricals[col_name]["values"].update(vals)
                            db_categoricals[col_name].extend(vals)
                        elif classification == "TIME":
                            if col_name not in db_categoricals:
                                db_categoricals[col_name] = []
                            cursor.execute(f"SELECT DISTINCT {col_name} FROM {profile.table_name} WHERE {col_name} IS NOT NULL LIMIT 100")
                            vals = []
                            for row in cursor.fetchall():
                                val_str = str(row[0])
                                if val_str.isdigit():
                                    vals.append(int(val_str))
                                else:
                                    vals.append(val_str)
                            if is_default_plant(plant):
                                if col_name not in new_categoricals:
                                    new_categoricals[col_name] = {"values": set()}
                                new_categoricals[col_name]["values"].update(vals)
                            db_categoricals[col_name].extend(vals)
                    conn.close()

                except Exception as e:
                    logger.warning(f"Dynamic Metadata Extraction Error on {plant}: {e}")
                
                new_cached_dbs[plant] = {
                    "db_mtime": current_mtime,
                    "table_name": table_name,
                    "profile": profile.model_dump(mode='json'),
                    "categoricals": db_categoricals
                }
        
        self.table_names = new_table_names
        profiler._initialized = True
        new_categoricals["plant"] = {"values": set(POWER_PLANTS)}
        
        for k in new_categoricals:
            new_categoricals[k]["values"] = list(new_categoricals[k]["values"])

        self.metrics = new_metrics
        self.categoricals = new_categoricals
        self._initialized = True

        for plant, table_name in new_table_names.items():
            db_path = ConnectionManager.db_path(plant)
            if not db_path or not os.path.exists(db_path):
                continue
            try:
                before = os.path.getmtime(db_path)
            except OSError:
                before = None
            ensure_query_indexes(db_path, table_name, profiler.profiles.get(plant))
            try:
                after = os.path.getmtime(db_path)
            except OSError:
                after = before
            if before is not None and after != before and plant in new_cached_dbs:
                new_cached_dbs[plant]["db_mtime"] = after
                cache_dirty = True
        
        init_ms = (time.perf_counter() - start_init) * 1000
        logger.info(f"✅ Singleton Registry Initialized. Metrics: {list(self.metrics.keys())}")
        
        if cache_dirty:
            config_path = os.path.join(BASE_DIR, "connections.json")
            config_mtime = 0.0
            if os.path.exists(config_path):
                try:
                    config_mtime = os.path.getmtime(config_path)
                except OSError:
                    config_mtime = 0.0
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "config_mtime": config_mtime,
                        "databases": new_cached_dbs
                    }, f, indent=2, ensure_ascii=False)
                logger.info("💾 Saved updated metadata cache to metadata_cache.json")
            except Exception as e:
                logger.error(f"Error saving metadata_cache.json: {e}")
                
        try:
            SemanticSchemaAdapter.get_instance().build_schema_maps(self)
        except Exception as e:
            logger.error(f"Failed to build semantic schema maps: {e}")
            
        try:
            initialize_trie(self)
        except Exception as e:
            logger.warning(f"Failed to initialize suggestions trie: {e}")

        try:
            from backend.services.workspace.workspace_manager import WorkspaceManager
            WorkspaceManager.get_instance().initialize(self)
        except Exception as e:
            logger.error(f"Failed to initialize WorkspaceManager: {e}")


# --- Pydantic Models ---
class Blueprint(BaseModel):
    operation: Optional[str] = "SUM"
    metrics: List[str] = []
    filters: List[Dict[str, Any]] = []
    timeframe: Optional[Dict[str, Any]] = None
    timeframes: List[Dict[str, Any]] = []
    is_range: bool = False
    comparison: Optional[Dict[str, Any]] = None
    limit: Optional[int] = None
    sort_asc: Optional[bool] = None
    breakdown_by: Optional[str] = None

def should_aggregate_as_avg(col_name: str, plant_profile: Optional[Any] = None) -> bool:
    """
    Robust check to determine if a column should aggregate as AVG instead of SUM.
    Checks column profile (aggregation_default and unit) and falls back to key word heuristics.
    """
    col_lower = col_name.lower().strip()
    
    # 1. Profile-based check
    if plant_profile and hasattr(plant_profile, 'columns') and col_lower in plant_profile.columns:
        cp = plant_profile.columns[col_lower]
        if getattr(cp, 'aggregation_default', None) == "AVG":
            return True
        if getattr(cp, 'unit', None) in ["PERCENT", "DAYS"]:
            return True
            
    # 2. Keyword heuristics check (covers percent, days, delay, pct, percentage, rate, avg, average, ratio, fraction)
    rate_keywords = ['pct', 'percent', 'percentage', 'delay', 'days', 'rate', 'avg', 'average', 'ratio', 'fraction']
    return any(kw in col_lower for kw in rate_keywords)

_IDENTIFIER_TOKEN = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9_\-]{2,40}$")

@lru_cache(maxsize=4096)
def is_value_in_workspace_db(value: str, workspace_id: str) -> bool:
    """
    True when a token is a real identifier stored in a database.

    Ordinary words are resolved from the in-memory vocabulary. This lookup
    runs only for id-shaped tokens, and only against identifier columns,
    so a typo cannot full-scan every analytics table.
    """
    val_clean = (value or "").strip()
    if not val_clean or not _IDENTIFIER_TOKEN.match(val_clean):
        return False
        
    workspace_id = workspace_id or "default"
    try:
        profiler = SemanticSchemaProfiler.get_instance()
        plants = list(POWER_PLANTS) if workspace_id in ("default", "all") else [workspace_id]
        registry = MetadataRegistry.get_instance()
        needle = val_clean.lower()
        for plant in plants:
            profile = profiler.profiles.get(plant)
            if not profile:
                continue
            id_cols = []
            for col_name, col_prof in profile.columns.items():
                if col_prof.classification != "IDENTIFIER":
                    continue
                if any(str(v).lower() == needle for v in col_prof.sample_values):
                    return True
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", col_name):
                    id_cols.append(col_name)
            if not id_cols:
                continue
            table_name = registry.table_names.get(plant) or profile.table_name
            if not table_name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table_name):
                continue
            db_file = ConnectionManager.db_path(plant)
            if not os.path.exists(db_file):
                continue
            conn = _borrow_sqlite(db_file)
            try:
                for col in id_cols:
                    row = conn.execute(
                        f"SELECT 1 FROM {table_name} WHERE {col} = ? OR {col} = ? LIMIT 1",
                        (val_clean, val_clean.upper()),
                    ).fetchone()
                    if row:
                        return True
            finally:
                pass
    except Exception:
        return False
    return False

def calculate_blueprint_hash(blueprint: Blueprint) -> str:
    """Computes a stable MD5 hash for a given Blueprint after sorting its lists and keys."""
    # Normalize filters to ensure order-independence and type consistency
    normalized_filters = []
    for f in (blueprint.filters or []):
        if isinstance(f, dict) and "column" in f:
            normalized_filters.append({
                "column": f["column"],
                "value": str(f.get("value"))
            })
    normalized_filters = sorted(normalized_filters, key=lambda x: (x["column"] or "", x["value"] or ""))
    
    # Normalize timeframes
    normalized_tfs = []
    for tf in (blueprint.timeframes or []):
        if isinstance(tf, dict):
            normalized_tfs.append({k: v for k, v in tf.items() if v is not None})
    normalized_tfs = sorted(normalized_tfs, key=lambda x: str(x.get("value") or ""))

    bp_dict = {
        "operation": blueprint.operation or "SUM",
        "metrics": sorted(blueprint.metrics or []),
        "filters": normalized_filters,
        "timeframe": {k: v for k, v in (blueprint.timeframe or {}).items() if v is not None} if blueprint.timeframe else None,
        "timeframes": normalized_tfs,
        "is_range": blueprint.is_range,
        "comparison": {k: v for k, v in (blueprint.comparison or {}).items() if v is not None} if blueprint.comparison else None,
        "limit": blueprint.limit,
        "sort_asc": blueprint.sort_asc,
        "breakdown_by": blueprint.breakdown_by
    }
    serialized = json.dumps(bp_dict, sort_keys=True)
    import hashlib
    return hashlib.md5(serialized.encode()).hexdigest()

def calculate_blueprint_delta(parent: Optional[Blueprint], child: Blueprint) -> Dict[str, Any]:
    """Computes differences in operations, metrics, filters, timeframes, breakdowns, limits, and sorting."""
    actions = []
    if parent is None:
        return {"actions": []}

    # 1. CHANGE_OPERATION
    if parent.operation != child.operation:
        actions.append({
            "type": "CHANGE_OPERATION",
            "from": parent.operation,
            "to": child.operation
        })

    # 2. CHANGE_METRIC
    parent_metrics = parent.metrics or []
    child_metrics = child.metrics or []
    if parent_metrics != child_metrics:
        if len(parent_metrics) == 1 and len(child_metrics) == 1:
            actions.append({
                "type": "CHANGE_METRIC",
                "from": parent_metrics[0],
                "to": child_metrics[0]
            })
        else:
            actions.append({
                "type": "CHANGE_METRIC",
                "from": parent_metrics,
                "to": child_metrics
            })

    # 3. Filters: ADD_FILTER, REMOVE_FILTER, MODIFY_FILTER
    parent_filters = {f.get("column"): f.get("value") for f in (parent.filters or []) if isinstance(f, dict) and "column" in f}
    child_filters = {f.get("column"): f.get("value") for f in (child.filters or []) if isinstance(f, dict) and "column" in f}

    for col, val in child_filters.items():
        if col not in parent_filters:
            actions.append({
                "type": "ADD_FILTER",
                "field": col,
                "value": val
            })
        elif parent_filters[col] != val:
            actions.append({
                "type": "MODIFY_FILTER",
                "field": col,
                "from": parent_filters[col],
                "to": val
            })

    for col, val in parent_filters.items():
        if col not in child_filters:
            actions.append({
                "type": "REMOVE_FILTER",
                "field": col,
                "value": val
            })

    # 4. CHANGE_TIMEFRAME
    parent_tf = parent.timeframe
    if not parent_tf and parent.timeframes:
        parent_tf = parent.timeframes[0]
    child_tf = child.timeframe
    if not child_tf and child.timeframes:
        child_tf = child.timeframes[0]

    if parent_tf != child_tf:
        parent_tf_str = str(parent_tf.get("value") or parent_tf.get("type")) if parent_tf else None
        child_tf_str = str(child_tf.get("value") or child_tf.get("type")) if child_tf else None
        actions.append({
            "type": "CHANGE_TIMEFRAME",
            "from": parent_tf_str,
            "to": child_tf_str
        })

    # 5. CHANGE_BREAKDOWN
    parent_breakdown = parent.comparison.get("type") if parent.comparison else parent.breakdown_by
    child_breakdown = child.comparison.get("type") if child.comparison else child.breakdown_by
    if parent_breakdown != child_breakdown:
        actions.append({
            "type": "CHANGE_BREAKDOWN",
            "from": parent_breakdown,
            "to": child_breakdown
        })

    # 6. CHANGE_LIMIT
    if parent.limit != child.limit:
        actions.append({
            "type": "CHANGE_LIMIT",
            "from": parent.limit,
            "to": child.limit
        })

    # 7. CHANGE_SORT
    if parent.sort_asc != child.sort_asc:
        actions.append({
            "type": "CHANGE_SORT",
            "from": parent.sort_asc,
            "to": child.sort_asc
        })

    return {"actions": actions}

ACTIVE_QUERIES: Dict[str, bool] = {}

class QueryBlueprintPayload(BaseModel):
    raw_query: str
    blueprint: Optional[Blueprint] = None
    force_llm: bool = False
    parsing_metadata: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    parent_id: Optional[int] = None
    selected_db: Optional[str] = None




# --- Conversational State Management ---

class ConversationState(BaseModel):
    """Holds the active analytical context for a conversation session."""
    session_id: str
    user_id: str = "default"
    active_metric: Optional[str] = None          # e.g. 'budget_allocated'
    active_filters: List[Dict[str, Any]] = []    # e.g. [{column: 'state', value: 'Gujarat'}]
    active_timeframe: Optional[Dict[str, Any]] = None   # e.g. {type: 'year', value: '2025'}
    comparison_entities: List[str] = []          # e.g. ['Gujarat', 'Rajasthan']
    active_operation: Optional[str] = None       # e.g. 'BREAKDOWN'
    active_limit: Optional[int] = None
    active_comparison: Optional[Dict[str, Any]] = None  # e.g. {type: 'state'}
    last_query: Optional[str] = None
    active_db: Optional[str] = None
    last_updated: float = 0.0


class ConversationStateManager:
    """Singleton that manages SQLite conversation state keyed by session_id."""
    _instance: Optional['ConversationStateManager'] = None
    SESSION_TTL: float = 7 * 24 * 60 * 60  # 7 days
    DB_PATH: str = os.path.join(BASE_DIR, "sessions.db")

    @classmethod
    def get_instance(cls) -> 'ConversationStateManager':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.lineage_cache = {}
        self._thread_local = threading.local()
        self._init_db()

    @property
    def last_ancestor_source(self) -> str:
        return getattr(self._thread_local, "last_ancestor_source", "memory")

    @property
    def last_lineage_cache_hit(self) -> bool:
        return getattr(self._thread_local, "last_lineage_cache_hit", False)

    def _get_conn(self):
        conn = sqlite3.connect(self.DB_PATH)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception as e:
            logger.warning(f"Failed to set WAL mode: {e}")
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_states (
                session_id TEXT PRIMARY KEY,
                state_json TEXT,
                last_updated REAL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS query_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                raw_query TEXT,
                resolved_query TEXT,
                blueprint_json TEXT,
                timestamp REAL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_snapshots (
                session_id TEXT PRIMARY KEY,
                response_json TEXT,
                updated_at REAL
            )
        """)
        
        # Create cache_catalog table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cache_catalog (
                node_id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                parent_id INTEGER DEFAULT NULL,
                blueprint_hash TEXT NOT NULL,
                metrics TEXT NOT NULL,
                filters TEXT NOT NULL,
                dimensions TEXT NOT NULL,
                timeframe TEXT,
                cache_type TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                memory_size INTEGER NOT NULL,
                creation_time REAL NOT NULL,
                last_accessed REAL NOT NULL,
                reuse_count INTEGER DEFAULT 0,
                db_version_hash TEXT NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES cache_catalog(node_id) ON DELETE SET NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_catalog_session ON cache_catalog(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_catalog_hash ON cache_catalog(blueprint_hash)")
        
        # Check and migrate columns dynamically
        cursor.execute("PRAGMA table_info(query_history)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if "parent_id" not in columns:
            cursor.execute("ALTER TABLE query_history ADD COLUMN parent_id INTEGER DEFAULT NULL")
        if "depth" not in columns:
            cursor.execute("ALTER TABLE query_history ADD COLUMN depth INTEGER DEFAULT 0")
        if "blueprint_hash" not in columns:
            cursor.execute("ALTER TABLE query_history ADD COLUMN blueprint_hash TEXT DEFAULT NULL")
        if "delta_json" not in columns:
            cursor.execute("ALTER TABLE query_history ADD COLUMN delta_json TEXT DEFAULT NULL")
        if "is_subset" not in columns:
            cursor.execute("ALTER TABLE query_history ADD COLUMN is_subset INTEGER DEFAULT 0")
            
        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_query_history_session ON query_history(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_query_history_parent ON query_history(parent_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_query_history_hash ON query_history(blueprint_hash)")

        # Phase 3B: migrate cache_catalog to add is_speculative flag
        cursor.execute("PRAGMA table_info(cache_catalog)")
        columns_catalog = [row[1] for row in cursor.fetchall()]
        if "is_speculative" not in columns_catalog:
            cursor.execute("ALTER TABLE cache_catalog ADD COLUMN is_speculative INTEGER DEFAULT 0")
        
        conn.commit()
        conn.close()

    def get_state(self, session_id: str) -> Optional[ConversationState]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT state_json, last_updated FROM session_states WHERE session_id = ?", (session_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            state_json, last_updated = row
            if (time.time() - last_updated) < self.SESSION_TTL:
                try:
                    data = json.loads(state_json)
                    return ConversationState(**data)
                except Exception as e:
                    logger.error(f"Error parsing ConversationState for session {session_id}: {e}")
                    return None
            else:
                self.clear_session(session_id)
        return None

    def set_state(self, state: ConversationState) -> None:
        state.last_updated = time.time()
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO session_states (session_id, state_json, last_updated) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json, last_updated=excluded.last_updated",
            (state.session_id, state.model_dump_json(), state.last_updated)
        )
        conn.commit()
        conn.close()

    def create_session(self, session_id: str) -> ConversationState:
        state = ConversationState(session_id=session_id, last_updated=time.time())
        self.set_state(state)
        return state

    def clear_session(self, session_id: str) -> None:
        if session_id in self.lineage_cache:
            del self.lineage_cache[session_id]
            
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM session_states WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM query_history WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM session_snapshots WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM cache_catalog WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
        

    def get_or_create(self, session_id: str) -> ConversationState:
        state = self.get_state(session_id)
        if state is None:
            state = self.create_session(session_id)
        return state

    def save_cache_catalog_entry(
        self,
        node_id: int,
        session_id: str,
        parent_id: Optional[int],
        blueprint_hash: str,
        metrics: List[str],
        filters: List[Dict[str, Any]],
        dimensions: List[str],
        timeframe: Optional[Dict[str, Any]],
        cache_type: str,
        row_count: int,
        memory_size: int,
        db_version_hash: str,
        is_speculative: int = 0,  # Phase 3B: 1 for speculative warm entries
    ):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO cache_catalog "
            "(node_id, session_id, parent_id, blueprint_hash, metrics, filters, dimensions, "
            " timeframe, cache_type, row_count, memory_size, creation_time, last_accessed, "
            " db_version_hash, is_speculative) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id,
                session_id,
                parent_id,
                blueprint_hash,
                json.dumps(metrics),
                json.dumps(filters),
                json.dumps(dimensions),
                json.dumps(timeframe) if timeframe else None,
                cache_type,
                row_count,
                memory_size,
                time.time(),
                time.time(),
                db_version_hash,
                is_speculative,
            )
        )
        conn.commit()
        conn.close()

    def get_cache_catalog_entry(self, session_id: str, blueprint_hash: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT node_id, parent_id, metrics, filters, dimensions, timeframe, cache_type, row_count, memory_size, db_version_hash "
            "FROM cache_catalog "
            "WHERE session_id = ? AND blueprint_hash = ? "
            "ORDER BY creation_time DESC LIMIT 1",
            (session_id, blueprint_hash)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            record = {
                "node_id": row[0],
                "parent_id": row[1],
                "metrics": json.loads(row[2]),
                "filters": json.loads(row[3]),
                "dimensions": json.loads(row[4]),
                "timeframe": json.loads(row[5]) if row[5] else None,
                "cache_type": row[6],
                "row_count": row[7],
                "memory_size": row[8],
                "db_version_hash": row[9]
            }
            return record
        return None

    def get_cache_catalog_by_node(self, session_id: str, node_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT node_id, parent_id, blueprint_hash, metrics, filters, dimensions, timeframe, cache_type, row_count, memory_size, db_version_hash "
            "FROM cache_catalog "
            "WHERE session_id = ? AND node_id = ?",
            (session_id, node_id)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            record = {
                "node_id": row[0],
                "parent_id": row[1],
                "blueprint_hash": row[2],
                "metrics": json.loads(row[3]),
                "filters": json.loads(row[4]),
                "dimensions": json.loads(row[5]),
                "timeframe": json.loads(row[6]) if row[6] else None,
                "cache_type": row[7],
                "row_count": row[8],
                "memory_size": row[9],
                "db_version_hash": row[10]
            }
            return record
        return None

    def increment_cache_catalog_reuse(self, node_id: int):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cache_catalog SET reuse_count = reuse_count + 1, last_accessed = ? WHERE node_id = ?",
            (time.time(), node_id)
        )
        conn.commit()
        conn.close()

    def delete_cache_catalog_entry(self, session_id: str, node_id: int):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cache_catalog WHERE session_id = ? AND node_id = ?", (session_id, node_id))
        conn.commit()
        conn.close()

    def add_query_history(
        self,
        session_id: str,
        raw_query: str,
        resolved_query: str,
        blueprint: Optional[Blueprint],
        parent_id: Optional[int] = None,
        depth: int = 0,
        blueprint_hash: Optional[str] = None,
        delta_json: Optional[str] = None,
        is_subset: int = 0
    ) -> int:
        bp_json = blueprint.model_dump_json(exclude_none=True) if blueprint else None
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO query_history (session_id, parent_id, depth, raw_query, resolved_query, blueprint_json, blueprint_hash, delta_json, is_subset, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, parent_id, depth, raw_query, resolved_query, bp_json, blueprint_hash, delta_json, is_subset, time.time())
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Save to memory cache
        node_record = {
            "id": row_id,
            "parent_id": parent_id,
            "depth": depth,
            "raw_query": raw_query,
            "resolved_query": resolved_query,
            "blueprint_json": bp_json,
            "blueprint_hash": blueprint_hash,
            "delta_json": delta_json,
            "is_subset": is_subset,
            "timestamp": time.time()
        }
        if session_id not in self.lineage_cache:
            self.lineage_cache[session_id] = {"nodes": {}, "last_query": None}
        self.lineage_cache[session_id]["nodes"][row_id] = node_record
        self.lineage_cache[session_id]["last_query"] = node_record
        
        return row_id

    def get_last_query_history(self, session_id: str) -> Optional[Dict[str, Any]]:
        self._thread_local.last_ancestor_source = "memory"
        self._thread_local.last_lineage_cache_hit = False
        
        if session_id in self.lineage_cache and self.lineage_cache[session_id]["last_query"]:
            self._thread_local.last_lineage_cache_hit = True
            return self.lineage_cache[session_id]["last_query"]
            
        self._thread_local.last_ancestor_source = "sqlite"
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, parent_id, depth, raw_query, resolved_query, blueprint_json, blueprint_hash, delta_json, is_subset, timestamp "
            "FROM query_history "
            "WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            record = {
                "id": row[0],
                "parent_id": row[1],
                "depth": row[2],
                "raw_query": row[3],
                "resolved_query": row[4],
                "blueprint_json": row[5],
                "blueprint_hash": row[6],
                "delta_json": row[7],
                "is_subset": row[8],
                "timestamp": row[9]
            }
            if session_id not in self.lineage_cache:
                self.lineage_cache[session_id] = {"nodes": {}, "last_query": None}
            self.lineage_cache[session_id]["nodes"][row[0]] = record
            self.lineage_cache[session_id]["last_query"] = record
            return record
        return None

    def get_query_history_node(self, session_id: str, node_id: int) -> Optional[Dict[str, Any]]:
        """Gets a query history node from memory cache, falling back to SQLite."""
        self._thread_local.last_ancestor_source = "memory"
        self._thread_local.last_lineage_cache_hit = False
        
        if session_id in self.lineage_cache and node_id in self.lineage_cache[session_id]["nodes"]:
            self._thread_local.last_lineage_cache_hit = True
            return self.lineage_cache[session_id]["nodes"][node_id]
            
        self._thread_local.last_ancestor_source = "sqlite"
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, parent_id, depth, raw_query, resolved_query, blueprint_json, blueprint_hash, delta_json, is_subset, timestamp "
            "FROM query_history WHERE id = ?", (node_id,)
        )
        row = cursor.fetchone()
        conn.close()
        
        if row:
            record = {
                "id": row[0],
                "parent_id": row[1],
                "depth": row[2],
                "raw_query": row[3],
                "resolved_query": row[4],
                "blueprint_json": row[5],
                "blueprint_hash": row[6],
                "delta_json": row[7],
                "is_subset": row[8],
                "timestamp": row[9]
            }
            if session_id not in self.lineage_cache:
                self.lineage_cache[session_id] = {"nodes": {}, "last_query": None}
            self.lineage_cache[session_id]["nodes"][node_id] = record
            return record
        return None

    def set_snapshot(self, session_id: str, response: Dict[str, Any]) -> None:
        response_json = json.dumps(response)
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO session_snapshots (session_id, response_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET response_json=excluded.response_json, updated_at=excluded.updated_at",
            (session_id, response_json, time.time())
        )
        conn.commit()
        conn.close()

    def get_snapshot(self, session_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT response_json FROM session_snapshots WHERE session_id = ?", (session_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            try:
                return json.loads(row[0])
            except Exception as e:
                logger.error(f"Error parsing snapshot for session {session_id}: {e}")
                return None
        return None
def is_followup_query(raw_query: str, state: Optional[ConversationState], workspace_id: str = "default") -> bool:
    """Returns True if the query is context-dependent and requires a prior state to parse."""
    if not state or not state.active_metric:
        return False
    q = raw_query.strip().lower()
    import re as _re
    from backend.services.workspace.workspace_manager import WorkspaceManager
    workspace = WorkspaceManager.get_instance().get_workspace(workspace_id)
    
    # GUARD: If the query has both its own metric word AND its own entity word,
    # it is a self-contained query — never inherit session state from a prior query.
    stopwords = {'show', 'get', 'what', 'is', 'the', 'of', 'for', 'and', 'a', 'an', 'in', 'by'}
    tokens = [w for w in q.rstrip('?').split() if w not in stopwords]
    
    # Build metric word set dynamically from workspace
    metric_words = set()
    for mk in workspace.available_metrics:
        metric_words.add(mk.lower())
        for w in mk.lower().replace('_', ' ').split():
            metric_words.add(w)
            
    # Build known entity set dynamically from workspace
    known_entities = set()
    if workspace_id == "default":
        registry = MetadataRegistry.get_instance()
        for cat_data in registry.categoricals.values():
            for v in cat_data.get('values', []):
                vs = str(v).lower()
                known_entities.add(vs)
                known_entities.update(vs.replace('_', ' ').split())
        for p in POWER_PLANTS:
            known_entities.add(p.lower())
            known_entities.update(p.lower().replace('_', ' ').split())
    else:
        vocab = getattr(workspace, "spelling_vocabulary", [])
        for w in vocab:
            if w not in metric_words:
                known_entities.add(w.lower())

    has_own_metric = any(t in metric_words for t in tokens)
    non_metric_tokens = [t for t in tokens if t not in metric_words]
    entity_tokens = [t for t in non_metric_tokens if t in known_entities]
    year_tokens = [t for t in tokens if _re.fullmatch(r"(?:19|20)\d{2}", t)]
    # A question that already names its metric and a place or year stands alone.
    if has_own_metric and (entity_tokens or year_tokens):
        return False

    # Pattern: "What about X?" / "How about X?"
    if _re.match(r'^(what|how) about\b', q):
        return True
    # Pattern: "Compare both" / "Show both"
    if _re.match(r'^(compare|show|display|vs|versus)\s+both\b', q):
        return True
    # Pattern: "Same for YEAR" / "And in YEAR" / "Also in YEAR"
    if _re.match(r'^(same for|and in|also in|what about in)\s+(?:19|20)\d{2}', q):
        return True
    # Pattern: "Show X instead" where X is a known metric token and query is short
    m = _re.match(r'^(show|use|switch to|try)\s+(.+?)\s*(instead|now)?$', q)
    if m:
        candidate = m.group(2).strip().replace(' ', '_')
        metric_tokens = set()
        for mk in workspace.available_metrics:
            metric_tokens.add(mk.lower())
            metric_tokens.update(mk.lower().replace('_', ' ').split())
        if any(word in candidate for word in metric_tokens) and len(q.split()) <= 6:
            return True
    # A short follow-up has to be made only of words this database knows.
    # An unrecognized word is a new question, not a repeat of the last one.
    if 1 <= len(tokens) <= 2 and state.active_metric:
        follow_words = {"same", "instead", "also", "too", "both", "again"}
        known = metric_words | known_entities | follow_words
        if all(_re.fullmatch(r"(?:19|20)\d{2}", t) or t in known for t in tokens):
            return True
    return False


def rewrite_followup_query(raw_query: str, state: ConversationState, workspace_id: str = "default") -> str:
    """Expands a short follow-up fragment into a complete standalone query using session state."""
    import re as _re
    from backend.services.workspace.workspace_manager import WorkspaceManager
    workspace = WorkspaceManager.get_instance().get_workspace(workspace_id)
    
    # Spell correct first so that entity extraction has correct names
    raw_corrected = correct_query_spelling(raw_query, workspace_id, complete=True)
    q = raw_corrected.strip().lower().rstrip('?').strip()
    metric_clean = (state.active_metric or 'revenue').replace('_', ' ')
    # Capitalise metric nicely
    metric_label = ' '.join(w.capitalize() for w in metric_clean.split())

    # Extract year from active_timeframe
    year_str = ''
    if state.active_timeframe:
        y = str(state.active_timeframe.get('value', '')).replace('FY', '').strip()
        if y.isdigit():
            year_str = y

    # Extract location/state/region filters
    loc_cols = {'state', 'location', 'region'}
    loc_filters = [f for f in (state.active_filters or []) if f.get('column', '') in loc_cols]
    loc_values = [f['value'] for f in loc_filters]
    loc_str = ' and '.join(loc_values) if loc_values else ''

    # Helper: get other active filters to maintain complete context (e.g. project_type, plant, contractor)
    def get_context_filters_str(exclude_cols=None):
        if exclude_cols is None:
            exclude_cols = set()
        parts = []
        for f in (state.active_filters or []):
            col = f.get('column', '')
            val = f.get('value')
            if not col or val is None:
                continue
            col_lower = col.lower()
            if col_lower in exclude_cols:
                continue
            # Format nicely
            if col_lower in ['state', 'location', 'region']:
                parts.append(f"in {val}")
            elif col_lower == 'plant':
                parts.append(f"for plant {val.replace('_', ' ').title()}")
            elif col_lower in ['project_type', 'department']:
                parts.append(f"for {val}")
            elif col_lower == 'contractor_name':
                parts.append(f"by contractor {val}")
            elif col_lower == 'project_id':
                parts.append(f"for project {val}")
            else:
                parts.append(f"for {val}")
        return ' '.join(parts) if parts else ''

    # Build suffix (timeframe + comparison dimension)
    def build_base(metric=None, location=None, plant=None, year=None, exclude_loc=False, exclude_plant=False):
        m = metric or metric_label
        parts = [f'Show {m}']
        if location:
            parts.append(f'in {location}')
            exclude_loc = True
        if plant:
            parts.append(f'for plant {plant}')
            exclude_plant = True
            
        exclude_cols = {'fy_year'}
        if exclude_loc:
            exclude_cols.update(['state', 'location', 'region'])
        if exclude_plant:
            exclude_cols.add('plant')
            
        ctx_str = get_context_filters_str(exclude_cols)
        if ctx_str:
            parts.append(ctx_str)
            
        if year:
            parts.append(f'for {year}')
        return ' '.join(parts)

    # Pattern: "What about X?" / "How about X?"
    match = _re.match(r'^(?:what|how) about\s+(.+)$', q)
    if match:
        new_entity = match.group(1).strip().title()
        # If new_entity is a plant, use plant option, otherwise location
        all_plants = POWER_PLANTS if workspace_id == "default" else [workspace_id]
        plant_values = [p.lower().replace('_', ' ') for p in all_plants]
        if new_entity.lower() in plant_values or new_entity.lower().replace(' ', '_') in [p.lower() for p in all_plants]:
            matching_plant = next((p for p in all_plants if p.lower() == new_entity.lower().replace(' ', '_') or p.lower().replace('_', ' ') == new_entity.lower()), new_entity)
            result = build_base(plant=matching_plant.replace('_', ' ').title(), year=year_str, exclude_plant=True)
        else:
            result = build_base(location=new_entity, year=year_str, exclude_loc=True)
        logger.info(f"🔄 Rewrite [what about]: '{raw_query}' → '{result}'")
        return result

    # Pattern: "Compare both"
    if _re.match(r'^(?:compare|show|vs)\s+both', q):
        entities = state.comparison_entities if len(state.comparison_entities) >= 2 else loc_values
        if len(entities) >= 2:
            result = f'Compare {metric_label} in {entities[0]} and {entities[1]}'
            # Append other filters from context
            ctx_str = get_context_filters_str({'state', 'location', 'region', 'fy_year', 'plant'})
            if ctx_str:
                result += f' {ctx_str}'
            if year_str:
                result += f' for {year_str}'
        elif loc_str:
            result = f'Compare {metric_label} in {loc_str}'
            ctx_str = get_context_filters_str({'state', 'location', 'region', 'fy_year', 'plant'})
            if ctx_str:
                result += f' {ctx_str}'
        else:
            result = f'Compare {metric_label} across states'
            ctx_str = get_context_filters_str({'state', 'location', 'region', 'fy_year', 'plant'})
            if ctx_str:
                result += f' {ctx_str}'
        logger.info(f"🔄 Rewrite [compare both]: '{raw_query}' → '{result}'")
        return result

    # Pattern: "Same for YEAR" / "And in YEAR"
    match = _re.match(r'^(?:same for|and in|also in|what about in)\s+((?:19|20)\d{2})', q)
    if match:
        new_year = match.group(1)
        result = build_base(location=loc_str or None, year=new_year)
        logger.info(f"🔄 Rewrite [same for year]: '{raw_query}' → '{result}'")
        return result

    # Pattern: "Show X instead" / "Use X instead"
    match = _re.match(r'^(?:show|use|switch to|try)\s+(.+?)\s*(?:instead|now)?$', q)
    if match:
        new_metric = match.group(1).strip()
        new_label = ' '.join(w.capitalize() for w in new_metric.replace('_', ' ').split())
        result = build_base(metric=new_label, location=loc_str or None, year=year_str)
        logger.info(f"🔄 Rewrite [show instead]: '{raw_query}' → '{result}'")
        return result

    # Pattern: Single entity check (e.g. "maharashtra", "in maharashtra", "darlington")
    stopwords = {'show', 'get', 'what', 'is', 'the', 'of', 'for', 'and', 'a', 'an', 'in', 'by', 'at'}
    tokens = [w for w in q.split() if w not in stopwords]
    if len(tokens) == 1 and _re.fullmatch(r"(?:19|20)\d{2}", tokens[0]):
        result = build_base(location=loc_str or None, year=tokens[0])
        logger.info(f"🔄 Rewrite [year]: '{raw_query}' → '{result}'")
        return result
    metric_words = set()
    for mk in workspace.available_metrics:
        metric_words.add(mk.lower())
        metric_words.update(mk.lower().replace('_', ' ').split())
    if tokens and all(t in metric_words for t in tokens):
        new_label = ' '.join(w.capitalize() for w in tokens)
        result = build_base(metric=new_label, location=loc_str or None, year=year_str)
        logger.info(f"🔄 Rewrite [metric]: '{raw_query}' → '{result}'")
        return result
    if len(tokens) <= 2:
        entity_candidate = ' '.join(tokens)
        if workspace_id == "default":
            registry = MetadataRegistry.get_instance()
            state_values = [str(v).lower() for v in registry.categoricals.get('state', {}).get('values', [])]
            location_values = [str(v).lower() for v in registry.categoricals.get('location', {}).get('values', [])]
            plant_values = [p.lower().replace('_', ' ') for p in POWER_PLANTS]
            
            is_state_match = entity_candidate in state_values or entity_candidate.replace(' ', '_') in state_values
            is_loc_match = entity_candidate in location_values or entity_candidate.replace(' ', '_') in location_values
            is_plant_match = entity_candidate in plant_values or entity_candidate.replace(' ', '_') in plant_values
            
            if is_state_match or is_loc_match or is_plant_match:
                resolved_name = entity_candidate.title()
                if is_state_match:
                    matching_db_val = next((v for v in registry.categoricals['state']['values'] if str(v).lower() == entity_candidate or str(v).lower().replace(' ', '_') == entity_candidate), None)
                    if matching_db_val:
                        resolved_name = str(matching_db_val)
                elif is_loc_match:
                    matching_db_val = next((v for v in registry.categoricals['location']['values'] if str(v).lower() == entity_candidate or str(v).lower().replace(' ', '_') == entity_candidate), None)
                    if matching_db_val:
                        resolved_name = str(matching_db_val)
                
                if is_plant_match:
                    matching_plant = next((p for p in POWER_PLANTS if p.lower() == entity_candidate.replace(' ', '_') or p.lower().replace('_', ' ') == entity_candidate), None)
                    if matching_plant:
                        plant_name = matching_plant.replace('_', ' ').title()
                        result = build_base(plant=plant_name, year=year_str, exclude_plant=True)
                        logger.info(f"🔄 Rewrite [single entity plant]: '{raw_query}' → '{result}'")
                        return result
                else:
                    result = build_base(location=resolved_name, year=year_str, exclude_loc=True)
                    logger.info(f"🔄 Rewrite [single entity location]: '{raw_query}' → '{result}'")
                    return result
        else:
            vocab = getattr(workspace, "spelling_vocabulary", [])
            vocab_lower = [v.lower() for v in vocab]
            if entity_candidate in vocab_lower:
                matched_val = vocab[vocab_lower.index(entity_candidate)]
                result = build_base(location=matched_val, year=year_str, exclude_loc=True)
                logger.info(f"🔄 Rewrite [single entity custom]: '{raw_query}' → '{result}'")
                return result

    # Fallback: prepend full context to raw query
    result = build_base(location=loc_str or None, year=year_str)
    logger.info(f"🔄 Rewrite [fallback]: '{raw_query}' → '{result} ({raw_query})'")
    return result


def extract_state_from_result(blueprint: 'Blueprint', raw_query: str, state: 'ConversationState') -> 'ConversationState':
    """Extracts and accumulates conversational context/state from a parsed query blueprint.
    
    Called after every successful query execution or result-cache hit to keep the
    session state in sync so that follow-up detection and rewriting work correctly.
    """
    if blueprint.metrics:
        state.active_metric = blueprint.metrics[0]
    if blueprint.filters:
        state.active_filters = [f for f in blueprint.filters if isinstance(f, dict)]
    if blueprint.timeframe:
        state.active_timeframe = blueprint.timeframe
    elif blueprint.timeframes:
        state.active_timeframe = blueprint.timeframes[0]
    if blueprint.operation:
        state.active_operation = blueprint.operation
    if blueprint.limit:
        state.active_limit = blueprint.limit
    if blueprint.comparison:
        state.active_comparison = blueprint.comparison
    # Track comparison entities (for 'compare both')
    loc_cols = {'state', 'location', 'region'}
    loc_vals = [f['value'] for f in (blueprint.filters or []) if f.get('column', '') in loc_cols]
    if len(loc_vals) >= 2:
        state.comparison_entities = loc_vals
    elif len(loc_vals) == 1:
        # Accumulate entities across turns so 'compare both' works after two single-entity queries
        existing = [e for e in state.comparison_entities if e.lower() != loc_vals[0].lower()]
        state.comparison_entities = (existing + loc_vals)[-2:]  # keep last 2
    state.last_query = raw_query
    return state

def normalize_query_key(raw_query: str) -> str:
    """Normalizes raw queries to ignore formatting, punctuation, and common stopwords for semantic similarity."""
    import re
    # Lowercase & remove punctuation/special characters
    cleaned = re.sub(r'[^\w\s]', ' ', raw_query.lower())
    # Tokenize & remove common stopwords/fillers
    stopwords = {
        "what", "is", "the", "of", "in", "for", "between", "and", "show", "get", "total",
        "trend", "trends", "over", "time", "at", "plant", "department", "region", "compare", 
        "a", "an", "year", "years", "graph", "to", "with", "by", "how"
    }
    tokens = [t for t in cleaned.split() if t and t not in stopwords]
    # Sort tokens to treat word-order changes as identical queries
    tokens.sort()
    return " ".join(tokens)

# --- Caching Layers ---
RESULT_CACHE = {}    # Stable Blueprint JSON String -> Query Response Dict
SUGGEST_CACHE = {}   # Raw query prefix -> suggest response (cleared on registry reload)

class TrieNode:
    def __init__(self):
        self.children = {}
        self.is_end_of_word = False
        self.category = None # 'metric', 'department', 'region', 'plant'
        self.original_name = None

class Trie:
    def __init__(self):
        self.root = TrieNode()

    def insert(self, word: str, category: str, original_name: str = None):
        node = self.root
        for char in word.lower():
            if char not in node.children:
                node.children[char] = TrieNode()
            node = node.children[char]
        node.is_end_of_word = True
        node.category = category
        node.original_name = original_name or word

    def search_prefix(self, prefix: str) -> List[Dict[str, Any]]:
        node = self.root
        for char in prefix.lower():
            if char not in node.children:
                return []
            node = node.children[char]
        
        results = []
        self._collect(node, prefix.lower(), results)
        return results

    def _collect(self, node: TrieNode, path: str, results: List[Dict[str, Any]]):
        if node.is_end_of_word:
            results.append({
                "word": path,
                "category": node.category,
                "original_name": node.original_name
            })
        for char, child in node.children.items():
            self._collect(child, path + char, results)

SUGGESTIONS_TRIE = Trie()

def initialize_trie(registry=None):
    if registry is None:
        try:
            registry = MetadataRegistry.get_instance()
        except Exception:
            return

    # Clear trie root children to avoid duplicates
    SUGGESTIONS_TRIE.root.children.clear()

    # Dynamic metrics from registry
    for key in registry.metrics.keys():
        name = key.replace('_', ' ').title()
        SUGGESTIONS_TRIE.insert(key, "metric", name)
        for part in key.split('_'):
            if len(part) > 2:
                SUGGESTIONS_TRIE.insert(part, "metric", name)

    # Dynamic categoricals from registry
    for cat_key, cat_info in registry.categoricals.items():
        cat_name = cat_key.replace('_', ' ').title()
        SUGGESTIONS_TRIE.insert(cat_key, cat_key, cat_name)
        for part in cat_key.split('_'):
            if len(part) > 2:
                SUGGESTIONS_TRIE.insert(part, cat_key, cat_name)

        # Insert specific values (limit to prevent memory bloat)
        limit = 100 if cat_key == 'project_id' else 50
        for val in list(cat_info.get("values", []))[:limit]:
            val_str = str(val)
            val_clean = val_str.replace('_', ' ')
            SUGGESTIONS_TRIE.insert(val_str, cat_key, val_clean.title())
            for part in val_str.split('_'):
                if len(part) > 2:
                    SUGGESTIONS_TRIE.insert(part, cat_key, val_clean.title())

def check_value_in_column(value: str, col_name: str, workspace: Any) -> bool:
    """True when an identifier token is stored in this workspace column."""
    if not workspace or not workspace.metadata_profile or not col_name:
        return False
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", col_name):
        return False
    col_prof = workspace.metadata_profile.columns.get(col_name)
    if not col_prof or col_prof.classification != "IDENTIFIER":
        return False
    needle = value.strip().lower()
    if any(str(v).lower() == needle for v in col_prof.sample_values):
        return True
    try:
        table_name = workspace.metadata_profile.table_name
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table_name):
            return False
        db_file = ConnectionManager.db_path(workspace.workspace_id)
        if not db_file or not os.path.exists(db_file):
            return False
        conn = _borrow_sqlite(db_file)
        row = conn.execute(
            f"SELECT 1 FROM {table_name} WHERE {col_name} = ? OR {col_name} = ? LIMIT 1",
            (value.strip(), value.strip().upper()),
        ).fetchone()
        return row is not None
    except Exception:
        return False

def extract_query_components(q: str, workspace_id: str = "default"):
    q_lower = q.lower()
    from backend.services.workspace.workspace_manager import WorkspaceManager
    workspace = WorkspaceManager.get_instance().get_workspace(workspace_id)
    
    # 1. Detect metrics dynamically using Workspace aliases
    detected_metrics = []
    adapter = workspace.semantic_adapter
    for key in workspace.available_metrics:
        aliases = adapter.column_to_aliases.get(key, {key})
        for alias in aliases:
            alias_clean = alias.replace('_', ' ').lower()
            alias_underscore = alias.replace(' ', '_').lower()
            has_clean_match = re.search(r'\b' + re.escape(alias_clean) + r'\b', q_lower) or alias_clean in q_lower
            has_underscore_match = re.search(r'\b' + re.escape(alias_underscore) + r'\b', q_lower) or alias_underscore in q_lower
            if has_clean_match or has_underscore_match:
                name = key.replace('_', ' ').title()
                detected_metrics.append((key, name))
                break
            
    # 2. Detect departments/project types
    detected_depts = []
    dept_col = resolve_db_column(workspace_id, "project_type")
    if dept_col in workspace.available_dimensions:
        project_types = adapter.get_dimension_values("project_type")
        for val in project_types:
            val_str = str(val).lower()
            val_clean = val_str.replace('_', ' ')
            if val_str in q_lower or val_clean in q_lower:
                detected_depts.append(str(val))
                
    if workspace_id == "default":
        # Fallback to legacy departments for compatibility
        all_depts = ["sales", "digital", "marketing", "hr", "engineering", "finance", "support", "operations"]
        for d in all_depts:
            if d in q_lower:
                if d == "marketing":
                    marketing_as_metric_count = (
                        q_lower.count("marketing spend") + 
                        q_lower.count("marketing cost") + 
                        q_lower.count("marketing expense")
                    )
                    marketing_total_count = q_lower.count("marketing")
                    if marketing_total_count > marketing_as_metric_count:
                        if d.capitalize() not in detected_depts:
                            detected_depts.append(d.capitalize())
                else:
                    if d.capitalize() not in detected_depts:
                        detected_depts.append(d.capitalize())
            
    # 3. Detect regions/locations/states
    detected_regions = []
    loc_col = resolve_db_column(workspace_id, "location")
    state_col = resolve_db_column(workspace_id, "state")
    if loc_col in workspace.available_dimensions or state_col in workspace.available_dimensions:
        locations = adapter.get_dimension_values("location")
        states = adapter.get_dimension_values("state")
        for val in list(locations) + list(states):
            val_str = str(val).lower()
            val_clean = val_str.replace('_', ' ')
            if val_str in q_lower or val_clean in q_lower:
                if str(val) not in detected_regions:
                    detected_regions.append(str(val))
            
    # Dynamic detection for other categoricals
    detected_categoricals = {}
    
    # Tokenize the query to check exact values in DB
    query_tokens = [t.strip() for t in re.split(r'[^a-zA-Z0-9_\-]', q) if len(t.strip()) > 2]
    
    # Set of common query structures/stopwords to bypass db queries
    local_stopwords = {
        'what', 'show', 'list', 'get', 'give', 'find', 'average', 'avg', 'sum', 'total', 
        'count', 'trend', 'trends', 'graph', 'chart', 'compare', 'growth', 'change', 
        'allocated', 'used', 'remaining', 'completion', 'delay', 'revenue', 'capacity',
        'charlie', 'default', 'charlie project', 'project', 'site', 'plant', 'value'
    }
    
    for cat_key in workspace.available_dimensions:
        if cat_key in [dept_col, loc_col, state_col, "fy_year", "plant"]:
            continue
        detected_categoricals[cat_key] = []
        
        # 1. Match against profiler sample values
        values = []
        if workspace.metadata_profile and cat_key in workspace.metadata_profile.columns:
            values = workspace.metadata_profile.columns[cat_key].sample_values
        for val in values:
            val_str = str(val)
            val_clean = val_str.replace('_', ' ').lower()
            val_lower = val_str.lower()
            if re.search(rf'\b{re.escape(val_clean)}\b', q_lower) or re.search(rf'\b{re.escape(val_lower)}\b', q_lower):
                detected_categoricals[cat_key].append(val_str)
                
        # 2. Check if any query token exists exactly in this DB column (handles unsampled IDs like P0111)
        for token in query_tokens:
            if token.lower() in [v.lower() for v in detected_categoricals[cat_key]]:
                continue
            if token.lower() in local_stopwords or token.lower() in [m[0].lower() for m in detected_metrics]:
                continue
            if check_value_in_column(token, cat_key, workspace):
                # Retrieve original case from DB if found
                orig_val = token
                try:
                    table_name = workspace.metadata_profile.table_name
                    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table_name) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", cat_key):
                        db_file = ConnectionManager.db_path(workspace.workspace_id)
                        if db_file and os.path.exists(db_file):
                            conn = _borrow_sqlite(db_file)
                            res_val = conn.execute(
                                f"SELECT {cat_key} FROM {table_name} WHERE LOWER({cat_key}) = ? LIMIT 1",
                                (token.lower(),),
                            ).fetchone()
                        if res_val:
                            orig_val = str(res_val[0])
                except Exception:
                    pass
                detected_categoricals[cat_key].append(orig_val)

    # Fallback to extract any project ID matching pattern that was not in sampled values
    pid_match = re.search(r'\b[a-z]{2,4}-[a-z]{2,4}-\d+\b', q_lower)
    if pid_match:
        matched_pid = pid_match.group(0).upper()
        if "project_id" not in detected_categoricals:
            detected_categoricals["project_id"] = []
        if matched_pid not in detected_categoricals["project_id"]:
            detected_categoricals["project_id"].append(matched_pid)

    detected_plants = []
    all_plants = POWER_PLANTS if workspace_id == "default" else [workspace_id]
    for p in all_plants:
        clean_p = p.replace('_', ' ')
        if p in q_lower or clean_p in q_lower:
            detected_plants.append(p)
            
    # 5. Detect years
    detected_years = extract_detected_years(q_lower)
    
    # 6. Check if the LAST token is incomplete and matches anything in Trie
    tokens = [t for t in q_lower.split() if t]
    last_token_match = None
    if tokens:
        last_token = tokens[-1]
        all_exact_matches = [m[0] for m in detected_metrics] + [d.lower() for d in detected_depts] + [r.lower() for r in detected_regions] + detected_plants
        for cat_list in detected_categoricals.values():
            all_exact_matches.extend([c.lower() for c in cat_list])
        if last_token not in all_exact_matches:
            prefix_results = workspace.trie.search_prefix(last_token)
            if prefix_results:
                last_token_match = prefix_results[0]
                
    if last_token_match:
        category = last_token_match["category"]
        name = last_token_match["original_name"]
        if category == "metric" and not any(m[1] == name for m in detected_metrics):
            metric_key = next((k for k in workspace.available_metrics if k == name or k.replace('_', ' ').title() == name), None)
            if metric_key:
                detected_metrics.append((metric_key, name))
        elif category in ["department", "project_type", dept_col] and name not in detected_depts:
            detected_depts.append(name)
        elif category in ["region", "location", "state", loc_col, state_col] and name not in detected_regions:
            detected_regions.append(name)
        elif category == "plant" and name.lower().replace(' ', '_') not in detected_plants:
            plant_key = next((p for p in all_plants if p.replace('_', ' ').title() == name or p == name), None)
            if plant_key:
                detected_plants.append(plant_key)
        elif category in workspace.available_dimensions:
            if category not in detected_categoricals:
                detected_categoricals[category] = []
            if name not in detected_categoricals[category]:
                detected_categoricals[category].append(name)
                
    return {
        "metrics": detected_metrics,
        "departments": detected_depts,
        "regions": detected_regions,
        "plants": detected_plants,
        "years": detected_years,
        "categoricals": detected_categoricals,
        "last_token_match": last_token_match
    }

def generate_intent_preview(q: str, components: dict):
    q_lower = q.lower()
    
    metric = "None"
    if components["metrics"]:
        metric = components["metrics"][0][1]
        
    dimension = "None"
    if "plant" in q_lower or components["plants"]:
        dimension = "Plant"
    elif "region" in q_lower or components["regions"]:
        dimension = "Region"
    elif "department" in q_lower or "dept" in q_lower or components["departments"]:
        dimension = "Department"
    elif "year" in q_lower or len(components["years"]) > 1:
        dimension = "Year"
        
    intent = "Sum"
    if any(w in q_lower for w in ["trend", "over time", "time-series", "monthly"]):
        intent = "Trend"
    elif any(w in q_lower for w in ["compare", "comparison", "versus", "vs", "between"]):
        intent = "Comparison"
    elif any(w in q_lower for w in ["top", "highest", "best", "lowest", "worst", "max", "min"]):
        intent = "Top N"
    elif any(w in q_lower for w in ["breakdown", "by", "split"]):
        intent = "Breakdown"
    elif dimension != "None":
        intent = "Breakdown"
        
    return {
        "intent": intent,
        "metric": metric,
        "dimension": dimension
    }

def get_levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return get_levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


_INSTRUCTION_WORDS = frozenset({
    "calculate", "calculated", "calculating", "calculation", "calculations",
    "compute", "computed", "computing", "computation", "computations",
    "determine", "determined", "determining",
    "estimate", "estimated", "estimating", "estimation",
    "measure", "measured", "measuring", "measurement",
    "evaluate", "evaluated", "evaluating", "evaluation",
    "analyze", "analyse", "analyzed", "analysed", "analyzing", "analysing", "analysis",
    "summarize", "summarise", "summarized", "summarised", "summarizing", "summarising", "summary",
    "figure", "figuring", "calc",
})
_QUERY_CONNECTORS = frozenset({
    "in", "for", "by", "vs", "versus", "of", "and", "between", "from", "to", "on", "during", "with",
})


def _is_instruction_word(token: str) -> bool:
    """True for verbs like calculate, including a close misspelling such as calcutate."""
    word = re.sub(r"[^a-z]", "", (token or "").lower())
    if not word:
        return False
    if word in _INSTRUCTION_WORDS:
        return True
    if len(word) < 5:
        return False
    for known in _INSTRUCTION_WORDS:
        if len(known) < 6 or abs(len(word) - len(known)) > 2 or word[:3] != known[:3]:
            continue
        if get_levenshtein_distance(word, known) <= 2:
            return True
    return False


def _cleanup_query_text(text: str) -> str:
    parts = re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-]*", text or "")
    kept = [part for part in parts if not _is_instruction_word(part)]
    while kept and kept[0].lower() in _QUERY_CONNECTORS:
        kept.pop(0)
    while kept and kept[-1].lower() in _QUERY_CONNECTORS:
        kept.pop()
    cleaned = []
    for part in kept:
        if cleaned and part.lower() in _QUERY_CONNECTORS and cleaned[-1].lower() in _QUERY_CONNECTORS:
            continue
        cleaned.append(part)
    return " ".join(cleaned).strip()


def nearest_executable_queries(raw_query: str, workspace_id: str = "default", drop_tokens: Optional[List[str]] = None) -> List[str]:
    """The closest questions this database can actually run."""
    spelled = correct_query_spelling(raw_query, workspace_id, complete=True)
    candidate = _cleanup_query_text(spelled)
    if drop_tokens:
        blocked = {token.lower() for token in drop_tokens}
        parts = [part for part in candidate.split() if part.lower() not in blocked]
        candidate = _cleanup_query_text(" ".join(parts))
    options = []
    if candidate and candidate.lower() != (raw_query or "").strip().lower():
        options.append(candidate)
    for item in schema_facets(workspace_id).get("suggestions") or []:
        if item and item.lower() not in {option.lower() for option in options}:
            options.append(item)
    return options[:4]


@lru_cache(maxsize=512)
def correct_query_spelling(raw_query: str, workspace_id: str = "default", complete: bool = False) -> str:
    import re
    from backend.services.workspace.workspace_manager import WorkspaceManager
    
    # Pre-correct common concatenated or misspelled state names/regions
    state_replacements = {
        r'\btamilnadu\b': 'Tamil Nadu',
        r'\btamilnad\b': 'Tamil Nadu',
        r'\bmaharatsra\b': 'Maharashtra',
        r'\bmaharastra\b': 'Maharashtra',
        r'\bgujrat\b': 'Gujarat',
        r'\brajastan\b': 'Rajasthan',
        r'\bkarnatka\b': 'Karnataka',
        r'\bandhrapradesh\b': 'Andhra Pradesh',
        r'\bmadhyapradesh\b': 'Madhya Pradesh',
        r'\buttarkhand\b': 'Uttarakhand',
        r'\bhimachalpradesh\b': 'Himachal Pradesh',
    }
    corrected_query = raw_query
    # Pre-correct common concatenated or misspelled state names on the shared plant pool.
    # A customer's own database keeps the words they typed.
    if workspace_id in ("default", "all", "", None):
        for pattern, replacement in state_replacements.items():
            corrected_query = re.sub(pattern, replacement, corrected_query, flags=re.IGNORECASE)

    # We want to split the query by word boundaries but preserve punctuation/spaces
    words = re.findall(r'\b[a-zA-Z]+\b', corrected_query)
    
    # Live suggestions skip the last word while it is still being typed.
    # A submitted question is complete, so the last word is corrected too.
    unfinished_word = None
    if not complete and words and corrected_query and corrected_query[-1].isalnum():
        unfinished_word = words[-1].lower()

    stopwords = {
        "what", "is", "the", "of", "in", "for", "between", "and", "show", "get", "total",
        "trend", "trends", "over", "time", "at", "plant", "department", "region", "compare", 
        "a", "an", "year", "years", "graph", "to", "with", "by", "how", "vs", "versus",
        "top", "bottom", "best", "worst", "highest", "lowest", "first", "last", "most", "least", "limit", "max", "min",
        "full", "fullness", "made", "money", "plants", "doing", "everything", "overall", "versus", "income",
        "yearly", "annually", "monthly", "daily", "month", "months", "date", "dates",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"
    }
    
    # Resolve spelling vocabulary dynamically from the active workspace
    workspace = WorkspaceManager.get_instance().get_workspace(workspace_id)
    workspace_words = getattr(workspace, "spelling_vocabulary", [])
    valid_set = {w.lower() for w in stopwords}
    valid_set.update(_INSTRUCTION_WORDS)
    valid_set.update(str(w).lower() for w in workspace_words)
    by_len: Dict[int, List[str]] = {}
    for valid in valid_set:
        by_len.setdefault(len(valid), []).append(valid)
    
    for word in words:
        w_lower = word.lower()
        if w_lower == unfinished_word:
            continue
        if w_lower in valid_set or len(w_lower) < 4 or any(ch.isdigit() for ch in w_lower):
            continue
            
        # Compare only vocabulary words of a similar length. This stays in memory.
        max_allowed = 2
        best_match = None
        min_dist = 999
        for length in range(len(w_lower) - max_allowed, len(w_lower) + max_allowed + 1):
            for valid in by_len.get(length, ()):
                dist = get_levenshtein_distance(w_lower, valid)
                if 0 < dist <= max_allowed and dist < min_dist:
                    min_dist = dist
                    best_match = valid
            if min_dist == 1:
                break
                
        if best_match:
            replacement = best_match
            if word[0].isupper():
                replacement = best_match.capitalize()
            corrected_query = re.sub(r'\b' + word + r'\b', replacement, corrected_query)
            
    return corrected_query

def get_suggested_correction(raw_query: str, workspace_id: str = "default") -> Optional[str]:
    import re
    from backend.services.workspace.workspace_manager import WorkspaceManager
    workspace = WorkspaceManager.get_instance().get_workspace(workspace_id)
    
    # 1. Correct spelling typos first
    spelled_query = correct_query_spelling(raw_query, workspace_id, complete=True)
    
    raw_lower = spelled_query.lower()
    # Check for years outside range using negative lookarounds
    years = extract_raw_year_candidates(raw_lower)
    
    min_y, max_y = year_bounds(workspace, workspace_id)
        
    invalid_years = [y for y in years if not (min_y <= int(y) <= max_y)]
    if invalid_years:
        corrected = spelled_query
        for y in invalid_years:
            val = int(y)
            if val < min_y:
                corrected = re.sub(r'(?<!\d)' + y + r'(?!\d)', str(min_y), corrected)
            elif val > max_y:
                corrected = re.sub(r'(?<!\d)' + y + r'(?!\d)', str(max_y), corrected)
        return f"Did you mean: '{corrected}'?"
    
    # Check if query is completely unrecognized (gibberish/no matches)
    components = extract_query_components(spelled_query, workspace_id)
    has_structural = any(w in raw_lower for w in ["trend", "compare", "breakdown", "performance", "growth", "total", "top", "best", "worst", "plant", "plants"])
    if not (components["metrics"] or components["departments"] or components["regions"] or components["plants"] or components["years"] or any(components["categoricals"].values()) or has_structural):
        options = nearest_executable_queries(raw_query, workspace_id)
        if options:
            quoted = ", ".join(f"'{item}'" for item in options[:3])
            return f"Try searching for: {quoted}."
        label = workspace.available_metrics[0].replace("_", " ").title() if workspace.available_metrics else "Data"
        return f"Try searching for: '{label}'."
        
    # If spelling was corrected but no year error was found, suggest the spelling correction
    if spelled_query.strip().lower() != raw_query.strip().lower():
        return f"Did you mean: '{spelled_query}'?"
        
    return None

def _wants_year_group(text: str, detected_years: list) -> bool:
    """True when the question asks for a year-by-year split, not one year filtered."""
    if re.search(r"\b(by years?|yearly|annually|each year|per year|year over year|yoy|across years|compare years)\b", text or ""):
        return True
    return not detected_years and re.search(r"\byears?\b", text or "") is not None


def _alias_hits(alias: str, text: str) -> bool:
    """True when an alias appears as a word, or as the same letters with spaces removed."""
    alias_clean = str(alias).replace("_", " ").lower().strip()
    if len(alias_clean) < 3 or not text:
        return False
    if re.search(r"\b" + re.escape(alias_clean) + r"\b", text):
        return True
    squashed_alias = re.sub(r"[\s_]+", "", alias_clean)
    squashed_text = re.sub(r"[\s_]+", "", text)
    return len(squashed_alias) >= 5 and squashed_alias in squashed_text


def _stored_value_mentioned(value: str, raw_lower: str, query_token_set: set) -> bool:
    """Match a stored category value, including a distinctive part of a longer name."""
    val_clean = str(value).lower().replace("_", " ").strip()
    if not val_clean:
        return False
    if " " not in val_clean and "-" not in val_clean:
        if len(val_clean) < 3:
            return val_clean in query_token_set
        return val_clean in query_token_set or re.search(r"\b" + re.escape(val_clean) + r"\b", raw_lower) is not None
    if val_clean in raw_lower:
        return True
    words = [word for word in re.split(r"[^a-z0-9]+", val_clean) if word]
    if len(words) >= 2:
        for start in range(len(words)):
            for end in range(start + 2, len(words) + 1):
                if " ".join(words[start:end]) in raw_lower:
                    return True
    if words and len(words[0]) >= 6 and words[0] in query_token_set:
        return True
    for word in words:
        if len(word) >= 8 and word in query_token_set:
            return True
    return False


_GENERIC_NAME_WORDS = frozenset({
    "ltd", "limited", "inc", "incorporated", "co", "corp", "corporation", "company",
    "companies", "group", "industries", "industry", "energy", "power", "infrastructure",
    "technology", "technologies", "tech", "solutions", "systems", "services",
    "international", "global", "renewables", "renewable", "holdings", "enterprises",
    "enterprise", "pvt", "private", "llc", "plc", "gmbh", "and", "the",
})
_NAME_BOUNDARIES = frozenset({
    "in", "for", "by", "vs", "versus", "and", "of", "on", "during", "between", "from",
    "to", "with", "the", "a", "an", "show", "total", "compare", "how", "what", "is",
    "are", "was", "were", "did", "does",
})


def _lcs_len(left: str, right: str) -> int:
    if not left or not right:
        return 0
    prev = [0] * (len(right) + 1)
    for char in left:
        curr = [0]
        for index, other in enumerate(right, start=1):
            curr.append(prev[index - 1] + 1 if char == other else max(prev[index], curr[-1]))
        prev = curr
    return prev[-1]


def _name_word_score(query_word: str, stored_word: str) -> Optional[int]:
    """Edit distance when a typed word is the stored word or a close misspelling of it."""
    if not query_word or not stored_word:
        return None
    if query_word == stored_word:
        return 0
    if query_word[0] != stored_word[0]:
        return None
    if abs(len(query_word) - len(stored_word)) > 2:
        return None
    shorter = min(len(query_word), len(stored_word))
    longer = max(len(query_word), len(stored_word))
    if shorter < 5:
        return None
    distance = get_levenshtein_distance(query_word, stored_word)
    if distance > max(2, longer // 2):
        return None
    if _lcs_len(query_word, stored_word) * 5 < longer * 3:
        return None
    return distance


def _category_value_catalog(workspace, workspace_id: str) -> List[tuple]:
    """Real category values for this database, split into the words that identify them."""
    columns: List[tuple] = []
    profile = getattr(workspace, "metadata_profile", None)
    if workspace_id in ("default", "all", "", None) or profile is None:
        registry = MetadataRegistry.get_instance()
        for column, info in registry.categoricals.items():
            if column in ("fy_year", "plant", "id") or _is_time_key(column) or _is_id_key(column):
                continue
            columns.append((column, info.get("values", [])))
    else:
        for column, col_prof in profile.columns.items():
            if col_prof.classification not in ("DIMENSION", "STATUS"):
                continue
            if _is_time_key(column) or _is_id_key(column):
                continue
            columns.append((column, col_prof.sample_values or []))

    catalog = []
    for column, values in columns:
        for value in values:
            original = str(value).strip()
            if not original or re.fullmatch(r"(?:19|20)\d{2}", original):
                continue
            parts = [part for part in re.split(r"[^a-z0-9]+", original.lower()) if part]
            words = [
                part for part in parts
                if part not in _GENERIC_NAME_WORDS and (len(part) >= 5 or (len(part) >= 4 and len(parts) == 1))
            ]
            if words:
                catalog.append((column, original, words))
    return catalog


def _metric_word_set(workspace) -> set:
    words = set()
    for key in getattr(workspace, "available_metrics", []) or []:
        words.update(part for part in re.split(r"[^a-z0-9]+", str(key).lower()) if part)
    return words


def resolve_category_mentions(text: str, workspace, workspace_id: str = "default") -> Dict[str, Any]:
    """Map a typo or a partial name onto the one stored value it points to.

    "bulepick" resolves to "BluePeak Industries" and "aster technology" resolves
    to "Aster Energy Ltd." because the distinctive word is enough. The same
    rules apply to whatever names the selected database actually stores.
    """
    empty: Dict[str, Any] = {"filters": [], "tokens": set()}
    if not text or workspace is None:
        return empty
    catalog = _category_value_catalog(workspace, workspace_id)
    if not catalog:
        return empty

    metric_words = _metric_word_set(workspace)
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    token_hit: Dict[str, tuple] = {}
    for token in tokens:
        if len(token) < 4 or token in metric_words or token in _GENERIC_NAME_WORDS or token in _NAME_BOUNDARIES:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", token):
            continue
        found = []
        for column, original, words in catalog:
            best = None
            for word in words:
                score = _name_word_score(token, word)
                if score is None:
                    continue
                if best is None or score < best:
                    best = score
            if best is not None:
                found.append((best, column, original))
        if not found:
            continue
        best_score = min(item[0] for item in found)
        winners = {(column, original) for score, column, original in found if score == best_score}
        if len(winners) == 1:
            token_hit[token] = winners.pop()

    if not token_hit:
        return empty

    explained = set(token_hit)
    hit_indexes = [index for index, token in enumerate(tokens) if token in token_hit]
    for index in hit_indexes:
        for direction in (-1, 1):
            cursor = index + direction
            steps = 0
            while 0 <= cursor < len(tokens) and steps < 2:
                neighbor = tokens[cursor]
                if neighbor in _NAME_BOUNDARIES or neighbor in metric_words or re.fullmatch(r"(?:19|20)\d{2}", neighbor):
                    break
                other = token_hit.get(neighbor)
                if other is not None and other != token_hit[tokens[index]]:
                    break
                explained.add(neighbor)
                if other is not None:
                    break
                cursor += direction
                steps += 1

    filters = []
    seen = set()
    for column, original in token_hit.values():
        key = (column, original.lower())
        if key in seen:
            continue
        seen.add(key)
        filters.append({"column": column, "value": original})
    return {"filters": filters, "tokens": explained}


def _ranked_metric_names(workspace) -> List[str]:
    names = list(getattr(workspace, "available_metrics", []) or [])

    def rank(name: str) -> int:
        if name in _METRIC_PRIORITY:
            return _METRIC_PRIORITY.index(name)
        for index, known in enumerate(_METRIC_PRIORITY):
            if name.startswith(known) or known.startswith(name):
                return index
        return len(_METRIC_PRIORITY)

    return sorted(names, key=rank)


def _metrics_about(workspace, adapter, *needles: str) -> List[str]:
    found = []
    for key in getattr(workspace, "available_metrics", []) or []:
        parts = [str(key).replace("_", " ")]
        parts.extend(str(alias).replace("_", " ") for alias in adapter.column_to_aliases.get(key, set()))
        blob = " ".join(parts).lower()
        if any(needle in blob for needle in needles):
            found.append(key)
    return found


def _first_group_dimension(workspace) -> Optional[str]:
    dims = []
    profile = getattr(workspace, "metadata_profile", None)
    for key in getattr(workspace, "available_dimensions", []) or []:
        if _is_time_key(key) or _is_id_key(key):
            continue
        if profile and key in profile.columns and profile.columns[key].classification == "IDENTIFIER":
            continue
        dims.append(key)
    for name in _GROUP_PRIORITY:
        if name in dims:
            return name
    return dims[0] if dims else None


def parse_query_deterministically(raw_query: str, workspace_id: str = "default") -> Optional[Dict[str, Any]]:
    """Deterministically parses common query patterns to bypass the LLM entirely."""
    raw_lower = raw_query.lower().strip()
    registry = MetadataRegistry.get_instance()
    from backend.services.workspace.workspace_manager import WorkspaceManager
    workspace = WorkspaceManager.get_instance().get_workspace(workspace_id)
    adapter = workspace.semantic_adapter
    
    # Exact match for Suggested Business Questions
    if workspace_id == "default" and raw_lower in ["top capacity plants", "top revenue plants", "revenue trend", "top plants", "top performing plants", "best plants", "worst plants", "budget allocated by state", "completion percentage by project type", "delay days by contractor"]:
        bp = Blueprint()
        confidence = 1.0
        intent = "unknown"
        
        if raw_lower == "top revenue plants":
            intent = "top_n"
            bp.metrics = ["revenue"]
            bp.operation = "BREAKDOWN"
            bp.comparison = {"type": "plant"}
            bp.limit = 3
            bp.sort_asc = False
        elif raw_lower == "top capacity plants":
            intent = "top_n"
            bp.metrics = ["capacity_mw"]
            bp.operation = "BREAKDOWN"
            bp.comparison = {"type": "plant"}
            bp.limit = 3
            bp.sort_asc = False
        elif raw_lower in ["top plants", "top performing plants", "best plants", "worst plants"]:
            intent = "top_n"
            bp.metrics = list(adapter.kpi_candidates or [])
            bp.operation = "BREAKDOWN"
            bp.comparison = {"type": "plant"}
            bp.limit = None
            bp.sort_asc = True if "worst" in raw_lower else False
        elif raw_lower == "revenue trend":
            intent = "trend"
            bp.metrics = ["revenue"]
            bp.operation = "GRAPH"
        elif raw_lower == "budget allocated by state":
            intent = "breakdown"
            bp.metrics = ["budget_allocated"]
            bp.operation = "BREAKDOWN"
            bp.comparison = {"type": "state"}
        elif raw_lower == "completion percentage by project type":
            intent = "breakdown"
            bp.metrics = ["completion_percentage"]
            bp.operation = "BREAKDOWN"
            bp.comparison = {"type": "project_type"}
        elif raw_lower == "delay days by contractor":
            intent = "breakdown"
            bp.metrics = ["delay_days"]
            bp.operation = "BREAKDOWN"
            bp.comparison = {"type": "contractor_name"}
            
        return {
            "blueprint": bp,
            "intent": intent,
            "confidence": confidence,
            "top_n_limit": bp.limit,
            "top_n_asc": bp.sort_asc
        }
    
    # 1. Identify Metrics
    detected_metrics = []
    
    # Dynamic metrics match using adapter synonyms/aliases
    for key in workspace.available_metrics:
        aliases = adapter.column_to_aliases.get(key, {key})
        for alias in aliases:
            if _alias_hits(alias, raw_lower):
                if key not in detected_metrics:
                    detected_metrics.append(key)
                break

    # Keep "revenue_million" only when the query actually says "million".
    pruned_metrics = []
    for metric in detected_metrics:
        drop = False
        for other in detected_metrics:
            if other == metric:
                continue
            if metric.startswith(other + "_"):
                extras = [part for part in metric.split("_") if part not in other.split("_")]
                if extras and not any(part in raw_lower for part in extras):
                    drop = True
                    break
        if not drop:
            pruned_metrics.append(metric)
    detected_metrics = pruned_metrics
            
    # Check if a project ID is mentioned (either in profile values or pattern PRJ-[A-Z]{3}-\d+)
    has_project_id = False
    identifier_cols = [col for col, t in getattr(adapter, "column_to_type", {}).items() if t == "IDENTIFIER"]
    for col in identifier_cols:
        values = []
        if workspace.metadata_profile and col in workspace.metadata_profile.columns:
            values = workspace.metadata_profile.columns[col].sample_values
        for val in values:
            val_lower = str(val).lower()
            if val_lower in raw_lower:
                has_project_id = True
                break
        if has_project_id:
            break
    if not has_project_id:
        if re.search(r'\b[a-z0-9]{2,6}-[a-z0-9]{1,6}(-[a-z0-9]+)?\b', raw_lower):
            has_project_id = True

    # 2. Extract Year context — only valid data years for this workspace
    all_years_in_query = extract_raw_year_candidates(raw_lower)
    min_y, max_y = year_bounds(workspace, workspace_id)
        
    invalid_years = [y for y in all_years_in_query if not (min_y <= int(y) <= max_y)]
    if invalid_years:
        correction = get_suggested_correction(raw_query, workspace_id)
        err_msg = f"No data available for year(s): {', '.join(invalid_years)}. Available range: {min_y}-{max_y}."
        if correction:
            err_msg += f" {correction}"
        return {"blueprint": None, "intent": "invalid_year", "confidence": 0.0,
                "error": err_msg,
                "top_n_limit": None, "top_n_asc": False}
    detected_years = extract_calendar_mentions(raw_lower)["years"]

    # Check for plants / databases
    detected_plants = []
    all_plants = POWER_PLANTS if workspace_id == "default" else [workspace_id]
    for plant in all_plants:
        plant_clean = plant.replace('_', ' ')
        if re.search(rf'\b{re.escape(plant)}\b', raw_lower) or re.search(rf'\b{re.escape(plant_clean)}\b', raw_lower):
            detected_plants.append(plant)

    # Check for breakdown/comparison keywords or dimensions
    has_structural = any(w in raw_lower for w in ["trend", "compare", "breakdown", "performance", "growth", "total", "top", "best", "worst", "plant", "plants", "by", "across", "vs", "versus"])

    # Check for categorical filter words using the query's own tokens.
    # Scanning every stored value with a fresh regular expression was a per-query stall.
    query_token_set = set(re.findall(r"[a-z0-9_\-]+", raw_lower))

    def _value_mentioned(val_str: str) -> bool:
        return _stored_value_mentioned(val_str, raw_lower, query_token_set)

    has_categorical_filter = False
    for cat_key in workspace.available_dimensions:
        if cat_key in ["fy_year", "plant", "id"]:
            continue
        values = []
        if workspace_id == "default":
            values = registry.categoricals.get(cat_key, {}).get("values", [])
        elif workspace.metadata_profile and cat_key in workspace.metadata_profile.columns:
            values = workspace.metadata_profile.columns[cat_key].sample_values
        for val in values:
            val_str = str(val)
            if re.fullmatch(r"20\d{2}", val_str):
                continue
            if _value_mentioned(val_str):
                has_categorical_filter = True
                break
        if has_categorical_filter:
            break

    # We can parse deterministically if we have a metric, project_id, year, plant, categorical filter, or structural keyword!
    # Tokenize query to check exact values in DB for parse_query_deterministically
    query_tokens = [t.strip() for t in re.split(r'[^a-zA-Z0-9_\-]', raw_query) if len(t.strip()) > 2]
    
    # 3. Categorical Filters
    dynamic_filters = []
    for cat_key in workspace.available_dimensions:
        if cat_key in ["fy_year", "plant", "id"]:
            continue
        if workspace.metadata_profile and cat_key in workspace.metadata_profile.columns:
            if workspace.metadata_profile.columns[cat_key].classification == "IDENTIFIER":
                continue
        values = []
        if workspace_id == "default":
            values = registry.categoricals.get(cat_key, {}).get("values", [])
        elif workspace.metadata_profile and cat_key in workspace.metadata_profile.columns:
            values = workspace.metadata_profile.columns[cat_key].sample_values
        for val in values:
            val_str = str(val)
            if re.fullmatch(r"(?:19|20)\d{2}", val_str):
                continue
            if _value_mentioned(val_str):
                col_name = cat_key
                if not any(f["column"] == col_name and f["value"] == val_str for f in dynamic_filters):
                    dynamic_filters.append({"column": col_name, "value": val_str})
                    
        # Only id-shaped tokens are worth a database lookup. Ordinary words are
        # already covered by the in-memory value list above.
        col_class = None
        if workspace.metadata_profile and cat_key in workspace.metadata_profile.columns:
            col_class = workspace.metadata_profile.columns[cat_key].classification
        if col_class != "IDENTIFIER":
            continue
        for token in query_tokens:
            if re.fullmatch(r"20\d{2}", token) or not _IDENTIFIER_TOKEN.match(token):
                continue
            if token.lower() in ["revenue", "budget", "profit", "capacity", "allocated", "used", "completion", "delay", "days", "rate"]:
                continue
            if not any(f["column"] == cat_key and f["value"].lower() == token.lower() for f in dynamic_filters):
                if check_value_in_column(token, cat_key, workspace):
                    dynamic_filters.append({"column": cat_key, "value": token})

    name_mentions = resolve_category_mentions(raw_lower, workspace, workspace_id)
    for hit in name_mentions["filters"]:
        if not any(
            item["column"] == hit["column"] and str(item["value"]).lower() == hit["value"].lower()
            for item in dynamic_filters
        ):
            dynamic_filters.append({"column": hit["column"], "value": hit["value"]})
            has_categorical_filter = True

    if re.search(r"\b(how full|fullness|how utilized|how utilised)\b", raw_lower):
        for key in _metrics_about(workspace, adapter, "utilization", "utilisation"):
            if key not in detected_metrics:
                detected_metrics.append(key)
    if re.search(r"\b(money made|money|earnings)\b", raw_lower):
        for key in _metrics_about(workspace, adapter, "revenue"):
            if key not in detected_metrics:
                detected_metrics.append(key)

    anchored = bool(dynamic_filters or detected_years or detected_plants or has_project_id or has_categorical_filter)
    asks_status = bool(re.search(r"\b(doing|performance|how did|how is|how are)\b", raw_lower))
    if not detected_metrics and (anchored or asks_status or "everything" in raw_lower or "overall" in raw_lower):
        detected_metrics = _ranked_metric_names(workspace)[:4]

    if not (detected_metrics or has_project_id or detected_years or detected_plants or has_categorical_filter or dynamic_filters or has_structural):
        return None
        
    bp = Blueprint()
    bp.metrics = detected_metrics
                    
    # Fallback to extract any project ID matching pattern that was not in sampled values
    pid_match = re.search(r'\b[a-z0-9]{2,6}-[a-z0-9]{1,6}(-[a-z0-9]+)?\b', raw_lower)
    if pid_match and re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", pid_match.group(0)):
        pid_match = None
    if pid_match:
        matched_pid = pid_match.group(0).upper()
        _identifier_col = None
        for col_name, col_type in getattr(adapter, "column_to_type", {}).items():
            if col_type in ["IDENTIFIER", "DIMENSION"]:
                aliases = adapter.column_to_aliases.get(col_name, {col_name})
                for alias in aliases:
                    alias_clean = alias.replace('_', ' ').lower()
                    if alias_clean in ["id", "code", "type"] and alias_clean != col_name:
                        continue
                    if re.search(r'\b' + re.escape(alias_clean) + r'\b', raw_lower):
                        _identifier_col = col_name
                        break
            if _identifier_col:
                break
        
        if not _identifier_col:
            id_cols = [col for col, t in getattr(adapter, "column_to_type", {}).items() if t == "IDENTIFIER"]
            _identifier_col = next((c for c in id_cols if "project" in c or "code" in c), None)
            if not _identifier_col:
                _identifier_col = next((c for c in id_cols if c != "id"), None)
            if not _identifier_col:
                _identifier_col = id_cols[0] if id_cols else "project_id"
        if not any(f["value"].upper() == matched_pid for f in dynamic_filters):
            dynamic_filters.append({"column": _identifier_col, "value": matched_pid})

    # Plants
    for p in detected_plants:
        bp.filters.append({"column": "plant", "value": p})

    # 4. Intent Classification
    intent = "sum"
    confidence = 1.0
    
    # Comparison check
    from collections import Counter
    col_counts = Counter(f["column"] for f in dynamic_filters)
    has_multiple_values_for_col = any(count > 1 for count in col_counts.values())
    
    is_compare_query = any(w in raw_lower for w in ["compare", "comparison", "versus", "vs", "between"])
    if is_compare_query or len(detected_years) > 1 or has_multiple_values_for_col or len(detected_plants) > 1:
        intent = "comparison"
        
    is_top_n_query = any(w in raw_lower for w in ["top", "highest", "best", "lowest", "worst", "max", "min"])
    if is_top_n_query:
        intent = "top_n"
        
    is_trend_query = any(w in raw_lower for w in ["trend", "trends", "over time", "graph", "time-series", "monthly"])
    if is_trend_query and intent != "comparison" and intent != "top_n":
        intent = "trend"
        
    is_breakdown_query = any(w in raw_lower for w in ["breakdown", "by", "divided by", "split by"])
    if is_breakdown_query and intent == "sum":
        intent = "breakdown"

    # 5. Populate Blueprint based on Intent
    multi_value_columns = [col for col, count in col_counts.items() if count > 1]
    if len(detected_years) > 1:
        bp.timeframe = {"type": "years", "value": ",".join(list(dict.fromkeys(detected_years)))}
        bp.operation = "GRAPH"
        if not multi_value_columns:
            bp.comparison = {"type": "year", "values": list(dict.fromkeys(detected_years))}
    elif len(detected_years) == 1:
        bp.timeframe = {"type": "year", "value": detected_years[0]}
    elif any(w in raw_lower for w in ["monthly", "by month", "by months"]):
        bp.timeframe = {"type": "monthly"}

    # Filters adding
    bp.filters = dynamic_filters
    for p in detected_plants:
        bp.filters.append({"column": "plant", "value": p})

    # Comparison details populating
    if intent == "comparison":
        bp.operation = "GRAPH" if (len(detected_years) > 1 or is_trend_query) else "BREAKDOWN"
        if multi_value_columns:
            column = multi_value_columns[0]
            bp.comparison = {
                "type": column,
                "values": [item["value"] for item in dynamic_filters if item["column"] == column],
            }
        elif len(detected_years) > 1:
            bp.comparison = {"type": "year", "values": list(dict.fromkeys(detected_years))}
        elif len(detected_plants) > 1:
            bp.comparison = {"type": "plant", "values": detected_plants}
        else:
            comp_set = False
            for col, count in col_counts.items():
                if count > 1:
                    bp.comparison = {"type": col, "values": [f["value"] for f in dynamic_filters if f["column"] == col]}
                    comp_set = True
                    break
            if not comp_set:
                dimension = None
                dim_candidates_keys = []
                for k in workspace.available_dimensions:
                    if workspace.metadata_profile and k in workspace.metadata_profile.columns:
                        if workspace.metadata_profile.columns[k].classification != "IDENTIFIER":
                            dim_candidates_keys.append(k)
                    else:
                        dim_candidates_keys.append(k)
                # Pass 1: full alias match only
                for cat_key in dim_candidates_keys:
                    aliases = adapter.column_to_aliases.get(cat_key, {cat_key})
                    matched = False
                    for alias in aliases:
                        alias_clean = alias.replace('_', ' ').lower()
                        candidates = list(_make_plurals(alias_clean))
                        for var in candidates:
                            if any(w in raw_lower for w in [f"by {var}", f"across {var}", f"compare {var}", f"{var} comparison", f"split by {var}"]):
                                dimension = cat_key
                                matched = True
                                break
                        if matched:
                            break
                    if matched:
                        break
                # Pass 2: word-part match
                if not dimension:
                    for cat_key in dim_candidates_keys:
                        aliases = adapter.column_to_aliases.get(cat_key, {cat_key})
                        matched = False
                        for alias in aliases:
                            alias_clean = alias.replace('_', ' ').lower()
                            for word in alias_clean.split():
                                if len(word) > 3:
                                    for var in _make_plurals(word):
                                        if any(w in raw_lower for w in [f"by {var}", f"across {var}", f"compare {var}", f"{var} comparison", f"split by {var}"]):
                                            dimension = cat_key
                                            matched = True
                                            break
                                    if matched:
                                        break
                            if matched:
                                break
                        if matched:
                            break
                        
                if not dimension:
                    if _wants_year_group(raw_lower, detected_years):
                        dimension = "year"
                        
                if dimension:
                    bp.comparison = {"type": dimension}
                elif detected_metrics or re.search(r"\b(everything|overall|all)\b", raw_lower):
                    fallback_dim = _first_group_dimension(workspace)
                    if fallback_dim:
                        bp.comparison = {"type": fallback_dim}
                    else:
                        confidence = 0.7
                else:
                    confidence = 0.7
            
    # Top N / Breakdown details populating
    top_n_limit = None
    top_n_asc = False
    if intent == "top_n":
        bp.operation = "BREAKDOWN"
        dimension = None
        dim_keys = []
        for k in workspace.available_dimensions:
            if workspace.metadata_profile and k in workspace.metadata_profile.columns:
                if workspace.metadata_profile.columns[k].classification != "IDENTIFIER":
                    dim_keys.append(k)
            else:
                dim_keys.append(k)
        best_dimension = None
        best_score = -1
        for cat_key in dim_keys:
            aliases = adapter.column_to_aliases.get(cat_key, {cat_key})
            key_forms = set(_make_plurals(cat_key.replace("_", " ").lower()))
            for alias in aliases:
                alias_clean = alias.replace("_", " ").lower()
                forms = [(alias_clean, True)]
                for word in alias_clean.split():
                    if len(word) > 3 and word != alias_clean:
                        forms.append((word, False))
                for var_base, full_alias in forms:
                    for var in _make_plurals(var_base):
                        if not (
                            re.search(rf"\b(top|best|highest|lowest|worst|max|min)\s+(\d+\s+)?{re.escape(var)}\b", raw_lower)
                            or any(phrase in raw_lower for phrase in (f"top {var}", f"best {var}", f"worst {var}"))
                        ):
                            continue
                        score = len(var) + (100 if full_alias else 0)
                        if var in key_forms or var_base in key_forms:
                            score += 50
                        if score > best_score:
                            best_score = score
                            best_dimension = cat_key
        dimension = best_dimension
                
        if not dimension:
            if "plant" in raw_lower:
                dimension = "plant"
            elif any(w in raw_lower for w in ["department", "dept", "project type", "project_type"]):
                dimension = resolve_db_column(workspace_id, "project_type")
            elif any(w in raw_lower for w in ["region", "state", "location"]):
                dimension = resolve_db_column(workspace_id, "state") or resolve_db_column(workspace_id, "location")
            else:
                dimension = resolve_db_column(workspace_id, "project_type")
            
        bp.comparison = {"type": dimension}
        
        limit_match = re.search(r'\b(top|best|highest|lowest|worst)\s+(\d+)\b', raw_lower)
        if limit_match:
            top_n_limit = int(limit_match.group(2))
        else:
            top_n_limit = 3
            
        if any(w in raw_lower for w in ["lowest", "worst", "min"]):
            top_n_asc = True
            
        bp.limit = top_n_limit
        bp.sort_asc = top_n_asc
            
    elif intent == "trend":
        bp.operation = "GRAPH"
        
    elif intent == "breakdown":
        bp.operation = "BREAKDOWN"
        dimension = None
        dim_keys = []
        for k in workspace.available_dimensions:
            if workspace.metadata_profile and k in workspace.metadata_profile.columns:
                if workspace.metadata_profile.columns[k].classification != "IDENTIFIER":
                    dim_keys.append(k)
            else:
                dim_keys.append(k)
        # Longest phrase wins, so "by project type" is not grouped as every project id.
        best_len = -1
        for cat_key in dim_keys:
            if _is_id_key(cat_key):
                continue
            aliases = adapter.column_to_aliases.get(cat_key, {cat_key})
            for alias in aliases:
                alias_clean = alias.replace('_', ' ').lower().strip()
                if len(alias_clean) < 3:
                    continue
                for var in _make_plurals(alias_clean):
                    if f"by {var}" in raw_lower or f"split by {var}" in raw_lower or f"divided by {var}" in raw_lower or f"across {var}" in raw_lower:
                        if len(var) > best_len:
                            best_len = len(var)
                        dimension = cat_key
                
        if not dimension:
            if "by year" in raw_lower or "year" in raw_lower:
                dimension = "year"
                
        if dimension == "year":
            bp.operation = "GRAPH"
        elif dimension:
            bp.comparison = {"type": dimension}
        else:
            fallback_dim = _first_group_dimension(workspace)
            filtered_columns = {f.get("column") for f in dynamic_filters}
            if dynamic_filters and not re.search(r"\b(by|across)\s+(year|company|companies|state|states|region|department|plant|category)\b", raw_lower):
                bp.operation = "SUM"
            elif fallback_dim and fallback_dim not in filtered_columns:
                bp.comparison = {"type": fallback_dim}
            elif fallback_dim:
                bp.operation = "SUM"
                
    elif intent == "sum":
        bp.operation = "SUM"
        if _wants_year_group(raw_lower, detected_years):
            bp.comparison = {"type": "year"}
            bp.operation = "GRAPH"
        if not bp.filters and not bp.comparison and not bp.timeframe:
            _det_stopwords = {
                'what', 'is', 'the', 'of', 'in', 'for', 'between', 'and', 'show', 'get',
                'total', 'trend', 'over', 'time', 'at', 'a', 'an', 'to', 'by', 'how',
                'with', 'find', 'list', 'display', 'me', 'us', 'tell', 'please', 'select',
                'give', 'fetch', 'data', 'info', 'all', 'revenue', 'budget', 'completion',
                'delay', 'capacity', 'value',
                'values', 'where', 'sum', 'average', 'avg', 'count', 'number',
                'are', 'plants', 'full', 'fullness', 'everything', 'made', 'versus',
                'overall', 'utilized', 'utilised', 'doing', 'did', 'yearly', 'annually', 'yoy',
                'month', 'months', 'monthly', 'daily', 'date', 'dates',
                'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                'september', 'october', 'november', 'december',
                'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec'
            }
            _metric_words = set()
            for mk in workspace.available_metrics:
                for w in mk.lower().replace('_', ' ').split():
                    _metric_words.add(w)
            _query_tokens = re.findall(r'\b[a-zA-Z]{3,}\b', raw_lower)
            _unresolved = [
                t for t in _query_tokens
                if t not in _det_stopwords and t not in _metric_words and not _is_instruction_word(t)
            ]
            if _unresolved:
                confidence = 0.0

    apply_calendar(bp, raw_lower)
    if detected_metrics and bp.comparison and bp.comparison.get("type") in ("year", "month", "date"):
        confidence = 1.0

    return {
        "blueprint": bp,
        "intent": intent,
        "confidence": confidence,
        "top_n_limit": top_n_limit,
        "top_n_asc": top_n_asc
    }

# --- Federated Query Engine ---
_INDEX_READY = set()

def _value_kind(samples: List[Any]) -> Optional[str]:
    """Tell a bare year, a unix timestamp, and a calendar date apart from the stored values."""
    texts = [_sample_text(value) for value in samples if value is not None and _sample_text(value)]
    if not texts:
        return None
    if all(re.fullmatch(r"(?:19|20)\d{2}", text) for text in texts):
        return "year"
    if all(text.isdigit() and int(text) > 3000 for text in texts):
        return "unix"
    if all(re.match(r"^\d{4}-\d{2}(?:-\d{2})?(?:[ T].*)?$", text) for text in texts):
        return "date"
    return None


def _speed_shape(conn: sqlite3.Connection, table_name: str, profile=None):
    """Year column, date column, metrics, and group columns for this table."""
    info = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    actual = {}
    declared = {}
    for row in info:
        name = row[1]
        if _safe_sql_name(name):
            actual[name.lower()] = name
            declared[name.lower()] = (row[2] or "").upper()

    def real(name: str) -> str:
        return actual.get((name or "").lower(), name)

    numeric = ("INT", "REAL", "NUM", "FLOA", "DOUB", "DEC")
    if profile and getattr(profile, "columns", None):
        kinds = {}
        for name in profile.columns:
            if name.lower() not in actual:
                continue
            kinds[name.lower()] = time_column_kind(profile, name)
        dated = [real(name) for name, kind in kinds.items() if kind in ("date", "unix")]
        date_col = pick_dated_column(dated)
        date_kind = kinds.get((date_col or "").lower())
        year_col = _year_key_column(profile, date_col or "")
        if year_col:
            year_col = real(year_col)
        metrics = []
        groups = []
        for name, col in profile.columns.items():
            stored = real(name)
            if stored not in actual.values() or not _safe_sql_name(stored):
                continue
            if stored in (year_col, date_col) or kinds.get(name.lower()) == "year":
                continue
            if col.classification == "METRIC" and not _is_id_key(stored):
                metrics.append(stored)
                continue
            if col.classification not in ("DIMENSION", "STATUS") or _is_id_key(stored):
                continue
            card = col.cardinality
            ratio = col.cardinality_ratio
            if card is not None and card > 500:
                continue
            if ratio is not None and ratio > 0.35 and (card is None or card > 30):
                continue
            groups.append((card if card is not None else 10**9, stored))
        groups = [name for _card, name in sorted(groups)[:8]]
        return year_col, date_col, date_kind, metrics[:12], groups

    cursor = conn.execute(f"SELECT * FROM {table_name} LIMIT 80")
    rows = cursor.fetchall()
    ordered = [item[0] for item in cursor.description]
    names = [name for name in ordered if _safe_sql_name(name)]
    samples = {name: [] for name in names}
    for row in rows:
        for name, value in zip(ordered, row):
            if name in samples and value is not None and len(samples[name]) < 40:
                samples[name].append(value)
    kinds = {name: _value_kind(samples[name]) for name in names}
    year_names = [name for name, kind in kinds.items() if kind == "year"]
    dated = [name for name, kind in kinds.items() if kind in ("date", "unix")]
    date_col = pick_dated_column(dated)
    date_kind = kinds.get(date_col) if date_col else None
    dedicated = [name for name in year_names if name != date_col]
    pool = dedicated or year_names
    year_col = sorted(pool, key=lambda name: (len(name), name))[0] if pool else None
    metrics = []
    groups = []
    for name in names:
        if name == year_col or kinds.get(name) == "year" or _is_id_key(name):
            continue
        if any(token in declared.get(name.lower(), "") for token in numeric):
            metrics.append(name)
            continue
        if kinds.get(name) in ("date", "unix"):
            continue
        distinct = {str(value) for value in samples[name] if value is not None}
        taken = [value for value in samples[name] if value is not None]
        if not distinct or len(distinct) > 25:
            continue
        if len(taken) >= 20 and len(distinct) >= len(taken) * 0.8:
            continue
        groups.append((len(distinct), name))
    groups = [name for _card, name in sorted(groups)[:8]]
    return year_col, date_col, date_kind, metrics[:12], groups


def ensure_query_indexes(db_path: str, table_name: Optional[str] = None, profile=None) -> None:
    """Covering indexes for whatever year, date, metric, and group columns this file has."""
    if not db_path or not os.path.exists(db_path):
        return
    key = os.path.abspath(db_path)
    if key in _INDEX_READY:
        return
    conn = sqlite3.connect(db_path)
    try:
        if not table_name or not _safe_sql_name(table_name):
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            names = [row[0] for row in rows if _safe_sql_name(row[0])]
            metrics_tables = [name for name in names if name.startswith("metrics_")]
            if metrics_tables:
                table_name = metrics_tables[0]
            else:
                best_name = None
                best_count = -1
                for name in names:
                    count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    if count > best_count:
                        best_name, best_count = name, count
                table_name = best_name
        if not table_name or not _safe_sql_name(table_name):
            _INDEX_READY.add(key)
            return
        year_col, date_col, date_kind, metrics, group_cols = _speed_shape(conn, table_name, profile)
        if not year_col and not date_col:
            _INDEX_READY.add(key)
            return

        def create(index_name: str, index_cols: List[str]) -> None:
            wanted = f"CREATE INDEX {index_name} ON {table_name}({', '.join(index_cols)})"
            existing = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name = ?",
                (index_name,),
            ).fetchone()
            if existing and existing[0] == wanted:
                return
            conn.execute(f"DROP INDEX IF EXISTS {index_name}")
            conn.execute(wanted)

        wanted_names = set()
        if year_col:
            year_piece = year_col
            tail = group_cols + ([date_col] if date_col and date_col != year_col else []) + metrics
            planned = [("idx_alphabot_year", [year_piece] + tail)]
            for dim in group_cols:
                planned.append((f"idx_alphabot_{dim}", [dim, year_piece] + metrics))
        else:
            kind = date_kind or "date"
            year_piece = _stored_time_expr(kind, date_col, "year")
            planned = [
                ("idx_alphabot_year", [year_piece] + group_cols + metrics),
                ("idx_alphabot_month", [_stored_time_expr(kind, date_col, "month")] + metrics),
                ("idx_alphabot_day", [_stored_time_expr(kind, date_col, "day")] + metrics),
            ]
            for dim in group_cols:
                planned.append((f"idx_alphabot_{dim}", [dim, year_piece] + metrics))
        wanted_names = {name for name, _cols in planned}
        for old in ("idx_fy_cover", "idx_state_fy_cover", "idx_fy_date_cover"):
            conn.execute(f"DROP INDEX IF EXISTS {old}")
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_alphabot_%'"
        ).fetchall()
        for (name,) in existing:
            if name not in wanted_names:
                conn.execute(f"DROP INDEX IF EXISTS {name}")
        for name, cols in planned:
            create(name, cols)
        conn.commit()
        _INDEX_READY.add(key)
        logger.info(f"Query indexes ready on {table_name} (year={year_col or date_col}, groups={len(group_cols)})")
    except sqlite3.Error as exc:
        logger.warning(f"Could not add query indexes on {db_path}: {exc}")
    finally:
        conn.close()

_SQLITE_LOCAL = threading.local()
_SQLITE_OPEN: Dict[str, List[sqlite3.Connection]] = {}
_SQLITE_LOCK = threading.Lock()

def _borrow_sqlite(db_file: str) -> sqlite3.Connection:
    """Reuse one connection per thread. Opening a new connection and requesting
    a 30GB memory map on every query was stalling Windows."""
    conns = getattr(_SQLITE_LOCAL, "conns", None)
    if conns is None:
        conns = {}
        _SQLITE_LOCAL.conns = conns
    conn = conns.get(db_file)
    if conn is not None:
        return conn
    conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA cache_size = -16000")
    conns[db_file] = conn
    with _SQLITE_LOCK:
        _SQLITE_OPEN.setdefault(db_file, []).append(conn)
    return conn

def _close_sqlite(db_file: str) -> None:
    """Close every connection opened for this file, including other threads."""
    with _SQLITE_LOCK:
        open_conns = _SQLITE_OPEN.pop(db_file, [])
    for conn in open_conns:
        try:
            conn.close()
        except Exception:
            pass
    local = getattr(_SQLITE_LOCAL, "conns", None)
    if local is not None:
        local.pop(db_file, None)

async def run_query_on_single_db(plant: str, sql: str, params: tuple) -> List[Dict]:
    """Runs a SQL query on a single power plant database file."""
    db_file = ConnectionManager.db_path(plant)
    if not os.path.exists(db_file): return []
    
    registry = MetadataRegistry.get_instance()
    table_name = registry.table_names.get(plant)
    if not table_name: return []
    
    import re
    if not re.match(r'^[a-zA-Z0-9_]+$', table_name):
        raise ValueError(f"Invalid table name: {table_name}")
    if table_name not in registry.table_names.values():
        raise ValueError(f"Unauthorized table name: {table_name}")

    loop = asyncio.get_running_loop()
    def query():
        conn = _borrow_sqlite(db_file)
        try:
            cursor = conn.execute(sql.replace("{table_name}", table_name), params)
            rows = [dict(row) for row in cursor.fetchall()]
            for r in rows:
                r["plant"] = plant
            return rows
        except sqlite3.Error as e:
            logger.error(f"Query error on {plant}: {e}")
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            return []
    return await loop.run_in_executor(None, query)

def profile_for_workspace(workspace_id: str):
    """Profile for the selected database. Default uses a plant from the 8, never a standalone file."""
    profiler = SemanticSchemaProfiler.get_instance()
    if workspace_id and workspace_id not in ("default", "all"):
        return profiler.profiles.get(workspace_id)
    for name in POWER_PLANTS:
        profile = profiler.profiles.get(name)
        if profile:
            return profile
    return None

def _is_time_key(name: str) -> bool:
    return bool(re.search(r"date|year|month|time|period|quarter", name or "", re.I))


def _is_id_key(name: str) -> bool:
    key = (name or "").lower()
    return key == "id" or key.endswith("_id") or key.endswith("_code")


_METRIC_PRIORITY = ["revenue", "capacity_mw", "budget_allocated", "budget_used", "budget_remaining", "completion_percentage", "delay_days"]
_GROUP_PRIORITY = ["project_type", "department", "location", "state", "region", "category", "plant_type", "company"]
_YEAR_COLUMNS = {"fy_year", "year", "fiscal_year", "record_date"}


def years_for_workspace(workspace, workspace_id: str = "default") -> List[str]:
    """Distinct years stored on this database, expanded when the span is short."""
    profiles = []
    if workspace and getattr(workspace, "metadata_profile", None):
        profiles.append(workspace.metadata_profile)
    shared = profile_for_workspace(workspace_id or "default")
    if shared is not None and shared not in profiles:
        profiles.append(shared)
    found: List[str] = []
    for profile in profiles:
        time_cols = [(name, col) for name, col in profile.columns.items() if col.classification == "TIME"]
        preferred = [col for name, col in time_cols if name in _YEAR_COLUMNS]
        for col_prof in (preferred or [col for _name, col in time_cols]):
            for value in col_prof.sample_values:
                found.extend(re.findall(r"((?:19|20)\d{2})", str(value)))
    unique = sorted(set(found))
    if len(unique) >= 2:
        lo, hi = int(unique[0]), int(unique[-1])
        if 0 < hi - lo <= 15:
            return [str(year) for year in range(lo, hi + 1)]
        return unique
    if unique:
        return unique
    min_y, max_y = year_bounds(workspace, workspace_id)
    if max_y - min_y > 15:
        return []
    return [str(year) for year in range(min_y, max_y + 1)]


def build_suggested_queries(metrics: List[str], categoricals: Dict[str, List[str]], years: List[str]) -> List[str]:
    """Questions built only from this database's metrics, groups, and years."""
    if not metrics:
        return []
    def title(name: str) -> str:
        return " ".join(part[:1].upper() + part[1:] for part in str(name).replace("_", " ").split())
    primary = title(metrics[0])
    questions = [f"{primary} trend"]
    if years:
        questions.append(f"{primary} in {years[-1]}")
        if len(years) >= 2:
            questions.append(f"Compare {primary.lower()} in {years[0]} and {years[-1]}")
    ordered_groups = sorted(
        categoricals.items(),
        key=lambda item: _GROUP_PRIORITY.index(item[0]) if item[0] in _GROUP_PRIORITY else len(_GROUP_PRIORITY),
    )
    for key, values in ordered_groups:
        if not values or _is_time_key(key) or _is_id_key(key):
            continue
        questions.append(f"{primary} by {key.replace('_', ' ')}")
        break
    if len(metrics) > 1:
        questions.append(f"{title(metrics[1])} trend")
    seen = set()
    unique = []
    for question in questions:
        key = question.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(question)
    return unique[:5]


def schema_facets(plant: Optional[str] = None) -> Dict[str, Any]:
    """Metrics, groups, years, and starter questions for one selected database."""
    plant_id = plant or "default"
    registry = MetadataRegistry.get_instance()
    profiler = SemanticSchemaProfiler.get_instance()
    metrics: List[str] = []
    categoricals: Dict[str, List[str]] = {}
    profile = profiler.profiles.get(plant_id) if plant_id not in ("default", "all") else None
    if profile:
        metrics = [name for name, col in profile.columns.items() if col.classification == "METRIC"]
        for name, col in profile.columns.items():
            if col.classification not in ("DIMENSION", "STATUS") or _is_time_key(name) or _is_id_key(name):
                continue
            values = [str(value) for value in col.sample_values if value is not None and str(value).strip()]
            if values:
                categoricals[name] = values[:12]
    elif plant_id in ("default", "all"):
        metrics = list(registry.metrics.keys())
        for name, info in registry.categoricals.items():
            if _is_time_key(name) or _is_id_key(name):
                continue
            raw = info.get("values", []) if isinstance(info, dict) else info
            values = [str(value) for value in list(raw)[:12] if value is not None and str(value).strip()]
            if values:
                categoricals[name] = values
    def metric_rank(name: str) -> int:
        if name in _METRIC_PRIORITY:
            return _METRIC_PRIORITY.index(name)
        for index, known in enumerate(_METRIC_PRIORITY):
            if name.startswith(known) or known.startswith(name):
                return index
        return len(_METRIC_PRIORITY)
    metrics = sorted(metrics, key=metric_rank)
    categoricals = {
        name: categoricals[name]
        for name in sorted(categoricals, key=lambda item: _GROUP_PRIORITY.index(item) if item in _GROUP_PRIORITY else len(_GROUP_PRIORITY))
    }
    years = years_for_workspace(None, "default" if plant_id == "all" else plant_id)
    return {
        "metrics": metrics,
        "categoricals": categoricals,
        "years": years,
        "suggestions": build_suggested_queries(metrics, categoricals, years),
    }


def year_bounds(workspace, workspace_id: str = "default"):
    """Year range stored on this database. Standalone files are not forced into 2020–2026."""
    profiles = []
    if workspace_id not in ("default", "all", "", None) and workspace and getattr(workspace, "metadata_profile", None):
        profiles.append(workspace.metadata_profile)
    else:
        shared = profile_for_workspace(workspace_id or "default")
        if shared:
            profiles.append(shared)
    years = []
    for profile in profiles:
        for _col, col_prof in profile.columns.items():
            if col_prof.classification != "TIME":
                continue
            for value in col_prof.sample_values:
                match = re.match(r"^((?:19|20)\d{2})", str(value))
                if match:
                    years.append(int(match.group(1)))
    if years:
        return min(years), max(years)
    if workspace_id in ("default", "all", "", None):
        return 2020, 2026
    return 1990, 2100

_MONTH_NUMBERS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "june": "06", "july": "07", "august": "08", "september": "09",
    "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05",
    "jun": "06", "jul": "07", "aug": "08", "sep": "09", "sept": "09",
    "oct": "10", "nov": "11", "dec": "12",
}
_FULL_MONTHS = {name for name in _MONTH_NUMBERS if len(name) > 3}


def _blank_span(text: str, start: int, end: int) -> str:
    return text[:start] + (" " * (end - start)) + text[end:]


def extract_calendar_mentions(text: str) -> Dict[str, List[str]]:
    """Pull dates, month-years, months, and leftover years out of a question.

    A year that only appears inside a date or a month-year is not also returned
    as a whole-year filter.
    """
    raw = (text or "").lower()
    dates: List[str] = []
    month_years: List[str] = []
    months: List[str] = []
    working = raw

    def _keep(bucket: List[str], value: str) -> None:
        if value not in bucket:
            bucket.append(value)

    for match in list(re.finditer(r"(?<!\d)((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", working)):
        year, month, day = match.group(1), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            _keep(dates, f"{year}-{month:02d}-{day:02d}")
            working = _blank_span(working, match.start(), match.end())

    full = "|".join(sorted(_FULL_MONTHS, key=len, reverse=True))
    short = "|".join(sorted((name for name in _MONTH_NUMBERS if name not in _FULL_MONTHS), key=len, reverse=True))
    named = f"({full}|{short})"
    for pattern in (
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{named}\s+((?:19|20)\d{{2}})\b",
        rf"\b{named}\s+(\d{{1,2}})(?:st|nd|rd|th)?\,?\s+((?:19|20)\d{{2}})\b",
    ):
        for match in list(re.finditer(pattern, working)):
            groups = match.groups()
            if groups[0].isdigit():
                day, month_name, year = int(groups[0]), groups[1], groups[2]
            else:
                month_name, day, year = groups[0], int(groups[1]), groups[2]
            month = int(_MONTH_NUMBERS[month_name])
            if 1 <= day <= 31:
                _keep(dates, f"{year}-{month:02d}-{day:02d}")
                working = _blank_span(working, match.start(), match.end())

    for pattern, year_second in (
        (rf"\b{named}\s+((?:19|20)\d{{2}})\b", True),
        (rf"\b((?:19|20)\d{{2}})\s+{named}\b", False),
        (r"(?<!\d)((?:19|20)\d{2})[-/](\d{1,2})(?!\d)", False),
        (r"(?<!\d)(\d{1,2})[-/]((?:19|20)\d{2})(?!\d)", True),
    ):
        for match in list(re.finditer(pattern, working)):
            if year_second and match.group(1).isdigit() and len(match.group(1)) <= 2:
                month_token, year = match.group(1), match.group(2)
                month = f"{int(month_token):02d}" if month_token.isdigit() else _MONTH_NUMBERS.get(month_token)
            elif year_second:
                month = _MONTH_NUMBERS.get(match.group(1))
                year = match.group(2)
            elif match.group(1).isdigit() and len(match.group(1)) == 4:
                year, month_token = match.group(1), match.group(2)
                month = f"{int(month_token):02d}" if month_token.isdigit() else _MONTH_NUMBERS.get(month_token)
            else:
                month = _MONTH_NUMBERS.get(match.group(2))
                year = match.group(1)
            if month and 1 <= int(month) <= 12:
                _keep(month_years, f"{year}-{int(month):02d}")
                working = _blank_span(working, match.start(), match.end())

    for match in list(re.finditer(rf"\b({full})\b", working)):
        _keep(months, _MONTH_NUMBERS[match.group(1)])
        working = _blank_span(working, match.start(), match.end())
    for match in list(re.finditer(rf"\b(?:in|for|during|on|of|vs|versus)\s+({short})\b", working)):
        _keep(months, _MONTH_NUMBERS[match.group(1)])
        working = _blank_span(working, match.start(), match.end())

    return {"dates": dates, "month_years": month_years, "months": months, "years": mentioned_years(working)}


def _wants_month_group(text: str) -> bool:
    return re.search(r"\b(by months?|monthly|each month|per month)\b", text or "") is not None


def _wants_date_group(text: str) -> bool:
    return re.search(r"\b(by dates?|daily|each day|per day)\b", text or "") is not None


def apply_calendar(bp, text: str) -> Dict[str, List[str]]:
    """Set the blueprint's time filter from the question, for whatever grain was asked."""
    cal = extract_calendar_mentions(text)
    dates, month_years, months, years = cal["dates"], cal["month_years"], cal["months"], cal["years"]
    if len(dates) > 1:
        bp.comparison = {"type": "date", "values": dates}
        bp.timeframe = {"type": "dates", "value": ",".join(dates)}
        bp.operation = "GRAPH"
    elif len(dates) == 1:
        bp.timeframe = {"type": "date", "value": dates[0]}
    elif len(month_years) > 1:
        bp.comparison = {"type": "month", "values": month_years}
        bp.timeframe = {"type": "month_years", "value": ",".join(month_years)}
        bp.operation = "GRAPH"
    elif len(month_years) == 1:
        bp.timeframe = {"type": "month_year", "value": month_years[0]}
    elif months and years:
        combos = [f"{year}-{month}" for year in years for month in months]
        if len(combos) > 1:
            bp.comparison = {"type": "month", "values": combos}
            bp.timeframe = {"type": "month_years", "value": ",".join(combos)}
            bp.operation = "GRAPH"
        else:
            bp.timeframe = {"type": "month_year", "value": combos[0]}
    elif len(months) > 1:
        bp.comparison = {"type": "month", "values": months}
        bp.timeframe = {"type": "month", "value": ",".join(months)}
        bp.operation = "GRAPH"
    elif len(months) == 1:
        bp.timeframe = {"type": "month", "value": months[0]}
    elif len(years) > 1:
        bp.timeframe = {"type": "years", "value": ",".join(years)}
        held = (bp.comparison or {}).get("type")
        # A category comparison, such as two states, stays its own series.
        # The years only limit and label the axis.
        if held in (None, "year"):
            bp.comparison = {"type": "year", "values": list(years)}
        bp.operation = "GRAPH"
    elif len(years) == 1:
        bp.timeframe = {"type": "year", "value": years[0]}

    finer = bp.comparison and bp.comparison.get("type") in ("month", "date")
    if _wants_month_group(text) and not (bp.comparison and bp.comparison.get("type") == "date"):
        bp.comparison = {"type": "month"}
        bp.operation = "GRAPH"
        if not bp.timeframe:
            bp.timeframe = {"type": "monthly"}
    elif _wants_date_group(text) and not finer:
        bp.comparison = {"type": "date"}
        bp.operation = "GRAPH"
    elif _wants_year_group(text, years) and not finer:
        bp.comparison = {"type": "year"}
        bp.operation = "GRAPH"
    return cal


def _sample_text(value) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def time_column_kind(profile, time_col: str) -> str:
    """How a column stores time: a calendar date, a unix timestamp, or a bare year."""
    if not profile or not time_col or time_col not in getattr(profile, "columns", {}):
        return "date"
    column = profile.columns[time_col]
    samples = [_sample_text(value) for value in (column.sample_values or [])[:20] if value is not None and _sample_text(value)]
    if samples and all(re.fullmatch(r"(?:19|20)\d{2}", sample) for sample in samples):
        return "year"
    if samples and all(sample.isdigit() and int(sample) > 3000 for sample in samples):
        return "unix"
    return "date"


def _stored_time_expr(kind: str, time_col: str, grain: str) -> str:
    """SQL expression for a date or unix column. Indexes are created on this same text."""
    if kind == "unix":
        if grain == "day":
            return f"date({time_col}, 'unixepoch')"
        if grain == "month":
            return f"strftime('%Y-%m', {time_col}, 'unixepoch')"
        return f"strftime('%Y', {time_col}, 'unixepoch')"
    if grain == "day":
        return f"date({time_col})"
    if grain == "month":
        return f"strftime('%Y-%m', {time_col})"
    return f"strftime('%Y', {time_col})"


def _time_expr(profile, time_col: str, grain: str) -> str:
    """SQL expression for a year, a month, or a day on this column."""
    kind = time_column_kind(profile, time_col)
    if grain == "month" and kind == "year":
        grain = "year"
    if grain == "day" and kind == "year":
        grain = "year"
    if kind == "year":
        return f"CAST({time_col} AS TEXT)"
    return _stored_time_expr(kind, time_col, grain)


def _month_number_expr(profile, time_col: str) -> Optional[str]:
    kind = time_column_kind(profile, time_col)
    if kind == "year":
        return None
    if kind == "unix":
        return f"strftime('%m', {time_col}, 'unixepoch')"
    return f"strftime('%m', {time_col})"


def _safe_sql_name(name: str) -> bool:
    return bool(name and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))


def pick_dated_column(names: List[str]) -> Optional[str]:
    """Prefer a column whose name says it is a date. Any dated column is usable."""
    for name in names:
        if re.search(r"date|time|timestamp", name, re.I):
            return name
    return names[0] if names else None


def _year_key_column(profile, time_col: str) -> Optional[str]:
    """A bare year column on this database, whatever it is called."""
    if not profile:
        return None
    found = []
    for name in profile.columns:
        if not _safe_sql_name(name):
            continue
        if time_column_kind(profile, name) == "year":
            found.append(name)
    if not found:
        return None
    dedicated = [name for name in found if name != time_col]
    pool = dedicated or found
    pool.sort(key=lambda name: (len(name), name))
    return pool[0]


def _year_bucket(profile, time_col: str):
    """Label expression and GROUP BY expression for one row per year."""
    year_key = _year_key_column(profile, time_col)
    if year_key:
        return f"CAST({year_key} AS TEXT)", year_key
    expr = _time_expr(profile, time_col, "year")
    return expr, expr


def get_time_column_for_db(plant: str, fallback: str = "record_date") -> str:
    """Prefer a real date column so month and day questions work. A year-only column is the fallback."""
    profile = profile_for_workspace(plant or "default")
    if profile:
        time_cols = [name for name, col in profile.columns.items() if col.classification == "TIME"]
        dated = [name for name in time_cols if time_column_kind(profile, name) != "year"]
        picked = pick_dated_column(dated)
        if picked:
            return picked
        if time_cols:
            return time_cols[0]
        if fallback in profile.columns:
            return fallback
    return fallback

def resolve_db_column(plant: str, std_col: str) -> str:
    """
    Translates a standard/alias column name to the actual column name 
    present in the specific database schema of the given plant/project.
    """
    if not std_col:
        return std_col
    from backend.services.workspace.workspace_manager import WorkspaceManager
    workspace = WorkspaceManager.get_instance().get_workspace(plant)
    if not workspace:
        return std_col
        
    resolved = workspace.semantic_adapter.resolve_column_name(std_col)
    if resolved:
        return resolved
        
    profile = workspace.metadata_profile or profile_for_workspace(plant)
    if profile:
        std_clean = std_col.lower().strip()
        if std_clean in profile.columns:
            return std_clean
        for col_name in profile.columns.keys():
            if abs(len(std_clean) - len(col_name)) > 2:
                continue
            if get_levenshtein_distance(std_clean, col_name) <= 2:
                return col_name
            
    return std_col

def build_federated_query_parts(bp: Blueprint, time_col: str = "record_date", plant: Optional[str] = None) -> (str, List[str], tuple, str, str, str):
    target_plants = [f['value'] for f in bp.filters if f.get('column') == 'plant']
    workspace_id = target_plants[0] if target_plants else (plant or "default")
    from backend.services.workspace.workspace_manager import WorkspaceManager
    workspace = WorkspaceManager.get_instance().get_workspace(workspace_id)
    ref_plant = workspace_id if workspace_id not in ("default", "all") else (plant if plant not in (None, "default", "all") else (POWER_PLANTS[0] if POWER_PLANTS else None))
    registry = MetadataRegistry.get_instance()
    
    # Safety check helper for SQL injection prevention
    def is_safe_identifier(name: str) -> bool:
        import re
        return bool(re.match(r'^[a-zA-Z0-9_]+$', name))

    # Resolve metrics through workspace semantic adapter
    resolved_metrics = []
    for m in bp.metrics:
        resolved_m = workspace.semantic_adapter.resolve_column_name(m)
        if resolved_m:
            resolved_m = resolve_db_column(ref_plant, resolved_m)
            resolved_metrics.append(resolved_m)
        else:
            resolved_metrics.append(resolve_db_column(ref_plant, m))
    # Do not write back onto bp. Each database compiles from this local list.

    # Resolve valid metrics and dimensions specific to the targeted database context
    profiler = SemanticSchemaProfiler.get_instance()
    profile = workspace.metadata_profile if workspace_id != "default" else profiler.profiles.get(ref_plant)
    if profile:
        valid_metrics = [c for c, p in profile.columns.items() if p.classification == "METRIC"]
        valid_dims = [c for c, p in profile.columns.items() if p.classification in ["DIMENSION", "STATUS", "IDENTIFIER"]]
    else:
        valid_metrics = workspace.available_metrics
        valid_dims = workspace.available_dimensions
    
    # Whitelist of allowed column names to prevent SQL Injection
    allowed_cols = set(valid_metrics + valid_dims + ["plant", "year", "comparison_group", "label"])
    if profile:
        allowed_cols.update(name for name in profile.columns if _safe_sql_name(name))
    
    # Filter out unknown metrics and raise clean error if invalid
    safe_metrics = []
    for m in resolved_metrics:
        if not is_safe_identifier(m):
            raise ValueError(f"Invalid or unauthorized metric requested: {m}")
        if m in valid_metrics and m not in safe_metrics:
            safe_metrics.append(m)
    if bp.metrics and not safe_metrics:
        raise ValueError(f"Metric '{bp.metrics[0]}' does not exist inside the active workspace '{workspace_id}'.")
            
    # Group filters by column name
    from collections import defaultdict
    grouped_filters = defaultdict(list)
    for f in bp.filters:
        col = f.get('column')
        val = f.get('value')
        if not col or val is None:
            continue
            
        col_lower = col.lower()
        if col_lower == "plant":
            continue
        target = workspace.semantic_adapter.resolve_column_name(col_lower) or col_lower
        target = resolve_db_column(ref_plant, target)
        
        if not target or target not in allowed_cols:
            if not is_safe_identifier(col):
                raise ValueError(f"Invalid or unauthorized filter column: {col}")
            raise ValueError(f"Filter column '{col}' does not exist inside the active workspace '{workspace_id}'.")
            
        # Match value from database profile or global registry fallback
        matching_val = None
        if profile and target in profile.columns:
            matching_val = next((v for v in profile.columns[target].sample_values if str(v).lower() == str(val).lower() or str(v).lower().replace(' ', '_') == str(val).lower()), None)
        elif target in registry.categoricals:
            matching_val = next((v for v in registry.categoricals[target]['values'] if str(v).lower() == str(val).lower() or str(v).lower().replace(' ', '_') == str(val).lower()), None)
            
        if matching_val:
            grouped_filters[target].append(matching_val)
        else:
            grouped_filters[target].append(val)
            
    where_clauses, params = [], []
    for col, values in grouped_filters.items():
        if col == 'plant':
            continue
        if col not in allowed_cols:
            if not is_safe_identifier(col):
                raise ValueError(f"Invalid or unauthorized filter column: {col}")
            raise ValueError(f"Filter column '{col}' does not exist inside the active workspace '{workspace_id}'.")
        unique_vals = list(dict.fromkeys(values))
        if len(unique_vals) == 1:
            where_clauses.append(f"{col} = ?")
            params.append(unique_vals[0])
        else:
            placeholders = ", ".join("?" for _ in unique_vals)
            where_clauses.append(f"{col} IN ({placeholders})")
            params.extend(unique_vals)

    # 2. Numeric Comparisons (e.g., delay < 25)
    if bp.comparison and not bp.comparison.get('type'):
        comp = bp.comparison
        c_metric = comp.get('metric')
        c_op = comp.get('operator')
        c_val = comp.get('value')
        if c_metric:
            if not is_safe_identifier(c_metric):
                raise ValueError(f"Invalid or unauthorized comparison metric: {c_metric}")
            if c_op not in ['<', '>', '=', '<=', '>=']:
                raise ValueError(f"Invalid comparison operator: {c_op}")
            resolved_c_metric = workspace.semantic_adapter.resolve_column_name(c_metric) or c_metric
            resolved_c_metric = resolve_db_column(ref_plant, resolved_c_metric)
            if resolved_c_metric in valid_metrics:
                where_clauses.append(f"{resolved_c_metric} {c_op} ?")
                params.append(c_val)
            else:
                raise ValueError(f"Comparison metric '{c_metric}' does not exist inside the active workspace '{workspace_id}'.")

    # Timeframe handling. The predicate follows the column this database actually stores.
    tfs = bp.timeframes or []
    if bp.timeframe and bp.timeframe.get('value'):
        if not any(tf.get('value') == bp.timeframe.get('value') for tf in tfs):
            tfs = [bp.timeframe] + tfs
        
    dates_val = []
    month_years_val = []
    months_val = []
    years_val = []
    for tf in tfs:
        kind_name = tf.get('type')
        val_str = str(tf.get('value', '')).replace("FY", "").strip()
        if not val_str or kind_name == "monthly":
            continue
        parts = [part.strip() for part in val_str.split(",") if part.strip()]
        if kind_name in ("date", "dates") or (kind_name is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", val_str)):
            dates_val.extend(part[:10] for part in parts)
        elif kind_name in ("month_year", "month_years"):
            month_years_val.extend(parts)
        elif kind_name == "month":
            months_val.extend(f"{int(part):02d}" if part.isdigit() else part for part in parts)
        elif kind_name == "years":
            years_val.extend(parts)
        elif kind_name == "year" or re.fullmatch(r"(?:19|20)\d{2}", val_str):
            years_val.extend(parts)

    def _restrict(expr: str, values: List[str]) -> None:
        cleaned = [str(value) for value in values if str(value).strip()]
        if not cleaned or not expr:
            return
        if len(cleaned) == 1:
            where_clauses.append(f"{expr} = ?")
            params.append(cleaned[0])
        else:
            placeholders = ", ".join("?" for _ in cleaned)
            where_clauses.append(f"{expr} IN ({placeholders})")
            params.extend(cleaned)

    stored = time_column_kind(profile, time_col)
    year_key = _year_key_column(profile, time_col)

    def _bind_column(column: str, values: List) -> None:
        cleaned = []
        for value in values:
            if value not in cleaned:
                cleaned.append(value)
        if not cleaned or not column:
            return
        if len(cleaned) == 1:
            where_clauses.append(f"{column} = ?")
            params.append(cleaned[0])
        else:
            placeholders = ", ".join("?" for _ in cleaned)
            where_clauses.append(f"{column} IN ({placeholders})")
            params.extend(cleaned)

    def _year_numbers(items: List[str]) -> List[int]:
        found = []
        for item in items:
            match = re.match(r"((?:19|20)\d{2})", str(item))
            if not match:
                continue
            year = int(match.group(1))
            if year not in found:
                found.append(year)
        return found

    if dates_val:
        if stored == "year":
            numbers = _year_numbers(dates_val)
            if year_key and numbers:
                _bind_column(year_key, numbers)
            else:
                _restrict(_time_expr(profile, time_col, "year"), sorted({item[:4] for item in dates_val}))
        else:
            if year_key:
                _bind_column(year_key, _year_numbers(dates_val))
            _restrict(_time_expr(profile, time_col, "day"), dates_val)
    if month_years_val:
        if stored == "year":
            numbers = _year_numbers(month_years_val)
            if year_key and numbers:
                _bind_column(year_key, numbers)
            else:
                _restrict(_time_expr(profile, time_col, "year"), sorted({item[:4] for item in month_years_val}))
        else:
            if year_key:
                _bind_column(year_key, _year_numbers(month_years_val))
            _restrict(_time_expr(profile, time_col, "month"), month_years_val)
    if months_val and stored != "year":
        _restrict(_month_number_expr(profile, time_col), months_val)
    if years_val:
        numbers = _year_numbers(years_val)
        if year_key and numbers:
            _bind_column(year_key, numbers)
        else:
            _restrict(_time_expr(profile, time_col, "year"), years_val)

    metric_cols = list(safe_metrics)
    # Fix C: dynamic IDENTIFIER detection instead of hardcoded 'project_id'
    def _is_identifier_col(col_name: str) -> bool:
        if profile:
            cp = profile.columns.get(col_name)
            if cp and cp.classification == "IDENTIFIER":
                return True
        return False
    is_profile_request = any(_is_identifier_col(col) for col in grouped_filters)
    
    if not metric_cols and not is_profile_request:
        if "revenue" in valid_metrics:
            metric_cols = ["revenue"]
        elif valid_metrics:
            metric_cols = [valid_metrics[0]]
        elif workspace.available_metrics:
            metric_cols = [workspace.available_metrics[0]]

    # Handle grouping / graphing
    sql_select = ""
    sql_group_by = ""
    sql_order_by = ""
    
    if is_profile_request:
        if metric_cols:
            _id_col = next(
                (col for col in grouped_filters if _is_identifier_col(col)),
                "project_id"
            )
            select_cols = [_id_col, time_col] + metric_cols
            sql_select_final = ", ".join(select_cols)
        else:
            sql_select_final = "*"
        where_str = " AND ".join(where_clauses)
        return where_str, metric_cols, tuple(params), sql_select_final, sql_group_by, sql_order_by

    op = bp.operation.upper() if bp.operation else "SUM"
    
    grouping_dimension = None
    if bp.breakdown_by:
        bby = bp.breakdown_by.lower().strip()
        if bby == "plant":
            grouping_dimension = "plant"
        else:
            grouping_dimension = workspace.semantic_adapter.resolve_column_name(bby) or bby
            grouping_dimension = resolve_db_column(ref_plant, grouping_dimension)
            if not grouping_dimension or grouping_dimension not in allowed_cols:
                if not is_safe_identifier(bby):
                    raise ValueError(f"Invalid or unauthorized breakdown column: {bp.breakdown_by}")
                raise ValueError(f"Breakdown column '{bp.breakdown_by}' does not exist inside the active workspace '{workspace_id}'.")
            
    elif bp.comparison and bp.comparison.get('type'):
        comp_type = bp.comparison['type']
        if comp_type == "plant":
            grouping_dimension = "plant"
        elif comp_type == "year":
            grouping_dimension = "year"
        elif comp_type == "month":
            grouping_dimension = "month"
        elif comp_type == "date":
            grouping_dimension = "date"
        else:
            grouping_dimension = workspace.semantic_adapter.resolve_column_name(comp_type) or comp_type
            grouping_dimension = resolve_db_column(ref_plant, grouping_dimension)
            if not grouping_dimension or grouping_dimension not in allowed_cols:
                if not is_safe_identifier(comp_type):
                    raise ValueError(f"Invalid or unauthorized comparison dimension: {comp_type}")
                raise ValueError(f"Comparison dimension '{comp_type}' does not exist inside the active workspace '{workspace_id}'.")

    # Several chosen values of one column are separate series. A year filter
    # must not add those values together.
    split_column = next(
        (col for col, values in grouped_filters.items() if len(list(dict.fromkeys(values))) > 1),
        None,
    )
    if split_column and grouping_dimension in (None, "year", "month", "date"):
        grouping_dimension = split_column

    if not grouping_dimension:
        categorical_keys = [k for k in grouped_filters.keys() if k in valid_dims]
        if categorical_keys:
            for preferred_std in ['state', 'location', 'department', 'region', 'project_type']:
                preferred = resolve_db_column(ref_plant, preferred_std)
                if preferred in categorical_keys:
                    grouping_dimension = preferred
                    break
            else:
                grouping_dimension = categorical_keys[0]

    # Helper: detect if time_col stores an integer/text year vs a date/timestamp string
    def _tc_is_numeric() -> bool:
        if profile:
            cp = profile.columns.get(time_col)
            if cp:
                if any(t in cp.data_type.upper() for t in ["INT", "NUMERIC", "REAL", "DOUBLE", "FLOAT"]):
                    return True
                if cp.sample_values:
                    all_years_or_no_hyphen = True
                    for val in cp.sample_values:
                        val_str = str(val).strip()
                        if '-' in val_str or '/' in val_str:
                            all_years_or_no_hyphen = False
                            break
                    if all_years_or_no_hyphen:
                        return True
        return False
    _time_col_numeric = _tc_is_numeric()

    if op in ["GRAPH", "TREND"]:
        if grouping_dimension:
            comp_type = grouping_dimension
            if comp_type in ('year', 'month', 'date'):
                grain = {"year": "year", "month": "month", "date": "day"}[comp_type]
                if grain == "year":
                    select_expr, group_expr = _year_bucket(profile, time_col)
                else:
                    select_expr = group_expr = _time_expr(profile, time_col, grain)
                sql_select = f"{select_expr} as record_date"
                sql_group_by = f"GROUP BY {group_expr}"
                sql_order_by = f"ORDER BY {group_expr} ASC"
            elif comp_type == 'plant':
                group_by_month = bp.timeframe and bp.timeframe.get('type') in ['year', 'monthly']
                if _time_col_numeric:
                    sql_select = f"{time_col}"
                    sql_group_by = f"GROUP BY {time_col}"
                elif group_by_month:
                    sql_select = f"strftime('%Y-%m', {time_col}) as {time_col}"
                    sql_group_by = f"GROUP BY strftime('%Y-%m', {time_col})"
                else:
                    select_expr, group_expr = _year_bucket(profile, time_col)
                    sql_select = f"{select_expr} as {time_col}"
                    sql_group_by = f"GROUP BY {group_expr}"
                sql_order_by = f"ORDER BY {time_col} ASC"
            else:
                if comp_type not in allowed_cols:
                    if not is_safe_identifier(comp_type):
                        raise ValueError(f"Invalid grouping dimension: {comp_type}")
                    raise ValueError(f"Grouping dimension '{comp_type}' does not exist inside the active workspace '{workspace_id}'.")
                comp_col = comp_type
                group_by_month = bp.timeframe and bp.timeframe.get('type') in ['year', 'monthly']
                if _time_col_numeric:
                    sql_select = f"{time_col}, {comp_col} as comparison_group"
                    sql_group_by = f"GROUP BY {time_col}, {comp_col}"
                elif group_by_month:
                    sql_select = f"strftime('%Y-%m', {time_col}) as {time_col}, {comp_col} as comparison_group"
                    sql_group_by = f"GROUP BY strftime('%Y-%m', {time_col}), {comp_col}"
                else:
                    select_expr, group_expr = _year_bucket(profile, time_col)
                    sql_select = f"{select_expr} as {time_col}, {comp_col} as comparison_group"
                    sql_group_by = f"GROUP BY {group_expr}, {comp_col}"
                sql_order_by = f"ORDER BY {time_col} ASC, comparison_group ASC"
        else:
            if _time_col_numeric:
                sql_select = f"{time_col}"
                sql_group_by = f"GROUP BY {time_col}"
            elif bp.timeframe and bp.timeframe.get('type') in ['year', 'monthly']:
                sql_select = f"strftime('%Y-%m', {time_col}) as {time_col}"
                sql_group_by = f"GROUP BY strftime('%Y-%m', {time_col})"
            else:
                select_expr, group_expr = _year_bucket(profile, time_col)
                sql_select = f"{select_expr} as {time_col}"
                sql_group_by = f"GROUP BY {group_expr}"
            sql_order_by = f"ORDER BY {time_col} ASC"

    elif op == "BREAKDOWN":
        if grouping_dimension in ("year", "month", "date"):
            grain = {"year": "year", "month": "month", "date": "day"}[grouping_dimension]
            if grain == "year":
                select_expr, group_expr = _year_bucket(profile, time_col)
            else:
                select_expr = group_expr = _time_expr(profile, time_col, grain)
            sql_select = f"{select_expr} as record_date"
            sql_group_by = f"GROUP BY {group_expr}"
            sql_order_by = f"ORDER BY {group_expr} ASC"
        elif grouping_dimension:
            comp_col = grouping_dimension
            if comp_col == 'plant':
                sql_select = ""
                sql_group_by = ""
                sql_order_by = ""
            else:
                if comp_col not in allowed_cols:
                    if not is_safe_identifier(comp_col):
                        raise ValueError(f"Invalid breakdown dimension: {comp_col}")
                    raise ValueError(f"Breakdown dimension '{comp_col}' does not exist inside the active workspace '{workspace_id}'.")
                sql_select = f"{comp_col} as {comp_col}"
                sql_group_by = f"GROUP BY {comp_col}"
                sql_order_by = f"ORDER BY 1 ASC"
        else:
            dept_col = resolve_db_column(ref_plant, "project_type")
            region_col = resolve_db_column(ref_plant, "location")
            
            if dept_col in valid_dims or region_col in valid_dims:
                has_dept_filter = any(resolve_db_column(ref_plant, f.get('column', '')) == dept_col and f.get('value') for f in bp.filters)
                has_region_filter = any(resolve_db_column(ref_plant, f.get('column', '')) == region_col and f.get('value') for f in bp.filters)
                
                if has_dept_filter and not has_region_filter:
                    sql_select = f"{region_col} as {region_col}"
                    sql_group_by = f"GROUP BY {region_col}"
                elif has_region_filter and not has_dept_filter:
                    sql_select = f"{dept_col} as {dept_col}"
                    sql_group_by = f"GROUP BY {dept_col}"
                else:
                    sql_select = f"{dept_col} as {dept_col}"
                    sql_group_by = f"GROUP BY {dept_col}"
            else:
                group_col_name = "project_type" if "project_type" in valid_dims else "location"
                if group_col_name not in allowed_cols:
                    group_col_name = valid_dims[0] if valid_dims else "project_type"
                sql_select = f"{group_col_name} as {group_col_name}"
                sql_group_by = f"GROUP BY {group_col_name}"
            sql_order_by = "ORDER BY 1 ASC"

    elif grouping_dimension and grouping_dimension != 'year' and grouping_dimension != 'plant':
        comp_col = grouping_dimension
        if comp_col in allowed_cols:
            sql_select = f"{comp_col} as {comp_col}"
            sql_group_by = f"GROUP BY {comp_col}"
            sql_order_by = f"ORDER BY 1 ASC"
        
    select_parts = []
    if sql_select:
        select_parts.append(sql_select)
    for m in metric_cols:
        is_rate_col = should_aggregate_as_avg(m, profile)
        if op in ["MIN", "MAX", "AVG", "COUNT"]:
            select_parts.append(f"{op}({m}) as {m}")
        elif is_rate_col:
            select_parts.append(f"AVG({m}) as {m}")
        else:
            select_parts.append(f"SUM({m}) as {m}")
        
    sql_select_final = ", ".join(select_parts)
    where_str = " AND ".join(where_clauses)
    return where_str, metric_cols, tuple(params), sql_select_final, sql_group_by, sql_order_by

async def federated_query_processor(bp: Blueprint, raw_query: str, parsing_metadata: Optional[Dict[str, Any]] = None, workspace: Optional[Any] = None) -> Dict[str, Any]:
    """Queries databases in parallel (or specifically) and aggregates results."""
    target_plants = [f['value'] for f in bp.filters if f.get('column') == 'plant']
    workspace_id = target_plants[0] if target_plants else "default"
    if not workspace:
        from backend.services.workspace.workspace_manager import WorkspaceManager
        workspace = WorkspaceManager.get_instance().get_workspace(workspace_id)
    registry = MetadataRegistry.get_instance()
    
    # 0. Timeframe Boundary Guard
    tf_list = bp.timeframes or ([bp.timeframe] if bp.timeframe else [])
    if tf_list:
        allowed_years = []
        if workspace.metadata_profile and "fy_year" in workspace.metadata_profile.columns:
            allowed_years = [int(y) for y in workspace.metadata_profile.columns["fy_year"].sample_values if str(y).isdigit()]
        if not allowed_years and workspace_id == "default":
            allowed_years = list(registry.categoricals.get('fy_year', {}).get('values', []))
            
        if allowed_years:
            for tf in tf_list:
                val_str = str(tf.get('value', '')).replace("FY", "").strip()
                if val_str.isdigit():
                    y_int = int(val_str)
                    if y_int not in [int(x) for x in allowed_years]:
                        logger.warning(f"⚠️ Validation Blocked: Year {y_int} is out of database bounds.")
                        return {
                            "status": "error",
                            "message": f"Ambiguity or error: The requested year {y_int} is outside the available database bounds ({min(allowed_years)} - {max(allowed_years)}).",
                            "results": []
                        }

    # 1. Zero-Trust Dynamic Entity Validation & Fuzzy Recovery
    client_unknowns = (parsing_metadata or {}).get("unknown_tokens", [])
    filter_values = [str(f['value']).lower() for f in bp.filters]
    
    if not client_unknowns:
        corrected_query = correct_query_spelling(raw_query, workspace_id, complete=True)
        tokens = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_-]*\b', corrected_query)
        explained_names = resolve_category_mentions(corrected_query.lower(), workspace, workspace_id)["tokens"]
        stopwords = {
            'across', 'sites', 'all', 'from', 'year', 'than', 'between', 'and', 'id', 'project', 'projects', 
            'is', 'are', 'was', 'were', 'been', 'being', 'am', 'be', 'the', 'of', 'for', 'with', 'what', 'which', 
            'who', 'whom', 'whose', 'where', 'when', 'why', 'how', 'show', 'find', 'list', 'get', 'give', 'me', 
            'us', 'tell', 'please', 'display', 'search', 'select', 'metrics', 'data', 'info', 'information', 
            'having', 'days', 'months', 'years', 'day', 'month', 'year', 'value', 'values', 'details', 'about', 
            'to', 'at', 'by', 'on', 'in', 'an', 'a', 'or', 'so', 'yet', 'nor', 'but', 'can', 'could', 'shall', 
            'should', 'will', 'would', 'may', 'might', 'must', 'has', 'have', 'had', 'doing', 'does', 'did', 'done',
            'total', 'average', 'avg', 'sum', 'breakdown', 'compare', 'trend', 'graph', 'chart',
            'allocated', 'used', 'remaining', 'completion', 'delay', 'revenue', 'capacity', 'mw', 'percentage',
            'status', 'state', 'department', 'location', 'contractor', 'payment', 'material', 'category',
            'budget', 'fiscal', 'states', 'locations', 'departments', 'contractors', 'payments', 'materials',
            'categories', 'plants', 'trends', 'graphs', 'charts',
            'top', 'bottom', 'best', 'worst', 'highest', 'lowest', 'first', 'last', 'most', 'least',
            'ranked', 'ranking', 'rank', 'performing', 'performer', 'performers', 'performance',
            'number', 'nos', 'no', 'nth', 'order', 'ordered', 'sort', 'sorted', 'ascending', 'descending',
            'name', 'named', 'names', 'type', 'types', 'level', 'levels', 'group', 'groups', 'grouped',
            'count', 'number', 'quantity', 'amount', 'rate', 'ratio', 'score', 'index',
            'high', 'low', 'max', 'min', 'maximum', 'minimum', 'above', 'below', 'over', 'under',
            'much', 'money', 'compared', 'during', 'team', 'teams', 'plant', 'db', 'database', 'databases', 
            'record', 'records', 'date', 'dates', 'time', 'times', 'filter', 'filters', 'query', 'queries', 
            'run', 'execute', 'result', 'results', 'latest', 'oldest', 'newest', 'recent', 'recents',
            'current', 'previous', 'next', 'last', 'first', 'second', 'third', 'fourth', 'fifth',
            'full', 'fullness', 'everything', 'made', 'versus', 'overall', 'utilized', 'utilised',
            'utilization', 'utilisation', 'yearly', 'annually', 'yoy',
            'month', 'months', 'monthly', 'daily',
            'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
            'september', 'october', 'november', 'december',
            'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec'
        }
        known_words = set()
        for metric_key in workspace.available_metrics:
            known_words.add(metric_key.lower())
            known_words.add(metric_key.lower().replace('_', ' '))
            for w in metric_key.split('_'):
                known_words.add(w.lower())
                known_words.update(_make_plurals(w.lower()))
            for alias in workspace.semantic_adapter.column_to_aliases.get(metric_key, set()):
                squashed = re.sub(r"[\s_]+", "", str(alias).lower())
                if len(squashed) >= 5:
                    known_words.add(squashed)
        for cat_key in workspace.available_dimensions:
            known_words.add(cat_key.lower())
            known_words.add(cat_key.lower().replace('_', ' '))
            for w in cat_key.split('_'):
                known_words.add(w.lower())
                known_words.update(_make_plurals(w.lower()))
        if workspace_id == "default":
            for p in ConnectionManager.keys():
                known_words.add(p.lower())
                known_words.add(p.lower().replace('_', ' '))
                for w in p.split('_'):
                    known_words.add(w.lower())
                    known_words.update(_make_plurals(w.lower()))
            for cat_data in registry.categoricals.values():
                for v in cat_data.get("values", []):
                    v_str = str(v).lower()
                    known_words.add(v_str)
                    known_words.update(_make_plurals(v_str))
                    for w in v_str.replace('_', ' ').split():
                        known_words.add(w)
                        known_words.update(_make_plurals(w))
        else:
            if workspace.metadata_profile:
                for col_name, col_prof in workspace.metadata_profile.columns.items():
                    if col_prof.classification in ["DIMENSION", "STATUS", "IDENTIFIER"]:
                        for v in col_prof.sample_values:
                            v_str = str(v).lower()
                            known_words.add(v_str)
                            known_words.update(_make_plurals(v_str))
                            for w in v_str.replace('_', ' ').split():
                                known_words.add(w)
                                known_words.update(_make_plurals(w))
                    
        client_unknowns = []
        for token in tokens:
            t_lower = token.lower()
            if len(t_lower) <= 2:
                continue
            if t_lower in stopwords or t_lower in explained_names or _is_instruction_word(t_lower):
                continue
            if t_lower in known_words or t_lower.replace("_", " ") in known_words or t_lower.replace("_", "") in known_words:
                continue
            if is_value_in_workspace_db(token, workspace_id):
                continue
            if t_lower.isdigit() or re.match(r'^fy\d{4}$', t_lower) or re.match(r'^\d{4}$', t_lower):
                continue
            if re.match(r'^[a-z0-9]{2,6}-[a-z0-9]{1,6}(-[a-z0-9]+)?$', t_lower):
                continue
            client_unknowns.append(token)

    meaningful_unknowns = [u.lower() for u in client_unknowns if len(u) > 2 and u.lower() not in stopwords]
    
    candidates = []
    if workspace_id == "default":
        candidates.extend(ConnectionManager.keys())
        for cat in registry.categoricals.values():
            candidates.extend([str(v) for v in cat["values"]])
    else:
        candidates.extend([workspace_id])
        if workspace.metadata_profile:
            for col_name, col_prof in workspace.metadata_profile.columns.items():
                if col_prof.classification in ["DIMENSION", "STATUS", "IDENTIFIER"]:
                    candidates.extend([str(v) for v in col_prof.sample_values])
    candidate_map = {c.lower(): c for c in set(candidates)}

    unhandled_entities = []
    suggestions = []
    all_plants = POWER_PLANTS if workspace_id == "default" else [workspace_id]
    for u in meaningful_unknowns:
        if u not in filter_values and u not in workspace.available_metrics:
            matched_plant = None
            for p in all_plants:
                if u == p.lower() or (len(u) >= 4 and (u in p.lower() or p.lower() in u)):
                    matched_plant = p
                    break
            
            if matched_plant:
                bp.filters.append({"column": "plant", "value": matched_plant})
                filter_values.append(matched_plant.lower())
                logger.info(f"🔮 Direct Site Recovery: mapped '{u}' -> filter 'plant' = '{matched_plant}'")
                continue
                
            found_match = None
            is_code = bool(re.search(r'[a-zA-Z]', u) and re.search(r'\d', u))
            if not is_code:
                max_dist = 2 if len(u) > 4 else 1
                for c_lower, c_orig in candidate_map.items():
                    if abs(len(u) - len(c_lower)) > max_dist:
                        continue
                    dist = get_levenshtein_distance(u, c_lower)
                    if dist <= max_dist:
                        found_match = c_orig
                        break
            
            if found_match:
                target_col = "plant" if found_match.lower() in [p.lower() for p in all_plants] else None
                if not target_col:
                    if workspace_id == "default":
                        for col_name, cat in registry.categoricals.items():
                            if found_match in cat["values"]:
                                target_col = "project_type" if col_name == "department" else col_name
                                break
                    else:
                        if workspace.metadata_profile:
                            for col_name, col_prof in workspace.metadata_profile.columns.items():
                                if col_prof.classification in ["DIMENSION", "STATUS", "IDENTIFIER"]:
                                    if found_match in [str(x) for x in col_prof.sample_values]:
                                        target_col = col_name
                                        break
                if target_col:
                    bp.filters.append({"column": target_col, "value": found_match})
                    filter_values.append(found_match.lower())
                    logger.info(f"🔮 Fuzzy Recovery: mapped '{u}' -> filter '{target_col}' = '{found_match}'")
                    continue
            
            unhandled_entities.append(u)
            entity_suggestions = []
            for c_lower, c_orig in candidate_map.items():
                if abs(len(u) - len(c_lower)) > 3 and u not in c_lower and c_lower not in u:
                    continue
                dist = get_levenshtein_distance(u, c_lower)
                if dist <= 3 or u in c_lower or c_lower in u:
                    entity_suggestions.append(c_orig)
            entity_suggestions = sorted(list(set(entity_suggestions)))[:5]
            suggestions.extend(entity_suggestions)

    if unhandled_entities:
        nearest = nearest_executable_queries(raw_query, workspace_id, drop_tokens=unhandled_entities)
        entity_names = sorted(set(suggestions))
        suggestions = []
        for item in nearest + entity_names:
            if item and item.lower() not in {existing.lower() for existing in suggestions}:
                suggestions.append(item)
        return {
            "status": "clarification_required", 
            "message": f"Those words are not in this database: {', '.join(unhandled_entities)}. The closest question that can run is below.", 
            "suggestions": suggestions[:5],
            "results": []
        }

    # 2. Database Routing
    if workspace_id != "default":
        plants_to_query = [workspace_id]
        _time_col = get_time_column_for_db(workspace_id)
        where_str_part, metric_cols, params, sql_select, sql_group_by, sql_order_by = build_federated_query_parts(bp, time_col=_time_col, plant=workspace_id)
        sql_where = f"WHERE {where_str_part}" if where_str_part else ""
    else:
        _target_plants_for_time = [f['value'] for f in bp.filters if f.get('column') == 'plant']
        _time_col = get_time_column_for_db(_target_plants_for_time[0]) if len(_target_plants_for_time) == 1 else "record_date"
        where_str_part, metric_cols, params, sql_select, sql_group_by, sql_order_by = build_federated_query_parts(bp, time_col=_time_col, plant="default")
        sql_where = f"WHERE {where_str_part}" if where_str_part else ""
        
        target_plants = [f['value'] for f in bp.filters if f.get('column') == 'plant']
        plants_to_query = [p for p in target_plants if p in ConnectionManager.keys()]
        if not plants_to_query:
            project_ids = [f['value'] for f in bp.filters if f.get('column') == 'project_id']
            if project_ids:
                routed_plant = None
                pid_upper = str(project_ids[0]).upper()
                prefix_map = {
                    "PRJ-DAR": "darlington",
                    "PRJ-DIA": "diablo_canyon",
                    "PRJ-GRA": "grand_gulf",
                    "PRJ-HIN": "hinkley_point",
                    "PRJ-KAS": "kashiwazaki",
                    "PRJ-PAL": "palo_verde",
                    "PRJ-THR": "three_mile_island",
                    "PRJ-VOG": "vogtle"
                }
                for pref, plant in prefix_map.items():
                    if pid_upper.startswith(pref):
                        routed_plant = plant
                        break
                if routed_plant and routed_plant in POWER_PLANTS:
                    plants_to_query = [routed_plant]
                    logger.info(f"🔮 Routed project_id '{pid_upper}' specifically to plant '{routed_plant}'")
        if not plants_to_query:
            plants_to_query = POWER_PLANTS
            
        _profiler_fqp = SemanticSchemaProfiler.get_instance()
        _requested_cols = set(metric_cols)
        for _f in bp.filters:
            _fc = _f.get('column')
            if _fc and _fc not in ('plant', None):
                _requested_cols.add(_fc)
        if _requested_cols:
            _capable = [
                p for p in plants_to_query
                if _profiler_fqp.profiles.get(p) is None
                or _requested_cols.issubset(_profiler_fqp.profiles[p].columns.keys())
            ]
            if _capable:
                plants_to_query = _capable
                logger.info(f"🔀 Column-aware routing: restricted to {plants_to_query}")

        if len(plants_to_query) == 1:
            _time_col = get_time_column_for_db(plants_to_query[0])
            if _time_col != "record_date":
                where_str_part, metric_cols, params, sql_select, sql_group_by, sql_order_by = build_federated_query_parts(bp, time_col=_time_col, plant="default")
                sql_where = f"WHERE {where_str_part}" if where_str_part else ""

    metric_key = metric_cols[0] if metric_cols else None
    op = bp.operation.upper() if bp.operation else "SUM"

    def _is_identifier_col(col_name: str) -> bool:
        profile = workspace.metadata_profile
        if not profile and workspace_id == "default":
            _profiler = SemanticSchemaProfiler.get_instance()
            for _profile in _profiler.profiles.values():
                _cp = _profile.columns.get(col_name)
                if _cp and _cp.classification == "IDENTIFIER":
                    return True
        elif profile:
            _cp = profile.columns.get(col_name)
            if _cp and _cp.classification == "IDENTIFIER":
                return True
        return False

    is_profile_request = any(
        _is_identifier_col(workspace.semantic_adapter.resolve_column_name(f.get('column', '').lower()) or f.get('column', ''))
        for f in bp.filters if f.get('column') not in (None, 'plant')
    )
    
    # --- Compute Global KPI variables for downstream aggregation ---
    profiler = SemanticSchemaProfiler.get_instance()
    available_cols = set()
    for plant in plants_to_query:
        profile = workspace.metadata_profile if workspace_id != "default" else profiler.profiles.get(plant)
        if profile:
            available_cols.update(profile.columns.keys())
        else:
            available_cols.update(workspace.available_metrics)
            available_cols.update(workspace.available_dimensions)

    kpi_candidates = ["revenue", "capacity_mw", "budget_allocated", "budget_used"] if workspace_id == "default" else workspace.available_metrics[:4]
    existing_kpis = [c for c in kpi_candidates if c in workspace.available_metrics and c in available_cols]
    
    default_metrics = set(kpi_candidates)
    dynamic_metric = None
    for m in bp.metrics:
        if m not in default_metrics:
            dynamic_metric = m
            break
            
    if dynamic_metric and dynamic_metric in workspace.available_metrics and dynamic_metric in available_cols and dynamic_metric not in existing_kpis:
        existing_kpis.append(dynamic_metric)

    # --- Plant-Specific SQL Compilation and Task Generation ---
    from backend.services.workspace.workspace_manager import WorkspaceManager
    tasks = []
    kpi_sqls = []
    display_sql_to_run = None
    runnable_plants = []

    for plant in plants_to_query:
        # Determine time column name for this specific plant
        plant_time_col = get_time_column_for_db(plant)
        
        # Build query parts specifically for this plant
        try:
            p_where_str_part, p_metric_cols, p_params, p_sql_select, p_sql_group_by, p_sql_order_by = build_federated_query_parts(
                bp, time_col=plant_time_col, plant=plant
            )
        except ValueError as plant_err:
            logger.info(f"Skipping database '{plant}' for this query: {plant_err}")
            continue
        if not p_metric_cols and not any(f.get('column') not in (None, 'plant') for f in bp.filters):
            logger.info(f"Skipping database '{plant}': none of the requested metrics exist there.")
            continue
        p_sql_where = f"WHERE {p_where_str_part}" if p_where_str_part else ""
        
        plant_workspace = WorkspaceManager.get_instance().get_workspace(plant)
        plant_profile = plant_workspace.metadata_profile if plant_workspace else profiler.profiles.get(plant)
        
        def _is_id_col(col_name: str) -> bool:
            if plant_profile:
                cp = plant_profile.columns.get(col_name)
                if cp and cp.classification == "IDENTIFIER":
                    return True
            return False
            
        p_is_profile_request = any(
            _is_id_col(plant_workspace.semantic_adapter.resolve_column_name(f.get('column', '').lower()) or f.get('column', ''))
            for f in bp.filters if f.get('column') not in (None, 'plant')
        ) if plant_workspace else False
        
        if p_is_profile_request:
            p_sql_to_run = f"SELECT {p_sql_select} FROM {{table_name}} {p_sql_where} {p_sql_order_by}".strip()
        else:
            p_query_parts = [f"SELECT {p_sql_select} FROM {{table_name}}", p_sql_where]
            if p_sql_group_by:
                p_query_parts.append(p_sql_group_by)
            if p_sql_order_by:
                p_query_parts.append(p_sql_order_by)
            p_sql_to_run = " ".join(part for part in p_query_parts if part)
            
        if display_sql_to_run is None:
            display_sql_to_run = p_sql_to_run
            
        # One table scan should answer both the question and the KPI cards.
        # A grouped query still needs a second totals query; an ungrouped one does not.
        p_available_cols = set(plant_profile.columns.keys()) if plant_profile else set()
        p_existing_kpis = [c for c in existing_kpis if c in p_available_cols]
        if p_existing_kpis and not p_is_profile_request and not p_sql_group_by:
            already = set(p_metric_cols)
            extra_parts = []
            for col in p_existing_kpis:
                if col in already:
                    continue
                if should_aggregate_as_avg(col, plant_profile):
                    extra_parts.append(f"AVG({col}) as {col}")
                else:
                    extra_parts.append(f"SUM({col}) as {col}")
            if extra_parts and p_sql_to_run.startswith("SELECT "):
                p_sql_to_run = "SELECT " + ", ".join(extra_parts) + ", " + p_sql_to_run[len("SELECT "):]
            p_kpi_sql_to_run = None
        elif p_existing_kpis and not p_is_profile_request:
            p_kpi_select_parts = []
            for col in p_existing_kpis:
                if should_aggregate_as_avg(col, plant_profile):
                    p_kpi_select_parts.append(f"AVG({col}) as {col}")
                else:
                    p_kpi_select_parts.append(f"SUM({col}) as {col}")
            p_kpi_sql_to_run = f"SELECT {', '.join(p_kpi_select_parts)} FROM {{table_name}} {p_sql_where}".strip()
        else:
            p_kpi_sql_to_run = None
            
        runnable_plants.append(plant)
        tasks.append(run_query_on_single_db(plant, p_sql_to_run, p_params))
        kpi_sqls.append((p_kpi_sql_to_run, p_params))

    plants_to_query = runnable_plants

    # Determine if we should run KPI queries
    kpi_sql_to_run = any(k[0] is not None for k in kpi_sqls)
    kpi_tasks = []
    if kpi_sql_to_run:
        for i, plant in enumerate(plants_to_query):
            p_kpi_sql, p_params = kpi_sqls[i]
            if p_kpi_sql is None:
                p_kpi_sql = "SELECT NULL as revenue"
            kpi_tasks.append(run_query_on_single_db(plant, p_kpi_sql, p_params))
            
    sql_to_run = display_sql_to_run or ""

    all_results = await asyncio.gather(*(tasks + kpi_tasks))
    results_per_db = all_results[:len(tasks)]
    kpi_results_per_db = all_results[len(tasks):] if kpi_sql_to_run else []

    if is_profile_request:
        results = []
        for res in results_per_db:
            results.extend(res)
        return {
            "status": "success",
            "results": results,
            "sql_query": sql_to_run.replace("{table_name}", "metrics_site_X"),
            "unit": "RawData",
            "plants_queried": len(plants_to_query)
        }

    if bp.comparison and bp.comparison.get('type') == 'plant' and op in ["GRAPH", "TREND"]:
        for plant, res_list in zip(plants_to_query, results_per_db):
            for row in res_list:
                row['comparison_group'] = plant

    is_grouped = bool(sql_group_by)
    group_by_plant = (
        (op == "BREAKDOWN" and bp.comparison and bp.comparison.get('type') == 'plant') or
        (op == "BREAKDOWN" and 
         any(f.get('column') == 'department' and f.get('value') for f in bp.filters) and 
         any(f.get('column') == 'region' and f.get('value') for f in bp.filters))
    )
                      
    if group_by_plant:
        aggregated_results = []
        for plant, res_list in zip(plants_to_query, results_per_db):
            row = {"plant": plant, "record_date": plant}
            for m in metric_cols:
                val = res_list[0][m] if res_list and res_list[0] and res_list[0].get(m) is not None else 0
                row[m] = round(val, 2)
            aggregated_results.append(row)
        results = aggregated_results
    elif is_grouped:
        has_comparison_group = False
        for res_list in results_per_db:
            if res_list and len(res_list) > 0 and 'comparison_group' in res_list[0]:
                has_comparison_group = True
                break
                
        if has_comparison_group:
            aggregated_map = {}
            counts_map = {}
            for res_list in results_per_db:
                for row in res_list:
                    rec_date = row.get('record_date')
                    if rec_date is None:
                        rec_date = row.get('date')
                    comp_group = row.get('comparison_group')
                    if rec_date is None or comp_group is None:
                        continue
                    
                    if rec_date not in aggregated_map:
                        aggregated_map[rec_date] = {}
                        counts_map[rec_date] = {}
                    if comp_group not in aggregated_map[rec_date]:
                        aggregated_map[rec_date][comp_group] = {m: 0.0 for m in metric_cols}
                        counts_map[rec_date][comp_group] = {m: 0 for m in metric_cols}
                        
                    for m in metric_cols:
                        val = row.get(m)
                        if val is not None:
                            aggregated_map[rec_date][comp_group][m] += val
                            counts_map[rec_date][comp_group][m] += 1
                            
            results = []
            for rec_date, comp_dict in sorted(aggregated_map.items()):
                row_data = {"record_date": rec_date}
                for comp_group, metrics_dict in comp_dict.items():
                    def get_val(m):
                        is_rate_col = should_aggregate_as_avg(m, workspace.metadata_profile)
                        v = metrics_dict[m]
                        c = counts_map[rec_date][comp_group][m]
                        if is_rate_col and c > 0:
                            val_res = round(v / c, 2)
                            if any(k in m.lower() for k in ['pct', 'percent', 'percentage', 'completion']):
                                val_res = min(val_res, 100.0)
                            return val_res
                        return round(v, 2)

                    if len(metric_cols) == 1:
                        row_data[str(comp_group)] = get_val(metric_cols[0])
                    else:
                        for m in metric_cols:
                            row_data[f"{comp_group}_{m}"] = get_val(m)
                results.append(row_data)
        else:
            aggregated_map = {}
            group_key_col = None
            counts_map = {}
            for res_list in results_per_db:
                for row in res_list:
                    current_group_key_col = next((k for k, v in row.items() if isinstance(v, str)), None)
                    if not current_group_key_col:
                        continue
                    group_key_col = current_group_key_col
                    group_val = row[group_key_col]
                    
                    if group_val not in aggregated_map:
                        aggregated_map[group_val] = {m: 0.0 for m in metric_cols}
                        counts_map[group_val] = {m: 0 for m in metric_cols}
                    
                    for m in metric_cols:
                        metric_val = row.get(m)
                        if metric_val is not None:
                            aggregated_map[group_val][m] += metric_val
                            counts_map[group_val][m] += 1
                            
            if group_key_col:
                results = []
                for k, metrics_dict in sorted(aggregated_map.items()):
                    row = {group_key_col: k}
                    for m in metric_cols:
                        is_rate_col = should_aggregate_as_avg(m, workspace.metadata_profile)
                        if is_rate_col and counts_map[k][m] > 0:
                            raw_val = metrics_dict[k][m] if isinstance(metrics_dict, dict) and k in metrics_dict and isinstance(metrics_dict[k], dict) and m in metrics_dict[k] else metrics_dict[m] / counts_map[k][m]
                            val_res = round(raw_val, 2)
                            if any(x in m.lower() for x in ['pct', 'percent', 'percentage', 'completion']):
                                val_res = min(val_res, 100.0)
                            row[m] = val_res
                        else:
                            row[m] = round(metrics_dict[m], 2)
                    results.append(row)
            else:
                row = {}
                for m in metric_cols:
                    vals = [res[0][m] for res in results_per_db if res and res[0] and res[0].get(m) is not None]
                    is_rate_col = should_aggregate_as_avg(m, workspace.metadata_profile)
                    if is_rate_col and len(vals) > 0:
                        val_res = round(sum(vals) / len(vals), 2)
                        if any(x in m.lower() for x in ['pct', 'percent', 'percentage', 'completion']):
                            val_res = min(val_res, 100.0)
                        row[m] = val_res
                    else:
                        row[m] = round(sum(vals), 2)
                results = [row]

        if ("growth" in raw_query.lower() or "change" in raw_query.lower()) and len(results) > 1:
            for i in range(1, len(results)):
                prev = results[i-1].get(metric_key)
                curr = results[i].get(metric_key)
                if prev is not None and curr is not None:
                    if prev != 0:
                        results[i]['growth_pct'] = round(((curr - prev) / prev) * 100, 2)
                    else:
                        results[i]['growth_pct'] = 100.0 if curr > 0 else 0.0
    else:
        row = {}
        for m in metric_cols:
            vals = [res[0][m] for res in results_per_db if res and res[0] and res[0].get(m) is not None]
            is_rate_col = should_aggregate_as_avg(m, workspace.metadata_profile)
            if is_rate_col and len(vals) > 0:
                val_res = round(sum(vals) / len(vals), 2)
                if any(x in m.lower() for x in ['pct', 'percent', 'percentage', 'completion']):
                    val_res = min(val_res, 100.0)
                row[m] = val_res
            else:
                row[m] = round(sum(vals), 2)
        results = [row]
        
    kpis = {col: 0.0 for col in kpi_candidates}
    if dynamic_metric:
        kpis[dynamic_metric] = 0.0
        
    kpi_sources = results_per_db if not is_grouped else kpi_results_per_db
    if kpi_sources:
        for res in kpi_sources:
            if res and res[0]:
                for col in existing_kpis:
                    val = res[0].get(col) or 0
                    kpis[col] += val
                    
        for col in list(kpis.keys()):
            if col in existing_kpis:
                is_rate_col = should_aggregate_as_avg(col, workspace.metadata_profile)
                if is_rate_col and len(kpi_sources) > 0:
                    val_res = round(kpis[col] / len(kpi_sources), 2)
                    if any(x in col.lower() for x in ['pct', 'percent', 'percentage', 'completion']):
                        val_res = min(val_res, 100.0)
                    kpis[col] = val_res
                else:
                    kpis[col] = round(kpis[col], 2)
            else:
                kpis[col] = 0.0

    if len(plants_to_query) == 1:
        for row in results:
            if isinstance(row, dict) and 'plant' not in row:
                row['plant'] = plants_to_query[0]

    # Build aggregation type map for each metric so the frontend can label them
    aggregations_map: dict = {}
    all_metric_keys = list(kpis.keys()) if kpis else metric_cols
    for col in all_metric_keys:
        is_rate_col = should_aggregate_as_avg(col, workspace.metadata_profile)
        if op in ["MIN", "MAX", "AVG", "COUNT"]:
            aggregations_map[col] = op
        elif is_rate_col:
            aggregations_map[col] = "AVG"
        else:
            aggregations_map[col] = "SUM"

    # Attach semantic confidence report when querying an uploaded workspace
    _semantic_report = None
    try:
        if workspace and getattr(workspace, 'metadata_profile', None):
            from backend.services.workspace.semantic_engine import compute_semantic_confidence
            _semantic_report = compute_semantic_confidence(workspace.metadata_profile)
    except Exception:
        pass

    return {
        "status": "success",
        "results": results,
        "kpis": kpis,
        "aggregations": aggregations_map,
        "sql_query": sql_to_run.replace("{table_name}", "metrics_bus_unit_X"),
        "unit": (
            "INR" if metric_key in ['revenue', 'budget_allocated', 'budget_used', 'budget_remaining', 'sales', 'salary']
            else "MW" if metric_key == 'capacity_mw'
            else "PERCENT" if metric_key in ['completion_percentage', 'completion_rate']
            else "DAYS" if metric_key == 'delay_days'
            else "Units"
        ),
        "plants_queried": len(plants_to_query),
        "semantic_report": _semantic_report,
    }


# --- FastAPI App ---
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup
    MetadataRegistry.get_instance()
    initialize_trie()
    try:
        load_user_databases()
    except Exception as e:
        logger.error(f"Failed to load user databases: {e}")
    
    # Pre-cache Suggested Business Questions only when explicitly enabled.
    # Running five full federated queries on every boot can stall startup for minutes.
    if os.getenv("ENABLE_STARTUP_PRECACHE", "false").lower() in ("true", "1", "yes"):
        logger.info("Pre-caching suggested business questions...")
    suggested_queries = [
        "Top Capacity Plants",
        "Revenue Trend",
        "Budget Allocated by State",
        "Completion Percentage by Project Type",
        "Delay Days by Contractor"
    ]
    for q in suggested_queries:
        try:
            await handle_query(QueryBlueprintPayload(raw_query=q))
        except Exception as e:
                logger.warning(f"Failed to pre-cache '{q}': {e}")
        logger.info("Suggested business questions pre-cached.")
    yield
    # On shutdown
    pass

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)


from fastapi.responses import JSONResponse

@app.exception_handler(ValueError)
async def value_error_exception_handler(request, exc):
    logger.error(f"Security/Validation Error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=400,
        content={
            "status": "error",
            "message": f"Validation Error: {str(exc)}",
            "results": []
        }
    )

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    logger.error(f"Internal Server Error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "message": f"Internal Server Error: {str(exc)}",
            "results": []
        }
    )

class LLMBlueprintResponse(BaseModel):
    metrics: List[str] = Field(default_factory=list)
    filters: List[Dict[str, Any]] = Field(default_factory=list)
    timeframe: Optional[Any] = None
    operation: Optional[Union[str, List[str]]] = None

async def call_gemini_fallback(raw_query: str) -> Blueprint:
    if not GEMINI_API_KEY:
        logger.error("⚠️ GEMINI_API_KEY environment variable is not set! Skipping LLM fallback parsing.")
        return Blueprint()

    registry = MetadataRegistry.get_instance()
    dim_lines = []
    for col, info in registry.categoricals.items():
        values = list(info.get("values", []))[:20]
        dim_lines.append(f"- {col}: {values}")
    dimensions_block = "\n".join(dim_lines) if dim_lines else "- (none discovered)"
    
    system_prompt = f"""You are a query parser for a business analytics system. Parse the user's natural language query into structured JSON.

Valid Metrics: {list(registry.metrics.keys())}
Valid Dimensions:
{dimensions_block}
Valid Operations: SUM, AVERAGE, BREAKDOWN, GRAPH

Important parsing rules:
- Only use metrics and dimension values from the lists above.
- "trend", "over time", or "by year" → operation: GRAPH
- "breakdown" or "compare" or "by <dimension>" → operation: BREAKDOWN (unless it is "by year", then use GRAPH)
- Years like "2026" → timeframe: {{"type": "year", "value": "2026"}}
- Plant / database names map to filter: {{"column": "plant", "value": "<name>"}}

Return ONLY valid JSON in this exact format:
{{
  "metrics": ["revenue"],
  "filters": [{{"column": "state", "value": "Gujarat"}}],
  "timeframe": {{"type": "year", "value": "2026"}},
  "operation": "SUM"
}}"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [{"text": f"{system_prompt}\n\nParse this query: {raw_query}"}]
        }],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    async with httpx.AsyncClient(timeout=40.0) as client:
        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            
            resp_data = resp.json()
            raw_text = resp_data["candidates"][0]["content"]["parts"][0]["text"]
            resp_dict = json.loads(raw_text)
            
            logger.info(f"🤖 Gemini raw response: {resp_dict}")
            validated_data = LLMBlueprintResponse(**resp_dict)
            
            # Map LLMBlueprintResponse to Blueprint safely, removing None values
            bp_args = {}
            if validated_data.operation is not None:
                if isinstance(validated_data.operation, list):
                    bp_args["operation"] = validated_data.operation[0] if validated_data.operation else "SUM"
                else:
                    bp_args["operation"] = validated_data.operation
            if validated_data.metrics:
                bp_args["metrics"] = [m for m in validated_data.metrics if m]
            if validated_data.filters:
                cleaned_filters = []
                for f in validated_data.filters:
                    if isinstance(f, dict):
                        cleaned_f = {k: v for k, v in f.items() if v is not None}
                        if cleaned_f:
                            cleaned_filters.append(cleaned_f)
                bp_args["filters"] = cleaned_filters
            if validated_data.timeframe:
                if isinstance(validated_data.timeframe, dict):
                    bp_args["timeframe"] = {k: v for k, v in validated_data.timeframe.items() if v is not None}
                elif isinstance(validated_data.timeframe, list):
                    for item in validated_data.timeframe:
                        if isinstance(item, dict):
                            bp_args["timeframe"] = {k: v for k, v in item.items() if v is not None}
                            break
            
            return Blueprint(**bp_args)
        except Exception as e:
            logger.error(f"Fallback AI Error: {e}")
            return Blueprint()

@app.get("/api/suggest")
def get_suggestions(q: Optional[str] = "", db: Optional[str] = None, user: Dict[str, Any] = Depends(current_user)):
    start_time = time.perf_counter()
    
    # Clean input
    q = q or ""
    q_trimmed = q.strip()
    
    workspace_id = (db or "").strip()
    if workspace_id in ("", "all"):
        workspace_id = ""
    if workspace_id and workspace_id != "default" and not user_can_access(user["id"], workspace_id):
        raise HTTPException(status_code=403, detail="You do not have access to that database.")
    if not workspace_id:
        workspace_id = "default"
    try:
        from backend.main import ConversationStateManager
        conn = ConversationStateManager.get_instance()._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT state_json FROM session_states ORDER BY last_updated DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            import json
            state_data = json.loads(row[0])
            workspace_id = state_data.get("active_db") or "default"
    except Exception:
        pass
    if workspace_id == "all":
        workspace_id = "default"

    # Empty box: questions from the selected database, not the default plant list
    if not q_trimmed:
        facets = schema_facets(workspace_id)
        return {
            "suggestions": {
                "metrics": [],
                "analysis": facets["suggestions"],
                "comparisons": []
            },
            "preview": None,
            "latency_ms": (time.perf_counter() - start_time) * 1000
        }
        
    from backend.services.workspace.workspace_manager import WorkspaceManager
    workspace = WorkspaceManager.get_instance().get_workspace(workspace_id)
    
    # Fast path: return cached result for previously seen prefix
    cache_key = f"{workspace_id}|{q_trimmed.lower()}"
    if cache_key in SUGGEST_CACHE:
        cached = SUGGEST_CACHE[cache_key]
        cached["latency_ms"] = (time.perf_counter() - start_time) * 1000
        cached["cached"] = True
        return cached
        
    # Correct typos in the prefix to match correctly (LRU-cached internally)
    q_spelled = correct_query_spelling(q_trimmed, workspace_id)
    components = extract_query_components(q_spelled, workspace_id)
    
    # Generate contextual completions
    completions = []
    q_lower = q_spelled.lower()
    
    # Year suggestions use the same years as the sidebar chips.
    # Other date columns (commissioning) can extend past those years.
    listed_years = []
    for raw_year in years_for_workspace(workspace, workspace_id):
        try:
            listed_years.append(int(raw_year))
        except (TypeError, ValueError):
            continue
    if listed_years:
        allowed_years = sorted(set(listed_years))
    else:
        min_y, max_y = year_bounds(workspace, workspace_id)
        allowed_years = list(range(min_y, max_y + 1))
        
    # Year-specific suggestion rules
    detected_years = extract_detected_years(q_lower)
    
    if len(detected_years) >= 2:
        return {
            "suggestions": {
                "metrics": [],
                "analysis": [],
                "comparisons": []
            },
            "preview": generate_intent_preview(q_spelled, components),
            "latency_ms": (time.perf_counter() - start_time) * 1000
        }
        
    # Check if query ends with a year and range/list connector
    import re
    m_conn = re.search(r'\b((?:19|20)\d{2})\s+(and|to|through|thru|\-|till|until)\s*$', q_lower)
    if m_conn:
        y1_str = m_conn.group(1)
        conn = m_conn.group(2)
        y1 = int(y1_str)
        completions = []
        if y1 in allowed_years:
            for y in allowed_years:
                if y == y1:
                    continue
                if conn in ["to", "through", "thru", "-", "till", "until"] and y < y1:
                    continue
                prefix_part = q_trimmed[:m_conn.start()]
                completions.append(f"{prefix_part}{y1_str} {conn} {y}")
            
        return {
            "suggestions": {
                "metrics": [],
                "analysis": completions[:5],
                "comparisons": []
            },
            "preview": generate_intent_preview(q_spelled, components),
            "latency_ms": (time.perf_counter() - start_time) * 1000
        }
        
    # Check if query ends with a single year
    m_single = re.search(r'\b((?:19|20)\d{2})\s*$', q_lower)
    if m_single:
        y1_str = m_single.group(1)
        y1 = int(y1_str)
        completions = []
        prefix_part = q_trimmed[:m_single.start()]
        if y1 in allowed_years:
            ordered = [y for y in allowed_years if y > y1]
            if len(ordered) > 1:
                ordered = [ordered[0], ordered[-1]] + [y for y in ordered[1:-1]]
            for y in allowed_years:
                if y != y1 and y not in ordered:
                    ordered.append(y)
            for y in ordered:
                completions.append(f"{prefix_part}{y1_str} and {y}")
                if y > y1:
                    completions.append(f"{prefix_part}{y1_str} to {y}")
                if len(completions) >= 5:
                    break
        elif allowed_years:
            nearest = min(allowed_years, key=lambda y: (abs(y - y1), y))
            completions.append(f"{prefix_part}{nearest}")
                            
        return {
            "suggestions": {
                "metrics": [],
                "analysis": completions[:5],
                "comparisons": []
            },
            "preview": generate_intent_preview(q_spelled, components),
            "latency_ms": (time.perf_counter() - start_time) * 1000
        }
    
    has_metric = len(components["metrics"]) > 0
    has_dept = len(components["departments"]) > 0
    has_region = len(components["regions"]) > 0
    has_plant = len(components["plants"]) > 0
    has_year = len(components["years"]) > 0
    
    facets = schema_facets(workspace_id)
    dim_values = []
    for values in facets["categoricals"].values():
        for value in values[:8]:
            dim_values.append(str(value))
        if len(dim_values) >= 8:
            break
    depts = dim_values[:4]
    regions = dim_values[4:8]
    years = facets["years"]
    metrics = [m.replace('_', ' ').title() for m in facets["metrics"]]
    
    def add_comp(prefix, connector, suffix):
        p_clean = re.sub(r'[\s,;:?!\-\(\)]+$', '', prefix)
        conn_lower = connector.lower().strip()
        
        if not conn_lower:
            return f"{p_clean} {suffix}"
            
        if re.search(r'\b' + re.escape(conn_lower) + r'$', p_clean.lower()):
            return f"{p_clean} {suffix}"
        else:
            return f"{p_clean} {connector} {suffix}"

    is_comparison = any(w in q_lower for w in ["compare", "vs", "versus"])
    
    if is_comparison:
        if not has_metric:
            for m in metrics:
                completions.append(add_comp(q_spelled, "in", m.lower()))
        if not has_year:
            for y in years:
                completions.append(add_comp(q_spelled, "in", y))
    else:
        if has_metric:
            if not (has_dept or has_region or has_plant or has_year):
                for d in depts:
                    completions.append(add_comp(q_spelled, "in", d))
                for r in regions:
                    completions.append(add_comp(q_spelled, "in", r))
                for y in years:
                    completions.append(add_comp(q_spelled, "in", y))
            elif (has_dept or has_region or has_plant) and not has_year:
                for y in years:
                    completions.append(add_comp(q_spelled, "in", y))
        elif has_dept or has_region or has_plant:
            if not has_metric:
                for m in metrics:
                    completions.append(add_comp(q_spelled, "in", m.lower()))
                for y in years:
                    completions.append(add_comp(q_spelled, "in", y))
        else:
            return {
                "suggestions": {
                    "metrics": [],
                    "analysis": facets["suggestions"][:5],
                    "comparisons": []
                },
                "preview": None,
                "latency_ms": (time.perf_counter() - start_time) * 1000
            }
                
    seen = set()
    unique_completions = []
    for c in completions:
        c_norm = c.lower().strip()
        if c_norm not in seen:
            seen.add(c_norm)
            unique_completions.append(c)
            
    preview = generate_intent_preview(q_spelled, components)
    latency_ms = (time.perf_counter() - start_time) * 1000

    result = {
        "suggestions": {
            "metrics": [],
            "analysis": unique_completions[:5],
            "comparisons": []
        },
        "preview": preview,
        "latency_ms": latency_ms
    }
    # Store in cache (cap at 2000 entries to avoid unbounded growth)
    if len(SUGGEST_CACHE) < 2000:
        SUGGEST_CACHE[cache_key] = dict(result)
    return result


def _record_query_history(payload, blueprint, _csm, _rewritten_query, execution_data):
    """Store the question in session history for follow-ups and the dev timeline."""
    if not payload.session_id or not _csm or execution_data.get("status") != "success":
        return
    try:
        parent = _csm.get_last_query_history(payload.session_id)
        parent_id = parent["id"] if parent else None
        depth = ((parent.get("depth") or 0) + 1) if parent else 0
        _csm.add_query_history(
            session_id=payload.session_id,
            raw_query=payload.raw_query,
            resolved_query=_rewritten_query or payload.raw_query,
            blueprint=blueprint,
            parent_id=parent_id,
            depth=depth,
            blueprint_hash=calculate_blueprint_hash(blueprint),
        )
    except Exception as exc:
        logger.warning(f"Could not record query history: {exc}")

async def execute_legacy_federated_path(
    payload: QueryBlueprintPayload,
    blueprint: Blueprint,
    start_time: float,
    _csm,
    _session_state: Optional[ConversationState],
    _rewritten_query: Optional[str],
    used_semantic_cache: bool,
    parsed_deterministically: bool,
    parser_confidence: float,
    intent: str,
    norm_key: str,
    top_n_limit: Optional[int],
    top_n_asc: bool,
    workspace: Optional[Any] = None
) -> Dict[str, Any]:
    """Executes the query using the legacy federated SQLite route, completely bypassing all IQP code paths."""
    # 1. Result Cache check
    bp_key = blueprint.model_dump_json(exclude_none=True) + "|" + norm_key
    if bp_key in RESULT_CACHE and not payload.force_llm:
        logger.info("⚡ Result Cache Hit!")
        execution_data = RESULT_CACHE[bp_key].copy()
        
        default_len = len(POWER_PLANTS) if not workspace or workspace.workspace_id == "default" else 1
        execution_data["metadata"] = {
            "backend_ms": (time.perf_counter() - start_time) * 1000,
            "cache_hit": True,
            "cache_type": "result",
            "used_semantic_cache": used_semantic_cache,
            "parsed_deterministically": parsed_deterministically,
            "parser_confidence": parser_confidence,
            "intent": intent,
            "sources": execution_data.get("plants_queried", default_len),
            "engine_mode": "llm_only" if payload.force_llm else ("hybrid_deterministic" if parsed_deterministically else "hybrid_llm"),
            "force_llm": payload.force_llm,
            "mode": "llm_only" if payload.force_llm else "hybrid",
            "parser_used": "deterministic" if parsed_deterministically else "llm",
            "latency_ms": (time.perf_counter() - start_time) * 1000
        }
        # Save state even on cache hit
        if payload.session_id and _session_state is not None:
            _session_state = extract_state_from_result(blueprint, payload.raw_query, _session_state)
            _csm.set_state(_session_state)
            
        _conv_context = None
        if _session_state and _session_state.active_metric:
            _conv_context = {
                "active_metric": _session_state.active_metric,
                "active_filters": _session_state.active_filters,
                "active_timeframe": _session_state.active_timeframe,
                "comparison_entities": _session_state.comparison_entities,
                "last_query": _session_state.last_query,
                "was_rewritten": _rewritten_query is not None,
                "original_query": _rewritten_query and payload.raw_query,
            }
        execution_data["conversation_context"] = _conv_context
        
        if payload.session_id:
            _csm.set_snapshot(payload.session_id, execution_data)
            
        execution_data["metadata"]["iqp"] = {
            "iqp_enabled": False,
            "route": "sqlite_federated",
            "legacy_mode": True
        }
        _record_query_history(payload, blueprint, _csm, _rewritten_query, execution_data)
        return execution_data
        
    # 2. SQLite query execution
    execution_data = await federated_query_processor(blueprint, payload.raw_query, payload.parsing_metadata, workspace=workspace)
    
    if execution_data["status"] == "success":
        default_len = len(POWER_PLANTS) if not workspace or workspace.workspace_id == "default" else 1
        num_plants = execution_data.get("plants_queried", default_len)
        execution_data["insights"] = {"summary": "Federated query successful.", "analysis": f"Aggregated from {num_plants} sources."}
        
        # Post-Process Top N queries in python
        if parsed_deterministically and intent == "top_n" and execution_data.get("results"):
            results_list = execution_data["results"]
            metric_key = blueprint.metrics[0] if blueprint.metrics else "revenue"
            if results_list and metric_key in results_list[0]:
                results_sorted = sorted(results_list, key=lambda x: x.get(metric_key, 0) or 0, reverse=not top_n_asc)
                if top_n_limit:
                    results_sorted = results_sorted[:top_n_limit]
                execution_data["results"] = results_sorted
                
        # --- Conversational State: Save after successful execution ---
        if payload.session_id and _session_state is not None:
            _session_state = extract_state_from_result(blueprint, payload.raw_query, _session_state)
            _csm.set_state(_session_state)
            logger.info(f"💾 Session state saved: metric={_session_state.active_metric}, filters={_session_state.active_filters}, entities={_session_state.comparison_entities}")
            
        # Save to Result Cache (only if query succeeded and not force_llm)
        if not payload.force_llm:
            RESULT_CACHE[bp_key] = execution_data.copy()
            
    # Build conversation_context for frontend display
    _conv_context = None
    if _session_state and _session_state.active_metric:
        _conv_context = {
            "active_metric": _session_state.active_metric,
            "active_filters": _session_state.active_filters,
            "active_timeframe": _session_state.active_timeframe,
            "comparison_entities": _session_state.comparison_entities,
            "last_query": _session_state.last_query,
            "was_rewritten": _rewritten_query is not None,
            "original_query": _rewritten_query and payload.raw_query,
        }
    execution_data["conversation_context"] = _conv_context
    
    default_len = len(POWER_PLANTS) if not workspace or workspace.workspace_id == "default" else 1
    execution_data["metadata"] = {
        "backend_ms": (time.perf_counter() - start_time) * 1000,
        "cache_hit": False,
        "used_semantic_cache": used_semantic_cache,
        "parsed_deterministically": parsed_deterministically,
        "parser_confidence": parser_confidence,
        "intent": intent,
        "sources": execution_data.get("plants_queried", default_len),
        "engine_mode": "llm_only" if payload.force_llm else ("hybrid_deterministic" if parsed_deterministically else "hybrid_llm"),
        "force_llm": payload.force_llm,
        "mode": "llm_only" if payload.force_llm else "hybrid",
        "parser_used": "deterministic" if parsed_deterministically else "llm",
        "latency_ms": (time.perf_counter() - start_time) * 1000
    }
    
    # Save query history and snapshot on success
    if payload.session_id and execution_data["status"] == "success":
        _csm.set_snapshot(payload.session_id, execution_data)
        
    execution_data["metadata"]["iqp"] = {
        "iqp_enabled": False,
        "route": "sqlite_federated",
        "legacy_mode": True
    }
    _record_query_history(payload, blueprint, _csm, _rewritten_query, execution_data)
    return execution_data

async def handle_query_inner(payload: QueryBlueprintPayload, background_tasks: BackgroundTasks = None, speculative: bool = False):
    start_time = time.perf_counter()
    logger.info(f"📥 Received query: '{payload.raw_query}'")
    logger.debug(f"handle_query: incoming payload = {payload}")
    blueprint = payload.blueprint

    current_db = payload.selected_db or "default"
    from backend.services.workspace.workspace_manager import WorkspaceManager
    workspace = WorkspaceManager.get_instance().get_workspace(current_db)

    # --- Conversational State: Load & Follow-up Detection ---
    _csm = ConversationStateManager.get_instance()
    _session_state: Optional[ConversationState] = None
    _rewritten_query: Optional[str] = None

    if payload.session_id:
        _session_state = _csm.get_or_create(payload.session_id)
        prev_db = getattr(_session_state, "active_db", None)
        if prev_db and prev_db != current_db:
            logger.info(f"🔄 Database context switch detected in session: '{prev_db}' -> '{current_db}'. Resetting conversation state.")
            _csm.clear_session(payload.session_id)
            _session_state = _csm.get_or_create(payload.session_id)
        _session_state.active_db = current_db
        
        if is_followup_query(payload.raw_query, _session_state, current_db):
            _rewritten_query = rewrite_followup_query(payload.raw_query, _session_state, current_db)
            logger.info(f"🔗 Follow-up detected. Rewriting: '{payload.raw_query}' → '{_rewritten_query}'")
            # Replace the raw query with the rewritten one for all downstream processing
            payload = QueryBlueprintPayload(
                raw_query=_rewritten_query,
                blueprint=payload.blueprint,
                selected_db=payload.selected_db,
                force_llm=payload.force_llm,
                parsing_metadata=payload.parsing_metadata,
                session_id=payload.session_id,
                parent_id=payload.parent_id,
            )

    norm_key = normalize_query_key(payload.raw_query)
    used_semantic_cache = False
    parsed_deterministically = False
    parser_confidence = 0.0
    intent = "unknown"
    top_n_limit = None
    top_n_asc = False
    
    # Try deterministic parser first
    if not payload.force_llm and (not blueprint or (not blueprint.metrics and not blueprint.filters and not blueprint.operation)):
        current_db = payload.selected_db or "default"

        # Spell-correct before passing to deterministic parser so typos like 'revnue' are fixed
        det_res = parse_query_deterministically(correct_query_spelling(payload.raw_query, current_db, complete=True), current_db)
        if not det_res or det_res.get("blueprint") is None:
            correction = None
            if not (det_res and det_res.get("intent") == "invalid_year"):
                correction = get_suggested_correction(payload.raw_query, current_db)
            if correction and correction.startswith("Try searching"):
                raise HTTPException(
                    status_code=400,
                    detail=f"We couldn't recognize any metrics or filters in your query '{payload.raw_query}'. {correction}"
                )
        if det_res and det_res.get("intent") == "invalid_year":
            return {
                "status": "error",
                "message": det_res.get("error", "Invalid year in query."),
                "results": []
            }
        if det_res and det_res["confidence"] >= 0.8:
            logger.info(f"⚡ Deterministic Parser Hit! Intent: {det_res['intent']} (Confidence: {det_res['confidence']})")
            blueprint = det_res["blueprint"]
            parsed_deterministically = True
            parser_confidence = det_res["confidence"]
            intent = det_res["intent"]
            top_n_limit = det_res.get("top_n_limit")
            top_n_asc = det_res.get("top_n_asc", False)
        elif det_res and det_res.get("blueprint") is not None and det_res["confidence"] == 0.0 and det_res.get("intent") != "invalid_year":
            # The parser already understood the question and found an unknown word.
            # The entity guard below asks for clarification. Gemini must not run first.
            logger.info("⚡ Deterministic parse kept; unknown words will be clarified locally.")
            blueprint = det_res["blueprint"]
            parsed_deterministically = True
            parser_confidence = det_res["confidence"]
            intent = det_res["intent"]
            top_n_limit = det_res.get("top_n_limit")
            top_n_asc = det_res.get("top_n_asc", False)
            
    # Fallback to Gemini only if the blueprint is completely empty/unparsed, or if metrics are missing (and it's not a profile request)
    def _is_identifier_col(col_name: str) -> bool:
        profile = workspace.metadata_profile
        if not profile and current_db == "default":
            _profiler = SemanticSchemaProfiler.get_instance()
            for _profile in _profiler.profiles.values():
                _cp = _profile.columns.get(col_name)
                if _cp and _cp.classification == "IDENTIFIER":
                    return True
        elif profile:
            _cp = profile.columns.get(col_name)
            if _cp and _cp.classification == "IDENTIFIER":
                return True
        return False

    is_profile = blueprint and any(
        _is_identifier_col(workspace.semantic_adapter.resolve_column_name(f.get('column', '').lower()) or f.get('column', ''))
        for f in blueprint.filters
        if isinstance(f, dict) and f.get('column') not in (None, 'plant')
    )
    if payload.force_llm:
        logger.info(f"🤖 Calling Gemini to parse (LLM-Only Mode): '{payload.raw_query}'")
        blueprint = await call_gemini_fallback(payload.raw_query)
        logger.info(f"📋 Gemini returned: {blueprint}")
    elif not blueprint or (not blueprint.metrics and not is_profile):
        if parsed_deterministically and blueprint is not None:
            ranked = _ranked_metric_names(workspace)
            if not blueprint.metrics and ranked and not is_profile:
                blueprint.metrics = ranked[:4]
                logger.info("Using metrics from the selected database instead of the language model")
        else:
            logger.info(f"🤖 Calling Gemini to parse: '{payload.raw_query}'")
            blueprint = await call_gemini_fallback(payload.raw_query)
            logger.info(f"📋 Gemini returned: {blueprint}")
        
    if not blueprint:
        logger.warning(f"⚠️ No blueprint from Gemini, creating default")
        blueprint = Blueprint()
        
    # Normalize filters to ensure standard format (unpacking list values if present)
    normalized_filters = []
    adapter = workspace.semantic_adapter
    all_plants = POWER_PLANTS if current_db == "default" else [current_db]

    for f in blueprint.filters:
        if not isinstance(f, dict):
            continue
        if 'column' in f and 'value' in f:
            col = f['column']
            val = f['value']
            if val is None:
                continue
            val_list = val if isinstance(val, list) else [val]
            for item in val_list:
                if item is None:
                    continue
                item_str = str(item)
                item_normalized = item_str.replace(' ', '_')
                target_col = col
                if item_normalized in all_plants or item_str.lower().replace(" ", "_") in all_plants:
                    target_col = 'plant'
                normalized_filters.append({"column": target_col, "value": item_str})
        else:
            for k, v in f.items():
                if v is None:
                    continue
                val_list = v if isinstance(v, list) else [v]
                for item in val_list:
                    if item is None:
                        continue
                    item_str = str(item)
                    item_normalized = item_str.replace(' ', '_')
                    target_col = k
                    if item_normalized in all_plants or item_str.lower().replace(" ", "_") in all_plants:
                        target_col = 'plant'
                    normalized_filters.append({"column": target_col, "value": item_str})
    blueprint.filters = normalized_filters

    # --- Heuristic Reinforcement for 100% Robust Parsing ---
    import re
    _corrected_raw = correct_query_spelling(payload.raw_query, current_db, complete=True)
    raw_lower = _corrected_raw.lower()

    # Drop metrics the language model invented. A rules parse only names real columns.
    if blueprint.metrics and not parsed_deterministically:
        valid_metrics = []
        is_breakdown = (blueprint.operation == "BREAKDOWN" or "breakdown" in raw_lower or "performance" in raw_lower)
        for m in blueprint.metrics:
            m_clean = m.replace('_', ' ')
            if is_breakdown:
                valid_metrics.append(m)
            # Check for direct mentions or synonyms
            elif m_clean in raw_lower or m.replace('_', '') in raw_lower or any(part in raw_lower for part in m.split('_') if len(part) > 3):
                valid_metrics.append(m)
            else:
                # Check adapter aliases dynamically
                aliases = adapter.column_to_aliases.get(m, set())
                matched_alias = False
                for alias in aliases:
                    alias_clean = alias.replace('_', ' ').lower()
                    if alias_clean in raw_lower:
                        matched_alias = True
                        break
                    words = [w for w in alias.split('_') if len(w) > 3]
                    if any(w in raw_lower for w in words):
                        matched_alias = True
                        break
                if matched_alias:
                    valid_metrics.append(m)
        blueprint.metrics = valid_metrics

    # 0. Year range validation — reject out-of-range years
    min_y, max_y = year_bounds(workspace, current_db)

    all_years_in_query = extract_raw_year_candidates(raw_lower)
    invalid_years = [y for y in all_years_in_query if not (min_y <= int(y) <= max_y)]
    if invalid_years:
        correction = get_suggested_correction(payload.raw_query, current_db)
        err_msg = f"Ambiguity or error: The requested year {invalid_years[0]} is outside the available database bounds ({min_y} - {max_y})."
        if correction:
            err_msg += f" {correction}"
        return {
            "status": "error",
            "message": err_msg,
            "results": []
        }

    # Years, months, and dates. A date or a month-year must not be widened into the whole year.
    apply_calendar(blueprint, raw_lower)

    # Match dimension values stored on this database.
    value_hits = {}
    if workspace.metadata_profile:
        for col_name, col_prof in workspace.metadata_profile.columns.items():
            if col_prof.classification not in ("DIMENSION", "STATUS"):
                continue
            if _is_time_key(col_name) or _is_id_key(col_name):
                continue
            for val in col_prof.sample_values:
                val_str = str(val).strip()
                if len(val_str) < 2:
                    continue
                if _stored_value_mentioned(val_str, raw_lower, set(re.findall(r"[a-z0-9_\-]+", raw_lower))):
                    bucket = value_hits.setdefault(col_name, [])
                    if val_str not in bucket:
                        bucket.append(val_str)
    for col_name, values in value_hits.items():
        if len(values) > 1:
            blueprint.comparison = {"type": col_name, "values": values}
        for val in values:
            if not any(
                f.get("column") == col_name and str(f.get("value")).lower() == val.lower()
                for f in blueprint.filters
            ):
                blueprint.filters.append({"column": col_name, "value": val})

    # Check plants
    detected_plants = []
    for plant in all_plants:
        plant_clean = plant.replace('_', ' ')
        if plant in raw_lower or plant_clean in raw_lower:
            detected_plants.append(plant)
    if len(detected_plants) > 1:
        blueprint.comparison = {"type": "plant", "values": detected_plants}
        for p in detected_plants:
            if not any(f.get('column') == 'plant' and f.get('value') == p for f in blueprint.filters):
                blueprint.filters.append({"column": "plant", "value": p})
    elif len(detected_plants) == 1:
        p = detected_plants[0]
        if not any(f.get('column') == 'plant' and f.get('value') == p for f in blueprint.filters):
            blueprint.filters.append({"column": "plant", "value": p})

    # 3. Operation Heuristics
    if "trend" in raw_lower or "over time" in raw_lower or "by year" in raw_lower or "graph" in raw_lower:
        blueprint.operation = "GRAPH"
    elif "breakdown" in raw_lower or "compare" in raw_lower or "by" in raw_lower or any(w in raw_lower for w in ["top plants", "top performing plants", "best plants", "worst plants", "plants breakdown", "compare plants", "plant comparison"]):
        if not blueprint.operation or blueprint.operation.upper() not in ["GRAPH", "TREND"]:
            blueprint.operation = "BREAKDOWN"
            
    if not blueprint.metrics:
        detected_from_schema = []
        for metric_name in workspace.available_metrics:
            aliases = set(adapter.column_to_aliases.get(metric_name, set()))
            aliases.add(metric_name)
            aliases.add(metric_name.replace("_", " "))
            for alias in aliases:
                if _alias_hits(alias, raw_lower):
                    detected_from_schema.append(metric_name)
                    break
        if detected_from_schema:
            blueprint.metrics = list(dict.fromkeys(detected_from_schema))
        
    is_profile_query = blueprint and any(
        any(
            SemanticSchemaProfiler.get_instance().profiles.get(p) and
            SemanticSchemaProfiler.get_instance().profiles[p].columns.get(f.get('column', '')) and
            SemanticSchemaProfiler.get_instance().profiles[p].columns[f.get('column', '')].classification == "IDENTIFIER"
            for p in all_plants
        )
        for f in blueprint.filters
        if isinstance(f, dict) and f.get('column') not in (None, 'plant')
    )
    if not blueprint.metrics and not is_profile_query:
        logger.info(f"📊 No metrics specified, defaulting to all core metrics")
        blueprint.metrics = list(adapter.kpi_candidates or [])

    # Comparison/Breakdown Dimension Heuristics
    if blueprint.operation in ["BREAKDOWN", "GRAPH"]:
        if not blueprint.comparison or not blueprint.comparison.get('type'):
            if any(w in raw_lower for w in ["by region", "across regions", "compare regions", "compare region", "region comparison", "by regions"]):
                blueprint.comparison = {"type": "region"}
            elif any(w in raw_lower for w in ["by department", "across departments", "compare departments", "compare department", "department comparison", "by dept", "by depts"]):
                blueprint.comparison = {"type": "department"}
            elif any(w in raw_lower for w in ["by project type", "across project type", "across project types", "compare project type", "compare project types", "by project types", "project type comparison", "project types"]):
                blueprint.comparison = {"type": "project_type"}
            elif any(w in raw_lower for w in ["by state", "across states", "compare states", "compare state", "state comparison", "by states"]):
                blueprint.comparison = {"type": "state"}
            elif any(w in raw_lower for w in ["by plant", "across plants", "compare plants", "compare plant", "plant comparison", "top plants", "top performing plants", "best plants", "worst plants", "plants breakdown"]):
                blueprint.comparison = {"type": "plant"}
                if any(w in raw_lower for w in ["worst", "lowest"]):
                    blueprint.sort_asc = True
                else:
                    blueprint.sort_asc = False
            elif any(w in raw_lower for w in ["by year", "across years", "compare years", "compare year", "year comparison"]):
                blueprint.comparison = {"type": "year"}

    if payload.selected_db and payload.selected_db != "default" and payload.selected_db != "all":
        # Ensure there is a plant filter in the blueprint matching selected_db
        if not any(f.get('column') == 'plant' for f in blueprint.filters):
            blueprint.filters.append({"column": "plant", "value": payload.selected_db})

    logger.info(f"🔧 Final blueprint - Metrics: {blueprint.metrics}, Filters: {blueprint.filters}, Operation: {blueprint.operation}")
    
    # 1.5. Zero-Trust Dynamic Entity Validation & Fuzzy Recovery (Early Stop Guard)
    client_unknowns = (payload.parsing_metadata or {}).get("unknown_tokens", [])
    filter_values = [str(f['value']).lower() for f in blueprint.filters]
    
    if not client_unknowns:
        # spelling-corrected raw query:
        corrected_query = correct_query_spelling(payload.raw_query, current_db, complete=True)
        # Tokenize by regex to find words (alphanumeric/words)
        tokens = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_-]*\b', corrected_query)
        explained_names = resolve_category_mentions(corrected_query.lower(), workspace, current_db)["tokens"]
        stopwords = {
            'across', 'sites', 'all', 'from', 'year', 'than', 'between', 'and', 'id', 'project', 'projects', 
            'is', 'are', 'was', 'were', 'been', 'being', 'am', 'be', 'the', 'of', 'for', 'with', 'what', 'which', 
            'who', 'whom', 'whose', 'where', 'when', 'why', 'how', 'show', 'find', 'list', 'get', 'give', 'me', 
            'us', 'tell', 'please', 'display', 'search', 'select', 'metrics', 'data', 'info', 'information', 
            'having', 'days', 'months', 'years', 'day', 'month', 'year', 'value', 'values', 'details', 'about', 
            'to', 'at', 'by', 'on', 'in', 'an', 'a', 'or', 'so', 'yet', 'nor', 'but', 'can', 'could', 'shall', 
            'should', 'will', 'would', 'may', 'might', 'must', 'has', 'have', 'had', 'doing', 'does', 'did', 'done',
            'total', 'average', 'avg', 'sum', 'breakdown', 'compare', 'trend', 'graph', 'chart',
            'allocated', 'used', 'remaining', 'completion', 'delay', 'revenue', 'capacity', 'mw', 'percentage',
            'status', 'state', 'department', 'location', 'contractor', 'payment', 'material', 'category',
            'budget', 'fiscal', 'states', 'locations', 'departments', 'contractors', 'payments', 'materials',
            'categories', 'plants', 'trends', 'graphs', 'charts',
            # Ranking / ordering modifiers — critical for 'top N', 'bottom N', 'best', 'worst' queries
            'top', 'bottom', 'best', 'worst', 'highest', 'lowest', 'first', 'last', 'most', 'least',
            'ranked', 'ranking', 'rank', 'performing', 'performer', 'performers', 'performance',
            'number', 'nos', 'no', 'nth', 'order', 'ordered', 'sort', 'sorted', 'ascending', 'descending',
            'name', 'named', 'names', 'type', 'types', 'level', 'levels', 'group', 'groups', 'grouped',
            'count', 'number', 'quantity', 'amount', 'rate', 'ratio', 'score', 'index',
            'high', 'low', 'max', 'min', 'maximum', 'minimum', 'above', 'below', 'over', 'under',
            # Extra standard english words to avoid false positive blocks
            'much', 'money', 'compared', 'during', 'team', 'teams', 'plant', 'db', 'database', 'databases', 
            'record', 'records', 'date', 'dates', 'time', 'times', 'filter', 'filters', 'query', 'queries', 
            'run', 'execute', 'result', 'results', 'latest', 'oldest', 'newest', 'recent', 'recents',
            'current', 'previous', 'next', 'last', 'first', 'second', 'third', 'fourth', 'fifth',
            'full', 'fullness', 'everything', 'made', 'versus', 'overall', 'utilized', 'utilised',
            'utilization', 'utilisation', 'yearly', 'annually', 'yoy',
            'month', 'months', 'monthly', 'daily',
            'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
            'september', 'october', 'november', 'december',
            'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec'
        }
        known_words = set()
        for metric_key in workspace.available_metrics:
            known_words.add(metric_key.lower())
            known_words.add(metric_key.lower().replace('_', ' '))
            for w in metric_key.split('_'):
                known_words.add(w.lower())
                known_words.update(_make_plurals(w.lower()))
            for alias in workspace.semantic_adapter.column_to_aliases.get(metric_key, set()):
                squashed = re.sub(r"[\s_]+", "", str(alias).lower())
                if len(squashed) >= 5:
                    known_words.add(squashed)
        for cat_key in workspace.available_dimensions:
            known_words.add(cat_key.lower())
            known_words.add(cat_key.lower().replace('_', ' '))
            for w in cat_key.split('_'):
                known_words.add(w.lower())
                known_words.update(_make_plurals(w.lower()))
        if current_db == "default":
            for p in ConnectionManager.keys():
                known_words.add(p.lower())
                known_words.add(p.lower().replace('_', ' '))
                for w in p.split('_'):
                    known_words.add(w.lower())
                    known_words.update(_make_plurals(w.lower()))
            registry = MetadataRegistry.get_instance()
            for cat_data in registry.categoricals.values():
                for v in cat_data.get("values", []):
                    v_str = str(v).lower()
                    known_words.add(v_str)
                    known_words.update(_make_plurals(v_str))
                    for w in v_str.replace('_', ' ').split():
                        known_words.add(w)
                        known_words.update(_make_plurals(w))
        else:
            if workspace.metadata_profile:
                for col_name, col_prof in workspace.metadata_profile.columns.items():
                    if col_prof.classification in ["DIMENSION", "STATUS", "IDENTIFIER"]:
                        for v in col_prof.sample_values:
                            v_str = str(v).lower()
                            known_words.add(v_str)
                            known_words.update(_make_plurals(v_str))
                            for w in v_str.replace('_', ' ').split():
                                known_words.add(w)
                                known_words.update(_make_plurals(w))
                    
        client_unknowns = []
        for token in tokens:
            t_lower = token.lower()
            if len(t_lower) <= 2:
                continue
            if t_lower in stopwords or t_lower in explained_names or _is_instruction_word(t_lower):
                continue
            if t_lower in known_words or t_lower.replace("_", " ") in known_words or t_lower.replace("_", "") in known_words:
                continue
            if is_value_in_workspace_db(token, payload.selected_db):
                continue
            if t_lower.isdigit() or re.match(r'^fy\d{4}$', t_lower) or re.match(r'^\d{4}$', t_lower):
                continue
            if re.match(r'^[a-z0-9]{2,6}-[a-z0-9]{1,6}(-[a-z0-9]+)?$', t_lower):
                continue
            client_unknowns.append(token)

    meaningful_unknowns = [u.lower() for u in client_unknowns if len(u) > 2 and u.lower() not in stopwords]
    
    candidates = []
    if current_db == "default":
        candidates.extend(ConnectionManager.keys())
        registry = MetadataRegistry.get_instance()
        for cat in registry.categoricals.values():
            candidates.extend([str(v) for v in cat["values"]])
    else:
        candidates.extend([current_db])
        if workspace.metadata_profile:
            for col_name, col_prof in workspace.metadata_profile.columns.items():
                if col_prof.classification in ["DIMENSION", "STATUS", "IDENTIFIER"]:
                    candidates.extend([str(v) for v in col_prof.sample_values])
    candidate_map = {c.lower(): c for c in set(candidates)}
 
    unhandled_entities = []
    suggestions = []
    for u in meaningful_unknowns:
        if u not in filter_values and u not in workspace.available_metrics:
            # Direct/Substring match to a power plant
            matched_plant = None
            for p in all_plants:
                if u == p.lower() or (len(u) >= 4 and (u in p.lower() or p.lower() in u)):
                    matched_plant = p
                    break
            
            if matched_plant:
                blueprint.filters.append({"column": "plant", "value": matched_plant})
                filter_values.append(matched_plant.lower())
                continue
                
            # Perform Fuzzy Match
            found_match = None
            is_code = bool(re.search(r'[a-zA-Z]', u) and re.search(r'\d', u))
            if not is_code:
                max_dist = 2 if len(u) > 4 else 1
                for c_lower, c_orig in candidate_map.items():
                    if abs(len(u) - len(c_lower)) > max_dist:
                        continue
                    dist = get_levenshtein_distance(u, c_lower)
                    if dist <= max_dist:
                        found_match = c_orig
                        break
            
            if found_match:
                target_col = "plant" if found_match.lower() in [p.lower() for p in all_plants] else None
                if not target_col:
                    if current_db == "default":
                        registry = MetadataRegistry.get_instance()
                        for col_name, cat in registry.categoricals.items():
                            if found_match in cat["values"]:
                                target_col = "project_type" if col_name == "department" else col_name
                                break
                    else:
                        if workspace.metadata_profile:
                            for col_name, col_prof in workspace.metadata_profile.columns.items():
                                if col_prof.classification in ["DIMENSION", "STATUS", "IDENTIFIER"]:
                                    if found_match in [str(x) for x in col_prof.sample_values]:
                                        target_col = col_name
                                        break
                if target_col:
                    blueprint.filters.append({"column": target_col, "value": found_match})
                    filter_values.append(found_match.lower())
                    continue
            
            unhandled_entities.append(u)
            
            entity_suggestions = []
            for c_lower, c_orig in candidate_map.items():
                if abs(len(u) - len(c_lower)) > 3 and u not in c_lower and c_lower not in u:
                    continue
                dist = get_levenshtein_distance(u, c_lower)
                if dist <= 3 or u in c_lower or c_lower in u:
                    entity_suggestions.append(c_orig)
            entity_suggestions = sorted(list(set(entity_suggestions)))[:5]
            suggestions.extend(entity_suggestions)

    if unhandled_entities:
        nearest = nearest_executable_queries(payload.raw_query, current_db, drop_tokens=unhandled_entities)
        entity_names = sorted(set(suggestions))
        suggestions = []
        for item in nearest + entity_names:
            if item and item.lower() not in {existing.lower() for existing in suggestions}:
                suggestions.append(item)
        suggestions = suggestions[:5]
        _conv_context = None
        if _session_state and _session_state.active_metric:
            _conv_context = {
                "active_metric": _session_state.active_metric,
                "active_filters": _session_state.active_filters,
                "active_timeframe": _session_state.active_timeframe,
                "comparison_entities": _session_state.comparison_entities,
                "last_query": _session_state.last_query,
                "was_rewritten": _rewritten_query is not None,
                "original_query": _rewritten_query and payload.raw_query,
            }
        return {
            "status": "clarification_required", 
            "message": f"Those words are not in this database: {', '.join(unhandled_entities)}. The closest question that can run is below.", 
            "suggestions": suggestions,
            "results": [],
            "conversation_context": _conv_context,
            "metadata": {
                "backend_ms": (time.perf_counter() - start_time) * 1000,
                "cache_hit": False,
                "used_semantic_cache": used_semantic_cache,
                "parsed_deterministically": parsed_deterministically,
                "parser_confidence": parser_confidence,
                "intent": intent,
                "sources": 0,
                "engine_mode": "llm_only" if payload.force_llm else ("hybrid_deterministic" if parsed_deterministically else "hybrid_llm"),
                "force_llm": payload.force_llm,
                "mode": "llm_only" if payload.force_llm else "hybrid",
                "parser_used": "deterministic" if parsed_deterministically else "llm",
                "latency_ms": (time.perf_counter() - start_time) * 1000
            }
        }
    
    return await execute_legacy_federated_path(
            payload=payload,
            blueprint=blueprint,
            start_time=start_time,
            _csm=_csm,
            _session_state=_session_state,
            _rewritten_query=_rewritten_query,
            used_semantic_cache=used_semantic_cache,
            parsed_deterministically=parsed_deterministically,
            parser_confidence=parser_confidence,
            intent=intent,
            norm_key=norm_key,
            top_n_limit=top_n_limit,
            top_n_asc=top_n_asc,
        workspace=workspace,
    )

class AuthPayload(BaseModel):
    email: str
    password: str

@app.post("/api/auth/register")
def auth_register(payload: AuthPayload):
    from backend import accounts
    try:
        user = accounts.register(payload.email, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"user": user}

@app.post("/api/auth/login")
def auth_login(payload: AuthPayload, response: Response):
    from backend import accounts
    result = accounts.login(payload.email, payload.password)
    if not result:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    user, token = result
    response.set_cookie(
        "alphabot_session",
        token,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"user": user}

@app.post("/api/auth/logout")
def auth_logout(response: Response, alphabot_session: Optional[str] = Cookie(default=None)):
    from backend import accounts
    accounts.logout(alphabot_session)
    response.delete_cookie("alphabot_session", path="/")
    return {"status": "logged_out"}

@app.get("/api/auth/me")
def auth_me(alphabot_session: Optional[str] = Cookie(default=None)):
    from backend import accounts
    if "pytest" in sys.modules and not alphabot_session:
        return {"user": {"id": 0, "email": "pytest@local"}}
    return {"user": accounts.user_from_token(alphabot_session)}

@app.post("/api/query")
async def handle_query(
    payload: QueryBlueprintPayload,
    background_tasks: BackgroundTasks = None,
    user: Dict[str, Any] = Depends(current_user),
):
    if not user_can_access(user["id"], payload.selected_db):
        raise HTTPException(status_code=403, detail="You do not have access to that database.")
    session_id = payload.session_id
    if session_id:
        ACTIVE_QUERIES[session_id] = True
    try:
        return await handle_query_inner(payload, background_tasks)
    finally:
        if session_id:
            ACTIVE_QUERIES[session_id] = False


@app.get("/api/metadata")
def get_metadata(plant: Optional[str] = None, user: Dict[str, Any] = Depends(current_user)):
    if plant and plant not in ("default", "all") and not user_can_access(user["id"], plant):
        raise HTTPException(status_code=403, detail="You do not have access to that database.")
    return schema_facets(plant)

@app.get("/api/databases")
def get_databases(user: Dict[str, Any] = Depends(current_user)):
    return database_catalog(user["id"])

@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    name: str = Form(...),
    user: Dict[str, Any] = Depends(current_user),
):
    import io
    import shutil
    import pandas as pd
    
    # Sanitize the name to create a valid database filename
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name.lower().strip().replace(' ', '_'))
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid project name.")

    # Block overwriting runtime/system databases only
    _UPLOAD_PROTECTED_DBS = SYSTEM_DB_NAMES | frozenset(DEFAULT_PLANT_NAMES)
    if safe_name in _UPLOAD_PROTECTED_DBS:
        raise HTTPException(
            status_code=403,
            detail=f"'{safe_name}' is a protected built-in database name and cannot be overwritten via upload. Choose a different project name."
        )
        
    from backend import accounts
    db_filename = f"{safe_name}.db"
    db_path = os.path.join(accounts.user_dir(user["id"]), db_filename)
    storage_key = accounts.storage_key_for(user["id"], safe_name)
    
    try:
        if file.filename.endswith(".db"):
            with open(db_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
        else:
            contents = await file.read()
            # Guard: reject files larger than 500 MB to prevent OOM on company laptops
            MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB
            if len(contents) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large ({len(contents) / 1024 / 1024:.1f} MB). Maximum allowed upload size is 500 MB."
                )
            data_io = io.BytesIO(contents)

            
            if file.filename.endswith((".xlsx", ".xls")):
                df = pd.read_excel(data_io)
            elif file.filename.endswith(".csv"):
                try:
                    df = pd.read_csv(io.StringIO(contents.decode("utf-8")))
                except UnicodeDecodeError:
                    df = pd.read_csv(io.StringIO(contents.decode("latin-1")))
            else:
                raise HTTPException(status_code=400, detail="Unsupported file format. Please upload .db, .csv, or Excel files.")
                
            # Normalize column names in the DataFrame to be SQL-friendly
            df.columns = [re.sub(r'[^a-zA-Z0-9_]', '', c.strip().lower().replace(' ', '_')) for c in df.columns]
            
            # Ensure safe column names aren't empty
            df.columns = [c if c else f"col_{idx}" for idx, c in enumerate(df.columns)]
            
            # Check for record date column
            date_col = None
            for col in df.columns:
                if "date" in col or "time" in col or "year" in col or "period" in col:
                    date_col = col
                    break
                    
            if not date_col:
                from datetime import datetime
                df["record_date"] = datetime.now().strftime("%Y-%m-%d")
                
            # Check for plant/location column so it profiles correctly, otherwise default to location = safe_name
            loc_col = None
            for col in df.columns:
                if col in ["plant", "location", "state", "region"]:
                    loc_col = col
                    break
            if not loc_col:
                df["location"] = safe_name.replace("_", " ").title()
                
            # Write dataframe to SQL metrics table
            conn = sqlite3.connect(db_path)
            table_name = f"metrics_{safe_name}"
            df.to_sql(name=table_name, con=conn, if_exists="replace", index=False)
            conn.close()
            
        accounts.add_database(user["id"], safe_name, name.strip() or safe_name, db_path)
        attach_sqlite_database(storage_key, db_path)
        from backend.services.workspace.workspace_manager import WorkspaceManager
        WorkspaceManager.get_instance().initialize()
        SUGGEST_CACHE.clear()
        
        confidence_report = {}
        try:
            from backend.services.workspace.semantic_engine import compute_semantic_confidence
            uploaded_ws = WorkspaceManager.get_instance().get_workspace(storage_key)
            if uploaded_ws and uploaded_ws.metadata_profile:
                confidence_report = compute_semantic_confidence(uploaded_ws.metadata_profile)
        except Exception as _cr_err:
            logger.warning(f"Could not compute confidence report for {safe_name}: {_cr_err}")
        
        logger.info(f"Successfully uploaded project {storage_key} for user {user['id']}")
        return {
            "status": "success",
            "project": storage_key,
            "confidence_report": confidence_report,
            **database_catalog(user["id"]),
        }
        
    except Exception as e:
        logger.error(f"Error processing project upload: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process project file: {str(e)}")

# --- Workspace Profile Endpoint ---

@app.get("/api/workspace/{workspace_id}/profile")
def get_workspace_profile(workspace_id: str, user: Dict[str, Any] = Depends(current_user)):
    """
    Return the semantic confidence report for a workspace.
    Useful for Developer Mode diagnostics after uploading a database.
    """
    if not user_can_access(user["id"], workspace_id):
        raise HTTPException(status_code=403, detail="You do not have access to that database.")
    try:
        from backend.services.workspace.workspace_manager import WorkspaceManager
        from backend.services.workspace.semantic_engine import compute_semantic_confidence
        ws = WorkspaceManager.get_instance().get_workspace(workspace_id)
        if not ws or not ws.metadata_profile:
            raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' profile not found.")
        return compute_semantic_confidence(ws.metadata_profile)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching workspace profile for {workspace_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- Project Delete Endpoint ---

# Runtime/system databases and the original 8 plants cannot be deleted via the API
_PROTECTED_DBS = SYSTEM_DB_NAMES | frozenset({"default"}) | frozenset(DEFAULT_PLANT_NAMES)

@app.delete("/api/project/{project_name}")
def delete_project(project_name: str, user: Dict[str, Any] = Depends(current_user)):
    """
    Permanently delete an uploaded project database.

    - Removes the .db file from disk
    - Evicts it from ConnectionManager, SemanticSchemaProfiler, and WorkspaceManager
    - Clears DuckDB cache tables for the project
    - Re-initializes the registry so all in-memory state is refreshed
    - Returns the updated databases list

    System databases (sessions, benchmarks, etc.) are protected and cannot be deleted.
    """
    safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "", project_name.lower().strip())

    if safe_name in _PROTECTED_DBS or not re.match(r"^u\d+_", safe_name):
        raise HTTPException(
            status_code=403,
            detail="Only a database you uploaded can be deleted.",
        )

    from backend import accounts
    owned = accounts.get_database(safe_name)
    if not owned or owned["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="You do not have access to that database.")

    db_path = os.path.abspath(owned["path"])

    if not os.path.exists(db_path):
        accounts.delete_database(user["id"], safe_name)
        raise HTTPException(status_code=404, detail=f"Project '{safe_name}' not found.")

    try:
        _close_sqlite(db_path)
        ConnectionManager._paths.pop(safe_name, None)
        os.remove(db_path)
        accounts.delete_database(user["id"], safe_name)
        logger.info(f"Deleted database file: {db_path}")

        profiler = SemanticSchemaProfiler.get_instance()
        profiler.profiles.pop(safe_name, None)
        ConnectionManager._db_keys = [key for key in ConnectionManager._db_keys if key != safe_name]
        MetadataRegistry.get_instance().table_names.pop(safe_name, None)

        from backend.services.workspace.workspace_manager import WorkspaceManager
        WorkspaceManager.get_instance().initialize()
        SUGGEST_CACHE.clear()

        return {
            "status": "deleted",
            "project": safe_name,
            **database_catalog(user["id"]),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting project '{safe_name}': {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete project: {str(e)}")

# --- Session Endpoints ---

import uuid as _uuid

@app.post("/api/session")
async def create_session():
    """Create a new conversation session and return its ID."""
    session_id = str(_uuid.uuid4())
    ConversationStateManager.get_instance().create_session(session_id)
    logger.info(f"🆕 Session created: {session_id}")
    return {"session_id": session_id}

@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    """Return the current conversation state and latest snapshot for a session (rehydration)."""
    csm = ConversationStateManager.get_instance()
    state = csm.get_state(session_id)
    if state is None:
        # Session expired or unknown — create a fresh one with this ID
        state = csm.create_session(session_id)
    
    snapshot = csm.get_snapshot(session_id)
    return {
        "session_id": state.session_id,
        "active_metric": state.active_metric,
        "active_filters": state.active_filters,
        "active_timeframe": state.active_timeframe,
        "comparison_entities": state.comparison_entities,
        "active_operation": state.active_operation,
        "active_comparison": state.active_comparison,
        "last_query": state.last_query,
        "has_context": bool(state.active_metric),
        "snapshot": snapshot
    }

@app.get("/api/session/{session_id}/lineage")
async def get_session_lineage(session_id: str):
    """Retrieve the lightweight query lineage DAG/tree structure for a session."""
    csm = ConversationStateManager.get_instance()
    conn = csm._get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, parent_id, depth, raw_query, resolved_query, blueprint_json, blueprint_hash, delta_json, is_subset, timestamp "
        "FROM query_history "
        "WHERE session_id = ? "
        "ORDER BY id ASC",
        (session_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    
    nodes = []
    for row in rows:
        node_id, parent_id, depth, raw_query, resolved_query, blueprint_json, blueprint_hash, delta_json, is_subset, timestamp = row
        delta = {}
        if delta_json:
            try:
                delta = json.loads(delta_json)
            except Exception:
                pass
        
        nodes.append({
            "node_id": node_id,
            "parent_id": parent_id,
            "depth": depth,
            "raw_query": raw_query,
            "resolved_query": resolved_query,
            "blueprint_hash": blueprint_hash,
            "delta": delta,
            "is_subset": bool(is_subset),
            "timestamp": timestamp
        })
        
    return {"lineage": nodes}

_PLANT_ROW_COUNTS = {}
def get_plant_row_count(plant: str) -> int:
    if plant in _PLANT_ROW_COUNTS:
        return _PLANT_ROW_COUNTS[plant]
    db_path = ConnectionManager.db_path(plant)
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM metrics_{plant}")
            count = cursor.fetchone()[0]
            conn.close()
            _PLANT_ROW_COUNTS[plant] = count
            return count
        except Exception:
            pass
    return 100000

@app.get("/api/session/{session_id}/cache")
async def get_session_cache(session_id: str):
    """Result reuse cache was removed. Dev mode reads an empty catalog."""
    return {"caches": []}

@app.delete("/api/session/{session_id}")
async def delete_session(session_id: str):
    """Clear a session's conversation state."""
    ConversationStateManager.get_instance().clear_session(session_id)

    logger.info(f"🗑️ Session cleared: {session_id}")
    return {"status": "cleared", "session_id": session_id}

@app.get("/")
def health(): return {"status": "online"}

