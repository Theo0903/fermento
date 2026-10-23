"""fin_testar.py - descobre o endereco das APIs financeiras e testa as 4 APIs.

A documentacao do iFood mostra so o final do caminho (/v3/sales), sem o endereco
completo. Este script tenta os enderecos provaveis e diz qual o iFood reconhece.

Como usar (no cmd, na pasta do projeto):
    python fin_testar.py
    python fin_testar.py --loja <merchantId>    (padrao: a primeira de IFOOD_FIN_LOJAS)

Nao grava nada no banco. Usa o header de teste se IFOOD_FIN_HOMOLOGACAO=1.
"""
import argparse
import sys
from datetime import date, timedelta

from fin_client import FinClient, FinError, em_homologacao, lojas_configuradas

CANDIDATAS = [
    "/financial/v3.0/merchants/{merchantId}/{recurso}",  # confirmado em 22/10/2026
    "/financial/v3/{recurso}",
    "/financial/v3.0/{recurso}",
    "/financial/v3/merchants/{merchantId}/{recurso}",
    "/v3/{recurso}",
]
# a conciliacao respondeu 404 dentro da rota das outras APIs; estes sao os enderecos provaveis
CONC_CANDIDATAS = [
    "/financial/v3.0/merchants/{merchantId}/reconciliation-on-demand",
    "/financial/v3.0/merchants/{merchantId}/reconciliation/on-demand",
    "/financial/v3.0/merchants/{merchantId}/reconciliation/ondemand",
    "/financial/v3.0/merchants/{merchantId}/reconciliations/on-demand",
    "/financial/v3.0/merchants/{merchantId}/reconciliation",
    "/financial/v3.1/merchants/{merchantId}/reconciliation-on-demand",
    "/financial/v3.1/merchants/{merchantId}/reconciliation/on-demand",
    "/financial/v3/merchants/{merchantId}/reconciliation-on-demand",
    "/financial/v3/merchants/{merchantId}/reconciliation/on-demand",
    "/financial/v3.0/reconciliation-on-demand",
    "/financial/v3/reconciliation-on-demand",
]


def rota_existe(resp):
    """404 'no Route matched' = endereco errado. Qualquer outra resposta = o endereco existe."""
    return not (resp.status_code == 404 and "Route" in str(resp.text))


def descobrir(fin, loja, ontem):
    for rota in CANDIDATAS:
        fin.rota = rota
        resp = fin.chamar("GET", "sales", loja, {"beginSalesDate": str(ontem), "endSalesDate": str(ontem), "page": 0},
                          aceitos=(200, 400, 401, 403, 404, 422))
        print(f"  {rota:<50} -> {resp.status_code}")
        if rota_existe(resp):
            return rota, resp
    return None, None


def descobrir_conciliacao(fin, loja, mes):
    """POST com a competencia em cada endereco provavel; o primeiro que nao for 404 de rota e o certo.
    (Pedir o arquivo de um mes nao altera nada na loja; repetir em 6 h so reaproveita o pedido.)"""
    for rota in CONC_CANDIDATAS:
        resp = fin.chamar("POST", "", loja, json={"competencia": mes}, rota=rota + "{recurso}",
                          aceitos=(200, 201, 202, 400, 401, 403, 404, 405, 409, 422))
        print(f"  {rota:<70} -> {resp.status_code}")
        if rota_existe(resp) and resp.status_code != 405:
            return rota, resp
    return None, None


def main(argv=None, fin=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--loja")
    a = ap.parse_args(argv)
    loja = a.loja or (lojas_configuradas() or [None])[0]
    if not loja:
        print("ERRO: preencha IFOOD_FIN_LOJAS no .env (merchantId da loja de teste) ou use --loja.")
        return 1
    try:
        fin = fin or FinClient()
    except FinError as e:
        print("ERRO:", e)
        return 1
    print(f"Loja: {loja} | ambiente: {'TESTE (x-request-homologation)' if em_homologacao() else 'PRODUCAO'}")
    hoje = date.today()
    ontem = hoje - timedelta(days=1)

    print("\n1) Procurando o endereco das APIs financeiras...")
    try:
        rota, resp = descobrir(fin, loja, ontem)
    except FinError as e:
        print("\nNAO DEU CERTO:", e)
        return 1
    if not rota:
        print("\nNenhum endereco reconhecido. Abra o Swagger do Financial no Portal do Desenvolvedor,")
        print("copie o endereco completo de 'GET sales' e me envie.")
        return 1
    print(f"\n   ENCONTRADO. Coloque no .env:   IFOOD_FIN_ROTA={rota}")
    if resp.status_code != 200:
        print(f"   (o endereco existe, mas a resposta foi {resp.status_code}: {str(resp.text)[:200]})")

    print("\n2) Testando as 4 APIs com esse endereco:")
    fin.rota = rota
    testes = [
        ("Vendas (Sales)", lambda: f"{len(fin.vendas(loja, hoje - timedelta(days=6), hoje))} venda(s) nos ultimos 7 dias"),
        ("Lancamentos (Financial Events)", lambda: f"{len(fin.eventos(loja, hoje - timedelta(days=6), hoje))} lancamento(s) nos ultimos 7 dias"),
        ("Repasses (Settlement)", lambda: (lambda r: f"balance {r.get('balance')} | {len(r.get('settlements') or [])} periodo(s)"
                                           + (" (nenhuma liquidacao no periodo)" if r.get("vazio") else ""))(
            fin.liquidacoes(loja, hoje - timedelta(days=30), hoje))),
    ]
    ok = True
    for nome, f in testes:
        try:
            print(f"   OK   {nome}: {f()}")
        except FinError as e:
            ok = False
            print(f"   ERRO {nome}: {e}")
    mes = (date(hoje.year, hoje.month, 1) - timedelta(days=1)).strftime("%Y-%m")
    try:
        rid, reaproveitado = fin.pedir_conciliacao(loja, mes)
        print(f"   OK   Conciliacao: arquivo de {mes} pedido (requestId {rid}{', reaproveitado' if reaproveitado else ''}).")
        print("        Acompanhe na aba 'Arquivo de conciliacao' do painel.")
    except FinError as e:
        if "Endereço da API" not in str(e):
            ok = False
            print(f"   ERRO Conciliacao ({mes}): {e}")
        else:
            print(f"   ...  Conciliacao: o endereco padrao nao existe. Procurando o certo:")
            try:
                rota_c, resp = descobrir_conciliacao(fin, loja, mes)
            except FinError as e2:
                rota_c, resp = None, None
                print(f"        ERRO: {e2}")
            if rota_c:
                print(f"\n   ENCONTRADO. Coloque TAMBEM no .env:   IFOOD_FIN_ROTA_CONC={rota_c}")
                print(f"   (resposta {resp.status_code}: {str(resp.text)[:200]})")
                if resp.status_code not in (200, 201, 202, 409):
                    ok = False
            else:
                ok = False
                print("\n   Nenhum endereco de conciliacao reconhecido. No Swagger do Financial, procure")
                print("   'reconciliation' e me envie o endereco completo do POST.")
    print("\nTUDO CERTO." if ok else "\nAlgumas APIs falharam: me envie esta tela inteira.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
