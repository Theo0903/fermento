"""fin_store.py - o "caderno" do financeiro (arquivo financeiro.db, SQLite).

Separado do pedidos.db para nao disputar o arquivo com o main.py e para facilitar
a migracao para o SQL Server (revisao do Teo).

Guarda TUDO: cada tabela tem a coluna `json` com o registro completo devolvido
pelo iFood (nenhum campo se perde), mais colunas organizadas para a tela.

Regravar e seguro (idempotente):
- vendas e titulos: atualiza pela chave (loja + pedido / loja + titulo);
- lancamentos e liquidacoes: substitui a SEMANA inteira extraida de novo;
- conciliacao: substitui o arquivo do mes.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.getenv("IFOOD_FIN_DB", os.path.join(PASTA, "financeiro.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fin_venda (
    merchant_id TEXT NOT NULL, order_id TEXT NOT NULL, short_id TEXT, loja_nome TEXT,
    criado_em TEXT, dia TEXT, status TEXT, canal TEXT, categoria TEXT, tipo TEXT,
    bag REAL, taxa_entrega REAL, taxa_servico REAL, bruto REAL,
    desc_total REAL, desc_ifood REAL, desc_loja REAL, desc_rede REAL, desc_externo REAL,
    desc_itens REAL, desc_entrega REAL,
    entrega_tipo TEXT, logistica TEXT, pagamento TEXT, responsavel TEXT,
    entrada REAL, comissao REAL, taxas REAL, subsidios REAL, outros REAL,
    liquido REAL, soma_lancamentos REAL, repasse_previsto TEXT,
    json TEXT, atualizado_em TEXT,
    PRIMARY KEY (merchant_id, order_id)
);
CREATE INDEX IF NOT EXISTS ix_fin_venda_dia ON fin_venda (merchant_id, dia);
CREATE TABLE IF NOT EXISTS fin_venda_lanc (
    merchant_id TEXT NOT NULL, order_id TEXT NOT NULL, nome TEXT, grupo TEXT, valor REAL
);
CREATE INDEX IF NOT EXISTS ix_fin_venda_lanc ON fin_venda_lanc (merchant_id, order_id);
CREATE TABLE IF NOT EXISTS fin_evento (
    merchant_id TEXT NOT NULL, semana TEXT NOT NULL, seq INTEGER,
    nome TEXT, descricao TEXT, produto TEXT, gatilho TEXT, data_hora TEXT, dia TEXT, competencia TEXT,
    periodo_ini TEXT, periodo_fim TEXT, ref_tipo TEXT, ref_id TEXT, ref_data TEXT,
    impacto INTEGER, valor REAL, base REAL, percentual REAL, repasse_esperado TEXT,
    metodo TEXT, bandeira TEXT, responsavel TEXT, json TEXT
);
CREATE INDEX IF NOT EXISTS ix_fin_evento ON fin_evento (merchant_id, semana);
CREATE INDEX IF NOT EXISTS ix_fin_evento_ref ON fin_evento (merchant_id, ref_id);
CREATE TABLE IF NOT EXISTS fin_liquidacao (
    merchant_id TEXT NOT NULL, janela_ini TEXT NOT NULL, janela_fim TEXT, balance REAL, json TEXT,
    PRIMARY KEY (merchant_id, janela_ini)
);
CREATE TABLE IF NOT EXISTS fin_titulo (
    merchant_id TEXT NOT NULL, titulo_id TEXT NOT NULL, periodo_ini TEXT, periodo_fim TEXT,
    tipo TEXT, produto TEXT, valor REAL, status TEXT, data_pagamento TEXT, transacao TEXT,
    banco TEXT, agencia TEXT, conta TEXT, json TEXT,
    PRIMARY KEY (merchant_id, titulo_id)
);
CREATE TABLE IF NOT EXISTS fin_conciliacao (
    merchant_id TEXT NOT NULL, competencia TEXT NOT NULL, request_id TEXT, status TEXT,
    mensagem TEXT, pedido_em TEXT, atualizado_em TEXT, pronto_em TEXT, arquivo TEXT,
    linhas INTEGER, total_impacto REAL,
    PRIMARY KEY (merchant_id, competencia)
);
CREATE TABLE IF NOT EXISTS fin_conc_linha (
    merchant_id TEXT NOT NULL, competencia TEXT NOT NULL, n INTEGER,
    pedido TEXT, fato_gerador TEXT, tipo_lancamento TEXT, descricao TEXT, valor REAL, impacto INTEGER,
    data_repasse TEXT, id_saldo TEXT, valor_transacao REAL, json TEXT
);
CREATE INDEX IF NOT EXISTS ix_fin_conc ON fin_conc_linha (merchant_id, competencia);
CREATE TABLE IF NOT EXISTS fin_job (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT, merchant_id TEXT, ini TEXT, fim TEXT,
    status TEXT, passo INTEGER, total INTEGER, mensagem TEXT, criado_em TEXT, fim_em TEXT
);
"""


def agora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _j(obj):
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------- gravar
def salvar_vendas(merchant_id, linhas):
    """linhas = saida de fin_calculos.venda_para_linha (dict com 'lancamentos')."""
    con = conn()
    try:
        with con:
            for l in linhas:
                campos = {k: v for k, v in l.items() if k != "lancamentos"}
                campos.update(merchant_id=merchant_id, atualizado_em=agora())
                cols = ", ".join(campos)
                con.execute(f"INSERT OR REPLACE INTO fin_venda ({cols}) VALUES ({', '.join('?' * len(campos))})",
                            list(campos.values()))
                con.execute("DELETE FROM fin_venda_lanc WHERE merchant_id=? AND order_id=?", (merchant_id, l["order_id"]))
                con.executemany("INSERT INTO fin_venda_lanc VALUES (?,?,?,?,?)",
                                [(merchant_id, l["order_id"], n, g, v) for n, g, v in l["lancamentos"]])
    finally:
        con.close()


def salvar_eventos_semana(merchant_id, semana, linhas):
    """Substitui todos os lancamentos daquela semana (rodar de novo nao duplica)."""
    con = conn()
    try:
        with con:
            con.execute("DELETE FROM fin_evento WHERE merchant_id=? AND semana=?", (merchant_id, semana))
            for i, l in enumerate(linhas):
                campos = dict(l, merchant_id=merchant_id, semana=semana, seq=i)
                cols = ", ".join(campos)
                con.execute(f"INSERT INTO fin_evento ({cols}) VALUES ({', '.join('?' * len(campos))})",
                            list(campos.values()))
    finally:
        con.close()


def salvar_liquidacao(merchant_id, ini, fim, resposta, titulos):
    con = conn()
    try:
        with con:
            con.execute("INSERT OR REPLACE INTO fin_liquidacao VALUES (?,?,?,?,?)",
                        (merchant_id, str(ini), str(fim), float(resposta.get("balance") or 0), _j(resposta)))
            for t in titulos:
                campos = dict(t, merchant_id=merchant_id)
                cols = ", ".join(campos)
                con.execute(f"INSERT OR REPLACE INTO fin_titulo ({cols}) VALUES ({', '.join('?' * len(campos))})",
                            list(campos.values()))
    finally:
        con.close()


def salvar_conciliacao(merchant_id, competencia, **campos):
    con = conn()
    try:
        with con:
            atual = con.execute("SELECT * FROM fin_conciliacao WHERE merchant_id=? AND competencia=?",
                                (merchant_id, competencia)).fetchone()
            dados = dict(atual) if atual else {"merchant_id": merchant_id, "competencia": competencia}
            dados.update(campos, atualizado_em=agora())
            cols = ", ".join(dados)
            con.execute(f"INSERT OR REPLACE INTO fin_conciliacao ({cols}) VALUES ({', '.join('?' * len(dados))})",
                        list(dados.values()))
    finally:
        con.close()


def salvar_linhas_conciliacao(merchant_id, competencia, linhas):
    con = conn()
    try:
        with con:
            con.execute("DELETE FROM fin_conc_linha WHERE merchant_id=? AND competencia=?", (merchant_id, competencia))
            for i, l in enumerate(linhas):
                campos = dict(l, merchant_id=merchant_id, competencia=competencia, n=i)
                cols = ", ".join(campos)
                con.execute(f"INSERT INTO fin_conc_linha ({cols}) VALUES ({', '.join('?' * len(campos))})",
                            list(campos.values()))
    finally:
        con.close()


# ---------------------------------------------------------------- tarefas em segundo plano
def criar_job(tipo, merchant_id, ini, fim, total):
    con = conn()
    try:
        with con:
            cur = con.execute("INSERT INTO fin_job (tipo, merchant_id, ini, fim, status, passo, total, mensagem, criado_em) "
                              "VALUES (?,?,?,?,?,?,?,?,?)",
                              (tipo, merchant_id, str(ini), str(fim), "rodando", 0, total, "", agora()))
            return cur.lastrowid
    finally:
        con.close()


def atualizar_job(job_id, **campos):
    if campos.get("status") in ("concluido", "erro", "parcial"):
        campos["fim_em"] = agora()
    con = conn()
    try:
        with con:
            sets = ", ".join(f"{k}=?" for k in campos)
            con.execute(f"UPDATE fin_job SET {sets} WHERE id=?", (*campos.values(), job_id))
    finally:
        con.close()


def jobs(limite=10):
    return consultar("SELECT * FROM fin_job ORDER BY id DESC LIMIT ?", (limite,))


def job_rodando():
    r = consultar("SELECT * FROM fin_job WHERE status='rodando' ORDER BY id DESC LIMIT 1")
    return r[0] if r else None


def marcar_jobs_interrompidos():
    """Ao abrir o painel: tarefas que ficaram 'rodando' (painel fechado no meio) viram 'interrompido'."""
    con = conn()
    try:
        with con:
            con.execute("UPDATE fin_job SET status='interrompido', mensagem='O painel foi fechado durante a "
                        "extração. Rode de novo: o que já veio fica guardado.' WHERE status='rodando'")
    finally:
        con.close()


# ---------------------------------------------------------------- ler
def consultar(sql, params=()):
    con = conn()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def cobertura():
    """O que ja esta no banco, por loja: primeira e ultima data de vendas e lancamentos."""
    vendas = {r["merchant_id"]: r for r in consultar(
        "SELECT merchant_id, MIN(dia) ini, MAX(dia) fim, COUNT(*) n, MAX(loja_nome) loja "
        "FROM fin_venda GROUP BY merchant_id")}
    eventos = {r["merchant_id"]: r for r in consultar(
        "SELECT merchant_id, MIN(semana) ini, MAX(semana) fim, COUNT(*) n FROM fin_evento GROUP BY merchant_id")}
    saida = []
    for m in sorted(set(vendas) | set(eventos)):
        v, e = vendas.get(m, {}), eventos.get(m, {})
        saida.append({"merchant_id": m, "loja": v.get("loja"), "v_ini": v.get("ini"), "v_fim": v.get("fim"),
                      "v_n": v.get("n", 0), "e_ini": e.get("ini"), "e_fim": e.get("fim"), "e_n": e.get("n", 0)})
    return saida
