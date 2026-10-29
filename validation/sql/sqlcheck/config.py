"""The adopter's `sqlcheck.toml`: where the project keeps its SQL, its migrations, its
models and its declared schema, and the settings of each step. Every relative path is
resolved against the directory that holds the config file (`root`). A step whose section
is absent keeps its defaults; a step that needs a value the config does not give is
skipped with a detail that says which key is missing."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KIT_DIR = Path(__file__).resolve().parent.parent
KIT_SQLFLUFF = KIT_DIR / ".sqlfluff"
KIT_SEMGREP_DIR = KIT_DIR / "semgrep"

ALL_STEPS = (
    "sqlfluff",
    "sql-rules",
    "alembic-lint",
    "orm-lint",
    "inline-queries",
    "semgrep",
    "schema-parity",
    "migrations",
    "sqlite-pragmas",
)

DEFAULT_TYPE_FAMILIES: dict[str, list[str]] = {
    "text": ["String"],
    "uuid": ["String", "Uuid"],
    "int": ["Integer"],
    "real": ["Float", "Numeric"],
    "numeric": ["Numeric"],
    "bool": ["Boolean"],
    "json": ["JSON"],
    "enum": ["Enum"],
    "timestamp": ["DateTime"],
    "date": ["Date"],
    "bytes": ["LargeBinary"],
}


@dataclass(frozen=True)
class ScanRegister:
    """Reads of every row a table holds that the project admits on purpose.

    `methods`: dotted `module.Class.method` / `module.function` names (relative to
    `module_root`); `classes`: class names whose every method may read every row;
    `complete_lists`: methods whose contract returns a complete list."""

    methods: frozenset[str] = frozenset()
    classes: frozenset[str] = frozenset()
    complete_lists: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Config:
    root: Path
    source: Path | None = None
    steps: tuple[str, ...] = ALL_STEPS
    sql_dirs: tuple[str, ...] = ("src", "alembic")
    python_dirs: tuple[str, ...] = ("src",)
    schema: Path | None = None
    alembic_versions: Path | None = None
    migration_sql_dirs: tuple[str, ...] = ("alembic/sql",)
    dialect: str = "sqlite"
    sqlfluff_config: Path = KIT_SQLFLUFF
    # orm-lint
    models_package: str = ""
    not_models: tuple[str, ...] = ()
    models_dir: Path | None = None
    module_root: str = "src"
    scan_register: ScanRegister = field(default_factory=ScanRegister)
    # inline-queries
    inline_loaders: dict[str, str] = field(default_factory=dict)
    inline_query_suffixes: tuple[str, ...] = (".sql",)
    inline_migration_dirs: tuple[str, ...] = ("alembic",)
    inline_orphan_roots: tuple[str, ...] = ()
    inline_exclude: tuple[str, ...] = ()
    # semgrep
    semgrep_rules: Path = KIT_SEMGREP_DIR
    semgrep_generated: Path | None = None
    semgrep_paths_include: tuple[str, ...] = ("**/src/**",)
    # schema-parity
    metadata: str = ""
    ignore_tables: tuple[str, ...] = ("alembic_version",)
    implicit_columns: tuple[str, ...] = ()
    conditional_unique_dialects: tuple[str, ...] = ("sqlite",)
    type_families: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_TYPE_FAMILIES))
    # migrations
    alembic_ini: Path | None = None
    migrations_url_template: str = "sqlite:///{path}"
    migrations_url_env: str = "DATABASE_URL"
    # sqlite-pragmas
    engine_factory: str = ""
    pragmas_url_template: str = "sqlite:///{path}"
    factory_kwargs: dict[str, Any] = field(default_factory=dict)
    expected_pragmas: dict[str, Any] = field(default_factory=dict)

    def rel(self, path: Path) -> str:
        """`path` relative to the project root when it is inside it, else as given."""
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def config_label(self) -> str:
        return self.rel(self.source) if self.source is not None else "sqlcheck.toml"


def _tuple(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        msg = f"expected a list of strings, got {value!r}"
        raise ValueError(msg)
    return tuple(value)


def _path(root: Path, value: object) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        msg = f"expected a path string, got {value!r}"
        raise ValueError(msg)
    path = Path(value)
    return path if path.is_absolute() else root / path


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name, {})
    if not isinstance(section, dict):
        msg = f"[{name}] must be a table"
        raise ValueError(msg)
    return section


def load_config(path: Path | None, root: Path | None = None) -> Config:
    """Read `path` (TOML). No file: every default, rooted at `root` or the cwd."""
    if path is None:
        return Config(root=(root or Path.cwd()).resolve())
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.resolve().parent
    paths = _table(data, "paths")
    project_root = _path(base, paths.get("root", ".")) or base
    run = _table(data, "run")
    sqlfluff = _table(data, "sqlfluff")
    orm = _table(data, "orm_lint")
    register = _table(orm, "scan_register")
    inline = _table(data, "inline_queries")
    loaders = _table(inline, "loaders")
    semgrep = _table(data, "semgrep")
    parity = _table(data, "schema_parity")
    migrations = _table(data, "migrations")
    pragmas = _table(data, "sqlite_pragmas")
    steps = _tuple(run.get("steps"), ALL_STEPS)
    unknown = sorted(set(steps) - set(ALL_STEPS))
    if unknown:
        msg = f"[run].steps names unknown steps {unknown}; known: {list(ALL_STEPS)}"
        raise ValueError(msg)
    families = parity.get("type_families")
    return Config(
        root=project_root,
        source=path.resolve(),
        steps=steps,
        sql_dirs=_tuple(paths.get("sql"), ("src", "alembic")),
        python_dirs=_tuple(paths.get("python"), ("src",)),
        schema=_path(project_root, paths.get("schema")),
        alembic_versions=_path(project_root, paths.get("alembic_versions")),
        migration_sql_dirs=_tuple(paths.get("migration_sql_dirs"), ("alembic/sql",)),
        dialect=str(sqlfluff.get("dialect", "sqlite")),
        sqlfluff_config=_path(project_root, sqlfluff.get("config")) or KIT_SQLFLUFF,
        models_package=str(orm.get("models_package", "")),
        not_models=_tuple(orm.get("not_models"), ()),
        models_dir=_path(project_root, orm.get("models_dir")),
        module_root=str(orm.get("module_root", "src")),
        scan_register=ScanRegister(
            methods=frozenset(_tuple(register.get("methods"), ())),
            classes=frozenset(_tuple(register.get("classes"), ())),
            complete_lists=frozenset(_tuple(register.get("complete_lists"), ())),
        ),
        inline_loaders={str(k): str(v) for k, v in loaders.items()},
        inline_query_suffixes=_tuple(inline.get("query_suffixes"), (".sql",)),
        inline_migration_dirs=_tuple(inline.get("migration_dirs"), ("alembic",)),
        inline_orphan_roots=_tuple(inline.get("orphan_roots"), ()),
        inline_exclude=_tuple(inline.get("exclude"), ()),
        semgrep_rules=_path(project_root, semgrep.get("rules")) or KIT_SEMGREP_DIR,
        semgrep_generated=_path(project_root, semgrep.get("generated_dir")),
        semgrep_paths_include=_tuple(semgrep.get("paths_include"), ("**/src/**",)),
        metadata=str(parity.get("metadata", "")),
        ignore_tables=_tuple(parity.get("ignore_tables"), ("alembic_version",)),
        implicit_columns=_tuple(parity.get("implicit_columns"), ()),
        conditional_unique_dialects=_tuple(parity.get("conditional_unique_dialects"), ("sqlite",)),
        type_families=(
            {str(k): list(v) for k, v in families.items()}
            if isinstance(families, dict)
            else dict(DEFAULT_TYPE_FAMILIES)
        ),
        alembic_ini=_path(project_root, migrations.get("alembic_ini")),
        migrations_url_template=str(migrations.get("url_template", "sqlite:///{path}")),
        migrations_url_env=str(migrations.get("url_env", "DATABASE_URL")),
        engine_factory=str(pragmas.get("factory", "")),
        pragmas_url_template=str(pragmas.get("url_template", "sqlite:///{path}")),
        factory_kwargs=dict(_table(pragmas, "factory_kwargs")),
        expected_pragmas=dict(_table(pragmas, "expected")),
    )
