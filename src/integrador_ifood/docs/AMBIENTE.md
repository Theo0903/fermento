# Ambiente e credenciais

## A ideia dos grupos

O integrador fala com **três aplicações diferentes** do iFood: uma de
Order+Events, uma de Analytics (BI) e uma de Financial. Cada uma tem credencial
própria. Por isso o `.env` é dividido em **grupos**, e `IFoodAuth` recebe o
prefixo do grupo e cuida do token daquele grupo.

É como três chaves diferentes no mesmo chaveiro: a porta da frente, a do depósito
e a do cofre. Trocar uma não troca as outras.

## Grupos do `.env`

| Grupo | Variáveis |
|---|---|
| base | `IFOOD_BASE_URL` (no `env.example` ela vem dentro do bloco Order+Events; `auth.py` tem o padrão `https://merchant-api.ifood.com.br`) |
| Order + Events | `IFOOD_CLIENT_ID`, `IFOOD_CLIENT_SECRET` |
| Analytics (BI) | `IFOOD_BI_CLIENT_ID`, `IFOOD_BI_CLIENT_SECRET`, `IFOOD_BI_TIPO`, `IFOOD_BI_MODELO`, `IFOOD_BI_ALVO_ID` |
| Financial | `IFOOD_FIN_CLIENT_ID`, `IFOOD_FIN_CLIENT_SECRET`, `IFOOD_FIN_LOJAS`, `IFOOD_FIN_HOMOLOGACAO`, `IFOOD_FIN_ROTA`, `IFOOD_FIN_ROTA_CONC` |

Só o **prefixo** é que é automático: `IFoodAuth(prefixo="IFOOD_FIN")` monta
`IFOOD_FIN_CLIENT_ID` e `IFOOD_FIN_CLIENT_SECRET`, e nada mais. As outras
variáveis do grupo (`_TIPO`, `_MODELO`, `_ALVO_ID`, `_LOJAS`, `_HOMOLOGACAO`,
`_ROTA`, `_ROTA_CONC`) são lidas uma a uma pelo módulo de cada frente. Sem
`CLIENT_ID`/`CLIENT_SECRET` do grupo, `IFoodAuth` levanta
`RuntimeError: Preencha ... no arquivo .env`.

`env.example` lista essas 14, **com valor vazio** — exceto `IFOOD_BASE_URL`,
`IFOOD_BI_TIPO=centralizado`, `IFOOD_BI_MODELO=merchant` e
`IFOOD_FIN_HOMOLOGACAO=1`, que vêm preenchidos porque são escolha de ambiente,
não segredo. Copie para `.env` e preencha só na sua máquina.

### Três variáveis que o código lê e o `env.example` não mostra

| Variável | Quem lê | Para quê |
|---|---|---|
| `IFOOD_DB` | `store.py` | troca o caminho do `pedidos.db` |
| `IFOOD_FIN_DB` | `fin_store.py` | troca o caminho do `financeiro.db` |
| `IFOOD_BI_TOKENS` | `auth_distribuido.py` | troca o caminho do `bi_tokens.json` |

Nenhuma é obrigatória; sem elas, os arquivos ficam na pasta do projeto.

### Armadilha corrigida em 23/10/2026
O `env.example` trazia `IFOOD_FIN_ROTA=/financeiro` e
`IFOOD_FIN_ROTA_CONC=/financeiro/conciliacao` — que são os caminhos do **painel
Flask**, não da API. Quem copiasse o modelo mandaria a chamada do Financial para
`https://merchant-api.ifood.com.br/financeiro`, e `_limpar_rota` não pega o erro
(começa com `/`, então "parece" válido). As duas agora vão **vazias** no modelo:
vazio faz o `fin_client.py` usar `ROTA_PADRAO`.

### Armadilha já vivida
Linha comentada com `#` deve conter **só** o comentário. Linha malformada no
`.env` já causou `ConnectionError` na conciliação, e o erro não aponta para o
`.env` — aponta para a rede. `_limpar_rota` ajuda, mas não conserta linha torta.

## Centralizado × distribuído

- **Centralizado**: usa `client_credentials` em
  `POST /authentication/v1.0/oauth/token`. É o que o integrador usa.
- **Distribuído**: exige fluxo *authorization-code* e **não pode** ser ativado em
  loja real antes de homologado. Homologação só funciona com app centralizado.
- No fluxo distribuído há dois códigos diferentes, e eles já foram confundidos:
  o `userCode` (que se mostra ao lojista) e o código de autorização.
- `auth_distribuido.py` e `autorizar_bi.py` existem para esse fluxo e estão
  **fora de uso** — mas não estão desligados: `analytics.py` cai no fluxo
  distribuído se `IFOOD_BI_TIPO` começar com "distrib" **ou**, quando a variável
  está vazia, se existir um `bi_tokens.json` com refresh token. Deixar
  `IFOOD_BI_TIPO=centralizado` preenchido é o que garante o caminho certo.

## Modelo: merchant × rede

O ID e o modelo **precisam combinar**:

| Modelo | ID que vai na chamada |
|---|---|
| `merchant` | o identificador da loja |
| `rede` | o identificador da rede (`chain_id`) |

Não combinar dá **403**. Loja de teste única (não é rede) → `modelo=merchant`.

## Teste × produção

| | Teste | Produção |
|---|---|---|
| `IFOOD_FIN_HOMOLOGACAO` | `1` (qualquer valor fora de `0`/`nao`/`não`/`false` conta como teste) | `0` |
| Header do Financial | `x-request-homologation: true` em toda chamada | sem o header |
| Header do Analytics | só sai com `homologacao=True` no código — hoje apenas `homologar_analytics.py`; **não há variável de `.env` para isso** | — |
| Credenciais | app de teste nas três frentes | apps de produção |
| Lojas | loja de teste | lojas reais |

**O ambiente de teste do Financial e do Analytics devolve sempre o mesmo
exemplo**, qualquer que seja o período pedido. Então repasse "inflado" no teste
não é bug: é o mesmo lançamento repetido em cada semana, e é o que a
deduplicação (`sem_repeticao`, em **`fin_web.py`**) trata. Em produção isso não
deve acontecer.

## Troca de secret

Quando um secret for regenerado no Portal:
1. Trocar o valor **só** no `.env`.
2. Reiniciar o painel (`Ctrl+C`, `python painel.py`).
3. Se der 401, o valor provavelmente foi colado com espaço ou aspas sobrando.

O cliente financeiro recarrega o `.env` a cada criação (`FinClient` chama
`load_dotenv(..., override=True)` quando não recebe um `client` pronto); as
outras frentes leem o `.env` na importação do módulo, então pegam o valor novo
só no reinício.

**Nunca** colar secret em chat, em log, em `env.example` ou neste documento.
