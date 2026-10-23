"""painel.py - a "comanda na tela".

Pagina local (http://127.0.0.1:5000) que mostra os pedidos gravados pelo
main.py, com o que os criterios de homologacao pedem para aparecer:
bandeira do cartao, troco, cupom e quem paga, observacoes dos itens,
codigo de coleta e CPF/CNPJ (mascarado: so os ultimos digitos, LGPD).
Os botoes chamam a API (orders.py).

Abas: Ativos (pedidos em andamento), Finalizados (concluidos/cancelados), Todos.
Logos: coloque logo_ifood.png e logo_marca.png na mesma pasta deste arquivo.

Uso:   python painel.py      (deixe o main.py rodando em outro terminal)
"""
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import Flask, abort, redirect, render_template_string, request, send_from_directory

import store
from client import IFoodError, checar
from fin_calculos import mascarar
from orders import IFoodOrders, OrderNotFound

PASTA = os.path.dirname(os.path.abspath(__file__))
FUSO = timezone(timedelta(hours=-3))  # horario de Brasilia (o iFood manda em UTC)
LOGOS = {"ifood": "logo_ifood.png", "marca": "logo_marca.png"}

QUEM_PAGA = {
    "IFOOD": "iFood", "MERCHANT": "Loja", "EXTERNAL": "Parceiro externo", "CHAIN": "Rede",
}
FORMAS = {
    "CREDIT": "Crédito", "DEBIT": "Débito", "MEAL_VOUCHER": "Vale-refeição",
    "FOOD_VOUCHER": "Vale-alimentação", "DIGITAL_WALLET": "Carteira digital",
    "PIX": "Pix", "CASH": "Dinheiro",
}
ALVOS = {
    "CART": "carrinho", "DELIVERY_FEE": "taxa de entrega", "ITEM": "item",
    "PROGRESSIVE_DISCOUNT_ITEM": "desconto progressivo",
}
# estado do iFood -> texto em portugues (a cor de cada um fica no CSS)
STATUS_PT = {
    "PLACED": "Novo — confirmar", "CONFIRMED": "Confirmado",
    "PREPARATION_STARTED": "Em preparo", "READY_TO_PICKUP": "Pronto para retirada",
    "DISPATCHED": "Despachado", "CONCLUDED": "Concluído", "CANCELLED": "Cancelado",
}
EVENTO_PT = {
    "PLACED": "Pedido recebido", "CONFIRMED": "Pedido confirmado",
    "PREPARATION_STARTED": "Preparo iniciado", "READY_TO_PICKUP": "Pronto para retirada",
    "DISPATCHED": "Saiu para entrega", "CONCLUDED": "Pedido concluído",
    "CANCELLATION_REQUESTED": "Cancelamento solicitado", "CANCELLED": "Pedido cancelado",
    "DELIVERY_DROP_CODE_REQUESTED": "Código de entrega solicitado",
}
ACAO_PT = {
    "confirm": "confirmado", "prepare": "em preparo", "ready": "pronto para retirada",
    "dispatch": "despachado",
}
TIPOS = {"DELIVERY": "Entrega", "TAKEOUT": "Retirada", "INDOOR": "No local"}
ENTREGA = {"IFOOD": "entregador iFood", "MERCHANT": "entregador da loja"}
# estado -> (texto do botao, acao). O cancelamento fica a parte.
PROXIMO = {
    "PLACED": ("Confirmar pedido", "confirm"),
    "CONFIRMED": ("Iniciar preparo", "prepare"),
    "PREPARATION_STARTED": ("Pronto para retirada", "ready"),
}
FINAIS = {"CONCLUDED", "CANCELLED"}
PRAZO_CONFIRMAR_MIN = 8  # o iFood cancela sozinho se nao confirmar a tempo


def brl(v):
    try:
        return "R$ " + f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:  # valor ausente (inclusive 'Undefined' do template): mostra traco
        return "—"


def _data(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(FUSO)
    except (TypeError, ValueError):
        return None


def hora(iso):
    """2026-10-21T16:38:00Z -> '13:38' (horario de Brasilia)."""
    d = _data(iso)
    return d.strftime("%H:%M:%S") if d else (iso or "—")


def dia_hora(iso):
    d = _data(iso)
    return d.strftime("%d/%m %H:%M:%S") if d else (iso or "—")


def minutos_desde(iso):
    d = _data(iso)
    if not d:
        return None
    return max(0, int((datetime.now(FUSO) - d).total_seconds() // 60))


def tempo(minutos):
    """7494 -> '5 d 4 h'; 135 -> '2 h 15 min'; 6 -> '6 min'."""
    if minutos is None:
        return ""
    if minutos < 60:
        return f"{minutos} min"
    if minutos < 1440:
        return f"{minutos // 60} h {minutos % 60} min"
    return f"{minutos // 1440} d {(minutos % 1440) // 60} h"


def troco(metodo):
    """Troco = valor entregue pelo cliente - valor a cobrar (so dinheiro)."""
    cash = (metodo.get("cash") or {}).get("changeFor")
    valor = metodo.get("value")
    if cash is None or valor is None:
        return None
    return max(0.0, float(cash) - float(valor))


def pagamentos(det):
    saida = []
    for m in (det.get("payments") or {}).get("methods") or []:
        forma = FORMAS.get((m.get("method") or "").strip(), m.get("method") or "?")
        marca = (m.get("card") or {}).get("brand")
        quando = "pago online" if m.get("type") == "ONLINE" else "cobrar na entrega"
        linha = {"forma": forma, "marca": marca, "quando": quando, "valor": m.get("value")}
        if (m.get("method") or "").strip() == "CASH":
            linha["troco_para"] = (m.get("cash") or {}).get("changeFor")
            linha["troco"] = troco(m)
        saida.append(linha)
    return saida


def cupons(det):
    saida = []
    for b in det.get("benefits") or []:
        pagam = [
            f"{QUEM_PAGA.get(s.get('name'), s.get('name'))} {brl(s.get('value'))}"
            for s in b.get("sponsorshipValues") or [] if (s.get("value") or 0) > 0
        ]
        saida.append({
            "valor": b.get("value"),
            "alvo": ALVOS.get(b.get("target"), b.get("target")),
            "campanha": (b.get("campaign") or {}).get("name"),
            "quem_paga": ", ".join(pagam) or "—",
        })
    return saida


def endereco(det):
    a = (det.get("delivery") or {}).get("deliveryAddress") or {}
    partes = [
        a.get("formattedAddress"),
        " ".join(x for x in (a.get("streetName"), a.get("streetNumber")) if x) or None,
        a.get("complement"), a.get("district"), a.get("city"), a.get("reference"),
    ]
    return " — ".join(p for p in partes if p) or "—"


def documento(det):
    """CPF/CNPJ do cliente mascarado (LGPD): so os ultimos digitos vao para a tela.

    Campo vazio ou ausente devolve "" (o template entende como "nao mostrar a linha").
    """
    return mascarar((det.get("customer") or {}).get("documentNumber"), None)


# Paleta (Fred): vinho = marca da Rede Demo; vermelho iFood so onde e iFood.
ESTILO = """
 :root{--fundo:#FAF6F7;--tinta:#2E0E17;--vinho:#5C1A2B;--rosa:#A3284D;--suave:#6E4A54;
  --borda:#F3DDE3;--tint:#FBE9EE;--ifood:#EA1D2C;--ok:#1E7A44;--ok-bg:#E8F5EE;
  --neg:#B3131F;--neg-bg:#FDECEE;--aten:#8A6100;--aten-bg:#FFF7DE}
 *{box-sizing:border-box}
 body{font-family:'DM Sans','Segoe UI',Arial,sans-serif;background:var(--fundo);margin:0;color:var(--tinta);
  font-size:15px;line-height:1.45}
 h1,h2,.num,.aba,.marca{font-family:'Montserrat','Segoe UI',Arial,sans-serif}
 header{background:var(--vinho);color:#fff;padding:14px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 header .logos{display:flex;align-items:center;gap:12px}
 header .logos img{height:44px;border-radius:10px;display:block;background:#fff}
 .marca{font-weight:800;font-size:17px;letter-spacing:.3px;background:#fff;color:var(--vinho);
  padding:9px 14px;border-radius:10px}
 header h1{font-size:19px;margin:0;font-weight:700;flex:1;min-width:200px}
 header .sub{font-weight:400;font-size:13px;opacity:.8;display:block}
 main{max-width:1100px;margin:0 auto;padding:18px 16px 40px}
 .msg{background:var(--aten-bg);border:1px solid #E6CF8A;color:var(--aten);padding:10px 14px;
  border-radius:12px;margin-bottom:14px;font-weight:500;word-break:break-word}
 .msg.ok{background:var(--ok-bg);border-color:#A9D9BC;color:var(--ok);font-weight:700;font-size:16px}
 .msg.erro{background:var(--neg-bg);border-color:#F0B8BE;color:var(--neg)}
 .hist td{font-size:13px;padding:3px 8px}.hist .quando{color:var(--suave);white-space:nowrap}
 .barra{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
 .abas{display:flex;gap:6px;background:#fff;border:1px solid var(--borda);padding:4px;border-radius:999px}
 .aba{padding:8px 16px;border-radius:999px;text-decoration:none;color:var(--suave);font-weight:600;font-size:14px}
 .aba b{background:var(--tint);color:var(--vinho);border-radius:999px;padding:1px 8px;margin-left:4px;font-size:12px}
 .aba.ativa{background:var(--vinho);color:#fff}.aba.ativa b{background:#fff3}
 .aba.ativa b{color:#fff}
 .importar{margin-left:auto;display:flex;gap:6px}
 input{padding:8px 12px;border:1px solid var(--borda);border-radius:10px;font:inherit;background:#fff;min-width:300px}
 .card{background:#fff;border:1px solid var(--borda);border-radius:22px;padding:18px 20px;margin-bottom:14px;
  box-shadow:0 2px 10px #5C1A2B0D}
 .card.alerta{border-color:#E6CF8A;box-shadow:0 0 0 3px var(--aten-bg)}
 .topo{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .num{font-size:24px;font-weight:800;color:var(--vinho)}
 .pill{padding:4px 12px;border-radius:999px;font-size:13px;font-weight:700}
 .st-PLACED{background:var(--aten-bg);color:var(--aten)}
 .st-CONFIRMED{background:#E8F0FB;color:#1F4E8C}
 .st-PREPARATION_STARTED{background:#F1E8FB;color:#5B2E91}
 .st-READY_TO_PICKUP{background:var(--ok-bg);color:var(--ok)}
 .st-DISPATCHED{background:#DDF3EA;color:#14663A}
 .st-CONCLUDED{background:#EEE8EA;color:var(--suave)}
 .st-CANCELLED{background:var(--neg-bg);color:var(--neg)}
 .st-None{background:#EEE8EA;color:var(--suave)}
 .tag{padding:3px 10px;border-radius:999px;font-size:12px;background:var(--tint);color:var(--vinho);font-weight:500}
 .tag.ifood{background:var(--ifood);color:#fff;font-weight:700;font-size:13px}
 .tag.agendado{background:#E8F0FB;color:#1F4E8C;font-weight:700}
 .agenda{background:#E8F0FB;color:#1F4E8C;border-radius:12px;padding:8px 12px;margin:6px 0;font-size:14px}
 .tag.teste{background:var(--aten-bg);color:var(--aten);font-weight:700}
 .idade{font-size:13px;color:var(--suave)} .idade.urg{color:var(--neg);font-weight:700}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px}
 h2{font-size:12px;margin:16px 0 6px;text-transform:uppercase;letter-spacing:.8px;color:var(--rosa)}
 table{border-collapse:collapse;width:100%;font-size:14px}
 td,th{padding:6px 8px;border-bottom:1px solid var(--borda);text-align:left;vertical-align:top}
 th{font-size:12px;color:var(--suave);font-weight:600}
 .obs{color:var(--neg);font-weight:600}.peq{color:var(--suave);font-size:12px;word-break:break-all}
 .codigo{font-size:20px;font-weight:800;letter-spacing:3px;color:var(--vinho);background:var(--tint);
  padding:2px 10px;border-radius:8px}
 .acoes{margin-top:16px;display:flex;gap:8px;flex-wrap:wrap}
 form{display:inline;margin:0}
 button{padding:10px 18px;border:0;border-radius:999px;background:var(--rosa);color:#fff;font:inherit;
  font-weight:700;cursor:pointer}
 button:hover{background:var(--vinho)}
 button.sec{background:#fff;color:var(--suave);border:1px solid var(--borda)}
 button.sec:hover{background:var(--tint)}
 button.perigo{background:#fff;color:var(--neg);border:1px solid #F0B8BE}
 button.perigo:hover{background:var(--neg-bg)}
 details>summary{cursor:pointer;list-style:none}details>summary::-webkit-details-marker{display:none}
 .mais{color:var(--rosa);font-size:13px;font-weight:600;margin-top:6px}
 .vazio{text-align:center;color:var(--suave);padding:40px 10px;background:#fff;border:1px dashed var(--borda);
  border-radius:22px}
 .eventos{background:#fff;border:1px solid var(--borda);border-radius:22px;padding:14px 20px}
 .eventos summary{font-family:'Montserrat',sans-serif;font-weight:700;color:var(--vinho)}
 a{color:var(--rosa)}
 .btn-conexao{background:#fff;color:var(--vinho);text-decoration:none;font-weight:700;padding:10px 18px;border-radius:999px}
 .btn-conexao:hover{background:var(--tint)}
 .ok{color:var(--ok);font-weight:700}.falha{color:var(--neg);font-weight:700}
 @media(max-width:600px){.importar{margin-left:0;width:100%}input{min-width:0;flex:1}}
 @media(prefers-reduced-motion:no-preference){.card{transition:box-shadow .2s}.card:hover{box-shadow:0 6px 20px #5C1A2B1A}}
"""

PAGINA = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Painel iFood - Rede Demo</title>
{% if not sem_refresh %}<meta http-equiv="refresh" content="15;url=/?aba={{ aba }}">{% endif %}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Montserrat:wght@600;700;800&display=swap" rel="stylesheet">
<style>{{ estilo|safe }}</style></head><body>
<header>
 <div class="logos">
  {% if tem_marca %}<img src="/logo/marca" alt="Rede Demo">{% else %}<span class="marca">Rede Demo</span>{% endif %}
  {% if tem_ifood %}<img src="/logo/ifood" alt="iFood">{% else %}<span class="tag ifood">iFood</span>{% endif %}
 </div>
 <h1>Painel de pedidos<span class="sub">Integração iFood · atualiza sozinho a cada 15 s</span></h1>
 <a href="/financeiro" class="btn-conexao">Financeiro</a>
 <a href="/analytics" class="btn-conexao">Analytics</a>
 <a href="/conexao" class="btn-conexao">Verificar conexão</a>
</header>
<main>
{% if msg %}<div class="msg {{ tipo }}">{{ msg }}</div>{% endif %}
<div class="barra">
 <nav class="abas">
  <a class="aba {{ 'ativa' if aba == 'ativos' }}" href="/?aba=ativos">Ativos <b>{{ n_ativos }}</b></a>
  <a class="aba {{ 'ativa' if aba == 'finalizados' }}" href="/?aba=finalizados">Finalizados <b>{{ n_finais }}</b></a>
  <a class="aba {{ 'ativa' if aba == 'todos' }}" href="/?aba=todos">Todos <b>{{ n_ativos + n_finais }}</b></a>
 </nav>
 <form method="post" action="/importar" class="importar">
  <input name="order_id" placeholder="Importar pedido pelo ID">
  <button class="sec">Importar</button>
 </form>
</div>
{% for p in pedidos %}{% set d = p.detalhes %}{% set final = p.status in finais %}
{% set idade = minutos_desde(p.status_em) %}
{% set urgente = p.status == 'PLACED' and idade is not none and idade >= prazo - 3 %}
<div class="card {{ 'alerta' if urgente }}">
<details {{ '' if final else 'open' }}>
 <summary>
 <div class="topo">
  <span class="num">#{{ p.display_id or '?' }}</span>
  <span class="pill st-{{ p.status }}">{{ status_pt.get(p.status, 'Sem status') }}</span>
  {% if d %}<span class="tag">{{ tipos.get(d.orderType, d.orderType) }}</span>
  <span class="tag">{{ entrega.get((d.delivery or {}).deliveredBy, '—') }}</span>
  {% if d.orderTiming == 'SCHEDULED' %}<span class="tag agendado">Agendado</span>{% endif %}
  {% if d.isTest %}<span class="tag teste">TESTE</span>{% endif %}
  <span class="tag ifood">iFood</span>
  <b>{{ brl((d.total or {}).orderAmount) }}</b>{% endif %}
  {% if idade is not none and not final %}<span class="idade {{ 'urg' if urgente }}">
   {% if p.status == 'PLACED' %}{{ idade }} min sem confirmar (prazo {{ prazo }} min){% else %}há {{ tempo(idade) }}{% endif %}</span>{% endif %}
 </div>
 {% if final %}<div class="mais">Ver detalhes do pedido</div>{% endif %}
 </summary>
{% if not d %}<p class="peq">Detalhes ainda não carregados (o main.py busca em instantes).</p>
{% else %}
 <h2>Itens</h2>
 <table><tr><th>Qtd</th><th>Item</th><th>Unit.</th><th>Total</th></tr>
 {% for i in d['items'] %}<tr><td>{{ i.quantity }}</td>
  <td>{{ i.name }}
   {% for o in i.options or [] %}<div class="peq">+ {{ o.quantity }}x {{ o.name }}{% if o.groupName %} ({{ o.groupName }}){% endif %}</div>{% endfor %}
   {% if i.observations %}<div class="obs">Obs.: {{ i.observations }}</div>{% endif %}</td>
  <td>{{ brl(i.unitPrice) }}</td><td>{{ brl(i.totalPrice) }}</td></tr>{% endfor %}</table>
 <div class="grid">
  <div><h2>Pagamento</h2>
   {% for m in pagamentos(d) %}<div>{{ m.forma }}{% if m.marca %} — bandeira <b>{{ m.marca }}</b>{% endif %}
    — {{ brl(m.valor) }} <span class="peq">({{ m.quando }})</span>
    {% if m.troco_para %}<div><b>Troco para {{ brl(m.troco_para) }} → devolver {{ brl(m.troco) }}</b></div>{% endif %}</div>{% endfor %}
   <h2>Cupons / descontos</h2>
   {% for c in cupons(d) %}<div>{{ brl(c.valor) }} em {{ c.alvo }}{% if c.campanha %} ({{ c.campanha }}){% endif %}
    <div class="peq">Quem paga: <b>{{ c.quem_paga }}</b></div></div>{% else %}<div class="peq">Sem cupom</div>{% endfor %}
   <h2>Totais</h2>
   {% set t = d.total or {} %}<div class="peq">Subtotal {{ brl(t.subTotal) }} · Entrega {{ brl(t.deliveryFee) }}
    · Taxas {{ brl(t.additionalFees) }} · Descontos {{ brl(t.benefits) }}</div>
   <div><b>Total: {{ brl(t.orderAmount) }}</b></div></div>
  <div><h2>Cliente</h2>
   <div>{{ (d.customer or {}).name or '—' }}</div>
   {% set doc = documento(d) %}
   {% if doc %}<div><b>{{ (d.customer or {}).documentType or 'CPF/CNPJ' }}:</b> {{ doc }}</div>{% endif %}
   <h2>Entrega</h2>
   {% set e = d.delivery or {} %}
   {% if d.orderTiming == 'SCHEDULED' and d.scheduled %}<div class="agenda"><b>Pedido agendado</b><br>
    Janela: {{ dia_hora(d.scheduled.deliveryDateTimeStart) }} até {{ dia_hora(d.scheduled.deliveryDateTimeEnd) }}
    {% if d.scheduled.preparationStartDateTime %}<br>Começar a preparar às <b>{{ dia_hora(d.scheduled.preparationStartDateTime) }}</b>{% endif %}</div>{% endif %}
   {% if e.pickupCode %}<div>Código de coleta: <span class="codigo">{{ e.pickupCode }}</span></div>{% endif %}
   {% if e.observations %}<div class="obs">Obs. de entrega: {{ e.observations }}</div>{% endif %}
   <div class="peq">{{ endereco(d) }}</div>
   <div class="peq" style="margin-top:8px">ID do pedido: {{ p.order_id }}</div></div>
 </div>
{% endif %}
 {% set hist = historico.get(p.order_id) %}
 {% if hist %}<h2>Histórico (eventos recebidos do iFood)</h2>
 <table class="hist">{% for ev in hist %}<tr><td class="quando">{{ dia_hora(ev.criado_em) }}</td>
  <td>✔ {{ evento_pt.get(ev.full_code or ev.code, ev.full_code or ev.code) }}</td>
  <td class="peq">{{ ev.full_code or ev.code }}</td></tr>{% endfor %}</table>{% endif %}
</details>
 {% if not final %}
 <div class="acoes">
 {% if p.status in proximo %}
  <form method="post" action="/acao/{{ p.order_id }}/{{ proximo[p.status][1] }}"><button>{{ proximo[p.status][0] }}</button></form>
 {% endif %}
 {% if p.status == 'READY_TO_PICKUP' and d and (d.delivery or {}).deliveredBy == 'MERCHANT' %}
  <form method="post" action="/acao/{{ p.order_id }}/dispatch"><button>Despachar (entrega própria)</button></form>
 {% endif %}
 <a href="/cancelar/{{ p.order_id }}"><button type="button" class="perigo">Cancelar…</button></a>
 </div>
 {% endif %}
</div>
{% else %}<div class="vazio">
 {% if aba == 'ativos' %}Nenhum pedido em andamento. Deixe o main.py rodando e gere um pedido de teste no portal.
 {% elif aba == 'finalizados' %}Nenhum pedido finalizado ainda.{% else %}Nenhum pedido ainda.{% endif %}</div>{% endfor %}
<details class="eventos"><summary>Eventos recentes ({{ eventos|length }})</summary>
<table><tr><th>Quando</th><th>Evento</th><th>Pedido</th></tr>
{% for ev in eventos %}<tr><td>{{ dia_hora(ev.criado_em) }}</td><td>{{ ev.full_code or ev.code }}</td><td class="peq">{{ ev.order_id }}</td></tr>{% endfor %}</table></details>
</main></body></html>"""

PAGINA_CANCELAR = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Cancelar pedido</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Montserrat:wght@600;700;800&display=swap" rel="stylesheet">
<style>{{ estilo|safe }}
 .caixa{max-width:600px;margin:30px auto}
 select{padding:10px;font:inherit;border:1px solid var(--borda);border-radius:10px;width:100%;margin:10px 0 14px;background:#fff}
</style></head><body>
<header><div class="logos"><span class="marca">Rede Demo</span></div><h1>Cancelar pedido</h1></header>
<main><div class="card caixa">
<h2 style="margin-top:0">Pedido #{{ display or order_id }}</h2>
<p>Motivos devolvidos pelo iFood. Escolha o que melhor descreve o cancelamento:</p>
<form method="post"><select name="codigo">
{% for c, t in motivos %}<option value="{{ c }}">{{ t }} ({{ c }})</option>{% endfor %}</select>
<div class="acoes"><button class="perigo">Solicitar cancelamento</button>
<a href="/"><button type="button" class="sec">Voltar</button></a></div></form>
</div></main></body></html>"""


PAGINA_CONEXAO = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Conexão com o iFood</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Montserrat:wght@600;700;800&display=swap" rel="stylesheet">
<style>{{ estilo|safe }} .caixa{max-width:760px;margin:24px auto}</style></head><body>
<header><div class="logos">
 {% if tem_marca %}<img src="/logo/marca" alt="Rede Demo">{% else %}<span class="marca">Rede Demo</span>{% endif %}
 {% if tem_ifood %}<img src="/logo/ifood" alt="iFood">{% else %}<span class="tag ifood">iFood</span>{% endif %}
 </div><h1>Conexão com o iFood<span class="sub">Verificado em {{ agora }} (horário de Brasília)</span></h1>
 <a href="/" class="btn-conexao">Voltar aos pedidos</a></header>
<main><div class="card caixa">
 <h2 style="margin-top:0">1. Autenticação do aplicativo</h2>
 {% if auth_ok %}<div class="ok">✔ Autenticado na API do iFood (token válido)</div>
 {% else %}<div class="falha">✘ Falha na autenticação</div><div class="peq">{{ auth_erro }}</div>{% endif %}
 <h2>2. Loja conectada</h2>
 {% if lojas is not none %}
  {% if lojas %}<div class="ok">✔ {{ lojas|length }} loja(s) visível(is) para este aplicativo</div>
   <table><tr><th>Loja</th><th>ID</th></tr>{% for l in lojas %}<tr><td>{{ l.nome }}</td><td class="peq">{{ l.id }}</td></tr>{% endfor %}</table>
  {% else %}<div class="falha">✘ Nenhuma loja retornada</div>{% endif %}
 {% else %}<div class="falha">✘ Não foi possível listar as lojas</div><div class="peq">{{ lojas_erro }}</div>{% endif %}
 <h2>3. Recebimento de eventos</h2>
 <div>{{ n_eventos }} evento(s) recebido(s) e gravado(s); último em {{ ultimo_evento }}.</div>
 <div class="peq">Modo: polling a cada 30 s (main.py), com acknowledgment de todos os eventos.</div>
 <div class="acoes"><a href="/conexao"><button type="button">Verificar de novo</button></a></div>
</div></main></body></html>"""


def _motivo(item):
    """A resposta pode vir com nomes de campo diferentes; aceita varios."""
    codigo = next((item[k] for k in ("cancelCodeId", "code", "cancellationCode", "id") if k in item), "")
    texto = next((item[k] for k in ("description", "reason", "name", "cancelCodeDescription") if k in item), "")
    return str(codigo), str(texto)


def criar_app(api=None, analytics=None, financeiro=None):
    app = Flask(__name__)
    app.config["ESTILO_PAINEL"] = ESTILO
    holder = {"api": api}

    def get_api():
        if holder["api"] is None:
            holder["api"] = IFoodOrders()  # so cria (e autentica) quando precisar
        return holder["api"]

    def voltar(msg, tipo=""):
        return redirect("/?msg=" + quote(msg) + ("&tipo=" + tipo if tipo else ""))

    def numero(order_id):
        lista = [p for p in store.listar_pedidos(200) if p["order_id"] == order_id]
        return "#" + str(lista[0]["display_id"]) if lista and lista[0]["display_id"] else order_id[:8]

    @app.get("/logo/<nome>")
    def logo(nome):
        if nome not in LOGOS or not os.path.exists(os.path.join(PASTA, LOGOS[nome])):
            abort(404)
        return send_from_directory(PASTA, LOGOS[nome])

    @app.get("/")
    def index():
        todos = store.listar_pedidos(200)
        ativos = [p for p in todos if p["status"] not in FINAIS]
        finais = [p for p in todos if p["status"] in FINAIS]
        aba = request.args.get("aba", "ativos")
        if aba not in ("ativos", "finalizados", "todos"):
            aba = "ativos"
        mostrar = {"ativos": ativos, "finalizados": finais, "todos": todos}[aba]
        historico = {}
        for ev in sorted(store.listar_eventos(1000), key=lambda e: e["criado_em"] or ""):
            historico.setdefault(ev["order_id"], []).append(ev)
        return render_template_string(
            PAGINA, estilo=ESTILO, pedidos=mostrar, eventos=store.listar_eventos(),
            aba=aba, n_ativos=len(ativos), n_finais=len(finais),
            tem_marca=os.path.exists(os.path.join(PASTA, LOGOS["marca"])),
            tem_ifood=os.path.exists(os.path.join(PASTA, LOGOS["ifood"])),
            msg=request.args.get("msg"), brl=brl, pagamentos=pagamentos, cupons=cupons,
            endereco=endereco, documento=documento, proximo=PROXIMO, finais=FINAIS, status_pt=STATUS_PT,
            tipos=TIPOS, entrega=ENTREGA, minutos_desde=minutos_desde, tempo=tempo, dia_hora=dia_hora,
            prazo=PRAZO_CONFIRMAR_MIN, historico=historico, evento_pt=EVENTO_PT,
            tipo=request.args.get("tipo", ""),
        )

    @app.get("/conexao")
    def conexao():
        auth_ok, auth_erro, lojas, lojas_erro = True, "", None, ""
        a = get_api()
        try:
            a.client.auth.get_token()
        except Exception as e:
            auth_ok, auth_erro = False, str(e)
        if auth_ok:
            try:
                resp = a.client.request("GET", "/merchant/v1.0/merchants")
                checar(resp)
                dados = resp.json() if resp.content else []
                if isinstance(dados, dict):
                    dados = dados.get("merchants") or dados.get("data") or []
                lojas = [{"id": m.get("id"), "nome": m.get("name") or m.get("corporateName") or "—"}
                         for m in dados]
            except Exception as e:
                lojas_erro = str(e)
        evs = store.listar_eventos(1)
        return render_template_string(
            PAGINA_CONEXAO, estilo=ESTILO, auth_ok=auth_ok, auth_erro=auth_erro,
            lojas=lojas, lojas_erro=lojas_erro,
            n_eventos=len(store.listar_eventos(100000)),
            ultimo_evento=dia_hora(evs[0]["criado_em"]) if evs else "—",
            agora=datetime.now(FUSO).strftime("%d/%m/%Y %H:%M:%S"),
            tem_marca=os.path.exists(os.path.join(PASTA, LOGOS["marca"])),
            tem_ifood=os.path.exists(os.path.join(PASTA, LOGOS["ifood"])),
        )

    @app.post("/importar")
    def importar():
        oid = (request.form.get("order_id") or "").strip()
        try:
            store.salvar_pedido(get_api().get(oid))
            return voltar(f"✔ Pedido {oid} importado.", "ok")
        except OrderNotFound:
            return voltar(f"Pedido {oid} não encontrado (404).", "erro")
        except Exception as e:
            return voltar(f"Erro ao importar: {e}", "erro")

    @app.post("/acao/<order_id>/<acao>")
    def acao(order_id, acao):
        a = get_api()
        metodos = {
            "confirm": a.confirm, "prepare": a.start_preparation,
            "ready": a.ready_to_pickup, "dispatch": a.dispatch,
        }
        if acao not in metodos:
            return voltar("Ação inválida.", "erro")
        try:
            metodos[acao](order_id)
            return voltar(f"✔ Pedido {numero(order_id)} {ACAO_PT[acao]} — aceito pelo iFood. O status muda quando o evento chegar (até 30 s).", "ok")
        except IFoodError as e:
            return voltar(f"✘ O iFood recusou a ação ({acao}): {e}", "erro")
        except Exception as e:
            return voltar(f"✘ Erro em {acao}: {e}", "erro")

    @app.route("/cancelar/<order_id>", methods=["GET", "POST"])
    def cancelar(order_id):
        a = get_api()
        if request.method == "POST":
            try:
                a.request_cancellation(order_id, request.form["codigo"])
                return voltar(f"✔ Cancelamento do pedido {numero(order_id)} solicitado — aceito pelo iFood. O pedido só fica cancelado quando chegar o evento CANCELLED.", "ok")
            except IFoodError as e:
                return voltar(f"✘ O iFood recusou o cancelamento: {e}", "erro")
        try:
            motivos = [_motivo(m) for m in a.cancellation_reasons(order_id)]
        except Exception as e:
            return voltar(f"✘ Não consegui buscar os motivos: {e}", "erro")
        lista = [p for p in store.listar_pedidos(200) if p["order_id"] == order_id]
        return render_template_string(
            PAGINA_CANCELAR, estilo=ESTILO, order_id=order_id, motivos=motivos,
            display=lista[0]["display_id"] if lista else None,
        )

    import analytics_web
    analytics_web.registrar(app, analytics)  # tela /analytics (modulo BI)
    import fin_web
    fin_web.registrar(app, financeiro)  # tela /financeiro (modulo Financial)
    return app


if __name__ == "__main__":
    print("Banco de dados:", store.DB)
    criar_app().run(host="127.0.0.1", port=5000)
