"""fin_extrator.py - busca o financeiro no iFood e grava no financeiro.db.

Periodos longos (ex.: desde janeiro, 6 meses, 1 ano) sao quebrados em SEMANAS
(segunda a domingo), como a documentacao recomenda. Para cada loja e semana:
vendas (Sales) + lancamentos (Financial Events) + liquidacao (Settlement).

Uso pelo painel (botao Extrair, roda em segundo plano) ou pelo cmd:
    python fin_extrator.py --ini 2026-01-01 --fim 2026-06-30
    python fin_extrator.py --recentes            (ultimos 35 dias)
    python fin_extrator.py --ini ... --fim ... --loja <merchantId>

Rodar de novo o mesmo periodo NAO duplica: substitui/atualiza o que ja existe.
"""
import argparse
import os
import sys
import threading
import time
from datetime import date, timedelta

import fin_calculos as calc
import fin_store as store
from fin_client import FinClient, FinError, lojas_configuradas

MAX_DIAS_EXTRACAO = 366
DIAS_RECENTES = 35
PASTA_ARQ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conciliacao")
_trava = threading.Lock()


def erro_fatal(e):
    """Erros que param TUDO (credencial ou endereco errado: nenhuma loja vai funcionar)."""
    return e.status == 401 or "Endereço da API" in str(e) or "login" in str(e).lower()


def checar_periodo(ini, fim, hoje=None):
    hoje = hoje or date.today()
    a, b = calc.data(ini), calc.data(fim)
    if a > b:
        return "A data inicial é depois da data final."
    if a > hoje:
        return "A data inicial está no futuro."
    if (b - a).days + 1 > MAX_DIAS_EXTRACAO:
        return f"Período grande demais: até {MAX_DIAS_EXTRACAO} dias (1 ano) por extração. Para mais, extraia em partes."
    return ""


# ---------------------------------------------------------------- extracao (vendas + lancamentos + repasses)
def extrair(fin, lojas, ini, fim, job_id=None, hoje=None, avisar=None):
    """Roda a extracao. Devolve dict com o resultado. Nunca deixa o job 'rodando' para tras."""
    sems = calc.semanas(ini, fim, hoje)
    total = len(sems) * len(lojas)
    passo, erros, primeira = 0, [], {}
    contagem = {"vendas": 0, "lancamentos": 0, "titulos": 0}
    try:
        for i, m in enumerate(lojas, 1):
            for k, (seg, ultimo) in enumerate(sems):
                rotulo = f"Loja {i}/{len(lojas)} · semana de {seg:%d/%m/%Y}"
                if job_id:
                    store.atualizar_job(job_id, passo=passo, mensagem=rotulo)
                if avisar:
                    avisar(f"[{passo + 1}/{total}] {rotulo}")
                try:
                    vendas = [calc.venda_para_linha(s) for s in fin.vendas(m, seg, ultimo)]
                    store.salvar_vendas(m, vendas)
                    eventos = [calc.evento_para_linha(e) for e in fin.eventos(m, seg, ultimo)]
                    store.salvar_eventos_semana(m, seg.isoformat(), eventos)
                    liq = fin.liquidacoes(m, seg, ultimo)
                    titulos = calc.titulos_da_liquidacao(liq)
                    store.salvar_liquidacao(m, seg, ultimo, liq, titulos)
                    contagem["vendas"] += len(vendas)
                    contagem["lancamentos"] += len(eventos)
                    contagem["titulos"] += len(titulos)
                    if (vendas or eventos) and m not in primeira:
                        primeira[m] = seg
                except FinError as e:
                    if erro_fatal(e):
                        raise
                    if e.status == 403:  # sem permissao NESTA loja: pula a loja, segue as outras
                        erros.append(f"Loja {m}: {e}")
                        passo += len(sems) - k
                        break
                    erros.append(f"{rotulo}: {e}")
                passo += 1
        status = "parcial" if erros else "concluido"
        msg = (f"{contagem['vendas']} vendas, {contagem['lancamentos']} lançamentos e {contagem['titulos']} títulos "
               f"de {len(sems)} semana(s).")
        if primeira:
            msg += " Primeira semana com dados: " + ", ".join(f"{d:%d/%m/%Y}" for d in primeira.values()) + "."
        elif total:
            msg += " Nenhum dado encontrado no período."
        if erros:
            msg += f" {len(erros)} semana(s) com falha (rode de novo para completar): " + " | ".join(erros[:5])
    except FinError as e:
        status, msg = "erro", f"Parou: {e}"
    except Exception as e:  # nunca deixa a tarefa "rodando" para sempre
        status, msg = "erro", f"Erro inesperado: {type(e).__name__}: {e}"
    if job_id:
        store.atualizar_job(job_id, status=status, passo=passo, mensagem=msg)
    return {"status": status, "mensagem": msg, "erros": erros, **contagem}


def iniciar_extracao(fabrica_fin, lojas, ini, fim, em_segundo_plano=True):
    """Cria a tarefa e roda numa linha separada (o painel continua respondendo)."""
    with _trava:
        if store.job_rodando():
            raise FinError("Já existe uma extração rodando. Espere terminar.")
        job_id = store.criar_job("extracao", ",".join(lojas), ini, fim, len(calc.semanas(ini, fim)) * len(lojas))
    def rodar():
        try:
            fin = fabrica_fin()
        except FinError as e:
            store.atualizar_job(job_id, status="erro", mensagem=str(e))
            return
        extrair(fin, lojas, ini, fim, job_id)
    if em_segundo_plano:
        threading.Thread(target=rodar, daemon=True).start()
    else:
        rodar()
    return job_id


# ---------------------------------------------------------------- arquivo de conciliacao (mensal)
def pedir_conciliacao(fin, m, comp):
    prob = calc.competencia_valida(comp)
    if prob:
        raise FinError(prob, 400)
    rid, reutilizado = fin.pedir_conciliacao(m, comp)
    store.salvar_conciliacao(m, comp, request_id=rid, status="solicitado", pedido_em=store.agora(),
                             pronto_em=None, mensagem="Pedido reaproveitado (já havia um recente)." if reutilizado
                             else "Pedido enviado ao iFood.")
    return rid


def verificar_conciliacao(fin, m, comp):
    """Uma verificacao. Se pronto: baixa, le e grava. Devolve o status."""
    reg = store.consultar("SELECT * FROM fin_conciliacao WHERE merchant_id=? AND competencia=?", (m, comp))
    if not reg or not reg[0]["request_id"]:
        raise FinError("Este mês ainda não foi pedido.")
    try:
        status, url = fin.status_conciliacao(m, reg[0]["request_id"])
    except FinError as e:
        if e.status == 404:
            store.salvar_conciliacao(m, comp, status="expirado",
                                     mensagem="O pedido expirou (vale 24 h). Peça o arquivo de novo.")
            return "expirado"
        raise
    if status != "pronto":
        store.salvar_conciliacao(m, comp, status="processando", mensagem="O iFood ainda está gerando o arquivo.")
        return "processando"
    os.makedirs(PASTA_ARQ, exist_ok=True)
    destino = os.path.join(PASTA_ARQ, f"{m}_{comp}.csv.gz")
    fin.baixar(url, destino)
    linhas, resumo, _ = calc.ler_conciliacao(destino)
    store.salvar_linhas_conciliacao(m, comp, linhas)
    store.salvar_conciliacao(m, comp, status="pronto", pronto_em=store.agora(), arquivo=destino,
                             linhas=resumo["linhas"], total_impacto=resumo["total_impacto"],
                             mensagem="Arquivo baixado e conferido.")
    return "pronto"


def acompanhar_conciliacao(fabrica_fin, m, comp, sleep=time.sleep, max_seg=30 * 60):
    """Polling em segundo plano: 30 s nas 5 primeiras vezes, depois 60 s; no maximo 30 min."""
    def rodar():
        try:
            fin = fabrica_fin()
            esperado, tentativa = 0, 0
            while esperado <= max_seg:
                st = verificar_conciliacao(fin, m, comp)
                if st in ("pronto", "expirado"):
                    return
                tentativa += 1
                pausa = 30 if tentativa <= 5 else 60
                sleep(pausa)
                esperado += pausa
            store.salvar_conciliacao(m, comp, status="demorando",
                                     mensagem="Passou de 30 min. Clique em 'Verificar agora' mais tarde.")
        except FinError as e:
            store.salvar_conciliacao(m, comp, status="erro", mensagem=str(e))
        except Exception as e:
            store.salvar_conciliacao(m, comp, status="erro", mensagem=f"Erro inesperado: {e}")
    t = threading.Thread(target=rodar, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- uso pelo cmd
def main(argv=None):
    ap = argparse.ArgumentParser(description="Extrai o financeiro do iFood para o financeiro.db")
    ap.add_argument("--ini")
    ap.add_argument("--fim")
    ap.add_argument("--recentes", action="store_true", help=f"últimos {DIAS_RECENTES} dias")
    ap.add_argument("--loja", help="merchantId (padrão: todas de IFOOD_FIN_LOJAS)")
    a = ap.parse_args(argv)
    hoje = date.today()
    if a.recentes:
        ini, fim = hoje - timedelta(days=DIAS_RECENTES), hoje
    elif a.ini and a.fim:
        ini, fim = calc.data(a.ini), calc.data(a.fim)
    else:
        ap.error("informe --ini e --fim, ou --recentes")
    prob = checar_periodo(ini, fim)
    if prob:
        print("ERRO:", prob)
        return 1
    lojas = [a.loja] if a.loja else lojas_configuradas()
    if not lojas:
        print("ERRO: preencha IFOOD_FIN_LOJAS no .env (merchantId da loja; varias separadas por vírgula).")
        return 1
    try:
        fin = FinClient()
    except FinError as e:
        print("ERRO:", e)
        return 1
    job = store.criar_job("extracao", ",".join(lojas), ini, fim, len(calc.semanas(ini, fim)) * len(lojas))
    r = extrair(fin, lojas, ini, fim, job, avisar=print)
    print(f"\n{r['status'].upper()}: {r['mensagem']}")
    return 0 if r["status"] == "concluido" else 1


if __name__ == "__main__":
    sys.exit(main())
