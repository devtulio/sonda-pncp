# Política de versão e release

Vale para este repositório. Baseada em SemVer 2.0.0, Keep a Changelog e
no que a literatura de release engineering mediu (referências no fim).
Mesmo modelo do [motor_pncp](https://github.com/devtulio/motor-pncp),
adaptado ao que a sonda expõe.

## 1. O que é a API pública (o contrato)

Só isto é coberto pelo número de versão:

- **Linha de comando** de `sonda_pncp.pyw`: as flags `--encerrar`,
  `--uma-rodada` e `--relatorio [N]`, seus códigos de saída e o que
  imprimem.
- **`config.json`**: nome, significado e tipo de cada chave e dos campos
  de cada alvo (`id`, `tipo`, `validar`, `url`, `limiar_lento_ms`,
  `accept`, `ausencia_404`); os marcadores de URL (`{hoje}`, `{cnpj}`, `{ano_compra}`,
  `{seq_compra}`, ...); as chaves `cnpj_teste` e `compra_teste`.
- **Formato do log** (`logs/sonda-AAAA-MM-DD.jsonl`): o nome e a
  organização dos arquivos, os campos de cada `tipo` (`sonda`, `rodada`,
  `evento`), os valores de `resultado`, `estado`, `cor`, `modo` e `evento`.
  Quem lê o log (o relatório, uma planilha, outro sistema) depende disso.
- **Relatório**: nome da pasta (`relatorios/relatorio-AAAAMMDD/`), nome
  dos arquivos e as colunas dos 5 CSVs (nome e posição), e as seções do
  `resumo_para_chamado.html`.
- **Semântica das métricas**: como se calcula disponibilidade, o que
  conta como falha, lenta, 429 e registro ausente.

**Não é contrato** (mesmo que alguém dependa — Hyrum's Law): as funções e
classes de `sonda_core.py` (a sonda não é uma biblioteca), texto de
notificações, de dica do ícone e de menu, layout visual e estilo do
HTML/SVG, **valores** default (intervalos, timeouts, limiares
`limiar_lento_ms`, `LIMIARES_DEMORA_S` — podem ser recalibrados em
patch), os alvos e URLs padrão, a porta da instância única, o conteúdo
dos campos de texto livre (`detalhe`, `curl_erro`, `corpo_trecho`) e a
ordem das linhas.

## 2. Como o número muda (SemVer estrito)

| Mudança | Bump | Exemplo |
|---|---|---|
| Quebra do contrato | **major** | remover/renomear campo do log, coluna de CSV ou chave do `config.json`; mudar o significado de `resultado` ou da disponibilidade; mudar o código de saída de uma flag |
| Adição compatível | **minor** | campo novo no log, coluna nova **no fim** de um CSV, resultado novo que não altera os existentes, chave nova no `config.json` com default, seção nova no resumo |
| Correção compatível, recalibração de limiar, refactor interno | **patch** | fix de lacuna/encerramento, ajuste de `limiar_lento_ms` ou timeout, otimização |
| Só documentação (README, MANUAL, CHANGELOG, comentário) | **nenhum** | commit em `master`, sem tag |

Versão reflete comportamento. Mudança que não altera comportamento não
altera versão. Coluna nova no **meio** de um CSV é quebra (muda a posição
das seguintes); por isso colunas novas entram no fim.

## 3. Toda versão = tag anotada + release no GitHub

Sem exceção e sem meio-termo: o release é o sinal que o consumidor lê.
Notas do release = a seção daquela versão no `CHANGELOG.md`.

```bash
git tag -a vX.Y.Z -m "Sonda PNCP X.Y.Z — <uma linha>"
git push && git push origin vX.Y.Z
gh release create vX.Y.Z --title "Sonda PNCP vX.Y.Z" --notes-file notas.md --latest
```

Quem usa a sonda em outra máquina (ex.: uma VPS como segundo ponto de
observação) faz checkout **da tag**, nunca de `master`. Ao subir a
versão, a sonda grava `versao` em cada `sonda_iniciada`: é o que permite
saber, olhando o log, com qual versão cada medição foi feita.

## 4. Cadência: sob demanda, lote pequeno

Cada correção vira o próprio patch assim que passa nos gates. Não
acumular. Release pequeno e frequente não piora qualidade e faz bug ser
corrigido mais rápido (Khomh et al.).

## 5. Gates antes de qualquer tag

1. `.venv\Scripts\python.exe -m unittest discover -s tests` verde.
2. `ruff check .` e `bandit -q -c pyproject.toml sonda_core.py sonda_pncp.pyw`
   limpos (o CI roda os dois; o `bandit` recebe os arquivos por nome porque
   o `-r` não olha `.pyw`).
3. **Mudança de comportamento exige smoke contra o PNCP real**:
   `.venv\Scripts\python.exe sonda_pncp.pyw --uma-rodada` com o resultado
   de cada alvo coerente com o que o portal está fazendo (ou a falha
   explicada como externa) e, se mexeu no relatório, `--relatorio 7`
   aberto e **visto** (o HTML renderizado, não só o teste). Mock não prova
   fronteira.
4. `CHANGELOG.md` com a seção da versão (Added / Changed / Deprecated /
   Removed / Fixed / Security), escrita no mesmo commit.
5. Versão bumpada em **um** lugar: `sonda_core.VERSAO`.
6. Push, CI verde, aí a tag. Nunca tag antes do CI.
7. Reiniciar a sonda em uso para ela passar a rodar a versão nova: o
   processo em execução não relê o código nem o `config.json`.

## 6. Depreciação

Nada é removido sem antes viver **pelo menos uma minor** sinalizado como
*Deprecated* no CHANGELOG (e, quando a sonda é quem lê — uma chave do
`config.json`, por exemplo — com aviso no log). Remoção só em major, com
*Removed* no CHANGELOG e nota de migração.

Churn rule: quando a sonda quebra contrato (formato do log, coluna do
CSV), a mudança traz a nota de migração pronta — quem lê os logs
antigos não deve descobrir lendo diff. Logs já gravados **não** são
reescritos: o relatório deve continuar lendo os formatos anteriores ou a
migração dizer como converter.

## 7. Suporte e pré-release

- Só a última release recebe correção. Sem backport.
- Pré-release (`X.Y.ZrcN`, PEP 440) só para mudança arriscada que precise
  de dias de coleta real antes de fechar. Não vira `--latest`.

## Referências

- SemVer 2.0.0 — <https://semver.org/> (§4: 0.x é desenvolvimento
  inicial; FAQ: "se está em produção, já deveria ser 1.0.0").
- Keep a Changelog — <https://keepachangelog.com/pt-BR/1.0.0/>.
- PEP 440 — esquema de versão do Python.
- Adams & McIntosh, "Modern Release Engineering in a Nutshell", SANER
  2016 — o pipeline integração → CI → build → release, cada etapa com gate.
- Raemaekers, van Deursen & Visser, "Semantic versioning and impact of
  breaking changes in the Maven repository", JSS 2017; replicação por
  Ochoa et al., EMSE 2022 — ~1/3 dos releases quebram algo e quase metade
  viola SemVer: o contrato só vale se estiver escrito.
- Khomh, Adams, Dhaliwal & Zou, "Understanding the impact of rapid
  releases on software quality", EMSE 2015 — ciclos curtos não aumentam
  bugs pós-release e encurtam o tempo de correção.
- Winters, Manshreck & Wright, *Software Engineering at Google*, cap. 15
  (Deprecation) e Hyrum's Law — todo comportamento observável vira
  dependência; declarar o contrato e absorver a migração.
