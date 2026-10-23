"""analytics_store.py - o "caderno" do Analytics.

Fica no mesmo pedidos.db (arquivo SQLite), em tabelas proprias:
- analytics_extracao: uma linha por extracao (alvo + periodo + quando foi feita);
- analytics_linhas:   os numeros daquela extracao (uma linha por dimensao/chave/status).

Repetir a extracao do MESMO periodo SUBSTITUI a anterior (nao duplica). Isso e
proposital: pedido concluido pode ser cancelado ate 15 dias depois, entao reler e
sobrescrever e a forma certa de manter o numero atualizado.
"""
import sqlite3
from datetime import datetime, timezone

import store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics_extracao (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alvo        TEXT NOT NULL,
    modelo      TEXT,
    ini         TEXT NOT NULL,
    fim         TEXT NOT NULL,
    extraido_em TEXT NOT NULL,
    erros       TEXT,
    UNIQUE (alvo, ini, fim)
);
CREATE TABLE IF NOT EXISTS analytics_linhas (
    extracao_id       INTEGER NOT NULL REFERENCES analytics_extracao(id) ON DELETE CASCADE,
    dimensao          TEXT NOT NULL,
    chave             TEXT,
    status            TEXT,
    pedidos           INTEGER,
    gmv               REAL,
    gmv_sem_entrega   REAL,
    taxa_entrega      REAL,
    subsidio_parceiro REAL,
    taxas_loja        REAL,
    reembolso         REAL
);
CREATE INDEX IF NOT EXISTS ix_analytics_linhas_ext ON analytics_linhas (extracao_id, dimensao);
"""
CAMPOS = ("dimensao", "chave", "status", "pedidos", "gmv", "gmv_sem_entrega",
          "taxa_entrega", "subsidio_parceiro", "taxas_loja", "reembolso")


def _conn():
    con = sqlite3.connect(store.DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(_SCHEMA)
    return con


def salvar_extracao(alvo, modelo, ini, fim, linhas, erros=()):
    """Grava (ou substitui) a extracao do periodo. Tudo ou nada (transacao)."""
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = _conn()
    try:
        with con:  # commit se tudo der certo, rollback se algo falhar
            con.execute("DELETE FROM analytics_extracao WHERE alvo=? AND ini=? AND fim=?",
                        (alvo, str(ini), str(fim)))
            cur = con.execute(
                "INSERT INTO analytics_extracao (alvo, modelo, ini, fim, extraido_em, erros) "
                "VALUES (?,?,?,?,?,?)",
                (alvo, modelo, str(ini), str(fim), agora, "\n".join(erros)),
            )
            ext_id = cur.lastrowid
            con.executemany(
                f"INSERT INTO analytics_linhas (extracao_id, {', '.join(CAMPOS)}) "
                f"VALUES (?, {', '.join('?' for _ in CAMPOS)})",
                [(ext_id, *[l.get(c) for c in CAMPOS]) for l in linhas],
            )
        return ext_id
    finally:
        con.close()


def buscar(alvo, ini, fim):
    """Devolve (extracao_dict, [linhas]) ou (None, [])."""
    con = _conn()
    try:
        ext = con.execute("SELECT * FROM analytics_extracao WHERE alvo=? AND ini=? AND fim=?",
                          (alvo, str(ini), str(fim))).fetchone()
        if not ext:
            return None, []
        linhas = con.execute(
            f"SELECT {', '.join(CAMPOS)} FROM analytics_linhas WHERE extracao_id=?", (ext["id"],)
        ).fetchall()
        return dict(ext), [dict(l) for l in linhas]
    finally:
        con.close()
