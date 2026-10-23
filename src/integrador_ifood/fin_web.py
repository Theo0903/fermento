"""fin_web.py - a tela "Financeiro" do painel.

Abas: Resumo · Vendas · Lancamentos · Repasses · Arquivo de conciliacao.
Botoes: Extrair periodo (ate 1 ano, em segundo plano) e Atualizar ultimos 35 dias.
Encaixa no painel.py com `registrar(app)`.
"""
import csv
import gzip
import io
import os
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from flask import Blueprint, Response, abort, redirect, render_template_string, request, send_file

import fin_calculos as calc
import fin_extrator as ext
import fin_store as store
from analytics_web import brl, inteiro
from fin_client import FinClient, FinError, em_homologacao, lojas_configuradas

PASTA = os.path.dirname(os.path.abspath(__file__))
FUSO = timezone(timedelta(hours=-3))
LIMITE_LINHAS = 500

STATUS_PT = {"CONCLUDED": "Concluído", "CANCELLED": "Cancelado", "PLACED": "Novo", "CONFIRMED": "Confirmado",
             "DISPATCHED": "Saiu p/ entrega", "READY_TO_PICKUP": "Pronto"}
RESP_PT = {"IFOOD": "iFood recebeu", "MERCHANT": "Loja recebeu"}
TIPO_TIT = {"REPASSE": "Repasse (loja recebe)", "BOLETO": "Boleto (loja deve)",
            "REGISTRO_RECEBIVEIS": "Retenção de recebíveis", "RENEGOCIADA": "Renegociada"}
STATUS_TIT = {"SUCCEED": "Pago", "PENDING": "Pendente", "FAILED": "Falhou",
              "TRANSFER_RENEGOTIATED": "Renegociado", "SCHEDULED": "Agendado"}
CONC_PT = {"solicitado": "Solicitado", "processando": "Processando", "pronto": "Pronto", "erro": "Erro",
           "expirado": "Expirado", "demorando": "Demorando"}


def br(d):
    if not d:
        return "—"
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return str(d)


def br_hora(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(FUSO).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(iso)


PAG_PT = {"CREDIT": "Crédito", "DEBIT": "Débito", "PIX": "Pix", "MEAL_VOUCHER": "Vale-refeição",
          "FOOD_VOUCHER": "Vale-alimentação", "DIGITAL_WALLET": "Carteira digital", "CASH": "Dinheiro",
          "VOUCHER": "Vale", "OTHER_VOUCHER": "Outro vale", "BANK_PAY": "Pagamento bancário"}


def pagamento_pt(txt):
    if not txt:
        return "—"
    return " + ".join(" ".join([PAG_PT.get(p.split(" ")[0], p.split(" ")[0])] + p.split(" ")[1:]) for p in txt.split(" + "))


def repasse_fmt(txt):
    """'2025-03-19' -> 19/03/2025 ; '2025-03-19 a 2025-05-21 (3 datas)' -> 19/03/2025 (+2 parcelas)"""
    if not txt:
        return "—"
    if " a " in txt:
        n = txt.split("(")[-1].split(" ")[0] if "(" in txt else "?"
        try:
            return f"{br(txt[:10])} (+{int(n) - 1} parcela{'s' if int(n) > 2 else ''})"
        except ValueError:
            return br(txt[:10])
    return br(txt)


def data_curta(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(FUSO).strftime("%d/%m/%y %H:%M")
    except ValueError:
        return str(iso)


def pct(v):
    return f"{v * 100:.1f}%".replace(".", ",")


def _datas(args):
    hoje = datetime.now(FUSO).date()
    try:
        fim = datetime.strptime(args.get("fim", ""), "%Y-%m-%d").date()
    except ValueError:
        fim = hoje
    try:
        ini = datetime.strptime(args.get("ini", ""), "%Y-%m-%d").date()
    except ValueError:
        ini = fim - timedelta(days=29)
    return ini, fim, hoje


def _lojas_filtro(loja):
    todas = lojas_configuradas() or [r["merchant_id"] for r in store.cobertura()]
    return ([loja] if loja and loja != "todas" else todas), todas


def _in(lst):
    return ",".join("?" * len(lst)) or "''"


def _dados(lojas, ini, fim):
    sems = [s.isoformat() for s, _ in calc.semanas(ini, fim, hoje=max(fim, date.today()))]
    vendas = store.consultar(f"SELECT * FROM fin_venda WHERE merchant_id IN ({_in(lojas)}) AND dia BETWEEN ? AND ? "
                             "ORDER BY criado_em DESC", (*lojas, str(ini), str(fim)))
    eventos = store.consultar(f"SELECT * FROM fin_evento WHERE merchant_id IN ({_in(lojas)}) AND semana IN ({_in(sems)}) "
                              "ORDER BY dia DESC, seq", (*lojas, *sems))
    eventos, repetidos = sem_repeticao(eventos)
    liqs = store.consultar(f"SELECT * FROM fin_liquidacao WHERE merchant_id IN ({_in(lojas)}) AND janela_ini IN ({_in(sems)})",
                           (*lojas, *sems))
    titulos = store.consultar(f"SELECT * FROM fin_titulo WHERE merchant_id IN ({_in(lojas)}) AND "
                              "COALESCE(periodo_fim, data_pagamento) >= ? AND COALESCE(periodo_ini, data_pagamento) <= ? "
                              "ORDER BY periodo_ini DESC, tipo", (*lojas, str(ini), str(fim)))
    return vendas, eventos, liqs, titulos, sems, repetidos


def sem_repeticao(eventos):
    """O MESMO lancamento (registro identico) vindo em semanas DIFERENTES conta uma vez so.
    Acontece no ambiente de teste do iFood, que devolve sempre o mesmo exemplo para qualquer periodo.
    Dentro da mesma semana nada e descartado. Devolve (lista_sem_repeticao, quantos_repetidos)."""
    primeira_semana, saida, repetidos = {}, [], 0
    for e in sorted(eventos, key=lambda x: x["semana"]):
        chave = (e["merchant_id"], e["json"])
        if chave in primeira_semana and primeira_semana[chave] != e["semana"]:
            repetidos += 1
            continue
        primeira_semana.setdefault(chave, e["semana"])
        saida.append(e)
    saida.sort(key=lambda x: (x["dia"] or "", -(x["seq"] or 0)), reverse=True)
    return saida, repetidos


def conferencia_semanal(eventos, liqs):
    """Mesma janela de busca nas duas APIs -> comparacao justa, semana a semana."""
    por = {}
    for e in eventos:
        k = (e["merchant_id"], e["semana"])
        por.setdefault(k, {"ev": [], "liq": []})["ev"].append(e)
    for l in liqs:
        por.setdefault((l["merchant_id"], l["janela_ini"]), {"ev": [], "liq": []})["liq"].append(l)
    linhas = []
    for (m, sem), d in sorted(por.items(), key=lambda x: x[0][1], reverse=True):
        c = calc.conferir_repasse(d["ev"], d["liq"])
        linhas.append({"merchant_id": m, "semana": sem, **c})
    return linhas


ESTILO_FIN = """
 .teste{background:#FFF7DE;border:2px dashed #E6CF8A;color:#8A6100;border-radius:14px;padding:10px 14px;margin-bottom:14px;font-weight:700}
 .abas-fin{display:flex;gap:6px;flex-wrap:wrap;background:#fff;border:1px solid var(--borda);padding:4px;border-radius:999px;margin-bottom:14px}
 .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:14px}
 .kpi{background:#fff;border:1px solid var(--borda);border-radius:18px;padding:14px 16px}
 .kpi .rot{font-size:12px;color:var(--suave);font-weight:600;text-transform:uppercase;letter-spacing:.6px}
 .kpi .val{font-family:'Montserrat',sans-serif;font-size:22px;font-weight:800;color:var(--vinho);margin-top:2px}
 .kpi .dica{font-size:12px;color:var(--suave)} .kpi.neg .val{color:var(--neg)} .kpi.pos .val{color:var(--ok)}
 .filtro{display:flex;gap:10px;align-items:end;flex-wrap:wrap}
 .filtro label{display:flex;flex-direction:column;font-size:12px;color:var(--suave);font-weight:600;gap:3px}
 .filtro input,.filtro select{min-width:0;padding:8px 12px;border:1px solid var(--borda);border-radius:10px;font:inherit;background:#fff}
 .aviso{background:#E8F0FB;border:1px solid #BCD1EE;color:#1F4E8C;border-radius:14px;padding:10px 14px;margin-bottom:14px;font-size:14px}
 .aviso ul{margin:4px 0 0 18px;padding:0}
 .num-d{text-align:right;white-space:nowrap} .cred{color:var(--ok);font-weight:700} .deb{color:var(--neg);font-weight:700}
 .tabela{overflow-x:auto} .tabela table{min-width:760px}
 .progresso{background:var(--tint);border-radius:999px;height:14px;overflow:hidden;margin:8px 0}
 .progresso i{display:block;height:14px;background:var(--rosa)}
 .pill.ok2{background:var(--ok-bg);color:var(--ok)} .pill.aten{background:var(--aten-bg);color:var(--aten)}
 .pill.neg2{background:var(--neg-bg);color:var(--neg)} .pill.neu{background:#EEE8EA;color:var(--suave)}
 .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px}
 .cascata td:first-child{padding-left:4px} .cascata tr.total td{font-weight:800;border-top:2px solid var(--vinho)}
 h2.tit{margin-top:0}
 main{max-width:1280px} .nowrap{white-space:nowrap}
"""

PAGINA = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Financeiro iFood - Rede Demo</title>
{% if atualizar %}<meta http-equiv="refresh" content="{{ atualizar }}">{% endif %}
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Montserrat:wght@600;700;800&display=swap" rel="stylesheet">
<style>{{ estilo|safe }}{{ extra|safe }}</style></head><body>
<header>
 <div class="logos">
  {% if tem_marca %}<img src="/logo/marca" alt="Rede Demo">{% else %}<span class="marca">Rede Demo</span>{% endif %}
  {% if tem_ifood %}<img src="/logo/ifood" alt="iFood">{% else %}<span class="tag ifood">iFood</span>{% endif %}
 </div>
 <h1>Financeiro iFood<span class="sub">Vendas, comissões, taxas, repasses e conciliação</span></h1>
 <a href="/analytics" class="btn-conexao">Analytics</a>
 <a href="/" class="btn-conexao">Pedidos</a>
</header>
<main>
{% if teste %}<div class="teste">⚠ AMBIENTE DE TESTE: dados da loja de teste do iFood (header x-request-homologation). Para produção, troque as credenciais IFOOD_FIN_ no .env e use IFOOD_FIN_HOMOLOGACAO=0.</div>{% endif %}
{% if msg %}<div class="msg {{ tipo }}">{{ msg }}</div>{% endif %}
{% if repetidos and aba in ('resumo', 'lancamentos', 'repasses') %}<div class="msg">⚠ {{ inteiro(repetidos) }} lançamento(s) idêntico(s) apareceram em mais de uma semana e foram contados <b>uma vez só</b>. É o que acontece no ambiente de teste, onde o iFood devolve sempre o mesmo exemplo para qualquer período.</div>{% endif %}

{% if job %}<div class="card">
 <b>Extração em andamento</b> · {{ job.mensagem or "começando..." }}
 <div class="progresso"><i style="width:{{ job.pct }}%"></i></div>
 <div class="peq">{{ job.passo }} de {{ job.total }} semana(s) · {{ job.pct }}% · esta página atualiza sozinha. Pode navegar à vontade.</div>
</div>{% endif %}

<div class="card">
 <form method="get" action="/financeiro" class="filtro">
  <input type="hidden" name="aba" value="{{ aba }}">
  <label>Data inicial<input type="date" name="ini" value="{{ ini }}" max="{{ hoje }}"></label>
  <label>Data final<input type="date" name="fim" value="{{ fim }}" max="{{ hoje }}"></label>
  <label>Loja<select name="loja">
   {% if todas|length > 1 %}<option value="todas" {{ 'selected' if loja == 'todas' }}>Todas ({{ todas|length }})</option>{% endif %}
   {% for m in todas %}<option value="{{ m }}" {{ 'selected' if loja == m }}>{{ nomes.get(m, m) }}</option>{% endfor %}
  </select></label>
  <button type="submit" class="sec">Mostrar</button>
  <button type="submit" formmethod="post" formaction="/financeiro/extrair" {{ 'disabled' if job }}>Extrair este período do iFood</button>
  <button type="submit" formmethod="post" formaction="/financeiro/recentes" class="sec" {{ 'disabled' if job }}>Atualizar últimos 35 dias</button>
 </form>
 <div class="peq" style="margin-top:8px">Mostrando <b>{{ ini_br }}</b> a <b>{{ fim_br }}</b>{% if not todas %} · <b>Nenhuma loja configurada:</b> preencha IFOOD_FIN_LOJAS no .env{% endif %}.</div>
 <details style="margin-top:8px"><summary class="mais">Quanto tempo para trás dá para extrair?</summary>
 <div class="aviso" style="margin-top:8px">
  <ul>
   <li><b>Por extração:</b> até <b>1 ano</b> (ex.: desde janeiro). O programa divide em semanas (segunda a domingo) e busca uma de cada vez, como o iFood recomenda. O período é esticado até a segunda e o domingo mais próximos.</li>
   <li><b>Limites do iFood por consulta:</b> vendas até 90 dias, lançamentos até 33 dias. Por isso a divisão em semanas é automática.</li>
   <li><b>Até quando o iFood guarda:</b> a documentação não informa. Ao final da extração aparece a <b>primeira semana com dados</b>, que é o limite real.</li>
   <li><b>Arquivo de conciliação:</b> só meses <b>fechados</b>, até <b>24 meses</b> atrás.</li>
   <li><b>Tempo:</b> cerca de 3 a 10 consultas por semana e por loja. 1 ano de 1 loja leva poucos minutos; 1 ano de 59 lojas pode levar perto de 1 hora (limite de 1000 consultas por minuto). Roda em segundo plano.</li>
   <li><b>Rodar de novo</b> o mesmo período <b>não duplica</b>: atualiza. Use "Atualizar últimos 35 dias" com frequência, porque cancelamentos e ajustes mudam valores recentes.</li>
  </ul>
 </div></details>
</div>

<nav class="abas-fin">
 {% for k, rot in abas %}<a class="aba {{ 'ativa' if aba == k }}" href="/financeiro?{{ qs }}&aba={{ k }}">{{ rot }}</a>{% endfor %}
</nav>

{% if aba == 'resumo' %}
 <div class="kpis">
  <div class="kpi"><div class="rot">Valor bruto</div><div class="val">{{ brl(r.bruto) }}</div><div class="dica">{{ inteiro(r.n_validas) }} pedidos válidos · ticket {{ brl(r.ticket) }}</div></div>
  <div class="kpi neg"><div class="rot">Descontos (total)</div><div class="val">{{ brl(r.desc_total) }}</div><div class="dica">todos os patrocinadores</div></div>
  <div class="kpi neg"><div class="rot">Comissão iFood</div><div class="val">{{ brl(r.comissao) }}</div><div class="dica">soma das comissões</div></div>
  <div class="kpi neg"><div class="rot">Taxas</div><div class="val">{{ brl(r.taxas) }}</div><div class="dica">entrega iFood, serviço, pagamento</div></div>
  <div class="kpi pos"><div class="rot">Líquido das vendas</div><div class="val">{{ brl(r.liquido) }}</div><div class="dica">{{ pct(r.pct_liquido) }} do bruto</div></div>
  <div class="kpi"><div class="rot">Cancelados</div><div class="val">{{ inteiro(r.n_canceladas) }}</div><div class="dica">{{ brl(r.bruto_cancelado) }} de bruto</div></div>
 </div>
 <div class="cols">
  <div class="card"><h2 class="tit">Do bruto ao líquido</h2>
   <table class="cascata">
    <tr><td>Valor bruto (itens + entrega + serviço)</td><td class="num-d">{{ brl(r.bruto) }}</td></tr>
    <tr><td>− Descontos dados ao cliente</td><td class="num-d deb">{{ brl(-r.desc_total) }}</td></tr>
    <tr><td>Entrada (o que o cliente pagou)</td><td class="num-d">{{ brl(r.entrada) }}</td></tr>
    <tr><td>+ Subsídios recebidos (iFood/rede)</td><td class="num-d cred">{{ brl(r.subsidios) }}</td></tr>
    <tr><td>− Comissão iFood</td><td class="num-d deb">{{ brl(r.comissao) }}</td></tr>
    <tr><td>− Taxas</td><td class="num-d deb">{{ brl(r.taxas) }}</td></tr>
    {% if r.outros %}<tr><td>± Outros</td><td class="num-d">{{ brl(r.outros) }}</td></tr>{% endif %}
    <tr class="total"><td>= Líquido (saleBalance)</td><td class="num-d">{{ brl(r.liquido) }}</td></tr>
   </table>
   <div class="peq" style="margin-top:6px">Entrada e líquido somam todos os pedidos, inclusive os efeitos de cancelamentos; bruto e descontos somam só os válidos.</div>
  </div>
  <div class="card"><h2 class="tit">Promoções: quem pagou o desconto</h2>
   <table><tr><th>Patrocinador</th><th class="num-d">Valor</th><th class="num-d">% do bruto</th></tr>
    <tr><td>Loja</td><td class="num-d">{{ brl(r.desc_loja) }}</td><td class="num-d">{{ pct(r.desc_loja / r.bruto if r.bruto else 0) }}</td></tr>
    <tr><td>Rede (franqueadora)</td><td class="num-d">{{ brl(r.desc_rede) }}</td><td class="num-d">{{ pct(r.desc_rede / r.bruto if r.bruto else 0) }}</td></tr>
    <tr><td>iFood</td><td class="num-d">{{ brl(r.desc_ifood) }}</td><td class="num-d">{{ pct(r.desc_ifood / r.bruto if r.bruto else 0) }}</td></tr>
    {% if r.desc_externo %}<tr><td>Externo</td><td class="num-d">{{ brl(r.desc_externo) }}</td><td class="num-d">{{ pct(r.desc_externo / r.bruto if r.bruto else 0) }}</td></tr>{% endif %}
   </table>
   <h2>Com promoção da loja/rede × sem</h2>
   <table><tr><th></th><th class="num-d">Pedidos</th><th class="num-d">Ticket</th><th class="num-d">Líquido ÷ bruto</th></tr>
    <tr><td>Com promoção</td><td class="num-d">{{ inteiro(r.n_com_promo) }}</td><td class="num-d">{{ brl(r.ticket_com_promo) }}</td><td class="num-d">{{ pct(r.margem_com_promo) }}</td></tr>
    <tr><td>Sem promoção</td><td class="num-d">{{ inteiro(r.n_sem_promo) }}</td><td class="num-d">{{ brl(r.ticket_sem_promo) }}</td><td class="num-d">{{ pct(r.margem_sem_promo) }}</td></tr>
   </table>
   <div class="peq" style="margin-top:6px">Leitura: se a promoção aumenta pedidos e ticket mais do que reduz o "líquido ÷ bruto", ela tende a compensar.</div>
  </div>
  <div class="card"><h2 class="tit">Conferências</h2>
   <table>
    <tr><td>Repasse pelos lançamentos (com impacto)</td><td class="num-d">{{ brl(rep.com_impacto) }}</td></tr>
    <tr><td>Repasse informado pelo iFood (liquidações)</td><td class="num-d">{{ brl(rep.balance) if rep.tem_liquidacao else "—" }}</td></tr>
    <tr><td>Diferença</td><td class="num-d">{% if rep.tem_liquidacao %}{% if rep.ok %}<span class="pill ok2">✔ bate (tolerância 0,01%)</span>{% else %}<span class="pill neg2">⚠ {{ brl(rep.diferenca) }}</span>{% endif %}{% else %}<span class="pill neu">sem liquidação no período</span>{% endif %}</td></tr>
    <tr><td>Vendas: lançamentos × líquido</td><td class="num-d">{% if integ %}<span class="pill neg2">⚠ {{ integ|length }} pedido(s)</span>{% else %}<span class="pill ok2">✔ {{ inteiro(vendas|length) }} conferidas</span>{% endif %}</td></tr>
    <tr><td>Pedido a pedido: líquido × lançamentos</td><td class="num-d">{% if ped.divergentes %}<span class="pill neg2">⚠ {{ ped.divergentes|length }} de {{ ped.conferidos }}</span>{% elif ped.conferidos %}<span class="pill ok2">✔ {{ ped.conferidos }} conferidos</span>{% else %}<span class="pill neu">sem dados</span>{% endif %}</td></tr>
   </table>
   <div class="peq" style="margin-top:6px">As duas primeiras linhas usam <b>semanas completas</b> ({{ sem_txt }}), porque o iFood apura de segunda a domingo; as vendas usam os dias exatos do filtro. Detalhes semana a semana na aba Repasses. Diferenças podem vir de retenção de recebíveis.</div>
  </div>
 </div>
 <div class="card"><h2 class="tit">O que já está no banco</h2>
  {% if cobertura %}<div class="tabela"><table><tr><th>Loja</th><th>Vendas</th><th>De</th><th>Até</th><th>Lançamentos</th><th>Semanas de</th><th>Até</th></tr>
  {% for c in cobertura %}<tr><td>{{ c.loja or c.merchant_id }}</td><td class="num-d">{{ inteiro(c.v_n) }}</td><td>{{ br(c.v_ini) }}</td><td>{{ br(c.v_fim) }}</td>
   <td class="num-d">{{ inteiro(c.e_n) }}</td><td>{{ br(c.e_ini) }}</td><td>{{ br(c.e_fim) }}</td></tr>{% endfor %}</table></div>
  {% else %}<div class="vazio">Nada extraído ainda. Escolha o período e clique em "Extrair este período do iFood".</div>{% endif %}
  {% if jobs %}<h2>Últimas extrações</h2><div class="tabela"><table><tr><th>Quando</th><th>Período</th><th>Situação</th><th>Resultado</th></tr>
  {% for j in jobs %}<tr><td class="peq">{{ br_hora(j.criado_em) }}</td><td>{{ br(j.ini) }} a {{ br(j.fim) }}</td>
   <td><span class="pill {{ {'concluido':'ok2','parcial':'aten','erro':'neg2','rodando':'aten'}.get(j.status,'neu') }}">{{ j.status }}</span></td><td class="peq">{{ j.mensagem }}</td></tr>{% endfor %}</table></div>{% endif %}
 </div>

{% elif aba == 'vendas' %}
 <div class="card"><h2 class="tit">Vendas ({{ inteiro(vendas|length) }}) <a href="/financeiro/csv/vendas?{{ qs }}"><button type="button" class="sec" style="float:right">Baixar CSV</button></a></h2>
  {% if integ %}<div class="msg erro">⚠ {{ integ|length }} venda(s) em que a soma dos lançamentos não bate com o líquido informado pelo iFood (marcadas com ⚠).</div>{% endif %}
  {% if vendas %}<div class="tabela"><table>
   <tr><th>Pedido</th><th>Data</th><th>Status</th><th>Pagamento</th><th class="num-d">Bruto</th><th class="num-d">Descontos</th><th class="num-d">Comissão</th><th class="num-d">Taxas</th><th class="num-d">Subsídios</th><th class="num-d">Líquido</th><th>Repasse previsto</th></tr>
   {% for v in vendas[:limite] %}<tr>
    <td><b>#{{ v.short_id }}</b>{% if v.order_id in integ_ids %} ⚠{% endif %}<div class="peq">{{ v.loja_nome or '' }}</div></td>
    <td class="peq nowrap">{{ data_curta(v.criado_em) }}</td>
    <td><span class="pill st-{{ v.status }}">{{ status_pt.get(v.status, v.status) }}</span></td>
    <td>{{ pagamento_pt(v.pagamento) }}<div class="peq">{{ v.responsavel and resp(v.responsavel) or '' }}</div></td>
    <td class="num-d">{{ brl(v.bruto) }}</td>
    <td class="num-d">{{ brl(v.desc_total) }}{% if v.desc_total %}<div class="peq">{% if v.desc_loja %}loja {{ brl(v.desc_loja) }} {% endif %}{% if v.desc_rede %}rede {{ brl(v.desc_rede) }} {% endif %}{% if v.desc_ifood %}iFood {{ brl(v.desc_ifood) }}{% endif %}</div>{% endif %}</td>
    <td class="num-d deb">{{ brl(v.comissao) }}</td><td class="num-d deb">{{ brl(v.taxas) }}</td>
    <td class="num-d cred">{{ brl(v.subsidios) }}</td><td class="num-d"><b>{{ brl(v.liquido) }}</b></td>
    <td class="peq nowrap">{{ repasse_fmt(v.repasse_previsto) }}</td>
   </tr>{% endfor %}
  </table></div>{% if vendas|length > limite %}<div class="peq">Mostrando {{ limite }} de {{ vendas|length }}. Baixe o CSV para ver todas.</div>{% endif %}
  {% else %}<div class="vazio">Nenhuma venda neste período. Use "Extrair este período do iFood".</div>{% endif %}
 </div>

{% elif aba == 'lancamentos' %}
 <div class="kpis">
  <div class="kpi pos"><div class="rot">Créditos (com impacto)</div><div class="val">{{ brl(rep.creditos) }}</div></div>
  <div class="kpi neg"><div class="rot">Débitos (com impacto)</div><div class="val">{{ brl(rep.debitos) }}</div></div>
  <div class="kpi"><div class="rot">Repasse calculado</div><div class="val">{{ brl(rep.com_impacto) }}</div><div class="dica">soma só dos lançamentos com impacto no repasse</div></div>
  <div class="kpi"><div class="rot">Sem impacto (informativo)</div><div class="val">{{ brl(rep.sem_impacto) }}</div><div class="dica">ex.: vale-refeição pago à loja, promoção da loja</div></div>
 </div>
 <div class="card"><h2 class="tit">Lançamentos ({{ inteiro(eventos_f|length) }})
   <a href="/financeiro/csv/lancamentos?{{ qs }}"><button type="button" class="sec" style="float:right">Baixar CSV</button></a></h2>
  <div class="filtro" style="margin-bottom:10px">
   <a href="/financeiro?{{ qs }}&aba=lancamentos&imp=todos"><button type="button" class="{{ '' if imp == 'todos' else 'sec' }}">Todos</button></a>
   <a href="/financeiro?{{ qs }}&aba=lancamentos&imp=sim"><button type="button" class="{{ '' if imp == 'sim' else 'sec' }}">Com impacto no repasse</button></a>
   <a href="/financeiro?{{ qs }}&aba=lancamentos&imp=nao"><button type="button" class="{{ '' if imp == 'nao' else 'sec' }}">Sem impacto</button></a>
  </div>
  {% if eventos_f %}<div class="tabela"><table>
   <tr><th>Data</th><th>Lançamento</th><th>Gatilho</th><th>Pedido</th><th class="num-d">Valor</th><th>Impacto no repasse</th><th class="num-d">Base · %</th><th>Repasse esperado</th></tr>
   {% for e in eventos_f[:limite] %}<tr>
    <td class="peq nowrap">{{ br(e.dia) }}</td><td><b>{{ e.nome }}</b><div class="peq">{{ e.descricao or '' }}</div></td>
    <td class="peq">{{ e.gatilho or '—' }}</td><td class="peq">{{ (e.ref_id or '—')[:8] }}</td>
    <td class="num-d {{ 'cred' if e.valor > 0 else 'deb' }}">{{ brl(e.valor) }}</td>
    <td>{% if e.impacto %}<span class="pill ok2">Sim</span>{% else %}<span class="pill neu">Não</span>{% endif %}</td>
    <td class="num-d peq">{{ brl(e.base) if e.base is not none else '—' }}{% if e.percentual is not none %} · {{ ('%.2f'|format(e.percentual)).replace('.', ',') }}%{% endif %}</td>
    <td class="peq">{{ br(e.repasse_esperado) }}</td></tr>{% endfor %}
  </table></div>{% if eventos_f|length > limite %}<div class="peq">Mostrando {{ limite }} de {{ eventos_f|length }}. Baixe o CSV para ver todos.</div>{% endif %}
  {% else %}<div class="vazio">Nenhum lançamento neste filtro.</div>{% endif %}
  <div class="peq" style="margin-top:8px">Desde março de 2024 as promoções da loja aparecem como "sem impacto". Dados mais antigos podem vir no formato anterior (débito + crédito).</div>
 </div>

{% elif aba == 'repasses' %}
 <div class="card"><h2 class="tit">Conferência semana a semana</h2>
  {% if semanal %}<div class="tabela"><table><tr><th>Semana</th><th class="num-d">Lançamentos com impacto</th><th class="num-d">Repasse do iFood (balance)</th><th class="num-d">Diferença</th><th>Situação</th></tr>
  {% for s in semanal %}<tr><td>{{ br(s.semana) }}{% if todas|length > 1 %}<div class="peq">{{ nomes.get(s.merchant_id, s.merchant_id) }}</div>{% endif %}</td>
   <td class="num-d">{{ brl(s.com_impacto) }}</td><td class="num-d">{{ brl(s.balance) if s.tem_liquidacao else '—' }}</td>
   <td class="num-d">{{ brl(s.diferenca) if s.tem_liquidacao else '—' }}</td>
   <td>{% if not s.tem_liquidacao %}<span class="pill neu">sem liquidação</span>{% elif s.ok %}<span class="pill ok2">✔ bate</span>{% else %}<span class="pill neg2">⚠ confira</span>{% endif %}</td></tr>{% endfor %}
  </table></div>{% else %}<div class="vazio">Sem dados. Extraia o período.</div>{% endif %}
 </div>
 <div class="card"><h2 class="tit">Títulos ({{ titulos|length }}) <a href="/financeiro/csv/repasses?{{ qs }}"><button type="button" class="sec" style="float:right">Baixar CSV</button></a></h2>
  {% if titulos %}<div class="tabela"><table><tr><th>Período apurado</th><th>Título</th><th>Tipo</th><th>Produto</th><th class="num-d">Valor</th><th>Status</th><th>Data de pagamento</th><th>Conta</th></tr>
  {% for t in titulos %}<tr><td class="peq">{{ br(t.periodo_ini) }} a {{ br(t.periodo_fim) }}</td><td>{{ t.titulo_id }}</td>
   <td>{{ tipo_tit.get(t.tipo, t.tipo) }}</td><td class="peq">{{ t.produto }}</td>
   <td class="num-d {{ 'deb' if t.tipo == 'BOLETO' else '' }}">{{ brl(t.valor) }}</td>
   <td><span class="pill {{ {'SUCCEED':'ok2','FAILED':'neg2'}.get(t.status,'aten') }}">{{ status_tit.get(t.status, t.status) }}</span></td>
   <td>{{ br(t.data_pagamento) }}</td>
   <td class="peq">{% if t.status == 'SUCCEED' and t.banco %}{{ t.banco }} · ag {{ t.agencia }} · {{ t.conta }}{% else %}—{% endif %}</td></tr>{% endfor %}
  </table></div>{% else %}<div class="vazio">Nenhum título neste período.</div>{% endif %}
  <div class="peq" style="margin-top:8px">Repasse = loja recebe · Boleto = loja deve ao iFood · Retenção de recebíveis = parte retida para pagar dívida com instituição financeira. A data de pagamento é quando a transferência foi ou será feita.</div>
 </div>

{% elif aba == 'conciliacao' %}
 <div class="card"><h2 class="tit">Pedir arquivo de conciliação</h2>
  <form method="post" action="/financeiro/conciliacao/pedir" class="filtro">
   <input type="hidden" name="volta" value="{{ qs }}">
   <label>Mês (competência)<input type="month" name="comp" value="{{ mes_padrao }}" max="{{ mes_padrao }}" required></label>
   <label>Loja<select name="loja">{% if todas|length > 1 %}<option value="todas">Todas ({{ todas|length }})</option>{% endif %}
    {% for m in todas %}<option value="{{ m }}" {{ 'selected' if loja == m }}>{{ nomes.get(m, m) }}</option>{% endfor %}</select></label>
   <button type="submit">Pedir arquivo ao iFood</button>
  </form>
  <div class="peq" style="margin-top:8px">Só meses fechados, até 24 meses atrás. O iFood gera o arquivo em alguns minutos; esta página acompanha sozinha e baixa quando ficar pronto. Pedir o mesmo mês de novo em menos de 6 h reaproveita o pedido anterior.</div>
 </div>
 <div class="card"><h2 class="tit">Arquivos</h2>
  {% if concs %}<div class="tabela"><table><tr><th>Mês</th><th>Loja</th><th>Situação</th><th>Pedido em</th><th>Concluído em</th><th class="num-d">Linhas</th><th class="num-d">Total com impacto</th><th>Ações</th></tr>
  {% for c in concs %}<tr>
   <td><b>{{ c.competencia }}</b></td><td class="peq">{{ nomes.get(c.merchant_id, c.merchant_id) }}</td>
   <td><span class="pill {{ {'pronto':'ok2','erro':'neg2','expirado':'neg2'}.get(c.status,'aten') }}">{{ conc_pt.get(c.status, c.status) }}</span><div class="peq">{{ c.mensagem or '' }}</div></td>
   <td class="peq">{{ br_hora(c.pedido_em) }}</td><td class="peq">{{ br_hora(c.pronto_em) }}</td>
   <td class="num-d">{{ inteiro(c.linhas) if c.linhas is not none else '—' }}</td>
   <td class="num-d">{{ brl(c.total_impacto) if c.total_impacto is not none else '—' }}</td>
   <td style="white-space:nowrap">
    {% if c.status != 'pronto' %}<form method="post" action="/financeiro/conciliacao/verificar"><input type="hidden" name="loja" value="{{ c.merchant_id }}"><input type="hidden" name="comp" value="{{ c.competencia }}"><input type="hidden" name="volta" value="{{ qs }}"><button class="sec">Verificar agora</button></form>{% endif %}
    {% if c.status == 'pronto' %}<a href="/financeiro/conciliacao/arquivo?loja={{ c.merchant_id }}&comp={{ c.competencia }}"><button type="button">Baixar CSV</button></a>
     <a href="/financeiro/conciliacao/arquivo?loja={{ c.merchant_id }}&comp={{ c.competencia }}&formato=gz"><button type="button" class="sec">Original .gz</button></a>{% endif %}
   </td></tr>
   {% if c.resumo %}<tr><td colspan="8"><details><summary class="mais">Resumo conciliado de {{ c.competencia }}</summary>
    <div class="cols" style="margin-top:8px">
     <div><table>
      <tr><td>Lançamentos com impacto no repasse</td><td class="num-d">{{ inteiro(c.resumo.com_impacto) }} · <b>{{ brl(c.resumo.total_impacto) }}</b></td></tr>
      <tr><td>Sem impacto (informativo)</td><td class="num-d">{{ inteiro(c.resumo.sem_impacto) }} · {{ brl(c.resumo.total_sem_impacto) }}</td></tr>
      <tr><td>Pedidos no arquivo</td><td class="num-d">{{ inteiro(c.resumo.pedidos) }}</td></tr>
      <tr><td>Lançamentos do mês na API (com impacto)</td><td class="num-d">{{ brl(c.api_impacto) if c.api_impacto is not none else '— (extraia o mês)' }}</td></tr>
      <tr><td>Arquivo × API</td><td class="num-d">{% if c.api_impacto is none %}<span class="pill neu">sem dados da API</span>{% elif c.bate %}<span class="pill ok2">✔ bate</span>{% else %}<span class="pill neg2">⚠ {{ brl(c.resumo.total_impacto - c.api_impacto) }}</span>{% endif %}</td></tr>
     </table></div>
     <div><table><tr><th>Tipo de lançamento (com impacto)</th><th class="num-d">Valor</th></tr>
      {% for k, v in c.resumo.por_tipo %}<tr><td>{{ k }}</td><td class="num-d">{{ brl(v) }}</td></tr>{% endfor %}</table></div>
     <div><table><tr><th>Repasse previsto por data</th><th class="num-d">Valor</th></tr>
      {% for k, v in c.resumo.por_data %}<tr><td>{{ br(k) }}</td><td class="num-d">{{ brl(v) }}</td></tr>{% else %}<tr><td colspan="2" class="peq">Período de apuração em aberto ou sem data de repasse.</td></tr>{% endfor %}</table></div>
    </div></details></td></tr>{% endif %}
  {% endfor %}</table></div>
  {% else %}<div class="vazio">Nenhum arquivo pedido ainda.</div>{% endif %}
 </div>
{% endif %}
</main></body></html>"""


def registrar(app, fabrica_fin=None):
    bp = Blueprint("fin_web", __name__)
    fabrica = fabrica_fin or (lambda: FinClient())
    store.marcar_jobs_interrompidos()

    def volta(qs, msg, tipo=""):
        return redirect(f"/financeiro?{qs}&msg={quote(msg)}" + (f"&tipo={tipo}" if tipo else ""))

    def qs_de(ini, fim, loja, aba=None):
        d = {"ini": str(ini), "fim": str(fim), "loja": loja}
        if aba:
            d["aba"] = aba
        return urlencode(d)

    def nomes():
        return {r["merchant_id"]: r["loja"] for r in store.cobertura() if r.get("loja")}

    @bp.get("/financeiro")
    def pagina():
        ini, fim, hoje = _datas(request.args)
        aba = request.args.get("aba", "resumo")
        lojas, todas = _lojas_filtro(request.args.get("loja"))
        loja = request.args.get("loja") or ("todas" if len(todas) > 1 else (todas[0] if todas else ""))
        vendas, eventos, liqs, titulos, sems, repetidos = _dados(lojas, ini, fim)
        sem_txt = f"{br(sems[0])} a {br(calc.data(sems[-1]) + timedelta(days=6))}" if sems else "—"
        imp = request.args.get("imp", "todos")
        eventos_f = [e for e in eventos if imp == "todos" or (imp == "sim") == bool(e["impacto"])]
        integ = calc.integridade_vendas(vendas)
        job = store.job_rodando()
        if job:
            job["pct"] = int(100 * (job["passo"] or 0) / job["total"]) if job["total"] else 0
        concs = []
        if aba == "conciliacao":
            for c in store.consultar(f"SELECT * FROM fin_conciliacao WHERE merchant_id IN ({_in(todas)}) "
                                     "ORDER BY competencia DESC, merchant_id", tuple(todas)):
                linhas = store.consultar("SELECT * FROM fin_conc_linha WHERE merchant_id=? AND competencia=?",
                                         (c["merchant_id"], c["competencia"]))
                c["resumo"] = calc.resumo_conciliacao(linhas) if linhas else None
                api = store.consultar("SELECT COUNT(*) n, SUM(valor) s FROM fin_evento WHERE merchant_id=? AND impacto=1 "
                                      "AND (competencia=? OR competencia LIKE ? OR (competencia IS NULL AND dia LIKE ?))",
                                      (c["merchant_id"], c["competencia"], c["competencia"] + "%", c["competencia"] + "%"))[0]
                c["api_impacto"] = round(api["s"], 2) if api["n"] else None
                c["bate"] = c["resumo"] is not None and c["api_impacto"] is not None and \
                    calc.dentro(c["resumo"]["total_impacto"], c["api_impacto"])
                concs.append(c)
        pendente = any(c["status"] in ("solicitado", "processando") for c in concs)
        primeiro_mes = date(hoje.year, hoje.month, 1) - timedelta(days=1)
        ctx = dict(
            estilo=app.config.get("ESTILO_PAINEL", ""), extra=ESTILO_FIN, teste=em_homologacao(),
            msg=request.args.get("msg"), tipo=request.args.get("tipo", ""), aba=aba,
            abas=[("resumo", "Resumo"), ("vendas", "Vendas"), ("lancamentos", "Lançamentos"),
                  ("repasses", "Repasses"), ("conciliacao", "Arquivo de conciliação")],
            ini=str(ini), fim=str(fim), hoje=str(hoje), ini_br=br(ini), fim_br=br(fim),
            loja=loja, todas=todas, nomes=nomes(), qs=qs_de(ini, fim, loja),
            vendas=vendas, eventos_f=eventos_f, imp=imp, titulos=titulos, integ=integ,
            integ_ids={v["order_id"] for v in integ}, r=calc.resumo_vendas(vendas),
            rep=calc.conferir_repasse(eventos, liqs), ped=calc.conferir_pedidos(vendas, eventos),
            semanal=conferencia_semanal(eventos, liqs) if aba == "repasses" else [],
            cobertura=store.cobertura(), jobs=store.jobs(5), job=job, concs=concs,
            mes_padrao=primeiro_mes.strftime("%Y-%m"), limite=LIMITE_LINHAS,
            atualizar=5 if job else (15 if pendente else None),
            brl=brl, inteiro=inteiro, pct=pct, br=br, br_hora=br_hora, status_pt=STATUS_PT,
            pagamento_pt=pagamento_pt, repetidos=repetidos, repasse_fmt=repasse_fmt, data_curta=data_curta, sem_txt=sem_txt,
            resp=lambda r: ", ".join(RESP_PT.get(x.strip(), x.strip()) for x in r.split(",")),
            tipo_tit=TIPO_TIT, status_tit=STATUS_TIT, conc_pt=CONC_PT,
            tem_marca=os.path.exists(os.path.join(PASTA, "logo_marca.png")),
            tem_ifood=os.path.exists(os.path.join(PASTA, "logo_ifood.png")),
        )
        return render_template_string(PAGINA, **ctx)

    def _extrair(ini, fim, loja):
        qs = qs_de(ini, fim, loja)
        prob = ext.checar_periodo(ini, fim)
        if prob:
            return volta(qs, prob, "erro")
        lojas, _ = _lojas_filtro(loja)
        if not lojas:
            return volta(qs, "Nenhuma loja configurada: preencha IFOOD_FIN_LOJAS no .env (merchantId).", "erro")
        try:
            ext.iniciar_extracao(fabrica, lojas, ini, fim)
        except FinError as e:
            return volta(qs, f"✘ {e}", "erro")
        n = len(calc.semanas(ini, fim))
        return volta(qs, f"✔ Extração iniciada: {n} semana(s) × {len(lojas)} loja(s). Acompanhe a barra abaixo.", "ok")

    @bp.post("/financeiro/extrair")
    def extrair():
        try:
            ini = datetime.strptime(request.form["ini"], "%Y-%m-%d").date()
            fim = datetime.strptime(request.form["fim"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            return redirect("/financeiro?msg=" + quote("Escolha as duas datas.") + "&tipo=erro")
        return _extrair(ini, fim, request.form.get("loja", "todas"))

    @bp.post("/financeiro/recentes")
    def recentes():
        hoje = datetime.now(FUSO).date()
        return _extrair(hoje - timedelta(days=ext.DIAS_RECENTES), hoje, request.form.get("loja", "todas"))

    @bp.post("/financeiro/conciliacao/pedir")
    def conc_pedir():
        comp, loja = request.form.get("comp", ""), request.form.get("loja", "todas")
        qs = request.form.get("volta", "") + "&aba=conciliacao"
        lojas, _ = _lojas_filtro(loja)
        try:
            fin = fabrica()
            for m in lojas:
                ext.pedir_conciliacao(fin, m, comp)
                ext.acompanhar_conciliacao(fabrica, m, comp)
        except FinError as e:
            return volta(qs, f"✘ {e}", "erro")
        return volta(qs, f"✔ Arquivo de {comp} pedido para {len(lojas)} loja(s). A página acompanha sozinha.", "ok")

    @bp.post("/financeiro/conciliacao/verificar")
    def conc_verificar():
        comp, m = request.form.get("comp", ""), request.form.get("loja", "")
        qs = request.form.get("volta", "") + "&aba=conciliacao"
        try:
            st = ext.verificar_conciliacao(fabrica(), m, comp)
        except FinError as e:
            store.salvar_conciliacao(m, comp, status="erro", mensagem=str(e))
            return volta(qs, f"✘ {e}", "erro")
        texto = {"pronto": "✔ Arquivo pronto e baixado.", "processando": "O iFood ainda está gerando o arquivo.",
                 "expirado": "O pedido expirou; peça o arquivo de novo."}[st]
        return volta(qs, texto, "ok" if st == "pronto" else "")

    @bp.get("/financeiro/conciliacao/arquivo")
    def conc_arquivo():
        m, comp = request.args.get("loja", ""), request.args.get("comp", "")
        reg = store.consultar("SELECT arquivo FROM fin_conciliacao WHERE merchant_id=? AND competencia=?", (m, comp))
        if not reg or not reg[0]["arquivo"] or not os.path.exists(reg[0]["arquivo"]):
            abort(404)
        caminho = reg[0]["arquivo"]
        if request.args.get("formato") == "gz":
            return send_file(caminho, as_attachment=True, download_name=f"conciliacao_{comp}_{m[:8]}.csv.gz")
        with open(caminho, "rb") as f:
            bruto = f.read()
        if bruto[:2] == b"\x1f\x8b":
            bruto = gzip.decompress(bruto)
        if not bruto.startswith(b"\xef\xbb\xbf"):
            bruto = b"\xef\xbb\xbf" + bruto  # acentos certos no Excel
        return Response(bruto, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=conciliacao_{comp}_{m[:8]}.csv"})

    @bp.get("/financeiro/csv/<tipo>")
    def baixar_csv(tipo):
        ini, fim, _ = _datas(request.args)
        lojas, _ = _lojas_filtro(request.args.get("loja"))
        vendas, eventos, _, titulos, _, _ = _dados(lojas, ini, fim)
        linhas = {"vendas": vendas, "lancamentos": eventos, "repasses": titulos}.get(tipo)
        if linhas is None:
            abort(404)
        return Response(csv_excel(linhas), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={tipo}_{ini}_{fim}.csv"})

    app.register_blueprint(bp)


def csv_excel(linhas):
    """CSV para Excel brasileiro: ';', decimal com virgula, sem a coluna json (fica no banco)."""
    buf = io.StringIO()
    if linhas:
        cols = [c for c in linhas[0] if c != "json"]
        w = csv.writer(buf, delimiter=";")
        w.writerow(cols)
        for l in linhas:
            w.writerow([f"{l[c]:.2f}".replace(".", ",") if isinstance(l[c], float) else ("" if l[c] is None else l[c])
                        for c in cols])
    return ("﻿" + buf.getvalue()).encode("utf-8")
