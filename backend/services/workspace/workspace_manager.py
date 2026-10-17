import os
import time
import logging
import threading
from typing import Dict, Any, List, Optional

logger = logging.getLogger("alphabot-workspace-manager")

class Workspace:
    """Represents a database/project workspace context."""
    def __init__(
        self,
        workspace_id: str,
        display_name: str,
        sqlite_engine: Optional[Any] = None,
        metadata_profile: Optional[Any] = None,
        semantic_adapter: Optional[Any] = None,
        trie: Optional[Any] = None,
        schema_hash: str = "",
        last_modified: float = 0.0,
        available_metrics: Optional[List[str]] = None,
        available_dimensions: Optional[List[str]] = None,
        aliases: Optional[Dict[str, List[str]]] = None,
        spelling_vocabulary: Optional[List[str]] = None
    ):
        self.workspace_id = workspace_id
        self.display_name = display_name
        self.sqlite_engine = sqlite_engine
        self.metadata_profile = metadata_profile
        self.semantic_adapter = semantic_adapter
        self.trie = trie
        self.schema_hash = schema_hash
        self.last_modified = last_modified
        self.available_metrics = available_metrics or []
        self.available_dimensions = available_dimensions or []
        self.aliases = aliases or {}
        self.spelling_vocabulary = spelling_vocabulary or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "display_name": self.display_name,
            "available_metrics": self.available_metrics,
            "available_dimensions": self.available_dimensions,
            "aliases": self.aliases,
            "last_modified": self.last_modified,
            "spelling_vocabulary": self.spelling_vocabulary
        }

class WorkspaceManager:
    """Manages workspace configurations and environments."""
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "WorkspaceManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._workspaces: Dict[str, Workspace] = {}

    def initialize(self, registry=None):
        """Build workspaces for all registered and default databases."""
        logger.info("Initializing WorkspaceManager...")
        # Lazy imports to avoid circular import issues
        from backend.main import (
            ConnectionManager, 
            SemanticSchemaProfiler, 
            MetadataRegistry, 
            SemanticSchemaAdapter, 
            Trie
        )
        import sqlite3
        import hashlib
        import json

        self._workspaces.clear()

        db_keys = ConnectionManager.keys()
        if registry is None:
            registry = MetadataRegistry.get_instance()
        profiler = SemanticSchemaProfiler.get_instance()

        for db_name in db_keys:
            db_path = ConnectionManager.db_path(db_name)
            if not os.path.exists(db_path):
                continue

            display_name = db_name.replace("_", " ").title()
            
            profile = profiler.profiles.get(db_name)
            if not profile:
                table_name = None
                try:
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'metrics_%'")
                    res = cursor.fetchone()
                    if res:
                        table_name = res[0]
                    conn.close()
                except Exception:
                    pass

                if table_name:
                    profile = profiler.profile_single_database(db_name, table_name, db_path)
                    if profile:
                        profiler.profiles[db_name] = profile

            # Schema hash calculation
            schema_hash = ""
            if profile:
                try:
                    profile_dump = json.dumps(profile.model_dump(mode='json'), sort_keys=True)
                    schema_hash = hashlib.md5(profile_dump.encode("utf-8")).hexdigest()
                except Exception:
                    pass

            available_metrics = []
            available_dimensions = []
            aliases = {}
            adapter = None
            trie = None

            if profile:
                available_metrics = [c for c, col_prof in profile.columns.items() if col_prof.classification == "METRIC"]
                available_dimensions = [c for c, col_prof in profile.columns.items() if col_prof.classification in ["DIMENSION", "STATUS", "IDENTIFIER"]]
                
                # Dynamic isolated adapter
                adapter = SemanticSchemaAdapter()
                adapter.build_schema_maps_for_profile(db_name, profile)
                aliases = {k: list(v) for k, v in adapter.column_to_aliases.items()}

                # Dynamic isolated suggestion trie
                trie = Trie()
                for key in available_metrics:
                    name = key.replace('_', ' ').title()
                    trie.insert(key, "metric", name)
                    for part in key.split('_'):
                        if len(part) > 2:
                            trie.insert(part, "metric", name)

                for cat_key in available_dimensions:
                    cat_name = cat_key.replace('_', ' ').title()
                    trie.insert(cat_key, cat_key, cat_name)
                    for part in cat_key.split('_'):
                        if len(part) > 2:
                            trie.insert(part, cat_key, cat_name)

                    # Retrieve categorical distinct values safely
                    values = []
                    try:
                        conn = sqlite3.connect(db_path)
                        cursor = conn.cursor()
                        limit = 100 if cat_key == 'project_id' else 50
                        cursor.execute(f"SELECT DISTINCT {cat_key} FROM {profile.table_name} WHERE {cat_key} IS NOT NULL LIMIT {limit}")
                        values = [str(row[0]) for row in cursor.fetchall()]
                        conn.close()
                    except Exception:
                        pass

                    for val in values:
                        val_str = str(val)
                        val_clean = val_str.replace('_', ' ')
                        trie.insert(val_str, cat_key, val_clean.title())
                        for part in val_str.split('_'):
                            if len(part) > 2:
                                trie.insert(part, cat_key, val_clean.title())

                # Phase 7E: Insert semantic alias tokens into trie for abbreviation-aware autocomplete
                # e.g. "employee", "headcount", "revenue", "sales amount" become valid trie prefixes
                if adapter:
                    for col_name, col_aliases in adapter.column_to_aliases.items():
                        col_category = "metric" if col_name in available_metrics else col_name
                        col_display = col_name.replace('_', ' ').title()
                        for alias in col_aliases:
                            alias_str = str(alias).lower().strip()
                            if len(alias_str) > 2:
                                trie.insert(alias_str, col_category, col_display)
                                # Also insert individual words of multi-word aliases
                                for word in alias_str.split():
                                    if len(word) > 2:
                                        trie.insert(word, col_category, col_display)

            last_modified = 0.0
            try:
                last_modified = os.path.getmtime(db_path)
            except OSError:
                pass

            # Build spelling vocabulary for custom workspace
            vocab = set()
            for k in available_metrics:
                vocab.add(k.lower())
                for part in k.split('_'):
                    if len(part) > 2:
                        vocab.add(part.lower())
            for cat_key in available_dimensions:
                vocab.add(cat_key.lower())
                for part in cat_key.split('_'):
                    if len(part) > 2:
                        vocab.add(part.lower())
            if adapter:
                for std_col, syns in adapter.column_to_aliases.items():
                    for syn in syns:
                        vocab.add(syn.lower())
                        for part in syn.split('_'):
                            if len(part) > 2:
                                vocab.add(part.lower())
            if profile:
                for col_name, col_prof in profile.columns.items():
                    if col_prof.classification in ["DIMENSION", "STATUS", "IDENTIFIER"]:
                        values = []
                        try:
                            conn = sqlite3.connect(db_path)
                            cursor = conn.cursor()
                            limit = 100 if col_name == 'project_id' else 50
                            cursor.execute(f"SELECT DISTINCT {col_name} FROM {profile.table_name} WHERE {col_name} IS NOT NULL LIMIT {limit}")
                            values = [str(row[0]) for row in cursor.fetchall()]
                            conn.close()
                        except Exception:
                            pass
                        for val in values:
                            val_str = str(val).lower()
                            vocab.add(val_str)
                            for part in val_str.replace('_', ' ').split():
                                if len(part) > 2:
                                    vocab.add(part)

            ws = Workspace(
                workspace_id=db_name,
                display_name=display_name,
                sqlite_engine=None,
                metadata_profile=profile,
                semantic_adapter=adapter,
                trie=trie,
                schema_hash=schema_hash,
                last_modified=last_modified,
                available_metrics=available_metrics,
                available_dimensions=available_dimensions,
                aliases=aliases,
                spelling_vocabulary=list(vocab)
            )
            self._workspaces[db_name] = ws

        # Register the default workspace mapping
        from backend.main import SUGGESTIONS_TRIE, POWER_PLANTS
        
        default_vocab = set()
        for k in registry.metrics:
            default_vocab.add(k.lower())
            for part in k.split('_'):
                if len(part) > 2:
                    default_vocab.add(part.lower())
        for cat_key, cat_info in registry.categoricals.items():
            default_vocab.add(cat_key.lower())
            for part in cat_key.split('_'):
                if len(part) > 2:
                    default_vocab.add(part.lower())
            for val in cat_info.get("values", []):
                val_str = str(val).lower()
                default_vocab.add(val_str)
                for part in val_str.replace('_', ' ').split():
                    if len(part) > 2:
                        default_vocab.add(part)
        for p in POWER_PLANTS:
            default_vocab.add(p.lower())
            default_vocab.add(p.lower().replace('_', ' '))
            for part in p.split('_'):
                if len(part) > 2:
                    default_vocab.add(part.lower())

        default_ws = Workspace(
            workspace_id="default",
            display_name="Default",
            sqlite_engine=None,
            metadata_profile=None,
            semantic_adapter=SemanticSchemaAdapter.get_instance(),
            trie=SUGGESTIONS_TRIE,
            schema_hash="",
            last_modified=0.0,
            available_metrics=list(registry.metrics.keys()),
            available_dimensions=list(registry.categoricals.keys()),
            aliases={k: list(v) for k, v in SemanticSchemaAdapter.get_instance().column_to_aliases.items()},
            spelling_vocabulary=list(default_vocab)
        )
        self._workspaces["default"] = default_ws
        logger.info(f"WorkspaceManager initialized. Registered workspaces: {list(self._workspaces.keys())}")

    def get_workspace(self, workspace_id: str) -> Workspace:
        """Retrieves a workspace by its workspace_id, falling back to 'default'."""
        if not self._workspaces:
            self.initialize()
        ws_id = workspace_id or "default"
        if ws_id not in self._workspaces:
            ws_id = ws_id.lower().strip()
        return self._workspaces.get(ws_id, self._workspaces.get("default"))

    def list_workspaces(self) -> List[Workspace]:
        """Lists all registered workspaces."""
        return list(self._workspaces.values())
