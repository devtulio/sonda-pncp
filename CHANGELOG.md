# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/).

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
