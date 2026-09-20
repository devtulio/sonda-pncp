# Manual — Sonda PNCP

Referência completa. Para instalação e visão geral, ver
[README.md](README.md). Tudo que está documentado aqui como formato ou
comportamento observável é **contrato** — só muda em major, com
depreciação antes; o que não está (módulos internos, texto de
notificações, layout visual do HTML, valores default dos limiares) não é.
Regras completas em [RELEASING.md](RELEASING.md).

## Índice

- [Como funciona](#como-funciona)
- [Alvos](#alvos)
- [Órgão e compra de teste](#órgão-e-compra-de-teste)
- [Classificação de cada medição](#classificação-de-cada-medição)
- [Estados, cores e modos](#estados-cores-e-modos)
- [Bandeja e linha de comando](#bandeja-e-linha-de-comando)
- [Configuração (`config.json`)](#configuração-configjson)
- [Log](#log)
- [Relatório](#relatório)
- [Notificações](#notificações)
- [Como ler os números](#como-ler-os-números)
- [Limitações](#limitações)
- [Armadilhas e solução de problemas](#armadilhas-e-solução-de-problemas)
- [Testes](#testes)

---

## Como funciona

Um laço em segundo plano executa uma **rodada** a cada 5 minutos (60 s
durante incidente). Cada rodada mede, em sequência e com 1,5 s entre um
alvo e outro, os 2 controles de internet e depois os 6 alvos do PNCP.

Cada medição é um `curl.exe` (do Windows) com `-w "%{json}"`, que devolve
de graça o IP, os tempos e o código de erro de rede. Os tempos
`dns_ms`, `tcp_ms`, `tls_ms`, `ttfb_ms` e `total_ms` são **acumulados**
do curl (cada um conta desde o início da requisição), não por fase. O
timeout é de 10 s para conectar e 30 s no total.

O User-Agent leva um token de navegador mais `SondaPNCP/1.0`: o WAF do
portal (`/app/`) reseta a conexão para o UA padrão do curl.

Se uma medição de alvo do PNCP falha, a sonda **repete uma vez** após 10 s.
Se a 2ª passa, o alvo é um **blip** (a rodada fica `degradado`); se a 2ª
também falha, é uma **falha confirmada**. As duas tentativas ficam no log
(campo `tentativa`).

Se os **2 controles falham**, a rodada é `sem_rede`: os alvos do PNCP
nem são medidos (não contam contra o PNCP) e a próxima rodada vem em 60 s.

## Alvos

Definidos em `config.json` (chave `alvos`). Os padrões:

| `id` | Tipo | O que consulta | Valida |
|---|---|---|---|
| `ctrl_google` | controle | `https://www.google.com/generate_204` | status HTTP |
| `ctrl_cloudflare` | controle | `https://www.cloudflare.com/cdn-cgi/trace` | status HTTP |
| `portal` | portal | `https://pncp.gov.br/app/` | HTML (`<html`, > 2000 bytes) |
| `api_contratacoes` | api | `/api/consulta/v1/contratacoes/publicacao` (ontem→hoje, modalidade 6) | `{"data": [...]}` |
| `api_contratos` | api | `/api/consulta/v1/contratos/atualizacao` (30 dias, um CNPJ) | `{"data": [...]}` |
| `api_atas` | api | `/api/consulta/v1/atas/atualizacao` (30 dias, um CNPJ) | `{"data": [...]}` |
| `api_pca` | api | `/api/consulta/v1/pca/atualizacao` (início do ano→hoje, um CNPJ) | `{"data": [...]}` |
| `api_itens` | api | `/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens` (1 compra fixa) | lista JSON |

Todas as consultas de API pedem `pagina=1&tamanhoPagina=10` — a sonda
mede a **resposta**, não coleta volume. Quatro alvos (contratos, atas, PCA e
itens) precisam apontar para um órgão e, no caso dos itens, para uma compra: a
sonda usa um **órgão de teste** e uma **compra de teste**, que você troca em
uma linha do `config.json` — ver [Órgão e compra de teste](#órgão-e-compra-de-teste).

Marcadores aceitos nas URLs:
- datas, no formato `AAAAMMDD`, calculadas na hora da medição: `{hoje}`,
  `{ontem}`, `{d7}`, `{d30}`, `{ini_ano}`;
- `{cnpj}`, `{ano_compra}` e `{seq_compra}`: valem `cnpj_teste` e
  `compra_teste.ano`/`compra_teste.sequencial` do `config.json`.

Uma URL sem marcadores (por exemplo, com o CNPJ escrito direto) continua
funcionando: os marcadores são só uma comodidade.

### Órgão e compra de teste

Os alvos padrão `api_contratos`, `api_atas`, `api_pca` e `api_itens` usam os
marcadores acima. Para medir outro órgão, edite duas chaves do `config.json`
e reinicie a sonda:

```json
"cnpj_teste": "00.000.000/0001-00",
"compra_teste": {"ano": 2026, "sequencial": 123}
```

- `cnpj_teste`: aceita com ou sem pontuação (a sonda guarda só os 14 dígitos)
  e recusa outro tamanho na partida, com uma mensagem que diz o problema em
  vez de deixar os alvos falharem com 404.
- `compra_teste`: uma compra desse órgão que **exista no PNCP** e não vá
  sumir (prefira uma antiga e homologada). Para achar uma, consulte as
  contratações do órgão e leia `anoCompra` e `sequencialCompra` de qualquer
  item da resposta:

  ```bat
  curl.exe -s -A "Mozilla/5.0" "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao?dataInicial=20260901&dataFinal=20260920&codigoModalidadeContratacao=6&cnpj=SEU_CNPJ&pagina=1&tamanhoPagina=10"
  ```

  Se vier vazio, tente outra modalidade (`codigoModalidadeContratacao`) ou
  um período maior. Se a compra escolhida sumir depois, a sonda avisa com
  [registro ausente](#registro-ausente) e não conta como queda.

**`config.json` antigo:** quem já tem o arquivo e nunca mexeu nos alvos ganha
as duas chaves com os valores padrão, mas mantém as URLs antigas (a chave
`alvos` do arquivo substitui a lista padrão). Para passar a usar os
marcadores, apague a chave `alvos` do `config.json` — a sonda volta a usar a
lista padrão, com as mesmas URLs — e reinicie.

Campos de cada alvo: `id`, `nome`, `tipo` (`controle` / `portal` / `api`),
`validar` (`status`, `html`, `json_data`, `json_lista`), `url`,
`limiar_lento_ms`, `accept` (opcional) e `ausencia_404` (opcional, ver
[registro ausente](#registro-ausente)).

## Classificação de cada medição

Campo `resultado` de cada registro `sonda`:

| Resultado | Quando | Conta como |
|---|---|---|
| `ok` | resposta 2xx válida, dentro do limiar | disponível |
| `lento` | resposta 2xx válida, `total_ms` acima de `limiar_lento_ms` | disponível (e "lenta") |
| `timeout` | curl estourou o tempo (`timeout_conexao` ou `timeout_resposta` no `detalhe`) | **falha** |
| `erro_rede` | DNS, conexão recusada/derrubada, TLS, certificado, resposta vazia ou parcial | **falha** |
| `erro_http` | HTTP fora de 2xx (exceto 429 e registro ausente) | **falha** |
| `corpo_invalido` | 2xx, mas o corpo não bate com o `validar` do alvo | **falha** |
| `bloqueio_429` | HTTP 429 | limitação — **fora** da disponibilidade |
| `registro_ausente` | HTTP 404 com a mensagem configurada em `ausencia_404` | problema do teste — **fora** da disponibilidade |

Controles só validam o status HTTP (não o corpo).

**429 não é queda.** É o WAF limitando a taxa. Nunca conta como falha,
nunca dispara repetição e nunca acelera a sondagem; deixa a rodada
`degradado`.

### Registro ausente

O alvo `api_itens` consulta uma compra fixa. Se ela sumir do PNCP, o
portal responde 404 com "Contratação não cadastrada" — que **não** é
queda. Um alvo com `ausencia_404` (texto procurado no corpo do 404) passa
a classificar esse caso como `registro_ausente`: a rodada fica
`degradado`, o alvo não repete, não entra na disponibilidade, e a sonda
grava o evento `registro_ausente` e notifica **uma vez** (rearma quando o
registro voltar). A solução é trocar a URL do alvo por outra compra
existente. Um 404 sem essa mensagem, ou em alvo sem `ausencia_404`,
continua sendo `erro_http`.

## Estados, cores e modos

Estado de cada **rodada**:

| Estado | Quando |
|---|---|
| `ok` | todos os alvos ok |
| `degradado` | nenhuma falha confirmada, mas houve blip, resposta lenta, 429 ou registro ausente |
| `falha` | pelo menos um alvo do PNCP falhou também na 2ª tentativa |
| `sem_rede` | os 2 controles falharam |

Cor do ícone: `ok` → verde; `degradado` → amarelo; `sem_rede` → cinza;
`falha` → amarelo na 1ª rodada e **vermelho** a partir de 2 rodadas
seguidas em falha.

**Modo incidente:** uma rodada em `falha` liga o modo incidente (rodadas
a cada 60 s); sai depois de 3 rodadas seguidas sem falha. `sem_rede`
também reverifica a cada 60 s, sem ligar o modo.

Disponibilidade das últimas 24 h (mostrada na dica do ícone) =
rodadas que não foram `falha` ÷ rodadas `ok`/`degradado`/`falha`
(`sem_rede` não entra); reconstruída do log ao iniciar.

## Bandeja e linha de comando

**Menu do ícone:** a 1ª linha mostra o estado e a hora da última rodada
(desabilitada, só informativa). Depois: *Verificar agora* (dispara uma
rodada; desabilitado se pausada), *Abrir pasta de logs*, *Gerar relatório
(últimos 7 dias)* (gera e abre a pasta), *Pausar sonda* (marcável),
*Iniciar com o Windows* (marcável), *Encerrar*.

**Instância única:** um socket em `127.0.0.1` (porta `porta_instancia`,
padrão 48650, `SO_EXCLUSIVEADDRUSE`) impede duas sondas. Uma 2ª execução
sai sem fazer nada.

| Comando | O que faz | Saída |
|---|---|---|
| `pythonw sonda_pncp.pyw` | sobe na bandeja | 0 |
| `sonda_pncp.pyw --encerrar` | pede à instância em execução para encerrar | 0 se achou a instância, 1 se não |
| `python sonda_pncp.pyw --uma-rodada` | uma rodada, imprime o registro da rodada (JSON) | 0 |
| `python sonda_pncp.pyw --relatorio [N]` | relatório dos últimos N dias (padrão 7), imprime a pasta | 0 |

`--uma-rodada` **grava no log** como qualquer rodada. Use o `python` do
`.venv` (`pythonw` não tem console: erros vão para `logs/sonda-erros.log`).

**Início automático:** atalho `Sonda PNCP.lnk` em
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`, criado na
primeira execução (se `iniciar_com_windows` for `true`) e alternável no
menu. Para remover: desmarcar no menu ou apagar o atalho.

**Encerrar** (menu ou `--encerrar`) grava o evento `sonda_encerrada`.
Se você encerra no meio de uma rodada, a rodada é abandonada **sem**
gravar o resumo dela (as medições já feitas ficam no log).

## Configuração (`config.json`)

Criado na primeira execução com os padrões. Chaves ausentes usam o padrão;
**a chave `alvos`, se presente, substitui a lista inteira** — um alvo novo
ou um campo novo dos padrões da versão (como `ausencia_404`) não chega a
um `config.json` que já existe: edite o arquivo.

| Chave | Padrão | O que é |
|---|---|---|
| `intervalo_normal_s` | `300` | Intervalo entre rodadas. |
| `intervalo_incidente_s` | `60` | Intervalo em modo incidente e em `sem_rede`. |
| `espera_entre_alvos_s` | `1.5` | Pausa entre um alvo e outro dentro da rodada. |
| `timeout_conexao_s` | `10` | Timeout de conexão do curl. |
| `timeout_total_s` | `30` | Timeout total do curl (acima disto é `timeout`). |
| `retry_apos_falha_s` | `10` | Espera antes da 2ª tentativa de um alvo que falhou. |
| `rodadas_sem_falha_para_sair_incidente` | `3` | Rodadas limpas para sair do modo incidente. |
| `falhas_seguidas_para_vermelho` | `2` | Rodadas em falha seguidas para o ícone ficar vermelho. |
| `limite_lacuna_x_intervalo` | `2.5` | Uma pausa maior que isto × intervalo (+ timeout) é registrada como lacuna. |
| `retencao_compactar_dias` | `30` | Logs mais velhos viram `.gz` (nada é apagado). |
| `notificar` | `true` | Liga as notificações do Windows. |
| `iniciar_com_windows` | `true` | Cria o atalho de início automático na 1ª execução. |
| `porta_instancia` | `48650` | Porta loopback da instância única. |
| `user_agent` | (navegador + `SondaPNCP/1.0`) | UA enviado em toda medição. |
| `cnpj_teste` | `83102277000152` | Órgão usado em `{cnpj}` nas URLs de contratos, atas, PCA e itens. |
| `compra_teste` | `{"ano": 2026, "sequencial": 495}` | Compra usada em `{ano_compra}`/`{seq_compra}` (alvo `api_itens`). |
| `alvos` | (tabela acima) | Lista de alvos. |

Os limiares `limiar_lento_ms` iniciais são **folgados de propósito**
(4 s portal/controles, 5–20 s por API): a linha de base era desconhecida.
Recalibre pelo p95 do relatório depois de ~7 dias de dados.

Mudanças no `config.json` só valem depois de reiniciar a sonda.

## Log

`logs/sonda-AAAA-MM-DD.jsonl` — um por **dia local**, uma linha JSON por
registro, aberto e fechado a cada escrita (uma queda de energia perde no
máximo uma linha; linha truncada é ignorada na leitura). Logs com mais de
`retencao_compactar_dias` viram `.jsonl.gz`. Erros internos da sonda vão
também para `logs/sonda-erros.log`.

Toda linha tem `tipo` (`sonda`, `rodada` ou `evento`), `ts_local` (com
fuso) e `ts_utc` (`...Z`). O nome do arquivo segue a data de `ts_local`.

### `tipo: sonda` — uma medição

`rodada` (id da rodada), `alvo`, `categoria`, `url`, `tentativa` (1 ou 2),
`resultado`, `detalhe`, `http`, `http_versao`, `ip`, `curl_exit`,
`curl_erro`, `dns_ms`, `tcp_ms`, `tls_ms`, `ttfb_ms`, `total_ms`, `bytes`,
`cabecalhos` (date, server, via, retry-after, content-type,
content-length) e `desvio_relogio_s` (estimativa: relógio local menos o
`Date` do servidor, ±latência).

Em **falha ou 429** ainda: `corpo_trecho` (400 primeiros caracteres),
`cabecalhos_completos` (sem cookies/authorization) e `pncp_ts_erro` (o
`timestamp` que o próprio PNCP escreve no corpo do erro, quando existe).
Em **sucesso**: `corpo_sha256` (16 primeiros caracteres do hash).

### `tipo: rodada` — resumo de uma rodada

`rodada`, `estado`, `cor`, `rede_local_ok`, `alvos` (resultado final por
alvo, com `blip` quando a 2ª tentativa passou), `falhas`, `blips`,
`lentos`, `bloqueios_429`, `registros_ausentes`, `modo` (`normal` ou
`incidente`), `streak_falha`, `duracao_ms`, `proxima_em_s`.

### `tipo: evento`

| `evento` | Quando | Campos extras |
|---|---|---|
| `sonda_iniciada` | ao subir | `versao`, `host`, `python`, `intervalo_normal_s`, `ultimo_registro_anterior`, `gap_desde_anterior_s`, `encerramento_anterior_limpo` |
| `sonda_encerrada` | ao encerrar | `motivo` (`menu`, `comando_encerrar`, `saida_inesperada`), `rodadas` |
| `mudanca_estado` | a cor do ícone mudou | `de`, `para`, `estado_rodada`, `falhas` |
| `mudanca_ip` | um alvo passou a responder de outro IP | `alvo`, `de`, `para` |
| `retomada_apos_lacuna` | intervalo entre rodadas muito maior que o esperado | `lacuna_s`, `desde`, `motivo` (`pausa_manual` ou `suspensao_ou_indisponibilidade`) |
| `pausada` / `retomada_manual` | menu *Pausar sonda* | — |
| `registro_ausente` | primeira ocorrência de [registro ausente](#registro-ausente) | `alvo`, `url` |
| `erro_interno` | exceção dentro da sonda | `erro`, `traceback` |

`encerramento_anterior_limpo: false` no início significa que o último
registro antes dele não foi um `sonda_encerrada` — a sonda caiu, o PC
desligou ou foi suspenso.

## Relatório

`--relatorio N` ou o menu do ícone. Lê os logs dos últimos N dias e grava
em `relatorios/relatorio-AAAAMMDD/` (data da geração). É **uma pasta por
dia**: gerar de novo no mesmo dia sobrescreve os arquivos — como o
relatório é sempre derivado dos logs, encerrar e reabrir a sonda não
perde nem fragmenta nada. Se um arquivo estiver aberto no Excel o Windows
bloqueia a sobrescrita (erro em `sonda-erros.log`): feche e gere de novo.

### `resumo_para_chamado.html`

Página única, imprimível em A4, autocontida (SVG inline, sem dependências):

- **Cobertura:** rodadas registradas × esperadas (da primeira à última
  rodada), período e nº de lacunas.
- **Resultado por serviço** (só alvos do PNCP, 1ª tentativa): medições,
  disponibilidade %, falhas, *recuperadas na 2ª tentativa*, *falhas
  confirmadas*, lentas, HTTP 429, p50/p95/máx (ms) e *Demora > 10 s* /
  *Demora > 20 s* %.
- **Estado e latência ao longo do tempo:** um cartão do período (serviços
  por rótulo e selo geral) e uma linha por serviço com bolinha de estado,
  uma **faixa de barras** e o rótulo à direita com disponibilidade e p95.
  - *Cor da barra:* verde = ok, âmbar = lenta, cinza = HTTP 429, **vermelho
    hachurado = falha** (a hachura mantém a leitura em preto e branco).
  - *Altura:* latência de 0 a 30 s em escala raiz (0,4 s ainda aparece);
    barra cheia = falha.
  - *Largura da barra:* acompanha o período que os dados cobrem — 5 min até
    ~25 h, 1 h até ~12 dias, 6 h até ~75 dias, depois 1 dia. Em intervalo
    maior que uma rodada, a cor é vermelha se ≥ 25% das medições falharam e
    âmbar se houve falha ou ≥ 25% lentas; a altura é o p95 do intervalo.
  - *Sem barra* = sem medição (a sonda estava desligada); nunca conta como
    disponibilidade. Passar o mouse mostra horário e números do intervalo.
  - *Rótulo do período:* disponibilidade ≥ 99% **Operacional**, ≥ 95% **Com
    problemas**, abaixo **Instável**; sem medição = **Sem dados**. Os limites
    são `LIMIARES_ROTULO` em `sonda_core.py` (podem ser recalibrados).
- **Janelas de incidente** (até 12; o CSV tem todas), **exemplos de falha**
  (até 8) e **método**.
- O campo **Solicitante**, editável direto na página: clique, digite e imprima ou
  salve em PDF. O texto fica no `localStorage` do navegador (chave
  `sonda_pncp_solicitante`), então relatórios seguintes, gerados no mesmo
  navegador, já abrem identificados; se o navegador bloquear o armazenamento,
  o campo continua editável, só não é lembrado. Enquanto vazio, mostra um aviso
  vermelho (impresso também: é de propósito, para não anexar sem identificação).

### CSVs

`;` como separador, UTF-8 com BOM, decimais com vírgula.

| Arquivo | Conteúdo |
|---|---|
| `1_resumo_diario.csv` | por dia × alvo: sondas, ok, lentas, falhas, 429, disponibilidade %, p50/p95 (ms), falhas por tipo, `Demora > 10 s %`, `Demora > 20 s %` |
| `2_janelas_de_incidente.csv` | rodadas em falha unidas quando a distância é ≤ 90 min: início, fim, duração, rodadas, alvos, falhas por tipo, mensagens do PNCP reconhecidas |
| `3_ocorrencias.csv` | toda medição que não foi ok/lenta (inclui 429, registro ausente e 2ª tentativas), com IP, tempos, trecho do corpo e horário do erro segundo o PNCP |
| `4_cobertura_diaria.csv` | rodadas registradas × esperadas por dia |
| `5_lacunas.csv` | intervalos sem registro, com o motivo quando conhecido |

## Notificações

Toasts do Windows (`winotify`), se `notificar` for `true`:

- **Falha confirmada** — o ícone ficou vermelho; lista os alvos afetados.
- **PNCP recuperou** — saiu de vermelho para verde/amarelo.
- **Registro de teste sumiu** — [registro ausente](#registro-ausente), uma vez.
- Ao gerar o relatório pelo menu, informa o nome da pasta.

A notificação é acessório: qualquer erro ao exibi-la é ignorado para nunca
derrubar a sonda. O toast mostra só a primeira linha da mensagem.

## Como ler os números

- **Disponibilidade** = (ok + lentas) / (ok + lentas + falhas), sobre a
  1ª tentativa de cada medição. Uma medição que falhou e passou na 2ª
  conta como falha aqui, mas a rodada dela fica `degradado` e não
  `falha`: são duas métricas diferentes, e o resumo mostra as duas
  colunas (*falhas* e *falhas confirmadas*).
- **Lenta ≠ falha.** Uma API que responde em 29 s conta como disponível.
  A coluna *Demora > X s %* existe para mostrar o serviço que
  "responde, mas tarde".
- **Percentis** (p50/p95/máx) usam só respostas válidas. *Demora > X s*
  usa respostas válidas **mais** os timeouts.
- **Poucos dados não são evidência.** Um relatório com poucas rodadas só
  mostra que o mecanismo funciona; espere dias de coleta antes de anexar.
- **Um ponto de observação.** Um problema na sua rede ou no seu provedor
  para o PNCP específico não é distinguível de um problema do PNCP; os
  controles cobrem só a queda geral de internet.

## Limitações

- Mede só com o PC ligado (e acordado): as lacunas ficam registradas.
- Um único ponto de observação (um IP de saída).
- Os endpoints medidos são leituras públicas de 10 registros; não
  exercitam publicação, autenticação nem grandes volumes.
- O PNCP muda rotas (por exemplo, `GET /compras/{ano}/{seq}` já responde
  301 apontando para `api/consulta`). Se um alvo passar a responder 301/404
  de forma permanente, ele deixa de medir o que deveria.

## Armadilhas e solução de problemas

- **Ícone não aparece:** está em "ícones ocultos" da barra de tarefas.
  Use `Encerrar Sonda PNCP.cmd` se precisar parar sem ver o ícone.
- **Nada acontece ao abrir:** já há uma instância (a 2ª execução sai em
  silêncio). Confira com o Gerenciador de Tarefas; vão aparecer **2
  processos `pythonw`** por instância (launcher do venv + filho): é normal.
- **Erro que "some":** `pythonw` não tem console. Veja
  `logs/sonda-erros.log`.
- **Ação de menu com mais de 2 parâmetros** dá `ValueError` só em
  execução (pystray) — o `pythonw` esconde o erro; ver o log acima.
- **`.cmd` com acento** quebra no cmd: os `.cmd` do projeto são ASCII puro.
- **Notificação não aparece:** Modo Foco/Não Perturbe ligado, ou
  notificações do app "Sonda PNCP" desativadas em Configurações → Sistema →
  Notificações.
- **Um alvo aparece sempre como falha ou registro ausente:** confira a URL
  no navegador; troque a `compra_teste` (ver [Órgão e compra de teste](#órgão-e-compra-de-teste)) se ela sumiu.
- **Reiniciar:** *Encerrar* pelo menu (ou `--encerrar`) e abrir de novo.
  Cria uma lacuna de segundos, registrada.

## Testes

```bat
.venv\Scripts\python.exe -m unittest discover -s tests
```

38 testes: a medição usa o `curl.exe` de verdade contra um servidor HTTP
falso local (200, 204, 429, 503, 404, timeout, corpo inválido, conexão
derrubada/recusada/DNS); a máquina de estados, as lacunas, o encerramento
no meio da rodada, o registro ausente e o relatório rodam com relógio e
medição falsos, sem rede. O smoke contra o PNCP real (`--uma-rodada`) é
manual e obrigatório antes de uma versão com mudança de comportamento.
