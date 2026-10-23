"""analytics_web.py - a tela "Analytics" do painel (filtro de data + botao Extrair).

Encaixa no painel.py com `registrar(app)`. Mostra o que a homologacao exige:
GMV, ticket medio, pedidos, % de cancelamento, distribuicao por canal / status /
pagamento / logistica, periodo consultado e o aviso de que o dado e D-1.
"""
import csv
import io
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import Blueprint, Response, redirect, render_template_string, request

import analytics_store
from analytics import AnalyticsError, IFoodAnalytics

PASTA = os.path.dirname(os.path.abspath(__file__))
FUSO = timezone(timedelta(hours=-3))
MAX_DIAS = 92

STATUS_PT = {"CONCLUDED": "Concluído", "CANCELLED": "Cancelado"}
DIA_PT = {"1": "Segunda", "2": "Terça", "3": "Quarta", "4": "Quinta", "5": "Sexta", "6": "Sábado", "7": "Domingo",
          "MONDAY": "Segunda", "TUESDAY": "Terça", "WEDNESDAY": "Quarta", "THURSDAY": "Quinta",
          "FRIDAY": "Sexta", "SATURDAY": "Sábado", "SUNDAY": "Domingo"}
ORDEM_DIA = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
ENTREGA_PT = {"IFOOD_DELIVERY": "Entrega iFood", "MERCHANT_DELIVERY": "Entrega própria", "DINE_IN": "Consumo no local"}
PAGAMENTO_PT = {"CREDIT": "Crédito", "DEBIT": "Débito", "PIX": "Pix", "DIGITAL_WALLET": "Carteira digital",
                "MEAL_VOUCHER": "Vale-refeição", "OTHER_VOUCHER": "Outro vale", "BANK_PAY": "Pagamento bancário",
                "CASH": "Dinheiro"}
ORIGEM_PT = {"MERCHANT": "Loja", "CUSTOMER": "Cliente", "PARTNER": "Parceiro", "LOGISTICS": "Logística", "SYSTEM": "Sistema"}
DIMENSOES = [  # (chave no banco, titulo, tradutor)
    ("salesChannel", "Por canal de venda", lambda k: k),
    ("deliveredBy", "Por modelo logístico", lambda k: ENTREGA_PT.get(k, k)),
    ("paymentMethod", "Por método de pagamento", lambda k: PAGAMENTO_PT.get(k, k)),
    ("dayOfWeek", "Por dia da semana", lambda k: DIA_PT.get(str(k).upper(), k)),
    ("orderCreationHour", "Por hora do pedido", lambda k: f"{int(k):02d}h" if str(k).isdigit() else k),
    ("merchantId", "Por loja (ID)", lambda k: k),
]


def brl(v):
    try:
        return "R$ " + f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "—"


def inteiro(v):
    try:
        return f"{int(v):,}".replace(",", ".")
    except Exception:
        return "—"


# ------------------------------------------------------------ calculos (puros)
def resumo(linhas):
    """KPIs principais a partir das linhas da dimensao 'status'."""
    por = {l["chave"]: l for l in linhas if l["dimensao"] == "status"}
    conc, canc = por.get("CONCLUDED"), por.get("CANCELLED")
    n_conc = conc["pedidos"] if conc else 0
    n_canc = canc["pedidos"] if canc else 0
    total = sum(l["pedidos"] for l in por.values())
    gmv = conc["gmv"] if conc else 0.0
    return {
        "gmv": gmv,
        "ticket": gmv / n_conc if n_conc else 0.0,   # gmv.sum / pedidos concluidos
        "concluidos": n_conc,
        "cancelados": n_canc,
        "total": total,
        "taxa_cancel": n_canc / total if total else 0.0,
        "gmv_sem_entrega": conc["gmv_sem_entrega"] if conc else 0.0,
        "taxa_entrega": conc["taxa_entrega"] if conc else 0.0,
        "subsidio": conc["subsidio_parceiro"] if conc else 0.0,
        "gmv_perdido": canc["gmv"] if canc else 0.0,
        "outros_status": sorted(k for k in por if k not in ("CONCLUDED", "CANCELLED")),
    }


def pivot(linhas, dimensao, traduz=lambda k: k):
    """Uma linha por chave, com concluidos x cancelados lado a lado."""
    mapa = {}
    for l in linhas:
        if l["dimensao"] != dimensao:
            continue
        r = mapa.setdefault(l["chave"], {"rotulo": traduz(l["chave"]), "conc": 0, "canc": 0, "gmv": 0.0, "gmv_canc": 0.0})
        if l["status"] == "CONCLUDED":
            r["conc"] += l["pedidos"]
            r["gmv"] += l["gmv"]
        elif l["status"] == "CANCELLED":
            r["canc"] += l["pedidos"]
            r["gmv_canc"] += l["gmv"]
    saida = list(mapa.values())
    if dimensao == "dayOfWeek":
        saida.sort(key=lambda r: ORDEM_DIA.index(r["rotulo"]) if r["rotulo"] in ORDEM_DIA else 99)
    elif dimensao == "orderCreationHour":
        saida.sort(key=lambda r: r["rotulo"])
    else:
        saida.sort(key=lambda r: -r["gmv"])
    return saida


def cancelamentos(linhas, dimensao, traduz=lambda k: k):
    itens = [{"rotulo": traduz(l["chave"]), "pedidos": l["pedidos"], "gmv": l["gmv"]}
             for l in linhas if l["dimensao"] == dimensao]
    return sorted(itens, key=lambda r: -r["pedidos"])


def serie_diaria(linhas):
    itens = sorted((l for l in linhas if l["dimensao"] == "dia"), key=lambda l: l["chave"])
    maior = max((l["gmv"] for l in itens), default=0) or 1
    return [{"dia": datetime.strptime(l["chave"], "%Y-%m-%d").strftime("%d/%m"), "pedidos": l["pedidos"],
             "gmv": l["gmv"], "pct": round(100 * l["gmv"] / maior)} for l in itens]


def csv_bytes(linhas):
    """CSV para Excel brasileiro: separador ';', decimal com virgula, acentos ok."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["dimensao", "chave", "status", "pedidos", "gmv", "gmv_sem_entrega", "taxa_entrega",
                "subsidio_parceiro", "taxas_loja", "reembolso"])
    for l in linhas:
        w.writerow([l["dimensao"], l["chave"], l["status"], l["pedidos"]] +
                   [f"{l[c]:.2f}".replace(".", ",") for c in
                    ("gmv", "gmv_sem_entrega", "taxa_entrega", "subsidio_parceiro", "taxas_loja", "reembolso")])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _periodo(args):
    """Le ini/fim da URL; padrao = ultimos 7 dias terminando em ONTEM (D-1)."""
    ontem = (datetime.now(FUSO) - timedelta(days=1)).date()
    try:
        fim = datetime.strptime(args.get("fim", ""), "%Y-%m-%d").date()
    except ValueError:
        fim = ontem
    try:
        ini = datetime.strptime(args.get("ini", ""), "%Y-%m-%d").date()
    except ValueError:
        ini = fim - timedelta(days=6)
    return ini, fim, ontem


def _problema_periodo(ini, fim, ontem):
    if ini > fim:
        return "A data inicial é depois da data final."
    if (fim - ini).days + 1 > MAX_DIAS:
        return f"Período grande demais: máximo de {MAX_DIAS} dias por extração (consulta longa fica lenta)."
    return ""


ESTILO_EXTRA = """
 .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:14px}
 .kpi{background:#fff;border:1px solid var(--borda);border-radius:18px;padding:14px 16px}
 .kpi .rot{font-size:12px;color:var(--suave);font-weight:600;text-transform:uppercase;letter-spacing:.6px}
 .kpi .val{font-family:'Montserrat',sans-serif;font-size:24px;font-weight:800;color:var(--vinho);margin-top:2px}
 .kpi .dica{font-size:12px;color:var(--suave)} .kpi.neg .val{color:var(--neg)}
 .filtro{display:flex;gap:10px;align-items:end;flex-wrap:wrap}
 .filtro label{display:flex;flex-direction:column;font-size:12px;color:var(--suave);font-weight:600;gap:3px}
 .filtro input{min-width:0}
 .aviso{background:#E8F0FB;border:1px solid #BCD1EE;color:#1F4E8C;border-radius:14px;padding:10px 14px;margin-bottom:14px;font-size:14px}
 .num-d{text-align:right;white-space:nowrap}
 .barra-d{background:var(--tint);border-radius:6px;height:10px;min-width:60px}.barra-d i{display:block;height:10px;background:var(--rosa);border-radius:6px}
 .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px}
 @media(max-width:600px){.cols{grid-template-columns:1fr}}
"""

PAGINA_ANALYTICS = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Analytics iFood - Rede Demo</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Montserrat:wght@600;700;800&display=swap" rel="stylesheet">
<style>{{ estilo|safe }}{{ extra|safe }}</style></head><body>
<header>
 <div class="logos">
  {% if tem_marca %}<img src="/logo/marca" alt="Rede Demo">{% else %}<span class="marca">Rede Demo</span>{% endif %}
  {% if tem_ifood %}<img src="/logo/ifood" alt="iFood">{% else %}<span class="tag ifood">iFood</span>{% endif %}
 </div>
 <h1>Analytics iFood<span class="sub">Números consolidados por período · dados de ontem para trás (D-1)</span></h1>
 <a href="/financeiro" class="btn-conexao">Financeiro</a>
 <a href="/" class="btn-conexao">Voltar aos pedidos</a>
</header>
<main>
{% if msg %}<div class="msg {{ tipo }}">{{ msg }}</div>{% endif %}
<div class="aviso"><b>Dados históricos (D-1).</b> O iFood atualiza uma vez por dia (até 9h) com os pedidos até a meia-noite de ontem.
 Hoje não aparece. Um pedido concluído ainda pode ser cancelado em até 15 dias, então o número de dias recentes pode mudar: extraia de novo para atualizar.
 Aqui só há totais agrupados, nunca pedido por pedido.</div>
<div class="card">
 <form method="post" action="/analytics/extrair" class="filtro">
  <label>Data inicial<input type="date" name="ini" value="{{ ini }}" max="{{ ontem }}" required></label>
  <label>Data final<input type="date" name="fim" value="{{ fim }}" max="{{ ontem }}" required></label>
  <button type="submit">Extrair do iFood</button>
  {% if ext %}<a href="/analytics/csv?ini={{ ini }}&fim={{ fim }}"><button type="button" class="sec">Baixar CSV</button></a>{% endif %}
 </form>
 <div class="peq" style="margin-top:8px">
  Período mostrado: <b>{{ ini_br }}</b> a <b>{{ fim_br }}</b> ({{ dias }} dia(s)).
  {% if ext %}Última extração deste período: <b>{{ extraido_em }}</b>.
  {% else %}Ainda não há extração deste período. Clique em <b>Extrair do iFood</b>.{% endif %}
 </div>
</div>
{% if ext %}
{% if ext.erros %}<div class="msg erro">Algumas partes não vieram do iFood (o resto está correto):<br>{{ ext.erros|replace("\\n","<br>")|safe }}</div>{% endif %}
{% if r.outros_status %}<div class="msg">Há pedidos em outros status ({{ r.outros_status|join(", ") }}); eles entram no total de pedidos, mas não no GMV nem no ticket.</div>{% endif %}
<div class="kpis">
 <div class="kpi"><div class="rot">GMV (concluídos)</div><div class="val">{{ brl(r.gmv) }}</div><div class="dica">produtos + taxa de entrega</div></div>
 <div class="kpi"><div class="rot">Ticket médio</div><div class="val">{{ brl(r.ticket) }}</div><div class="dica">GMV ÷ pedidos concluídos</div></div>
 <div class="kpi"><div class="rot">Pedidos concluídos</div><div class="val">{{ inteiro(r.concluidos) }}</div><div class="dica">de {{ inteiro(r.total) }} no total</div></div>
 <div class="kpi neg"><div class="rot">Pedidos cancelados</div><div class="val">{{ inteiro(r.cancelados) }}</div><div class="dica">{{ ("%.1f"|format(r.taxa_cancel*100))|replace(".", ",") }}% de cancelamento</div></div>
 <div class="kpi neg"><div class="rot">GMV perdido</div><div class="val">{{ brl(r.gmv_perdido) }}</div><div class="dica">soma dos cancelados</div></div>
 <div class="kpi"><div class="rot">GMV sem entrega</div><div class="val">{{ brl(r.gmv_sem_entrega) }}</div><div class="dica">só os produtos</div></div>
 <div class="kpi"><div class="rot">Taxa de entrega paga</div><div class="val">{{ brl(r.taxa_entrega) }}</div><div class="dica">pelos clientes (já com desconto de frete)</div></div>
 <div class="kpi"><div class="rot">Subsídios do parceiro</div><div class="val">{{ brl(r.subsidio) }}</div><div class="dica">promoções patrocinadas pela loja</div></div>
</div>
{% if serie %}<div class="card"><h2 style="margin-top:0">GMV por dia (concluídos)</h2>
 <table>{% for d in serie %}<tr><td style="width:60px">{{ d.dia }}</td><td style="width:55%"><div class="barra-d"><i style="width:{{ d.pct }}%"></i></div></td>
 <td class="num-d">{{ brl(d.gmv) }}</td><td class="num-d peq">{{ inteiro(d.pedidos) }} pedidos</td></tr>{% endfor %}</table></div>{% endif %}
<div class="cols">
{% for titulo, itens in quebras %}{% if itens %}
 <div class="card"><h2 style="margin-top:0">{{ titulo }}</h2>
  <table><tr><th>{{ titulo.replace("Por ","").capitalize() }}</th><th class="num-d">Concluídos</th><th class="num-d">Cancelados</th><th class="num-d">GMV concluídos</th></tr>
  {% for i in itens %}<tr><td class="{{ 'peq' if titulo == 'Por loja (ID)' else '' }}">{{ i.rotulo }}</td><td class="num-d">{{ inteiro(i.conc) }}</td>
   <td class="num-d">{{ inteiro(i.canc) }}</td><td class="num-d">{{ brl(i.gmv) }}</td></tr>{% endfor %}</table></div>
{% endif %}{% endfor %}
{% for titulo, itens in cancel %}{% if itens %}
 <div class="card"><h2 style="margin-top:0">{{ titulo }}</h2>
  <table><tr><th>{{ 'Origem' if 'origem' in titulo else 'Motivo' }}</th><th class="num-d">Pedidos</th><th class="num-d">GMV perdido</th></tr>
  {% for i in itens %}<tr><td>{{ i.rotulo }}</td><td class="num-d">{{ inteiro(i.pedidos) }}</td><td class="num-d">{{ brl(i.gmv) }}</td></tr>{% endfor %}</table></div>
{% endif %}{% endfor %}
</div>
{% endif %}
</main></body></html>"""


# ------------------------------------------------------------ rotas
def registrar(app, get_analytics=None):
    """Liga as rotas /analytics no app Flask do painel."""
    bp = Blueprint("analytics_web", __name__)
    holder = {"api": None}

    def api():
        if get_analytics:
            return get_analytics()
        if holder["api"] is None:  # so cria (e autentica) quando precisar
            holder["api"] = IFoodAnalytics()
        return holder["api"]

    def alvo():
        return (os.getenv("IFOOD_BI_ALVO_ID") or "").strip()

    def volta(ini, fim, msg, tipo=""):
        return redirect(f"/analytics?ini={ini}&fim={fim}&msg={quote(msg)}" + (f"&tipo={tipo}" if tipo else ""))

    @bp.get("/analytics")
    def pagina():
        ini, fim, ontem = _periodo(request.args)
        msg, tipo = request.args.get("msg"), request.args.get("tipo", "")
        prob = _problema_periodo(ini, fim, ontem)
        if prob and not msg:
            msg, tipo = prob, "erro"
        ext, linhas = (None, [])
        try:
            a = get_analytics() if get_analytics else None
            ext, linhas = analytics_store.buscar(a.alvo_id if a else alvo(), ini, fim)
        except Exception:
            pass
        ctx = dict(
            estilo=app.config.get("ESTILO_PAINEL", ""), extra=ESTILO_EXTRA, msg=msg, tipo=tipo,
            ini=ini.isoformat(), fim=fim.isoformat(), ontem=ontem.isoformat(),
            ini_br=ini.strftime("%d/%m/%Y"), fim_br=fim.strftime("%d/%m/%Y"), dias=(fim - ini).days + 1,
            ext=ext, brl=brl, inteiro=inteiro,
            tem_marca=os.path.exists(os.path.join(PASTA, "logo_marca.png")),
            tem_ifood=os.path.exists(os.path.join(PASTA, "logo_ifood.png")),
        )
        if ext:
            ctx["extraido_em"] = datetime.fromisoformat(ext["extraido_em"]).astimezone(FUSO).strftime("%d/%m/%Y %H:%M")
            ext["erros"] = (ext.get("erros") or "").strip().replace("&", "&amp;").replace("<", "&lt;")
            ctx.update(
                r=resumo(linhas), serie=serie_diaria(linhas),
                quebras=[(t, pivot(linhas, d, f)) for d, t, f in DIMENSOES],
                cancel=[("Cancelamentos por motivo", cancelamentos(linhas, "cancellationReason")),
                        ("Cancelamentos por origem", cancelamentos(linhas, "cancellationOrigin", lambda k: ORIGEM_PT.get(k, k)))],
            )
        return render_template_string(PAGINA_ANALYTICS, **ctx)

    @bp.post("/analytics/extrair")
    def extrair():
        try:
            ini = datetime.strptime(request.form["ini"], "%Y-%m-%d").date()
            fim = datetime.strptime(request.form["fim"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            return redirect("/analytics?msg=" + quote("Escolha as duas datas.") + "&tipo=erro")
        prob = _problema_periodo(ini, fim, None)
        if prob:
            return volta(ini, fim, prob, "erro")
        try:
            a = api()
            resultado = a.extrair(ini, fim)
            analytics_store.salvar_extracao(a.alvo_id, a.modelo, ini, fim, resultado["linhas"], resultado["erros"])
        except AnalyticsError as e:
            return volta(ini, fim, f"✘ {e}", "erro")
        except Exception as e:  # nunca derruba o painel
            return volta(ini, fim, f"✘ Erro inesperado na extração: {e}", "erro")
        n = len(resultado["linhas"])
        aviso = " Algumas partes falharam (veja abaixo)." if resultado["erros"] else ""
        return volta(ini, fim, f"✔ Extração concluída: {n} linhas gravadas.{aviso}", "erro" if resultado["erros"] else "ok")

    @bp.get("/analytics/csv")
    def baixar_csv():
        ini, fim, _ = _periodo(request.args)
        a = get_analytics() if get_analytics else None
        ext, linhas = analytics_store.buscar(a.alvo_id if a else alvo(), ini, fim)
        if not ext:
            return redirect(f"/analytics?ini={ini}&fim={fim}&msg=" + quote("Extraia o período antes de baixar o CSV.") + "&tipo=erro")
        return Response(csv_bytes(linhas), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=analytics_{ini}_{fim}.csv"})

    app.register_blueprint(bp)
