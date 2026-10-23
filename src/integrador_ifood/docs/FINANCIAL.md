# Financial — as quatro APIs

Base: `/financial/v3.0/merchants/{merchantId}/{recurso}`
(`ROTA_PADRAO` em `fin_client.py`, com o comentário "confirmado no teste de
22/10/2026"; `IFOOD_FIN_ROTA` sobrepõe).
Situação: Sales, Financial Events e Settlement têm método próprio e tratamento de
erro em `fin_client.py`. A quarta (Reconciliation) **ainda está com o endereço em
descoberta** — veja o item 4. A homologação do Financial é por **chamado**
(Portal > Suporte > Chamados > Homologação, com questionário e vídeos dos
cenários) e ainda não foi aberta *(a confirmar: nada disso aparece no código)*.

## 1. Sales

`GET sales` — `fin_client.py:vendas`

| Parâmetro | Observação |
|---|---|
| `beginSalesDate`, `endSalesDate` | janela máxima de **90 dias**, contados inclusive (`_checar_janela`: `(fim-ini).days + 1 > 90` recusa) |
| `page` | começa em **0** |

- **Não manda `size`**: o tamanho é o padrão da API.
- A lista vem na chave `sales` da resposta.
- Para quando o lote volta vazio ou quando `pageCount`/`totalPages` acaba; teto
  de segurança de 1000 páginas.

## 2. Financial Events

`GET financial-events` — `fin_client.py:eventos`

| Parâmetro | Observação |
|---|---|
| `beginDate`, `endDate` | janela máxima de **33 dias**, também inclusive |
| `page` | começa em **1** (diferente de Sales) |
| `size` | 100 |
| `hasNextPage` | na resposta, diz se continua |

Atenção à inconsistência real da API: em Sales a página começa em 0, aqui começa
em 1. Já foi fonte de página faltando.

O **nome da lista** de lançamentos não está na documentação: `lista_da_resposta`
aceita `financialEvents`, `events`, `data`, `content`, `items` e, em último caso,
qualquer lista de objetos da resposta.

## 3. Settlement

`GET settlements`, com `beginCalculationDate` / `endCalculationDate`
— `fin_client.py:liquidacoes`.

- Usar `beginDate` aqui dá **400**. Os nomes são outros.
- **404 significa vazio** (devolve `{"balance": 0, "settlements": [], "vazio": True}`),
  não erro — `aceitos=(200, 404)`.
- **Exceção:** 404 com `Route` no corpo é endereço errado, não vazio: aí vira
  erro mandando rodar `python fin_testar.py`.
- Vazio na loja de teste.

## 4. Reconciliation on-demand

`POST` no endereço montado por `fin_client.py:_rota_conc()`: na falta de
`IFOOD_FIN_ROTA_CONC`, é a rota base com o recurso
**`reconciliation-on-demand`** (com hífen), isto é
`/financial/v3.0/merchants/{merchantId}/reconciliation-on-demand`.

*(a confirmar: o endereço certo. O comentário de `fin_testar.py` diz que "a
conciliação respondeu 404 dentro da rota das outras APIs", e `CONC_CANDIDATAS`
ainda lista 11 endereços possíveis — entre eles `/reconciliation/on-demand`, que
estava escrito aqui como se fosse o confirmado.)*

- O corpo leva `competence` **e** `competencia`, os dois com o mesmo `AAAA-MM`,
  porque o nome do campo não está firme; depois o código confere o mês devolvido
  e levanta erro se o iFood registrou outro mês.
- Aceita **200, 201, 202 e 409**; o `requestId` sai de `requestId` ou `id`.
  `GET .../{requestId}` acompanha (202 = ainda processando).
- **409** reaproveita o `lastRequestId` — não é falha, é pedido já em andamento.
  Sem `lastRequestId` nem `requestId` na resposta, a mensagem manda tentar de
  novo em 6 horas.
- O resultado é um **`.csv.gz`**, separador `;`, com a coluna
  `impacto_no_repasse` (`sim()` aceita `SIM`, `TRUE`, `1`, `S`, `YES`).
- Só **meses fechados**, até 24 meses atrás (`fin_calculos.competencia_valida`).
- O pedido **vale 24 h**; o acompanhamento automático consulta a cada 30 s nas 5
  primeiras vezes, depois a cada 60 s, por no máximo 30 min
  (`fin_extrator.acompanhar_conciliacao`).

## Limites e retentativa

- Teto de 1000 req/min; o integrador usa **900** por margem
  (`LIMITE_POR_MIN` em `fin_client.py`).
- **429**: respeita o `retry-after` do header (ou `retryAfter` do corpo),
  **com teto de 120 s**; na falta dele, usa a espera crescente.
- **5xx e queda de rede**: 4 tentativas — imediata, 2 s, 4 s, 8 s.

## Regras de número

- **Repasse** = soma dos lançamentos com `hasTransferImpact` verdadeiro
  (`fin_calculos.conferir_repasse`), comparada com o `balance` do Settlement.
  Tolerância de **0,01%** — na prática `max(0,01; |referência| × 0,0001)`, para
  não acusar diferença de centavo em valor pequeno.
- **Taxa efetiva** = 1 − repasse ÷ GMV — *(a confirmar: não existe no código.
  O que `fin_calculos.resumo_vendas` calcula é `pct_liquido` = líquido
  (`saleBalance`) ÷ bruto, mais `margem_com_promo` / `margem_sem_promo` e
  `pct_promo_loja`.)*
- Lançamento idêntico que aparece em semanas diferentes conta **uma vez**
  (`sem_repeticao` — está em **`fin_web.py`**, não em `fin_calculos.py`). Dentro
  da mesma semana nada é descartado.
- Por pedido, `conferir_pedidos` compara o `saleBalance` da venda com a soma dos
  lançamentos com impacto daquele pedido; `integridade_vendas` acusa venda em que
  a soma dos lançamentos não bate com o líquido informado.

## Extração

`fin_extrator.py`, por **semanas** (segunda a domingo), até 366 dias por
execução (`MAX_DIAS_EXTRACAO`), em thread de segundo plano com progresso. As
gravações são **idempotentes**: venda e título por chave (loja + pedido / loja +
título); lançamento e liquidação **substituem a semana** inteira; conciliação
substitui o mês. Rodar duas vezes a mesma semana não duplica.

```
python fin_extrator.py --recentes
python fin_extrator.py --ini 2026-01-01 --fim 2026-03-31
python fin_extrator.py --ini 2026-01-01 --fim 2026-03-31 --loja <merchantId>
```

`--recentes` é **liga/desliga** e vale sempre **35 dias** (`DIAS_RECENTES`): não
aceita número depois (`--recentes 7` quebra o comando). `--loja` sozinho também
não roda — precisa de `--ini`+`--fim` ou de `--recentes`.

Erro de credencial ou de endereço (401, "Endereço da API", "login") para a
extração toda; 403 pula **só aquela loja** e segue as outras.

Já testada extração de 44 semanas *(a confirmar: não há como ver isso no
código)*.

## Em aberto

- Quanto histórico o iFood guarda (só dá para saber em produção).
- Settlement com dados reais.
- Modelo de dados para o Téo levar de SQLite ao SQL Server.
