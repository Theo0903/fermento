"""store.py - o "caderno" da padaria.

Guarda em um arquivo SQLite (pedidos.db) tudo que o main.py recebe:
- eventos: cada aviso do iFood (com o id, para nao repetir);
- pedidos: os detalhes completos (JSON) e o status atual.

O main.py escreve; o painel.py le (e tambem grava ao importar um pedido).
Como e SQLite, voce pode abrir pedidos.db no DB Browser ou no Power BI.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

# O banco fica SEMPRE na pasta deste arquivo, nao importa de onde o programa
# foi aberto (assim main.py e painel.py enxergam o mesmo caderno).
PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.getenv("IFOOD_DB", os.path.join(PASTA, "pedidos.db"))

# Os estados que o iFood usa no ciclo de vida do pedido
ESTADOS = {
    "PLACED", "CONFIRMED", "PREPARATION_STARTED", "READY_TO_PICKUP",
    "DISPATCHED", "CONCLUDED", "CANCELLED",
}
# codigos curtos que aparecem no campo "code" (usados se faltar o fullCode)
CODIGOS_CURTOS = {
    "PLC": "PLACED", "CFM": "CONFIRMED", "PRS": "PREPARATION_STARTED",
    "RTP": "READY_TO_PICKUP", "DSP": "DISPATCHED", "CON": "CONCLUDED",
    "CAN": "CANCELLED",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eventos (
    event_id   TEXT PRIMARY KEY,
    order_id   TEXT,
    full_code  TEXT,
    code       TEXT,
    criado_em  TEXT,
    bruto      TEXT
);
CREATE TABLE IF NOT EXISTS pedidos (
    order_id   TEXT PRIMARY KEY,
    display_id TEXT,
    status     TEXT,
    status_em  TEXT,
    detalhes   TEXT
);
"""


def _conn():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def estado_do_evento(ev):
    """Traduz um evento em estado do pedido (ou None, se nao muda o estado)."""
    cod = ev.get("fullCode") or ev.get("code")
    cod = CODIGOS_CURTOS.get(cod, cod)
    return cod if cod in ESTADOS else None


def salvar_evento(ev):
    """Grava o evento e, se ele muda o estado do pedido, atualiza o estado."""
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO eventos VALUES (?,?,?,?,?,?)",
            (ev["id"], ev.get("orderId"), ev.get("fullCode"), ev.get("code"),
             ev.get("createdAt"), json.dumps(ev, ensure_ascii=False)),
        )
        estado = estado_do_evento(ev)
        if estado and ev.get("orderId"):
            quando = ev.get("createdAt") or datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT OR IGNORE INTO pedidos (order_id) VALUES (?)",
                (ev["orderId"],),
            )
            # so aceita se for mais novo (evita um evento atrasado "voltar" o estado)
            con.execute(
                "UPDATE pedidos SET status=?, status_em=? "
                "WHERE order_id=? AND (status_em IS NULL OR status_em <= ?)",
                (estado, quando, ev["orderId"], quando),
            )


def salvar_pedido(pedido):
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO pedidos (order_id) VALUES (?)", (pedido["id"],)
        )
        con.execute(
            "UPDATE pedidos SET display_id=?, detalhes=? WHERE order_id=?",
            (pedido.get("displayId"), json.dumps(pedido, ensure_ascii=False), pedido["id"]),
        )


def tem_detalhes(order_id):
    with _conn() as con:
        r = con.execute(
            "SELECT detalhes FROM pedidos WHERE order_id=?", (order_id,)
        ).fetchone()
    return bool(r and r["detalhes"])


def status(order_id):
    with _conn() as con:
        r = con.execute("SELECT status FROM pedidos WHERE order_id=?", (order_id,)).fetchone()
    return r["status"] if r else None


def listar_pedidos(limite=30):
    with _conn() as con:
        linhas = con.execute(
            "SELECT * FROM pedidos ORDER BY COALESCE(status_em, '') DESC, order_id LIMIT ?",
            (limite,),
        ).fetchall()
    saida = []
    for r in linhas:
        d = dict(r)
        d["detalhes"] = json.loads(r["detalhes"]) if r["detalhes"] else None
        saida.append(d)
    return saida


def listar_eventos(limite=40):
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT event_id, order_id, full_code, code, criado_em FROM eventos "
            "ORDER BY COALESCE(criado_em, '') DESC LIMIT ?", (limite,)
        ).fetchall()]
