"""fin_calculos.py - as contas do financeiro (sem rede, sem banco: so calculo, facil de testar).

- transforma a resposta do iFood em linhas organizadas (vendas, lancamentos, titulos);
- classifica os lancamentos (comissao, taxas, subsidios...);
- confere integridade e faz a conciliacao (tolerancia de 0,01%, como a documentacao pede);
- le o arquivo de conciliacao (.csv.gz, separador ';').
"""
import csv
import gzip
import io
import json
from datetime import date, datetime, timedelta, timezone

FUSO_HORAS = -3
TOLERANCIA = 0.0001  # 0,01%


# ---------------------------------------------------------------- utilidades
def num(v):
    """Aceita 12.34, "12.34", "12,34", "1.234,56". Texto nao numerico -> None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("R$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def sim(v):
    return str(v).strip().upper() in ("TRUE", "SIM", "1", "S", "YES")


def data(v):
    return v if isinstance(v, date) and not isinstance(v, datetime) else datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def dia_local(iso):
    """'2025-02-20T01:41:25Z' (UTC) -> '2025-02-19' (horario de Brasilia)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo:  # tem fuso: converte para Brasilia; sem fuso: ja e horario local
            dt = dt.astimezone(timezone(timedelta(hours=FUSO_HORAS)))
        return dt.date().isoformat()
    except ValueError:
        return str(iso)[:10]


def semanas(ini, fim, hoje=None):
    """Quebra [ini, fim] em semanas de segunda a domingo (como o iFood apura).

    Estica o inicio ate a segunda-feira e o fim ate o domingo, sem passar de hoje.
    Devolve [(segunda, ultimo_dia), ...]. A segunda-feira e a "chave" da semana:
    extrair de novo a mesma semana substitui, nao duplica.
    """
    hoje = hoje or date.today()
    a, b = data(ini), min(data(fim), hoje)
    seg = a - timedelta(days=a.weekday())
    saida = []
    while seg <= b:
        dom = seg + timedelta(days=6)
        saida.append((seg, min(dom, hoje)))
        seg += timedelta(days=7)
    return saida


# ---------------------------------------------------------------- lancamentos de venda
def grupo_lancamento(nome):
    n = str(nome or "").upper()
    if "COMMISSION" in n:
        return "comissao"
    if "SUBSIDY" in n:
        return "subsidio"
    if "PAYMENT" in n:
        return "entrada"
    if "FEE" in n:
        return "taxa"
    return "outros"


GRUPO_PT = {"entrada": "Entrada (pago pelo cliente)", "comissao": "Comissão iFood", "taxa": "Taxas",
            "subsidio": "Subsídios recebidos", "outros": "Outros"}
PATROCINADOR = {"IFOOD": "ifood", "MERCHANT": "loja", "CHAIN": "rede"}


def venda_para_linha(s):
    """Uma venda da API Sales -> linha organizada (+ lista de lancamentos)."""
    m = s.get("merchant") or {}
    g = s.get("saleGrossValue") or {}
    bag, entrega, servico = (num(g.get(k)) or 0.0 for k in ("bag", "deliveryFee", "serviceFee"))

    desc = {"ifood": 0.0, "loja": 0.0, "rede": 0.0, "externo": 0.0}
    alvo = {"ITEM": 0.0, "DELIVERY_FEE": 0.0}
    b = s.get("benefits") or {}
    for ben in b.get("benefits") or []:
        alvo[ben.get("target")] = alvo.get(ben.get("target"), 0.0) + (num(ben.get("value")) or 0.0)
        for sp in ben.get("sponsorships") or []:
            desc[PATROCINADOR.get(str(sp.get("name")).upper(), "externo")] += num(sp.get("value")) or 0.0
    desc_total = num(b.get("totalValue"))
    if desc_total is None:
        desc_total = sum(desc.values())

    metodos, responsaveis = [], []
    for mt in (s.get("payments") or {}).get("methods") or []:
        partes = [mt.get("method") or "?"]
        if (mt.get("card") or {}).get("brand"):
            partes.append(mt["card"]["brand"])
        if (mt.get("wallet") or {}).get("name"):
            partes.append(mt["wallet"]["name"])
        parc = (mt.get("installment") or {}).get("maxInstallments")
        if parc and int(parc) > 1:
            partes.append(f"{parc}x")
        metodos.append(" ".join(partes))
        if mt.get("liability") and mt["liability"] not in responsaveis:
            responsaveis.append(mt["liability"])

    bs = s.get("billingSummary") or {}
    lancs, somas = [], {"entrada": 0.0, "comissao": 0.0, "taxa": 0.0, "subsidio": 0.0, "outros": 0.0}
    for e in bs.get("billingEntries") or []:
        v = num(e.get("value")) or 0.0
        gr = grupo_lancamento(e.get("name"))
        somas[gr] += v
        lancs.append((e.get("name"), gr, v))

    datas_repasse = sorted({ent.get("expectedPaymentDate")
                            for ev in s.get("orderEvents") or []
                            for ent in ((ev.get("metadata") or {}).get("entries") or [])
                            if ent.get("expectedPaymentDate")})
    if not datas_repasse:
        repasse = None
    elif len(datas_repasse) == 1:
        repasse = datas_repasse[0]
    else:
        repasse = f"{datas_repasse[0]} a {datas_repasse[-1]} ({len(datas_repasse)} datas)"

    entrega_info = s.get("delivery") or {}
    return {
        "order_id": s.get("id"), "short_id": str(s.get("shortId") or ""), "loja_nome": m.get("name"),
        "criado_em": s.get("createdAt"), "dia": dia_local(s.get("createdAt")),
        "status": s.get("currentStatus"), "canal": s.get("salesChannel"),
        "categoria": s.get("category"), "tipo": s.get("type"),
        "bag": bag, "taxa_entrega": entrega, "taxa_servico": servico, "bruto": round(bag + entrega + servico, 2),
        "desc_total": round(desc_total, 2), "desc_ifood": round(desc["ifood"], 2), "desc_loja": round(desc["loja"], 2),
        "desc_rede": round(desc["rede"], 2), "desc_externo": round(desc["externo"], 2),
        "desc_itens": round(alvo.get("ITEM", 0.0), 2), "desc_entrega": round(alvo.get("DELIVERY_FEE", 0.0), 2),
        "entrega_tipo": entrega_info.get("type"),
        "logistica": (entrega_info.get("deliveryParameters") or {}).get("logisticProvider"),
        "pagamento": " + ".join(metodos), "responsavel": ", ".join(responsaveis),
        "entrada": round(somas["entrada"], 2), "comissao": round(somas["comissao"], 2), "taxas": round(somas["taxa"], 2),
        "subsidios": round(somas["subsidio"], 2), "outros": round(somas["outros"], 2),
        "liquido": num(bs.get("saleBalance")), "soma_lancamentos": round(sum(v for _, _, v in lancs), 2),
        "repasse_previsto": repasse, "json": json.dumps(s, ensure_ascii=False),
        "lancamentos": lancs,
    }


# ---------------------------------------------------------------- lancamentos financeiros (Financial Events)
def evento_para_linha(e):
    ref, per = e.get("reference") or {}, e.get("period") or {}
    bil, pag = e.get("billing") or {}, e.get("payment") or {}
    amount = e.get("amount")
    valor = num(amount.get("value") if isinstance(amount, dict) else amount)
    quando = e.get("dateTime") or ref.get("date") or per.get("beginDate")
    return {
        "nome": e.get("name"), "descricao": e.get("description"), "produto": e.get("product"),
        "gatilho": e.get("trigger"), "data_hora": quando, "dia": dia_local(quando),
        "competencia": e.get("competence"), "periodo_ini": per.get("beginDate"), "periodo_fim": per.get("endDate"),
        "ref_tipo": ref.get("type"), "ref_id": ref.get("id"), "ref_data": ref.get("date"),
        "impacto": 1 if sim(e.get("hasTransferImpact")) else 0,
        "valor": valor if valor is not None else 0.0,
        "base": num(bil.get("baseValue")), "percentual": num(bil.get("feePercentage")),
        "repasse_esperado": (e.get("settlement") or {}).get("expectedDate"),
        "metodo": pag.get("method"), "bandeira": pag.get("brand"), "responsavel": pag.get("liability"),
        "json": json.dumps(e, ensure_ascii=False),
    }


# ---------------------------------------------------------------- liquidacoes (Settlement)
def mascarar(conta, digito):
    if not conta:
        return ""
    c = str(conta)
    return ("•" * max(len(c) - 3, 0)) + c[-3:] + (f"-{digito}" if digito else "")


def titulos_da_liquidacao(resp):
    saida = []
    for st in resp.get("settlements") or []:
        for it in st.get("closingItems") or []:
            ac = it.get("accountDetails") or {}
            saida.append({
                "titulo_id": str(it.get("id")), "periodo_ini": st.get("startDateCalculation"),
                "periodo_fim": st.get("endDateCalculation"), "tipo": it.get("type"), "produto": it.get("product"),
                "valor": num(it.get("amount")) or 0.0, "status": it.get("status"),
                "data_pagamento": it.get("paymentDate"), "transacao": it.get("transactionId"),
                "banco": " ".join(x for x in (ac.get("bankNumber"), ac.get("bankName")) if x),
                "agencia": ac.get("branchCode"), "conta": mascarar(ac.get("accountNumber"), ac.get("accountDigit")),
                "json": json.dumps(it, ensure_ascii=False),
            })
    return saida


# ---------------------------------------------------------------- resumo e conferencias
def resumo_vendas(vendas):
    validas = [v for v in vendas if v["status"] != "CANCELLED"]
    canceladas = [v for v in vendas if v["status"] == "CANCELLED"]
    s = lambda lst, c: round(sum((v.get(c) or 0.0) for v in lst), 2)
    bruto = s(validas, "bruto")
    r = {
        "n": len(vendas), "n_validas": len(validas), "n_canceladas": len(canceladas),
        "bruto": bruto, "bruto_cancelado": s(canceladas, "bruto"),
        "bag": s(validas, "bag"), "taxa_entrega": s(validas, "taxa_entrega"), "taxa_servico": s(validas, "taxa_servico"),
        "desc_total": s(validas, "desc_total"), "desc_ifood": s(validas, "desc_ifood"), "desc_loja": s(validas, "desc_loja"),
        "desc_rede": s(validas, "desc_rede"), "desc_externo": s(validas, "desc_externo"),
        "entrada": s(vendas, "entrada"), "comissao": s(vendas, "comissao"), "taxas": s(vendas, "taxas"),
        "subsidios": s(vendas, "subsidios"), "outros": s(vendas, "outros"), "liquido": s(vendas, "liquido"),
        "com_promo_loja": sum(1 for v in validas if (v["desc_loja"] or 0) > 0 or (v["desc_rede"] or 0) > 0),
    }
    # promocao da loja/rede: compara pedidos COM x SEM (ticket e quanto sobra liquido do bruto)
    tem_promo = lambda v: (v["desc_loja"] or 0) > 0 or (v["desc_rede"] or 0) > 0
    for rot, grupo in (("com", [v for v in validas if tem_promo(v)]), ("sem", [v for v in validas if not tem_promo(v)])):
        b_, l_ = s(grupo, "bruto"), s(grupo, "liquido")
        r[f"n_{rot}_promo"] = len(grupo)
        r[f"ticket_{rot}_promo"] = round(b_ / len(grupo), 2) if grupo else 0.0
        r[f"margem_{rot}_promo"] = l_ / b_ if b_ else 0.0
    r["ticket"] = round(bruto / len(validas), 2) if validas else 0.0
    r["pct_liquido"] = r["liquido"] / bruto if bruto else 0.0
    r["custo_promo_loja"] = round(r["desc_loja"] + r["desc_rede"], 2)
    r["pct_promo_loja"] = r["custo_promo_loja"] / bruto if bruto else 0.0
    return r


def integridade_vendas(vendas):
    """Vendas em que a soma dos lancamentos nao bate com o liquido informado (saleBalance)."""
    return [v for v in vendas if v.get("liquido") is not None and abs((v["soma_lancamentos"] or 0) - v["liquido"]) > 0.01]


def dentro(valor, referencia):
    return abs(valor - referencia) <= max(0.01, abs(referencia) * TOLERANCIA)


def conferir_repasse(eventos, liquidacoes):
    """Documentacao: balance (Settlement) = soma dos lancamentos com impacto no repasse (tol. 0,01%)."""
    com_impacto = round(sum(e["valor"] for e in eventos if e["impacto"]), 2)
    sem_impacto = round(sum(e["valor"] for e in eventos if not e["impacto"]), 2)
    balance = round(sum((l.get("balance") or 0.0) for l in liquidacoes), 2)
    tem_liq = bool(liquidacoes)
    return {
        "com_impacto": com_impacto, "sem_impacto": sem_impacto, "balance": balance,
        "diferenca": round(com_impacto - balance, 2), "tem_liquidacao": tem_liq,
        "ok": tem_liq and dentro(com_impacto, balance),
        "creditos": round(sum(e["valor"] for e in eventos if e["impacto"] and e["valor"] > 0), 2),
        "debitos": round(sum(e["valor"] for e in eventos if e["impacto"] and e["valor"] < 0), 2),
    }


def conferir_pedidos(vendas, eventos):
    """Por pedido: liquido da venda (saleBalance) x soma dos lancamentos com impacto daquele pedido."""
    por_pedido = {}
    for e in eventos:
        if e.get("ref_id") and e["impacto"]:
            por_pedido[e["ref_id"]] = por_pedido.get(e["ref_id"], 0.0) + e["valor"]
    conferidos, divergentes = 0, []
    for v in vendas:
        if v["order_id"] in por_pedido and v.get("liquido") is not None:
            conferidos += 1
            if not dentro(por_pedido[v["order_id"]], v["liquido"]):
                divergentes.append({**v, "soma_eventos": round(por_pedido[v["order_id"]], 2)})
    return {"conferidos": conferidos, "divergentes": divergentes}


# ---------------------------------------------------------------- arquivo de conciliacao
def ler_conciliacao(caminho_ou_bytes):
    """Le o .csv.gz (ou .csv) do iFood. Devolve (linhas_organizadas, resumo, cabecalho)."""
    if isinstance(caminho_ou_bytes, (bytes, bytearray)):
        bruto = bytes(caminho_ou_bytes)
    else:
        with open(caminho_ou_bytes, "rb") as f:
            bruto = f.read()
    if bruto[:2] == b"\x1f\x8b":
        bruto = gzip.decompress(bruto)
    texto = bruto.decode("utf-8-sig", errors="replace")
    leitor = csv.DictReader(io.StringIO(texto), delimiter=";")
    cab = [c.strip() for c in (leitor.fieldnames or [])]
    linhas = []
    for r in leitor:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        linhas.append({
            "pedido": r.get("pedido_associado_ifood") or None, "fato_gerador": r.get("fato_gerador"),
            "tipo_lancamento": r.get("tipo_lancamento"), "descricao": r.get("descricao_lancamento"),
            "valor": num(r.get("valor")) or 0.0, "impacto": 1 if sim(r.get("impacto_no_repasse")) else 0,
            "data_repasse": r.get("data_repasse_esperada") or None, "id_saldo": r.get("id_saldo") or r.get("titulo") or None,
            "valor_transacao": num(r.get("valor_transacao")), "json": json.dumps(r, ensure_ascii=False),
        })
    return linhas, resumo_conciliacao(linhas), cab


def resumo_conciliacao(linhas):
    imp = [l for l in linhas if l["impacto"]]
    por_tipo, por_fato = {}, {}
    for l in imp:
        por_tipo[l["tipo_lancamento"] or "—"] = por_tipo.get(l["tipo_lancamento"] or "—", 0.0) + l["valor"]
        por_fato[l["fato_gerador"] or "—"] = por_fato.get(l["fato_gerador"] or "—", 0.0) + l["valor"]
    # repasse previsto por data: soma de valor_transacao de cada saldo (sem repetir o mesmo saldo)
    saldos = {}
    for l in linhas:
        if l["data_repasse"] and l["valor_transacao"] is not None:
            saldos[(l["id_saldo"], l["data_repasse"])] = l["valor_transacao"]
    por_data = {}
    for (_, d), v in saldos.items():
        por_data[d] = por_data.get(d, 0.0) + v
    return {
        "linhas": len(linhas), "com_impacto": len(imp), "sem_impacto": len(linhas) - len(imp),
        "total_impacto": round(sum(l["valor"] for l in imp), 2),
        "total_sem_impacto": round(sum(l["valor"] for l in linhas if not l["impacto"]), 2),
        "pedidos": len({l["pedido"] for l in linhas if l["pedido"]}),
        "por_tipo": sorted(((k, round(v, 2)) for k, v in por_tipo.items()), key=lambda x: x[1]),
        "por_fato": sorted(((k, round(v, 2)) for k, v in por_fato.items()), key=lambda x: x[1]),
        "por_data": sorted((k, round(v, 2)) for k, v in por_data.items()),
        "periodo_aberto": any(not l["data_repasse"] for l in linhas) and not por_data,
    }


def competencia_valida(comp, hoje=None):
    """So meses FECHADOS, ate 24 meses atras (regra da documentacao). Devolve texto do problema ou ''."""
    hoje = hoje or date.today()
    try:
        ano, mes = (int(x) for x in str(comp).split("-"))
        if not 1 <= mes <= 12:
            raise ValueError
    except ValueError:
        return "Competência inválida: use AAAA-MM (ex.: 2026-09)."
    atual = hoje.year * 12 + hoje.month
    pedido = ano * 12 + mes
    if pedido >= atual:
        return "Só dá para pedir meses já fechados (o mês atual ainda recebe lançamentos)."
    if atual - pedido > 24:
        return "O iFood só gera o arquivo dos últimos 24 meses."
    return ""
