# Arquitetura do integrador

## Em uma frase

Quatro frentes da API do iFood (Order, Events, Analytics, Financial) entram por
**um único cliente HTTP** (`client.py`), com **três grupos de credencial** no
`.env` (Order+Events, BI, Financial), e saem em dois SQLite locais e num painel
Flask.

## O caminho dos dados

```
.env (grupos de credencial)
      |
   auth.py  IFoodAuth(prefixo=...)  -> token por grupo
      |
   client.py  IFoodClient.request   -> põe o token e repete UMA vez no 401
      |        (é o telefone das TRÊS frentes, não só do Order)
      |
      +-- orders.py / events.py --- Order + Events --> store.py ----> pedidos.db
      |     orders.py (ações no pedido, _acao)
      |     events.py (novos_eventos: polling + reconhecimento)
      |
      +-- analytics.py --- KPIs de D-1 ----> analytics_store.py --> pedidos.db
      |     IFoodAnalytics.consulta              (tabelas analytics_*)
      |
      +-- fin_client.py -- 3 APIs financeiras + conciliação -> fin_store.py
            fin_extrator.py (extração por semanas)            --> financeiro.db
            fin_calculos.py (conversão, classificação, conferências)
                                  |
                        painel.py (Flask)
                        criar_app(api=None, analytics=None, financeiro=None)
                            /           |            \
                    pedidos        analytics_web    fin_web
                                   (/analytics)     (/financeiro)
```

## Quem é quem

| Arquivo | Responsabilidade |
|---|---|
| `auth.py` | `IFoodAuth`: token por **grupo de credencial** (o prefixo escolhe `{prefixo}_CLIENT_ID`/`_CLIENT_SECRET`). A margem de renovação é **parâmetro**: padrão `0` (Order/Events só troca depois de vencer); Analytics e Financial pedem `margem_renovacao=600` |
| `client.py` | cliente HTTP **de todas as frentes** (`IFoodClient.request`): monta a URL, põe o token e, no 401, invalida e repete uma vez. `analytics.py` e `fin_client.py` também o usam |
| `orders.py` | ações no pedido (confirmar, preparar, pronto, despachar, cancelar). `_acao` concentra a chamada |
| `events.py` | `novos_eventos`: busca eventos novos e reconhece **todos** os recebidos; `eventos_vistos.json` evita reprocessar (lembra 5000 ids) |
| `store.py` | persistência de pedidos e eventos em `pedidos.db`. `_conn` é o ponto único de conexão |
| `analytics.py` | `IFoodAnalytics`: KPIs agregados. `validar_corpo` pega o 400 antes de enviar, retry 2 s/4 s, limite de 480 req/min, erro traduzido em instrução |
| `analytics_store.py` | persistência dos KPIs — **no mesmo `pedidos.db`**, em tabelas próprias (`analytics_extracao`, `analytics_linhas`). Reextrair o mesmo período substitui |
| `analytics_web.py` | tela `/analytics` (`registrar(app, get_analytics)`), máximo de 92 dias; `csv_bytes` exporta no padrão do Excel brasileiro (`;`, decimal com vírgula, BOM) |
| `fin_client.py` | cliente financeiro. `chamar` centraliza a requisição; `FinError` é o erro da frente; `_limpar_rota` conserta linha torta do `.env` |
| `fin_store.py` | `financeiro.db`. `conn` é o ponto único de conexão (9 pontos de chamada no próprio arquivo) |
| `fin_calculos.py` | conversão da resposta em linhas, classificação do lançamento, resumo de vendas e **conferências** (tolerância 0,01%), leitura do `.csv.gz`, `competencia_valida`. **Não** é aqui que mora a deduplicação |
| `fin_extrator.py` | extração por semanas, CLI `--ini/--fim/--recentes/--loja`, thread com progresso e controle de job |
| `fin_web.py` | tela `/financeiro`: Resumo, Vendas, Lançamentos, Repasses, Arquivo de conciliação; exporta CSV. **`sem_repeticao` (deduplicação entre semanas) está aqui** |
| `fin_testar.py` | descobre rotas (`CANDIDATAS`, `CONC_CANDIDATAS`) e testa as APIs financeiras |
| `painel.py` | Flask. `criar_app(api=None, analytics=None, financeiro=None)` monta as três áreas; sobe em `127.0.0.1:5000`, sem debug nem reload |
| `homologar_analytics.py` | gera `homologacao_payload.json` (a resposta da **página 1**) e imprime os números do wizard |
| `auth_distribuido.py`, `autorizar_bi.py` | fluxo distribuído — **referência, fora de uso**. Atenção: `analytics.py` ainda importa `auth_distribuido` e passa a usá-lo se `IFOOD_BI_TIPO=distribuido` **ou** se existir `bi_tokens.json` com refresh token (`tokens_existem()`) |

## Pontos de concentração

Mudança nestes símbolos tem alcance grande; rode
`trail callers <símbolo> --depth 2` antes:

Os cinco símbolos existem com esses nomes e nesses arquivos (conferido). Os
números abaixo saíram do grafo de chamadas; o que dá para contar no código está
entre parênteses:

- `conn` (`fin_store.py`) — 9 referências (9 pontos de chamada, todos no próprio
  `fin_store.py`; quem é de fora passa por `consultar`)
- `FinError` (`fin_client.py`) — 8 *(a confirmar: o nome aparece em
  `fin_client.py`, `fin_extrator.py`, `fin_web.py` e `fin_testar.py`, bem mais de
  8 vezes somando levantamento e captura)*
- `_conn` (`store.py`) — 6 (6 pontos de chamada, confere). Atenção: existe um
  segundo `_conn`, em `analytics_store.py`, que abre o **mesmo** `pedidos.db` e
  liga `PRAGMA foreign_keys = ON` (é o que faz o `ON DELETE CASCADE` das tabelas
  de Analytics valer). São funções diferentes com o mesmo nome — ao buscar
  chamadores, repare no arquivo.
- `AnalyticsError` (`analytics.py`) — 5 *(a confirmar: aparece em
  `analytics.py`, `analytics_web.py` e `homologar_analytics.py`)*
- `chamar` (`fin_client.py`) — 5 (os 5 métodos de API: `vendas`, `eventos`,
  `liquidacoes`, `pedir_conciliacao`, `status_conciliacao`)

## Onde os dados param

| Destino | O que guarda | Vai para o Git? |
|---|---|---|
| `pedidos.db` | pedidos e eventos **e os KPIs do Analytics** (tabelas `analytics_extracao` e `analytics_linhas`) | não |
| `financeiro.db` | vendas, lançamentos, liquidações, títulos, conciliação e jobs | não |
| `eventos_vistos.json` | ids já processados | não |
| `homologacao_payload.json` | payloads reais da homologação | não |
| `bi_autorizacao_pendente.json` | estado de autorização do BI (traz o `verifier`) | não |
| `bi_tokens.json` | access + refresh token do fluxo distribuído | não |
| `conciliacao/<loja>_<AAAA-MM>.csv.gz` | arquivo de conciliação do iFood, dado financeiro real | não |

O caminho de cada banco pode ser trocado por variável de ambiente: `IFOOD_DB`
(`store.py`) e `IFOOD_FIN_DB` (`fin_store.py`); o arquivo de token, por
`IFOOD_BI_TOKENS`. Nenhuma das três está no `env.example`.

**O que o integrador escreve é SQLite local.** O caminho para o SQL Server
corporativo (o nome do banco fica na configuração do Téo, fora deste repo) é
por tabela de *staging* desenhada por ele —
`stg_ifood_financeiro` e `stg_ifood_analytics`. Não criar tabela no banco
corporativo sem ele.
