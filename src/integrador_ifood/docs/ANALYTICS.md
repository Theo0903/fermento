# Analytics (BI) — KPIs e homologação

KPIs **agregados de D-1** (o dia anterior fechado).

`POST /analytics/v1.0/merchants/{id}/orders/kpis`
ou `POST /analytics/v1.0/chain/{id}/orders/kpis` quando o modelo é rede.

O header `x-request-homologation: true` sai **só quando `IFoodAnalytics` é criado
com `homologacao=True`**, e hoje quem faz isso é apenas
`homologar_analytics.py`. Diferente do Financial, o Analytics **não** tem
variável no `.env` para isso: pelo painel (`IFoodAnalytics()`) o header não vai.

## Limites

| Limite | Valor |
|---|---|
| Tentativas | **1 a cada 4 horas** *(a confirmar: é regra do Portal; não existe no código)* |
| Período de consulta | 92 dias no painel (`MAX_DIAS` em `analytics_web.py`) |
| Requisições | 480 req/min (`LIMITE_POR_MIN`; o iFood permite 500) |
| Paginação | `page` de 1 a 1000, `size` de 1 a 10000; `consulta_todas` para na última página ou na 1000ª |

`validar_corpo` recusa, **antes de enviar**, o que o iFood devolveria como 400:
sem `filter.referenceDate`, `gte` junto com `gt`, data inicial depois da final,
nenhuma agregação, função de métrica/termo inválida ou repetida, campo de
`groupBy` fora da lista. Com o limite de uma tentativa por período, essa
conferência local é que evita queimar a janela.

O limite de 1 a cada 4 horas é o que mais machuca: errar o payload custa meio
dia. Por isso `homologar_analytics.py` gera o `homologacao_payload.json`
**antes** de chamar, para você conferir o que vai ser enviado.

## O wizard de homologação

Cinco passos. A regra que derrubou a primeira tentativa:

> **As métricas digitadas saem somente da PÁGINA 1** — a soma das linhas da
> página 1, não o total de todas as páginas.

Isso está validado. Somar todas as páginas faz o wizard rejeitar.

É o que `homologar_analytics.py:resumo` entrega: ele imprime a soma de todas as
páginas e, **quando `totalPages > 1`**, imprime também o bloco "SÓ 1a página",
que é o que vai no wizard. O `homologacao_payload.json` guardado é a resposta da
página 1.

## Permissão

App de BI centralizado **precisa de permissão no Portal**:
Meus aplicativos > (app de BI) > Permissões > Pedir autorização, informando o
identificador da loja (modelo merchant) ou da rede (modelo rede). Depois é
preciso **aprovar** o pedido no Portal do Parceiro.

Sem isso o token volta **403 "No permissions granted to client"** — e a mensagem
de erro de `analytics.py:consulta` já diz exatamente esse caminho (o texto no
código cita o app pelo nome: "Meus aplicativos > APP_BI > Permissões >
Pedir autorização").

## Cliente

`analytics.py`, classe `IFoodAnalytics`:

- `consulta(corpo)` faz **uma** chamada (uma página) e devolve o JSON ou levanta
  `AnalyticsError`; `consulta_todas(corpo)` segue as páginas e devolve
  `(linhas, última_resposta)`.
- Retry em 2 s e 4 s para status transitório (429, 500, 502, 503, 504) e para
  falha de rede. **Não lê o `retry-after`** — isso só o `fin_client.py` faz.
- Cada erro conhecido vira **instrução**: o 403 sem permissão explica o caminho
  no Portal; o "Unsupported grant type" avisa que o app é distribuído e manda
  rodar `autorizar_bi.py`. Mantenha esse padrão ao adicionar erro novo.
- Dois lugares tratam erro, e é bom não confundir: as mensagens de token recusado
  ficam no `except requests.HTTPError` de `consulta`; as do erro **da consulta**
  (400, 401, 403, 404, 429, 5xx) ficam em `_erro`. O 403 de `_erro` termina com
  "Não vou tentar de novo sozinho" — 403 não é retentado.
- `extrair(ini, fim)` roda o conjunto do painel: se uma consulta secundária
  falha, as outras seguem e o erro vai para a lista; se a principal (por
  `orderStatus`) falha, o erro sobe.

## Em aberto

- Significado exato do status "Em revisão" no Portal e prazo de liberação
  (**hipótese**: dias úteis até ~3 semanas; não validado).
- Em produção: trocar para as credenciais do app de produção e, no modelo rede,
  usar o `chain_id`.
