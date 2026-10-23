"""homologar_analytics.py - faz o "exercicio de teste" do assistente de homologacao.

Como usar (no cmd, na pasta do projeto):
    python homologar_analytics.py                 -> usa IFOOD_BI_ALVO_ID do .env
    python homologar_analytics.py --id <UUID>     -> usa outro ID (ex.: loja de teste)

O que ele faz:
  1) chama a API de KPIs com o header x-request-homologation: true (ambiente de teste);
  2) salva a resposta completa em homologacao_payload.json (serve de prova/print);
  3) imprime, ja somados, os numeros que o assistente do Portal pede.
Ele NAO escreve nada no banco e nao mexe em pedidos.
"""
import argparse
import json
import os
import sys

from analytics import AnalyticsError, IFoodAnalytics

PASTA = os.path.dirname(os.path.abspath(__file__))

# Corpo identico ao exemplo oficial da documentacao de homologacao.
CORPO = {
    "agg": {
        "metrics": {"gmv": ["sum"], "gmvWithoutDelivery": ["sum", "avg"]},
        "groupBy": {"fields": ["dayOfWeek", "orderStatus", "deliveredBy", "paymentMethod", "salesChannel"]},
    },
    "filter": {"referenceDate": {"gte": "2026-06-01 00:00:00", "lte": "2026-06-07 23:59:59"}},
    "page": 1,
    "size": 20,
    "sort": {"groupByKey": "desc"},
}


def _soma(linhas, status=None):
    """Soma pedidos (groupByKey.count) e GMV das linhas (filtrando por status, se dado)."""
    pedidos = gmv = sem_entrega = 0.0
    for r in linhas:
        gk = r.get("groupByKey") or {}
        if status and (gk.get("value") or {}).get("orderStatus") != status:
            continue
        pedidos += gk.get("count") or 0
        gmv += (r.get("gmv") or {}).get("sum") or 0
        sem_entrega += (r.get("gmvWithoutDelivery") or {}).get("sum") or 0
    return int(pedidos), round(gmv, 2), round(sem_entrega, 2)


def resumo(pagina1, todas):
    """Monta os valores do assistente. Devolve dict (testavel)."""
    return {
        "totalItems": pagina1.get("totalItems"),
        "totalPages": pagina1.get("totalPages"),
        "registros_pagina_1": len(pagina1.get("data") or []),
        "concluidos": _soma(todas, "CONCLUDED"),
        "cancelados": _soma(todas, "CANCELLED"),
        "consolidado": _soma(todas),
        "pagina1": {  # mesmos numeros, mas SO com as linhas da 1a pagina
            "concluidos": _soma(pagina1.get("data") or [], "CONCLUDED"),
            "cancelados": _soma(pagina1.get("data") or [], "CANCELLED"),
            "consolidado": _soma(pagina1.get("data") or []),
        },
        "varias_paginas": (pagina1.get("totalPages") or 1) > 1,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="chain_id (rede) ou merchant_id (loja); padrao: IFOOD_BI_ALVO_ID")
    args = ap.parse_args(argv)
    try:
        api = IFoodAnalytics(alvo_id=args.id, homologacao=True)
        print(f"Modelo: {api.modelo} | ID: {api.alvo_id} | ambiente de TESTE (x-request-homologation)")
        pagina1 = api.consulta(CORPO)
        todas, _ = api.consulta_todas(CORPO)
    except AnalyticsError as e:
        print("\nNAO DEU CERTO:", e)
        return 1

    with open(os.path.join(PASTA, "homologacao_payload.json"), "w", encoding="utf-8") as f:
        json.dump(pagina1, f, ensure_ascii=False, indent=2)
    r = resumo(pagina1, todas)

    print("\n=== Passo 3 do assistente: PAGINACAO ===")
    print(f"totalItems ............... {r['totalItems']}")
    print(f"totalPages ............... {r['totalPages']}")
    print(f"registros na 1a pagina ... {r['registros_pagina_1']}")
    print("\n=== Passo 4 do assistente: METRICAS ===")
    for rotulo, chave in (("PEDIDOS CONCLUIDOS", "concluidos"), ("PEDIDOS CANCELADOS", "cancelados")):
        n, gmv, sem = r[chave]
        print(f"{rotulo}: total de pedidos = {n} | GMV total = {gmv:.2f} | GMV sem entrega = {sem:.2f}")
    n, gmv, _ = r["consolidado"]
    print(f"ANALISE CONSOLIDADA: GMV total = {gmv:.2f} | total de pedidos = {n}")
    if r["varias_paginas"]:
        p = r["pagina1"]
        print("\n=== ATENCAO: a resposta tem varias paginas ===")
        print("Os numeros acima somam TODAS as paginas. Se a tela do assistente disser que os")
        print("valores sao 'da primeira pagina' / 'do payload retornado', use estes (SO 1a pagina):")
        for rotulo, chave in (("PEDIDOS CONCLUIDOS", "concluidos"), ("PEDIDOS CANCELADOS", "cancelados")):
            n, gmv, sem = p[chave]
            print(f"{rotulo}: total de pedidos = {n} | GMV total = {gmv:.2f} | GMV sem entrega = {sem:.2f}")
        n, gmv, _ = p["consolidado"]
        print(f"ANALISE CONSOLIDADA: GMV total = {gmv:.2f} | total de pedidos = {n}")
    print("\nResposta completa salva em homologacao_payload.json (use para o print, se pedirem).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
