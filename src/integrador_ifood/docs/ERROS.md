# Erros conhecidos: sintoma → causa → solução

Tabela validada em teste real. **Erro novo entra aqui depois que o Bruno
confirmar a solução** — antes disso é hipótese, e hipótese não entra na tabela.

A coluna **Tratado em** diz onde o código reconhece o sintoma. "Fora do código"
é sintoma de Portal ou de operação: a tabela vale, mas não há o que procurar nos
`.py`.

| Sintoma | Causa | Solução | Tratado em |
|---|---|---|---|
| Token 400 "Unsupported grant type" | app **distribuído** usado com `client_credentials` | usar app **centralizado** | `analytics.py:consulta`, `fin_client.py:_erro_token` (procuram o texto no corpo da resposta) |
| Token 403 "No permissions granted to client" | app de BI sem permissão de rede/loja | pedir autorização em Permissões no Portal e aprovar | `analytics.py:consulta`, `fin_client.py:_erro_token` |
| 403 na chamada com modelo rede | ID e modelo não combinam | `chain_id` com rede, identificador da loja com merchant | `analytics.py:_erro` (403), `fin_client.py:_erro` (403) |
| Portal: "não é possível ativar integrações não homologadas" | app ainda não homologado | concluir homologação; testar com app centralizado de teste | fora do código |
| Wizard do Analytics rejeita as métricas | somou **todas** as páginas | digitar a soma **só da página 1** | `homologar_analytics.py:resumo` (bloco "SÓ 1a página") |
| Settlement responde 400 | parâmetro `beginDate` errado | usar `beginCalculationDate` / `endCalculationDate` | `fin_client.py:liquidacoes` |
| Reconciliation 404, ou o mês é ignorado | rota ou campo errado | campo `competence` (`AAAA-MM`) — o código manda `competence` **e** `competencia` e confere o mês devolvido. Rota: *(a confirmar)*, o padrão do código é `reconciliation-on-demand` e `fin_testar.py` ainda procura a certa | `fin_client.py:pedir_conciliacao`, `fin_testar.py:CONC_CANDIDATAS` |
| 404 com `Route` no corpo | endereço da API errado | `python fin_testar.py` e ajustar `IFOOD_FIN_ROTA` | `fin_client.py:_erro` (404 + "Route") |
| `ConnectionError` na conciliação | **linha malformada no `.env`** | corrigir a linha (comentário com `#` sozinho na linha) | `fin_client.py:_limpar_rota` (hoje levanta `FinError` explicando a linha) |
| Painel mostra 404 de endpoint que já foi corrigido | Flask rodando o código velho | `Ctrl+C` e `python painel.py` | fora do código (`painel.py` sobe sem debug/reloader) |
| Repasse inflado no teste | ambiente de teste repete o mesmo exemplo em cada semana | é esperado; `sem_repeticao` trata | **`fin_web.py:sem_repeticao`** (não `fin_calculos.py`) |
| 401 depois de trocar o secret | valor colado com espaço ou aspas | repreencher a variável no `.env` | `analytics.py:_erro` (401), `fin_client.py:_erro` (401) |
| 429 | passou do limite | esperar e repetir | `fin_client.py:_retry_after` **respeita o `retry-after`** (teto de 120 s); `analytics.py` **não lê o `retry-after`**, só espera 2 s e 4 s |

## Como ler um erro novo

1. O iFood **respondeu** com erro (status HTTP) ou **não respondeu** (rede)?
   `analytics.py:consulta` separa os dois casos: `requests.HTTPError` é resposta
   com erro; `requests.RequestException` é falha de rede.
2. Se foi resposta, o corpo quase sempre diz o motivo em texto. Leia os primeiros
   300 caracteres antes de supor.
3. Antes de mexer no código, confira o `.env`: grupo certo, sem espaço, sem
   aspas, comentário em linha própria.
4. Se o painel é a tela que está errada, reinicie o Flask antes de investigar.

## Ao adicionar um erro

Acrescente a linha na tabela **e** traduza o erro em instrução no código, no
padrão de `analytics.py:consulta`: uma frase dizendo o que fazer, não o status
cru. Quem lê a mensagem é o Bruno, não um desenvolvedor.
