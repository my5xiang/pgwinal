from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class AttributeDef:
    attnum: int
    attname: str
    type_oid: int
    type_name: str
    typmod: int = -1
    attnotnull: bool = False
    is_dropped: bool = False
    attndims: int = 0
    collation: int = 0


@dataclass
class RelationDef:
    rel_oid: int
    schema_name: str
    rel_name: str
    relfilenode: int
    reltablespace: int
    db_oid: int
    relkind: str = "r"
    attributes: list[AttributeDef] = field(default_factory=list)
    def is_user_table(self) -> bool:
        return self.relkind in ("r", "p", "m", "t")

    def qualified_name(self) -> str:
        return f'"{self.schema_name}"."{self.rel_name}"'


@dataclass
class DataDictionary:
    version: str = "1"
    pg_version: str = ""
    system_id: str = ""
    created_at: str = ""
    relations: list[RelationDef] = field(default_factory=list)

    def find_by_relfilenode(self, rel_number: int, db_oid: int | None = None) -> Optional[RelationDef]:
        # relfilenode unique within database+tablespace
        cands = [r for r in self.relations if r.relfilenode == rel_number]
        if db_oid is not None:
            exact = [r for r in cands if r.db_oid == db_oid]
            if exact:
                return exact[0]
        return cands[0] if cands else None

    def find_by_oid(self, rel_oid: int) -> Optional[RelationDef]:
        for r in self.relations:
            if r.rel_oid == rel_oid:
                return r
        return None


DEFAULT_DICT_PATH = Path("dict/pgwinal_dict.sqlite")


class DictStore:
    """SQLite-backed data dictionary (exportable / loadable offline)."""

    def __init__(self, path: Path | str = DEFAULT_DICT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS relations (
                rel_oid INTEGER PRIMARY KEY,
                schema_name TEXT NOT NULL,
                rel_name TEXT NOT NULL,
                relfilenode INTEGER NOT NULL,
                reltablespace INTEGER NOT NULL DEFAULT 0,
                db_oid INTEGER NOT NULL DEFAULT 0,
                relkind TEXT NOT NULL DEFAULT 'r',
                UNIQUE(schema_name, rel_name)
            );
            CREATE TABLE IF NOT EXISTS attributes (
                rel_oid INTEGER NOT NULL,
                attnum INTEGER NOT NULL,
                attname TEXT NOT NULL,
                type_oid INTEGER NOT NULL,
                type_name TEXT,
                typmod INTEGER DEFAULT -1,
                attnotnull INTEGER DEFAULT 0,
                is_dropped INTEGER DEFAULT 0,
                attndims INTEGER DEFAULT 0,
                collation INTEGER DEFAULT 0,
                PRIMARY KEY(rel_oid, attnum)
            );
            CREATE INDEX IF NOT EXISTS idx_relfn ON relations(relfilenode, db_oid);
            """
        )
        c.commit()

    def clear(self) -> None:
        self.conn.execute("DELETE FROM attributes")
        self.conn.execute("DELETE FROM relations")
        self.conn.execute("DELETE FROM meta")
        self.conn.commit()

    def save_dictionary(self, d: DataDictionary) -> None:
        self.clear()
        cur = self.conn.cursor()
        meta = {
            "version": d.version,
            "pg_version": d.pg_version,
            "system_id": d.system_id,
            "created_at": d.created_at,
        }
        for k, v in meta.items():
            cur.execute("INSERT INTO meta(key,value) VALUES(?,?)", (k, v))
        for rel in d.relations:
            cur.execute(
                """INSERT INTO relations
                (rel_oid, schema_name, rel_name, relfilenode, reltablespace, db_oid, relkind)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    rel.rel_oid,
                    rel.schema_name,
                    rel.rel_name,
                    rel.relfilenode,
                    rel.reltablespace,
                    rel.db_oid,
                    rel.relkind,
                ),
            )
            for a in rel.attributes:
                cur.execute(
                    """INSERT INTO attributes
                    (rel_oid, attnum, attname, type_oid, type_name, typmod,
                     attnotnull, is_dropped, attndims, collation)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rel.rel_oid,
                        a.attnum,
                        a.attname,
                        a.type_oid,
                        a.type_name,
                        a.typmod,
                        int(a.attnotnull),
                        int(a.is_dropped),
                        a.attndims,
                        a.collation,
                    ),
                )
        self.conn.commit()

    def load_dictionary(self) -> DataDictionary:
        d = DataDictionary()
        meta = {r["key"]: r["value"] for r in self.conn.execute("SELECT key,value FROM meta")}
        d.version = meta.get("version", "1")
        d.pg_version = meta.get("pg_version", "")
        d.system_id = meta.get("system_id", "")
        d.created_at = meta.get("created_at", "")
        rels = {
            r["rel_oid"]: RelationDef(
                rel_oid=r["rel_oid"],
                schema_name=r["schema_name"],
                rel_name=r["rel_name"],
                relfilenode=r["relfilenode"],
                reltablespace=r["reltablespace"],
                db_oid=r["db_oid"],
                relkind=r["relkind"],
            )
            for r in self.conn.execute("SELECT * FROM relations")
        }
        for a in self.conn.execute("SELECT * FROM attributes ORDER BY rel_oid, attnum"):
            rel = rels.get(a["rel_oid"])
            if not rel:
                continue
            rel.attributes.append(
                AttributeDef(
                    attnum=a["attnum"],
                    attname=a["attname"],
                    type_oid=a["type_oid"],
                    type_name=a["type_name"] or "",
                    typmod=a["typmod"],
                    attnotnull=bool(a["attnotnull"]),
                    is_dropped=bool(a["is_dropped"]),
                    attndims=a["attndims"],
                    collation=a["collation"],
                )
            )
        d.relations = list(rels.values())
        return d

    def export_json(self, path: Path) -> None:
        d = self.load_dictionary()
        payload = {
            "version": d.version,
            "pg_version": d.pg_version,
            "system_id": d.system_id,
            "created_at": d.created_at,
            "relations": [
                {
                    **{k: v for k, v in asdict(r).items() if k != "attributes"},
                    "attributes": [asdict(a) for a in r.attributes],
                }
                for r in d.relations
            ],
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def import_json(self, path: Path) -> DataDictionary:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        d = DataDictionary(
            version=data.get("version", "1"),
            pg_version=data.get("pg_version", ""),
            system_id=data.get("system_id", ""),
            created_at=data.get("created_at", ""),
        )
        for r in data.get("relations", []):
            rel = RelationDef(
                rel_oid=r["rel_oid"],
                schema_name=r["schema_name"],
                rel_name=r["rel_name"],
                relfilenode=r["relfilenode"],
                reltablespace=r.get("reltablespace", 0),
                db_oid=r.get("db_oid", 0),
                relkind=r.get("relkind", "r"),
            )
            for a in r.get("attributes", []):
                rel.attributes.append(AttributeDef(**a))
            d.relations.append(rel)
        self.save_dictionary(d)
        return d
