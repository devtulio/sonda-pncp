# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/).

## [Não lançado]

### Changed
- **Cadência de início a início.** A espera até a próxima rodada era contada do
  **fim** da anterior, então o período real era `duração + intervalo`: mediana
  medida de 5,6 min em modo normal e 3,6 min em incidente (documentado: 5 min e
  60 s), com 59% das rodadas de 20-21/09 em incidente. Agora
  `espera = max(0, intervalo - duração)` e `proxima_em_s` grava essa espera. Rodada
  mais longa que o intervalo (PNCP em timeout, 5 a 8 min) emenda na seguinte sem
  pausa; `espera_entre_alvos_s` continua espaçando as requisições. Se uma rodada
  falhar por erro interno, a espera é o intervalo configurado (e não a última
  espera, que pode ser ~0: seria um laço quente). A detecção de lacuna e o vigia
  usam a espera nova. Smoke real (`curl.exe` e relógio reais, servidor local): com
  intervalo de 15 s, o período entre inícios era 17,9 s no núcleo antigo e 15,0 s no
  novo; com rodadas de 8,3 s e intervalo de 4 s, as rodadas emendam (período 8,4 s,
  `proxima_em_s = 0`). **Relatórios de antes e depois desta versão têm cadências
  diferentes.**
- O relatório (CSVs, resumo HTML e gráfico, ~460 linhas) saiu de `sonda_core.py` para
  `sonda_relatorio.py`, sem mudar comportamento: o relatório gerado dos logs reais
  sai idêntico byte a byte antes e depois. `sonda_core.py` fica com configuração,
  medição, log e a máquina de estados. Módulo interno, fora do contrato.

## [1.4.1] — 2026-09-21

Correções da auditoria de código de 21/09/2026: a promessa "a sonda não morre
calada" não se sustentava, e havia falhas de evidência no relatório. Cada
correção nasceu de uma reprodução (os testes novos falham no código anterior)
ou de uma medição nos logs reais. Sem mudança de contrato: nenhum evento,
resultado ou coluna nova; só comportamento que contradizia o que já estava
documentado. Validado com `--uma-rodada` e `--relatorio` contra o PNCP real e
com a bandeja real (`pythonw` + `pystray`) numa cópia isolada.

### Fixed
- **Laço de medição morrendo em silêncio.** `registrar_erro` gravava o evento
  no log antes do `sonda-erros.log`; com o log inacessível (disco cheio, arquivo
  travado) a exceção estourava dentro do `except` e a thread morria sem rastro,
  sem voltar. Agora o `sonda-erros.log` vai primeiro, `registrar_erro` nunca
  levanta, e o laço inteiro está protegido. Também: `disparar.wait` fora do
  `try` matava o laço com um intervalo em texto; falha ao redesenhar o ícone ou
  ao notificar derrubava a cadência do incidente e perdia a rodada.
- **Partida frágil.** Uma exceção em `iniciar()` (compactar um log antigo
  travado pelo antivírus, log com bytes inválidos) ou no PowerShell do atalho
  impedia o laço e o `--encerrar` de subirem, com o ícone cinza para sempre.
  Cada etapa é protegida e o laço sobe mesmo assim.
- **Porta da instância única ocupada por outro programa** fazia a sonda sair com
  código 0, sem log e sem aviso. Agora a 2ª execução confere com um `ping`: se
  não for uma Sonda, mostra uma caixa de erro e registra. `--encerrar` só devolve
  0 quando a Sonda **confirmou** (antes bastava conectar).
- **"Encerrar" sem efeito** com o log falhando: `parar` agora é marcado antes de
  gravar o evento. Medição em andamento no encerramento é descartada, para nada
  ser gravado depois de `sonda_encerrada`.
- **Config inválido** (intervalo `0` ou em texto, `alvos` vazio, só controles,
  URL com marcador inválido) só aparecia horas depois. Agora é validado na
  partida, com mensagem que cita a chave; chave desconhecida vira uma linha em
  `sonda-erros.log`. Um alvo com erro interno não derruba mais a rodada dos
  outros, e alvo não medido nunca resulta em rodada `ok`. O `config.json` é
  gravado de forma atômica.
- **Log:** uma linha cortada por queda de energia engolia o registro seguinte
  (perdiam-se dois, não um). A escrita agora garante a quebra de linha. A leitura
  tolera bytes inválidos, linha que não é registro e `.gz` truncado, e conta o
  que ignorou (o relatório informa); `.jsonl` e `.jsonl.gz` do mesmo dia não
  contam em dobro; `compactar_antigos` não levanta com arquivo travado.
- **Relatório:** o `4_cobertura_diaria.csv` acusava "cobertura 0%" em dias em que
  a sonda ainda não existia; agora começa no dia do primeiro registro. Publicação
  atômica da pasta do dia (arquivo aberto no Excel gera uma pasta completa com a
  hora no nome, em vez de misturar duas gerações). Falha ao gerar pelo menu avisa.
  O motivo de uma lacuna considera `erro_interno`.
- **Gráfico:** em períodos ≤ ~25 h os baldes de 5 min ficavam vazios (10–12% com a
  sonda ligada o tempo todo) enquanto a legenda dizia "sonda desligada". Agora é
  uma barra por medição, na posição real. Registro de teste ausente tem cor
  própria (antes aparecia como "ok").
- `desvio_relogio_s` só é gravado com resposta abaixo de 2 s (com respostas
  lentas o erro da estimativa chegava a ±14 s). `mudanca_ip` deixa de ser
  registrado para os controles (72% dos eventos, todos ruído). O resumo
  `rodada`, gravado no fim da rodada com o horário do início, não é mais
  tomado como "último registro" ao calcular o intervalo desde a partida
  anterior. O timeout da guarda do curl tem detalhe próprio (`timeout_guarda`).
- Argumento desconhecido ou `--relatorio` fora de 1–365 não sobe mais a sonda
  na bandeja (código 2).

### Added
- **Vigia** na bandeja: se a thread do laço morrer, ou ficar viva sem medir por
  mais que a rodada mais lenta possível (duas verificações seguidas; o relógio
  monotônico evita alarme falso ao voltar de suspensão), o ícone fica vermelho,
  a dica diz "PAROU de medir" e sai uma notificação. Antes o ícone ficava verde
  para sempre com o laço morto.
- Aviso quando o `curl.exe` está ausente ou bloqueado (uma vez, com notificação):
  antes parecia apenas "sem rede local".
- Caixa de erro do Windows para falha de partida (config inválido, porta ocupada).
- 44 testes novos (`tests/test_robustez.py`), incluindo os primeiros da bandeja:
  cobertura de `sonda_pncp.pyw` de 0% para 52%, do núcleo de 90% para 92%.

### Security
- Texto do PNCP que começa com `=`, `+`, `-` ou `@` (corpo da resposta em
  `3_ocorrencias.csv`) recebe um `'` na frente: o Excel de quem abre o anexo não
  o executa mais como fórmula.
- O `curl.exe` vem do `System32` (`shutil.which` procurava antes no diretório
  atual, e o atalho de início automático define o diretório de trabalho); o
  curl só segue `http`/`https`, inclusive em redirecionamentos. O PowerShell do
  atalho recebe os caminhos por variáveis de ambiente, não dentro do texto do
  script.

### Não incluído (fica para a 1.5.0, exige contrato novo ou decisão)
Cadência "início a início" (hoje a espera conta do fim da rodada: ~5,6 min em vez
de 5 e ~3,6 min em vez de 60 s no incidente); coluna de disponibilidade
ponderada por tempo (a contagem tem viés de ~3 p.p. nos dados reais); resultado
`erro_local` e validação do corpo dos controles (portal cativo); `uptime_pc_s` em
`sonda_iniciada`; autostart independente de logon.

## [1.4.0] — 2026-09-20

O órgão e a compra usados nos alvos de contratos, atas, PCA e itens deixam
de estar escritos dentro das URLs: o repositório é público e cada usuário
quer medir o seu órgão. Trocar passa a ser editar duas chaves. Validado contra
o PNCP real (os 4 alvos respondem pelas URLs geradas dos marcadores).

### Added
- Chaves `cnpj_teste` (padrão `83102277000152`, aceita pontuação) e
  `compra_teste` (`{"ano": 2026, "sequencial": 495}`) no `config.json`.
- Marcadores `{cnpj}`, `{ano_compra}` e `{seq_compra}` nas URLs dos alvos;
  os alvos padrão passam a usá-los. Uma URL sem marcadores continua valendo.
- Validação na partida: `cnpj_teste` com outro tamanho que 14 dígitos ou
  `compra_teste` malformado interrompem com mensagem clara em vez de virar
  404 nos alvos.
- MANUAL: seção "Órgão e compra de teste" (como escolher e como achar uma
  compra que exista).

### Changed
- A notificação de registro ausente manda trocar `compra_teste`.
- `config.json` já existente **não muda de comportamento**: mantém as URLs
  antigas (a chave `alvos` do arquivo substitui a lista padrão). Para usar os
  marcadores, apague a chave `alvos` (MANUAL, "config.json antigo").

## [1.3.0] — 2026-09-20

O campo de identificação do resumo para o chamado passa a ser preenchido
direto no HTML, sem editar arquivo nem configuração: o repositório é público
e cada usuário tem a sua identificação. Validado com o relatório real.

### Added
- Campo **Solicitante** editável na página (`contenteditable`), lembrado no
  `localStorage` do navegador (`sonda_pncp_solicitante`); aviso vermelho
  enquanto vazio, inclusive na impressão. Se o navegador bloquear o
  armazenamento, o campo continua editável, só não é lembrado.

### Changed
- O texto fixo `Solicitante: [preencher identificação antes de anexar]` foi
  substituído por esse campo.

## [1.2.0] — 2026-09-20

O gráfico do resumo para o chamado foi refeito no padrão de faixas de
barras do statuslicitacoes.com.br (uma linha por serviço, barra = amostra,
cor = estado, altura = latência). Validado com o relatório real e com 7
dias de log sintético renderizados no navegador.

### Added
- Cartão do período no topo do gráfico: contagem de serviços por rótulo
  (operacional / com problemas / instável / sem dados) e selo geral.
- Rótulo por serviço no período: disponibilidade ≥ 99% *Operacional*, ≥ 95%
  *Com problemas*, abaixo *Instável* (`LIMIARES_ROTULO`, recalibrável).
- Falha hachurada além de vermelha: o gráfico continua legível em preto e
  branco. Tooltip em cada barra.

### Changed
- O gráfico de pontos por serviço foi **substituído** pelas faixas de
  barras. A largura da barra acompanha o período coberto pelos dados (5 min
  até ~25 h; 1 h até ~12 dias; 6 h; 1 dia). Cor de um intervalo maior que uma
  rodada: vermelho se ≥ 25% das medições falharam, âmbar se houve falha ou
  ≥ 25% lentas. Intervalo sem medição fica sem barra (lacuna visível).
- A tabela de janelas de incidente do HTML mostra no máximo 12 linhas e
  aponta para o CSV, que continua completo.

## [1.1.1] — 2026-09-20

Lint e análise de segurança entram no CI. Sem mudança de comportamento:
o que mudou no código é refatoração neutra pedida pelo `ruff`. Smoke real
(`--uma-rodada` contra o PNCP) e os 30 testes verdes.

### Added (CI; sem efeito na API)
- Job `qualidade` no CI: `ruff check .` e `bandit` (`pyproject.toml`,
  `requirements-dev.txt`). Os dois passam a valer também para o
  `sonda_pncp.pyw`, que o `ruff` e o `bandit -r` não varrem por padrão
  (`extend-include` e arquivos passados por nome).

### Changed
- `datetime.UTC` no lugar de `timezone.utc`; `zip(..., strict=False)`;
  `TimeoutError` no lugar de `socket.timeout`; variável sem uso removida;
  `# nosec` (com a justificativa ao lado) nas chamadas intencionais de
  `subprocess` e `os.startfile`, que só tocam o `curl.exe`, o PowerShell do
  atalho de início automático e pastas da própria sonda.

## [1.1.0] — 2026-09-20

Primeiro lote depois da 1.0.0, no mesmo dia: o relatório passa a servir
de anexo direto para um chamado (resumo imprimível com gráfico e
métrica de demora), o alvo de itens deixa de acusar falso alarme quando
o registro de teste some, e o relatório vira um por dia. Mudanças de
comportamento validadas com smoke contra o PNCP real (`--uma-rodada` e
classificação de uma compra inexistente).

### Added
- `resumo_para_chamado.html` no relatório: página única imprimível (A4),
  autocontida, com cobertura, resultado por serviço (disponibilidade,
  p50/p95/máx, falhas × falhas confirmadas × recuperadas na 2ª tentativa),
  janelas de incidente, exemplos de falha com o horário do erro segundo o
  PNCP e método. Traz `[preencher identificação antes de anexar]`.
- Gráfico SVG no resumo: faixa de rodadas por estado e um painel de
  latência por serviço ao longo do tempo (falha, lenta e 429 com forma
  própria, não só cor; tooltip por ponto).
- Colunas `Demora > 10 s %` e `Demora > 20 s %` no CSV `1_resumo_diario` e
  no resumo: parcela das medições (respostas válidas + timeouts) que
  passaram de X segundos — mostra o serviço que "responde, mas tarde".
- Resultado `registro_ausente` (HTTP 404 com a mensagem configurada na
  chave de alvo `ausencia_404`): não é falha nem entra na disponibilidade,
  não repete, deixa a rodada `degradado`. Campo `registros_ausentes` na
  rodada, evento `registro_ausente` e notificação do Windows, uma vez por
  ocorrência. O alvo `api_itens` já vem com a marca.
- Repositório público `devtulio/sonda-pncp` e CI (GitHub Actions, Windows,
  Python 3.11 e 3.13: os testes rodam o `curl.exe` de verdade).
- Documentação: `README.md`, `MANUAL.md`, `RELEASING.md`, `CHANGELOG.md`,
  `LICENSE` (MIT), `requirements.txt` e `.gitignore`.

### Changed
- O relatório passa a ser **um por dia**: `relatorios/relatorio-AAAAMMDD/`
  (antes `relatorio-AAAAMMDD-HHMM/`, uma pasta nova a cada geração). Gerar
  de novo no mesmo dia sobrescreve; como é derivado dos logs, encerrar e
  reabrir a sonda não fragmenta o relatório.
- `sonda_core.VERSAO` passa a ser a fonte única da versão (`1.1.0`).
- Requisito declarado: Python 3.11+ (o log usa `datetime.fromisoformat` com
  sufixo `Z`).

### Fixed
- Encerrar a sonda no meio de uma rodada gravava o resumo parcial da
  rodada **depois** do evento `sonda_encerrada`; o início seguinte lia
  esse resumo como último registro e reportava
  `encerramento_anterior_limpo: false` e uma lacuna inexistente. Agora a
  rodada interrompida é abandonada sem gravar o resumo (as medições já
  feitas ficam no log). O resumo parcial também marcava `ok` alvos que
  nem tinham sido medidos.

## [1.0.0] — 2026-09-20

Versão inicial, criada e testada no mesmo dia e posta a rodar na bandeja
com início automático. Declara o contrato descrito em
[RELEASING.md](RELEASING.md) §1.

### Added
- Medição de 2 controles de internet e 6 alvos do PNCP (portal e 5
  endpoints de consulta) via `curl.exe`, com DNS/TCP/TLS/TTFB/total,
  User-Agent aceito pelo WAF do portal e timeouts de 10 s / 30 s.
- Classificação `ok`, `lento`, `erro_http`, `erro_rede`, `timeout`,
  `corpo_invalido` e `bloqueio_429` (429 = limitação do WAF, fora da
  disponibilidade); repetição de falha após 10 s (`blip` = a 2ª passou).
- Estados `ok`/`degradado`/`falha`/`sem_rede`, ícone de bandeja verde/
  amarelo/vermelho/cinza, modo incidente (60 s) e notificações do Windows.
- Log JSONL por dia (medições, rodadas e eventos), com evidência completa
  na falha, compactação de logs antigos e detecção de lacunas
  (suspensão × pausa manual).
- Relatório com 5 CSVs (resumo diário, janelas de incidente, ocorrências,
  cobertura diária, lacunas).
- Instância única por socket loopback, início automático com o Windows,
  `--encerrar`, `--uma-rodada` e `--relatorio`.
- 25 testes (medição real contra servidor HTTP falso, máquina de estados,
  lacunas e relatório).
