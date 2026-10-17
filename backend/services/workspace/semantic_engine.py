"""
AlphaBot Semantic Engine -- Phase 7E
Automatic Semantic Schema Discovery & Universal Workspace Intelligence

Provides:
- ABBREVIATION_MAP: common business column abbreviations -> expansions
- BUSINESS_SYNONYM_MAP: concept -> list of NL synonyms
- expand_column_name(): generates all aliases for a column name
- classify_column_semantic(): enhanced classification heuristics
- resolve_canonical_concept(): maps column to a known business concept
- compute_semantic_confidence(): diagnostic report for a SchemaProfile
"""

import re
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("alphabot-semantic-engine")

# ---------------------------------------------------------------------------
# ABBREVIATION MAP
# Maps abbreviated column name tokens -> list of human-readable expansions.
# Keys are lowercase. Both full column names and individual tokens are looked
# up against this dict during alias generation.
# ---------------------------------------------------------------------------
ABBREVIATION_MAP: Dict[str, List[str]] = {
    # ---- Amounts / Finance ----
    "amt":          ["amount", "value"],
    "tot":          ["total"],
    "tot_amt":      ["total amount", "total value", "revenue"],
    "rev":          ["revenue"],
    "inv":          ["invoice", "inventory"],
    "inv_val":      ["inventory value"],
    "inv_value":    ["inventory value"],
    "exp":          ["expense", "expenditure"],
    "expn":         ["expense", "expenditure"],
    "pct":          ["percentage", "percent", "rate"],
    "pnl":          ["profit and loss", "profit"],
    "opex":         ["operating expense", "expense"],
    "capex":        ["capital expenditure", "budget"],
    "cogs":         ["cost of goods sold", "cost"],
    "ebitda":       ["earnings before interest", "profit"],
    "gm":           ["gross margin", "margin"],
    "np":           ["net profit", "profit"],
    "gp":           ["gross profit", "profit"],
    "ar":           ["accounts receivable", "receivable"],
    "ap":           ["accounts payable", "payable"],
    "gl":           ["general ledger"],
    "ytd":          ["year to date"],
    "mtd":          ["month to date"],
    "qtd":          ["quarter to date"],
    # ---- People / HR ----
    "emp":          ["employee"],
    "emp_ct":       ["employee count", "headcount"],
    "emp_count":    ["employee count", "headcount"],
    "hc":           ["headcount"],
    "dept":         ["department"],
    "mgr":          ["manager"],
    "sal":          ["salary"],
    "ctc":          ["cost to company", "salary"],
    "yoe":          ["years of experience", "experience"],
    "exp_yrs":      ["experience years", "experience"],
    "attrition":    ["attrition", "turnover rate"],
    # ---- Inventory / Supply Chain ----
    "stk":          ["stock"],
    "inv_qty":      ["inventory quantity", "stock quantity"],
    "wh":           ["warehouse"],
    "whse":         ["warehouse"],
    "sup":          ["supplier"],
    "supp":         ["supplier"],
    "qty":          ["quantity", "units"],
    "vol":          ["volume"],
    "sku":          ["product", "item", "sku"],
    "uom":          ["unit of measure"],
    "poi":          ["purchase order"],
    # ---- Geography ----
    "rgn":          ["region"],
    "cty":          ["city"],
    "loc":          ["location"],
    "ctry":         ["country"],
    "addr":         ["address"],
    "zip":          ["zip code", "postal code"],
    "pin":          ["pin code", "postal code"],
    # ---- Customers / Sales ----
    "cust":         ["customer", "client"],
    "cust_name":    ["customer name", "client name", "customer"],
    "cust_id":      ["customer id", "client id"],
    "clnt":         ["client"],
    "acct":         ["account"],
    "ord":          ["order"],
    "ord_val":      ["order value"],
    "sales_amt":    ["sales", "revenue", "sales amount"],
    "sls":          ["sales"],
    "ltv":          ["lifetime value", "customer value"],
    # ---- General Metrics ----
    "ct":           ["count"],
    "cnt":          ["count"],
    "num":          ["number", "count"],
    "avg":          ["average"],
    "max":          ["maximum"],
    "min":          ["minimum"],
    "ttl":          ["total"],
    "val":          ["value"],
    "rt":           ["rate"],
    "rat":          ["rate", "ratio"],
    "ratio":        ["ratio", "rate"],
    "idx":          ["index"],
    "score":        ["score", "rating"],
    "kpi":          ["key performance indicator", "metric"],
    # ---- Time ----
    "dt":           ["date"],
    "yr":           ["year"],
    "mo":           ["month"],
    "qtr":          ["quarter"],
    "wk":           ["week"],
    "ts":           ["timestamp", "date"],
    "fy":           ["fiscal year"],
    "cy":           ["calendar year"],
    # ---- General / Misc ----
    "id":           ["identifier", "id"],
    "cd":           ["code"],
    "nm":           ["name"],
    "desc":         ["description"],
    "cat":          ["category"],
    "typ":          ["type"],
    "sts":          ["status"],
    "src":          ["source"],
    "ref":          ["reference"],
    "no":           ["number"],
    "nr":           ["number"],
    "prd":          ["product"],
    "prod":         ["product"],
    "proj":         ["project"],
    "prj":          ["project"],
    "dept_nm":      ["department name", "department"],
    "emp_nm":       ["employee name"],
    "mgr_nm":       ["manager name"],
    "cust_nm":      ["customer name"],
    "sup_nm":       ["supplier name"],
    "ord_no":       ["order number"],
    "inv_no":       ["invoice number"],
    "item":         ["item", "product", "sku"],
    "dsc":          ["discount"],
    "disc":         ["discount"],
    "tax":          ["tax"],
    "fee":          ["fee", "charge"],
    "bal":          ["balance"],
    "due":          ["due amount", "outstanding"],
}

# ---------------------------------------------------------------------------
# BUSINESS SYNONYM MAP
# Maps canonical business concept -> list of NL synonyms that should resolve
# to the same concept during query parsing.
# ---------------------------------------------------------------------------
BUSINESS_SYNONYM_MAP: Dict[str, List[str]] = {
    "revenue": [
        "sales", "income", "earnings", "turnover", "sales_amt", "rev", "tot_amt",
        "total amount", "total sales", "gross revenue", "net revenue", "receipts",
        "sales amount", "total revenue", "top line",
    ],
    "profit": [
        "net income", "pnl", "net profit", "margin", "gain", "surplus",
        "gross profit", "gp", "np", "bottom line", "profit margin", "net margin",
    ],
    "expense": [
        "cost", "expenditure", "spending", "exp", "opex", "capex", "overhead",
        "operating cost", "operating expense", "budget used", "outgoing", "spend",
        "costs", "outlays",
    ],
    "salary": [
        "pay", "compensation", "wages", "sal", "ctc", "remuneration",
        "cost to company", "payroll", "earnings", "take home",
    ],
    "quantity": [
        "qty", "units", "count", "volume", "amount", "stock", "stk",
        "pieces", "items", "no of units", "nos",
    ],
    "employee": [
        "emp", "headcount", "staff", "emp_ct", "hc", "personnel",
        "workforce", "employee count", "workers", "associates", "people",
    ],
    "customer": [
        "cust", "client", "buyer", "cust_name", "account", "consumer",
        "shopper", "purchaser", "clnt", "end user",
    ],
    "supplier": [
        "vendor", "sup", "supp", "partner", "provider", "distributor",
        "contractor", "manufacturer", "seller",
    ],
    "inventory": [
        "stock", "stk", "goods", "inv", "inv_val", "inv_value", "warehouse stock",
        "products", "items on hand", "current stock", "stock on hand",
    ],
    "region": [
        "area", "zone", "territory", "rgn", "geography", "location",
        "district", "state", "city", "province",
    ],
    "department": [
        "dept", "division", "business_unit", "function", "team", "group",
        "unit", "branch", "section", "cost center",
    ],
    "date": [
        "dt", "period", "time", "record_date", "timestamp", "datetime",
        "transaction date", "created date", "posting date",
    ],
    "category": [
        "type", "class", "segment", "cat", "group", "kind",
        "classification", "product type", "product category",
    ],
    "warehouse": [
        "wh", "whse", "facility", "store", "depot", "storage",
        "distribution center", "fulfillment center",
    ],
    "status": [
        "sts", "state", "stage", "phase", "condition", "flag",
        "indicator", "progress", "standing",
    ],
    "order": [
        "ord", "purchase", "transaction", "sale", "booking",
        "requisition", "purchase order",
    ],
    "experience": [
        "yoe", "exp_yrs", "years of experience", "tenure", "seniority",
        "work experience", "service years",
    ],
    "discount": [
        "rebate", "offer", "reduction", "markdown", "dsc", "disc",
        "price reduction", "promotion",
    ],
    "budget": [
        "allocation", "capex", "approved amount", "planned spend",
        "budget allocated", "sanctioned amount",
    ],
}

# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def _split_snake_case(name: str) -> List[str]:
    """Split a snake_case name into non-empty lowercase tokens."""
    return [t for t in name.lower().split("_") if t]


def _split_camel_case(name: str) -> List[str]:
    """Split camelCase or PascalCase into lowercase tokens."""
    spaced = re.sub(r"([A-Z][a-z]+)", r" \1", re.sub(r"([A-Z]+)(?=[A-Z][a-z])", r" \1", name))
    return [t.lower() for t in spaced.split() if t]


def expand_column_name(col: str) -> List[str]:
    """
    Generate all semantic aliases for a given column name.

    Algorithm:
    1.  Base forms (original, space variant, underscore variant)
    2.  Full-name ABBREVIATION_MAP lookup
    3.  snake_case token splitting + per-token ABBREVIATION_MAP lookup
    4.  Reconstruct expanded phrase from per-token primary expansions
    5.  camelCase / PascalCase split + per-token lookup
    6.  BUSINESS_SYNONYM_MAP: if any alias resolves to a known concept,
        include ALL synonyms for that concept
    7.  Deduplicate, remove blanks, return sorted list

    Args:
        col: Column name (any case, snake_case or camelCase)

    Returns:
        Sorted, deduplicated list of alias strings
    """
    col_lower = col.lower().strip()
    aliases: set = set()

    # 1. Base forms
    aliases.add(col_lower)
    aliases.add(col_lower.replace("_", " "))
    aliases.add(col_lower.replace(" ", "_"))

    # 2. Full-name ABBREVIATION_MAP lookup
    full_expansions: List[str] = ABBREVIATION_MAP.get(col_lower, [])
    for exp in full_expansions:
        exp_l = exp.lower()
        aliases.add(exp_l)
        aliases.add(exp_l.replace(" ", "_"))

    # 3. snake_case token splitting + per-token lookup
    snake_tokens = _split_snake_case(col_lower)
    if len(snake_tokens) > 1:
        aliases.add(" ".join(snake_tokens))

    primary_expanded: List[str] = []
    for token in snake_tokens:
        token_exps = ABBREVIATION_MAP.get(token, [token])
        primary_expanded.append(token_exps[0])
        for exp in token_exps:
            aliases.add(exp.lower())

    # 4. Reconstruct expanded phrase
    if len(primary_expanded) > 1:
        phrase = " ".join(primary_expanded)
        aliases.add(phrase)
        aliases.add(phrase.replace(" ", "_"))

    # 5. camelCase / PascalCase split
    camel_tokens = _split_camel_case(col)
    if camel_tokens and camel_tokens != snake_tokens:
        aliases.add(" ".join(camel_tokens))
        for token in camel_tokens:
            for exp in ABBREVIATION_MAP.get(token, [token]):
                aliases.add(exp.lower())

    # 6. BUSINESS_SYNONYM_MAP
    check_aliases = (
        {col_lower, col_lower.replace("_", " ")}
        | {e.lower() for e in full_expansions}
    )
    if len(primary_expanded) > 1:
        check_aliases.add(" ".join(primary_expanded))

    for concept, synonyms in BUSINESS_SYNONYM_MAP.items():
        hit = False
        if concept in check_aliases:
            hit = True
        else:
            for syn in synonyms:
                syn_l = syn.lower()
                if syn_l in check_aliases or col_lower == syn_l:
                    hit = True
                    break
            if not hit:
                for candidate in check_aliases:
                    if concept in candidate or candidate in concept:
                        hit = True
                        break
        if hit:
            aliases.add(concept)
            for syn in synonyms:
                syn_l = syn.lower()
                aliases.add(syn_l)
                aliases.add(syn_l.replace(" ", "_"))

    # 7. Clean up
    aliases = {a.strip() for a in aliases if a and a.strip()}
    return sorted(aliases)


def classify_column_semantic(
    col_name: str,
    col_type: str,
    cardinality_ratio: float,
    sample_values: list,
    row_count: int = 0,
) -> str:
    """
    Enhanced column classification using semantic name analysis and sample values.

    Returns one of: METRIC, DIMENSION, TIME, IDENTIFIER, STATUS
    """
    col_lower = col_name.lower().strip()
    col_type_upper = col_type.upper()

    is_numeric = any(
        t in col_type_upper
        for t in ["INT", "NUMERIC", "REAL", "DOUBLE", "FLOAT", "DECIMAL"]
    )

    # --- TIME ---
    time_re = re.compile(
        r"(?i)(date|time|year|month|period|quarter|fy_|timestamp|"
        r"created_at|updated_at|dt$|yr$|mo$|qtr$|wk$)"
    )
    if time_re.search(col_lower):
        return "TIME"
    if not is_numeric and sample_values:
        date_re = re.compile(
            r"^\d{4}[-/]\d{2}[-/]\d{2}|^\d{2}[-/]\d{2}[-/]\d{4}|^\d{4}$"
        )
        sample_strs = [str(v) for v in sample_values[:10] if v is not None]
        date_hits = sum(1 for s in sample_strs if date_re.match(s))
        if sample_strs and date_hits >= max(1, len(sample_strs) * 0.6):
            return "TIME"

    # --- IDENTIFIER ---
    id_re = re.compile(r"(?i)(_id|_code|_uuid|_ref|identifier|_no$|_nr$|_num$|id$|code$)")
    if id_re.search(col_lower):
        return "IDENTIFIER"
    if not is_numeric and cardinality_ratio > 0.85 and row_count >= 50:
        if not re.search(r"(?i)(name|_name|label)$", col_lower):
            return "IDENTIFIER"

    # --- STATUS ---
    status_re = re.compile(
        r"(?i)(status|state|stage|phase|payment|flag|active|enabled|is_|has_|sts$)"
    )
    if status_re.search(col_lower):
        if "status" in col_lower:
            return "STATUS"
        if sample_values:
            sample_set = {str(v).strip().lower() for v in sample_values[:20] if v is not None}
            bool_values = {"0", "1", "true", "false", "yes", "no", "y", "n", "active", "inactive"}
            if not sample_set or sample_set.issubset(bool_values):
                return "STATUS"
        else:
            return "STATUS"


    # --- METRIC (numeric) ---
    if is_numeric:
        metric_keywords = {
            "amount", "total", "revenue", "sales", "profit", "salary", "pay",
            "cost", "expense", "budget", "quantity", "qty", "count", "volume",
            "value", "rate", "pct", "percentage", "score", "rating", "margin",
            "income", "earnings", "spend", "headcount", "capacity", "output",
            "production", "target", "actual", "forecast", "variance", "price",
            "fee", "tax", "discount", "balance", "stock", "inventory", "emp",
        }
        # Gather expanded tokens for this column
        expanded_set: set = {col_lower, col_lower.replace("_", " ")}
        for token in col_lower.split("_"):
            expanded_set.add(token)
            expanded_set.update(ABBREVIATION_MAP.get(token, []))
        expanded_set.update(ABBREVIATION_MAP.get(col_lower, []))

        for alias in expanded_set:
            for kw in metric_keywords:
                if kw in alias or alias in kw:
                    return "METRIC"
        return "METRIC"  # Default: numeric columns are metrics

    return "DIMENSION"


def resolve_canonical_concept(col_name: str, expanded_aliases: List[str]) -> Optional[str]:
    """
    Attempt to match a column to a known canonical business concept.

    Checks BUSINESS_SYNONYM_MAP. A column is linked only when its own name
    or an alias generated from that name matches a concept.

    Returns the canonical concept key, or None if no match found.
    """
    col_lower = col_name.lower().strip()
    check_set = {col_lower, col_lower.replace("_", " ")} | {a.lower() for a in expanded_aliases}

    for concept, synonyms in BUSINESS_SYNONYM_MAP.items():
        if concept in check_set:
            return concept
        for syn in synonyms:
            if syn.lower() in check_set:
                return concept

    return None


def compute_semantic_confidence(profile: Any) -> Dict[str, Any]:
    """
    Compute a semantic confidence diagnostic report for a SchemaProfile.

    Confidence is a weighted score:
    - 40%: fraction of columns with a resolved canonical concept
    - 30%: presence of at least one metric column
    - 20%: presence of at least one dimension column
    - 10%: presence of at least one time column (0.5 partial credit if absent)

    Args:
        profile: SchemaProfile Pydantic model instance (or None)

    Returns:
        JSON-serializable diagnostic dict
    """
    if not profile:
        return {}

    columns = profile.columns
    total_cols = len(columns)
    if total_cols == 0:
        return {
            "workspace":           profile.database_name,
            "columns":             0,
            "semantic_confidence": 0.0,
            "warnings":            ["No columns found in profile."],
        }

    metrics     = [c for c, p in columns.items() if p.classification == "METRIC"]
    dimensions  = [c for c, p in columns.items() if p.classification in ["DIMENSION", "STATUS"]]
    time_cols   = [c for c, p in columns.items() if p.classification == "TIME"]
    identifiers = [c for c, p in columns.items() if p.classification == "IDENTIFIER"]

    total_aliases        = sum(len(p.aliases) for p in columns.values())
    cols_with_canonical  = sum(1 for p in columns.values() if p.canonical_concept is not None)

    canonical_score = cols_with_canonical / total_cols
    metric_score    = 1.0 if metrics else 0.0
    dim_score       = 1.0 if dimensions else 0.0
    time_score      = 1.0 if time_cols else 0.5

    confidence = round(
        0.40 * canonical_score
        + 0.30 * metric_score
        + 0.20 * dim_score
        + 0.10 * time_score,
        3,
    )

    warnings: List[str] = []
    if not metrics:
        warnings.append("No numeric metric columns detected -- check column data types.")
    if not dimensions:
        warnings.append("No dimension columns detected -- filtering may be limited.")
    if not time_cols:
        warnings.append("No date/time column detected -- trend analysis unavailable.")
    if cols_with_canonical == 0:
        warnings.append("No columns matched standard business concepts -- alias coverage may be low.")

    column_breakdown: Dict[str, Any] = {}
    for col_name, col_prof in columns.items():
        column_breakdown[col_name] = {
            "classification":  col_prof.classification,
            "resolved_as":     col_prof.canonical_concept or col_name,
            "aliases_count":   len(col_prof.aliases),
            "aliases_preview": col_prof.aliases[:8],
            "unit":            col_prof.unit,
            "aggregation":     col_prof.aggregation_default,
        }

    return {
        "workspace":                      profile.database_name,
        "table":                          profile.table_name,
        "tables":                         1,
        "columns":                        total_cols,
        "metrics_detected":               len(metrics),
        "dimensions_detected":            len(dimensions),
        "time_columns":                   len(time_cols),
        "identifier_columns":             len(identifiers),
        "aliases_generated":              total_aliases,
        "columns_with_canonical_concept": cols_with_canonical,
        "semantic_confidence":            confidence,
        "column_breakdown":               column_breakdown,
        "warnings":                       warnings,
    }
