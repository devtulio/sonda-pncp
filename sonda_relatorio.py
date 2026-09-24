"""Sonda PNCP - relatório: CSVs, resumo HTML imprimível para anexar a um chamado e o gráfico de faixas de barras.

Tudo é derivado dos logs (`sonda_core.ler_registros`): gerar de novo no mesmo dia sobrescreve o relatório do dia, e
encerrar e reabrir a sonda não fragmenta nada. Sem dependências além da biblioteca padrão.
"""

import csv
import html
import math
import os
import shutil
import socket
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sonda_core import FALHAS, OKS, VERSAO, carregar_config, ler_registros


def _pct(valores, p):
    if not valores:
        return ""
    s = sorted(valores)
    return s[max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))]


LIMIARES_DEMORA_S = (10, 20)  # "demorou": resposta válida ou timeout acima de X s (erro rápido fica fora)


def _demora(s1, x):
    """% das medições com resposta válida ou timeout que passaram de x segundos ('' se não houver)."""
    base = [s for s in s1 if s["resultado"] in OKS or s["resultado"] == "timeout"]
    return f"{100 * sum(s['total_ms'] > x * 1000 for s in base) / len(base):.1f}".replace(".", ",") if base else ""


GAP_PONDERADA_S = 15 * 60  # lacuna maior que isso não conta pra nenhum lado: não dá pra saber o estado real nela


def _disp_ponderada(s1):
    """Disponibilidade % ponderada pelo TEMPO entre medições (não pela contagem): cada medição vale o
    tempo até a próxima, exceto lacuna > 15 min (PC desligado etc., peso 0 — nem disponível nem falha).
    429/registro_ausente/erro_local ficam fora, como na disponibilidade por contagem."""
    s1 = sorted(s1, key=lambda s: s["ts_local"])
    disp = total = 0.0
    for s, prox in zip(s1, s1[1:], strict=False):
        if s["resultado"] not in OKS and s["resultado"] not in FALHAS:
            continue
        delta = (datetime.fromisoformat(prox["ts_local"]) - datetime.fromisoformat(s["ts_local"])).total_seconds()
        if delta > GAP_PONDERADA_S:
            continue
        total += delta
        if s["resultado"] in OKS:
            disp += delta
    return f"{100 * disp / total:.2f}".replace(".", ",") if total else ""


def _causa_lacuna(ev):
    """Pelo `sonda_iniciada` que fechou a lacuna (1.6.0+): PC reiniciado, PC parado (desligado/suspenso, inclusive
    o "Desligar" com Inicialização Rápida) ou sonda parada com o PC ligado. '' se o evento não traz os campos."""
    if ev.get("pc_reiniciou"):
        return "PC reiniciado"
    parado, gap = ev.get("pc_parado_s"), ev.get("gap_desde_anterior_s")
    if not isinstance(parado, (int, float)) or not gap:
        return ""
    return "PC desligado ou suspenso" if parado >= gap / 2 else "sonda parada com o PC ligado"


def _fmt(iso):
    return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M:%S") if iso else ""


def _celula(v):
    """Texto que vem do PNCP (corpo da resposta) e começa com = + - @ vira fórmula no Excel de quem abre o anexo."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def _csv(caminho, cabecalho, linhas):
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:  # ';' e BOM: Excel em português
        w = csv.writer(f, delimiter=";")
        w.writerow(cabecalho)
        w.writerows([_celula(v) for v in linha] for linha in linhas)


MARCAS_PNCP = (("banco de dados", "Erro na comunicação com o banco de dados"),
               ("JDBC", "Failed to obtain JDBC Connection"), ("Bad gateway", "Bad gateway"))


def gerar_relatorio(pasta, dias=7, agora=None):
    """Gera 5 CSVs + resumo HTML em `relatorios/relatorio-AAAAMMDD/` (sobrescreve o do dia) e devolve a pasta."""
    cfg = carregar_config(pasta)
    agora = (agora or datetime.now()).astimezone()
    ini, fim = (agora - timedelta(days=dias - 1)).date(), agora.date()
    rejeitadas = []
    regs = list(ler_registros(pasta, ini, fim, rejeitadas))
    sondas = [r for r in regs if r["tipo"] == "sonda"]
    rodadas = sorted((r for r in regs if r["tipo"] == "rodada"), key=lambda r: r["ts_utc"])
    eventos = [r for r in regs if r["tipo"] == "evento"]
    destino = Path(pasta) / "relatorios" / f"relatorio-{agora:%Y%m%d}"  # 1 por dia: gerar de novo sobrescreve
    # tudo é gerado numa pasta de trabalho e só então vira a pasta do dia: falha no meio não deixa arquivos de
    # duas gerações misturados (o que acontece se um CSV estiver aberto no Excel e a cópia parar no meio)
    out = destino.with_name(destino.name + ".novo")
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    # 1) resumo diário por alvo (1ª tentativa; 429 fora da disponibilidade)
    grupos = defaultdict(list)
    for s in sondas:
        if s["tentativa"] == 1:
            grupos[(s["ts_local"][:10], s["alvo"], s["categoria"])].append(s)
    linhas = []
    for (dia, alvo, cat), lst in sorted(grupos.items()):
        c = Counter(s["resultado"] for s in lst)
        fal = sum(c[k] for k in FALHAS)
        val = c["ok"] + c["lento"]
        lat = [s["total_ms"] for s in lst if s["resultado"] in OKS]
        tipos = Counter(s["detalhe"] for s in lst if s["resultado"] in FALHAS)
        linhas.append([datetime.strptime(dia, "%Y-%m-%d").strftime("%d/%m/%Y"), alvo, cat, len(lst), c["ok"],
                       c["lento"], fal, c["bloqueio_429"],
                       f"{100 * val / (val + fal):.2f}".replace(".", ",") if val + fal else "",
                       _pct(lat, 50), _pct(lat, 95),
                       ", ".join(f"{k}×{v}" for k, v in tipos.most_common()),
                       *(_demora(lst, x) for x in LIMIARES_DEMORA_S), _disp_ponderada(lst)])
    _csv(out / "1_resumo_diario.csv", ["Data", "Alvo", "Categoria", "Sondas", "OK", "Lentas", "Falhas",
                                       "Bloqueios 429", "Disponibilidade %", "Latência p50 (ms)",
                                       "Latência p95 (ms)", "Falhas por tipo",
                                       *(f"Demora > {x} s %" for x in LIMIARES_DEMORA_S),
                                       "Disp. ponderada por tempo %"], linhas)

    # 2) janelas de incidente: rodadas em falha, unidas se a distância for ≤ 90 min
    jan = []
    for r in (x for x in rodadas if x["estado"] == "falha"):
        t = datetime.fromisoformat(r["ts_local"])
        t_fim = t + timedelta(milliseconds=r["duracao_ms"])
        if jan and t - jan[-1]["fim"] <= timedelta(minutes=90):
            j = jan[-1]
            j["fim"] = max(j["fim"], t_fim)
            j["n"] += 1
            j["alvos"].update(r["falhas"])
        else:
            jan.append({"ini": t, "fim": t_fim, "n": 1, "alvos": set(r["falhas"])})
    linhas = []
    for j in jan:
        dentro = [s for s in sondas if s["resultado"] in FALHAS
                  and j["ini"] <= datetime.fromisoformat(s["ts_local"]) <= j["fim"] + timedelta(minutes=1)]
        tipos = Counter(s["detalhe"] for s in dentro)
        msgs = sorted({texto for s in dentro for chave, texto in MARCAS_PNCP if chave in s.get("corpo_trecho", "")})
        linhas.append([j["ini"].strftime("%d/%m/%Y %H:%M:%S"), j["fim"].strftime("%d/%m/%Y %H:%M:%S"),
                       round((j["fim"] - j["ini"]).total_seconds() / 60, 1), j["n"], ", ".join(sorted(j["alvos"])),
                       ", ".join(f"{k}×{v}" for k, v in tipos.most_common()), " | ".join(msgs)])
    _csv(out / "2_janelas_de_incidente.csv", ["Início", "Fim", "Duração (min)", "Rodadas em falha",
                                              "Alvos afetados", "Falhas por tipo", "Mensagens do PNCP"], linhas)

    # 3) ocorrências: toda sonda que não foi ok/lento (inclui 429 e as 2ª tentativas)
    linhas = [[_fmt(s["ts_local"]), s["ts_utc"], s["alvo"], s["tentativa"], s["resultado"], s["detalhe"],
               s["http"] or "", s["curl_erro"], s["ip"], s["dns_ms"], s["tcp_ms"], s["tls_ms"], s["ttfb_ms"],
               s["total_ms"], s.get("corpo_trecho", "")[:200].replace("\n", " "), s.get("pncp_ts_erro", ""),
               s["rodada"]] for s in sondas if s["resultado"] not in OKS]
    _csv(out / "3_ocorrencias.csv", ["Início (local)", "UTC", "Alvo", "Tentativa", "Resultado", "Detalhe", "HTTP",
                                     "Erro do curl", "IP", "DNS (ms)", "TCP (ms)", "TLS (ms)", "TTFB (ms)",
                                     "Total (ms)", "Trecho do corpo", "Horário do erro segundo o PNCP",
                                     "Rodada"], linhas)

    # 4) cobertura diária e 5) lacunas (ausência de registro NÃO é disponibilidade)
    por_dia = Counter(r["ts_local"][:10] for r in rodadas)
    linhas = []
    # só conta a partir de quando a sonda existe: antes do 1º registro não havia o que esperar (não é "cobertura 0%")
    primeira = datetime.fromisoformat(rodadas[0]["ts_local"]) if rodadas else None
    d = max(ini, primeira.date()) if primeira else ini
    while d <= fim:
        de = max(datetime.combine(d, datetime.min.time(), tzinfo=agora.tzinfo), primeira) if primeira else agora
        ate = agora if d == agora.date() else datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=agora.tzinfo)
        esp = max(1, (ate - de).total_seconds() / cfg["intervalo_normal_s"])
        n = por_dia.get(str(d), 0)
        linhas.append([d.strftime("%d/%m/%Y"), n, round(esp), f"{min(100, 100 * n / esp):.1f}".replace(".", ",")])
        d += timedelta(days=1)
    _csv(out / "4_cobertura_diaria.csv", ["Data", "Rodadas registradas", "Rodadas esperadas", "Cobertura %"], linhas)
    linhas = []
    limite = timedelta(seconds=cfg["intervalo_normal_s"] * cfg["limite_lacuna_x_intervalo"] + cfg["timeout_total_s"])
    for a, b in zip(rodadas, rodadas[1:], strict=False):
        ta, tb = datetime.fromisoformat(a["ts_utc"]), datetime.fromisoformat(b["ts_utc"])
        if tb - ta > limite:
            ev = [e for e in eventos if e.get("evento") in ("retomada_apos_lacuna", "sonda_iniciada", "pausada",
                                                              "erro_interno")
                  and ta <= datetime.fromisoformat(e["ts_utc"]) <= tb]
            motivo = ev[-1].get("motivo") or ev[-1]["evento"] if ev else "sem registro (PC desligado, suspenso ou sonda parada?)"
            linhas.append([_fmt(a["ts_local"]), _fmt(b["ts_local"]), round((tb - ta).total_seconds() / 60, 1), motivo,
                           _causa_lacuna(ev[-1] if ev else {})])
    _csv(out / "5_lacunas.csv", ["Último registro antes", "Primeiro registro depois", "Duração (min)", "Motivo",
                                 "Causa provável"], linhas)
    _resumo_html(out, cfg, ini, fim, sondas, rodadas, jan, len(linhas), len(rejeitadas))
    return _publicar(out, destino, agora)


def _publicar(pronta, destino, agora):
    """Troca a pasta do dia pela recém-gerada. Se a antiga não puder ser renomeada (arquivo aberto no Excel),
    a nova fica numa pasta com a hora no nome: melhor duas pastas completas do que uma misturada."""
    velha = destino.with_name(destino.name + ".velha")
    shutil.rmtree(velha, ignore_errors=True)
    try:
        if destino.exists():
            os.replace(destino, velha)
        os.replace(pronta, destino)
    except OSError:
        if velha.exists() and not destino.exists():
            os.replace(velha, destino)  # desfaz: o dia não pode ficar sem pasta
        alt = destino.with_name(f"{destino.name}-{agora:%H%M%S}")
        os.replace(pronta, alt)
        return alt
    shutil.rmtree(velha, ignore_errors=True)
    return destino


def br(x, casas=2):
    return f"{x:.{casas}f}".replace(".", ",")


LIMIARES_ROTULO = (99.0, 95.0)  # disponibilidade % do período: >= 99 Operacional, >= 95 Com problemas, senão Instável
ROTULOS = {"ok": "Operacional", "lento": "Com problemas", "falha": "Instável", "vazio": "Sem dados"}
COR_BARRA = {"ok": "#16a34a", "lento": "#d97706", "falha": "#dc2626", "429": "#64748b", "vazio": "#94a3b8",
             "ausente": "#8b5cf6", "local": "#0ea5e9"}
NOME_BARRA = {"ok": "ok", "lento": "lenta", "falha": "falha", "429": "HTTP 429", "ausente": "registro de teste ausente",
              "local": "erro local da sonda"}
MAX_BARRAS = 300
MAX_JANELAS_HTML = 12  # a lista completa fica no CSV; o HTML precisa caber no A4


def _granularidade(span_s, base_s):
    """Menor largura de barra (5 min → 1 h → 6 h → 1 dia) que mantém o gráfico com até MAX_BARRAS barras."""
    for tam, nome in ((base_s, f"{base_s // 60} min"), (3600, "1 hora"), (6 * 3600, "6 horas")):
        if span_s / tam <= MAX_BARRAS:
            return tam, nome
    return 86400, "1 dia"


def _cor_do_balde(n, nf, nl, n429, naus=0, nloc=0):
    """Cor de um intervalo: vermelho se >= 25% falharam; âmbar se houve falha ou >= 25% lentas; cinza se só 429;
    violeta se só registro de teste ausente; azul se só erro local da sonda (nenhum dos dois é queda do alvo)."""
    if nf / n >= 0.25:
        return "falha"
    if nf or nl / n >= 0.25:
        return "lento"
    if naus == n:
        return "ausente"
    if nloc == n:
        return "local"
    return "429" if n429 == n else "ok"


def _baldes(medicoes, t0, tam, teto_s):
    """medicoes: [(epoch_s, resultado, total_ms)] → {indice: (cor, p95_s, n, nf, nl)}; só existe balde com dado."""
    grupos = defaultdict(list)
    for t, res, ms in medicoes:
        grupos[int((t - t0) // tam)].append((res, ms))
    out = {}
    for i, g in grupos.items():
        nf = sum(r in FALHAS for r, _ in g)
        nl = sum(r == "lento" for r, _ in g)
        n429 = sum(r == "bloqueio_429" for r, _ in g)
        naus = sum(r == "registro_ausente" for r, _ in g)
        nloc = sum(r == "erro_local" for r, _ in g)
        fora = ("bloqueio_429", "registro_ausente", "erro_local")
        lat = [teto_s if r in FALHAS else ms / 1000 for r, ms in g if r not in fora]
        out[i] = (_cor_do_balde(len(g), nf, nl, n429, naus, nloc), _pct(lat, 95) if lat else 0, len(g), nf, nl)
    return out


def _periodo_mediano(por_alvo, base_s):
    """Mediana do tempo entre medições consecutivas do mesmo alvo (ignora lacunas de verdade, > 3 × o intervalo)."""
    gaps = []
    for v in por_alvo.values():
        ts = sorted(m[0] for m in v)
        gaps += [b - a for a, b in zip(ts, ts[1:], strict=False) if b - a < 3 * base_s]
    gaps.sort()
    return gaps[len(gaps) // 2] if gaps else base_s


def _cor_da_medicao(resultado):
    if resultado in FALHAS:
        return "falha"
    return {"lento": "lento", "bloqueio_429": "429", "registro_ausente": "ausente",
            "erro_local": "local"}.get(resultado, "ok")


def _rotulo(disp):
    if disp is None:
        return "vazio"
    return "ok" if disp >= LIMIARES_ROTULO[0] else ("lento" if disp >= LIMIARES_ROTULO[1] else "falha")


def _grafico(sondas, alvos, cfg):
    """Cartão do período + uma linha por serviço (bolinha, faixa de barras, rótulo). SVG inline, sem dependências."""
    esc = html.escape
    teto = cfg["timeout_total_s"]
    por_alvo = {a["id"]: [(datetime.fromisoformat(s["ts_utc"]).timestamp(), s["resultado"], s["total_ms"])
                          for s in sondas if s["alvo"] == a["id"] and s["tentativa"] == 1] for a in alvos}
    todos = [m for v in por_alvo.values() for m in v]
    if not todos:
        return "<p>Sem dados no período.</p>"
    t0, t1 = min(m[0] for m in todos), max(m[0] for m in todos)
    tam, nome_tam = _granularidade(t1 - t0, cfg["intervalo_normal_s"])
    fino = tam == cfg["intervalo_normal_s"]  # período curto: uma barra por medição, na posição real
    if fino:
        nome_tam = "1 medição"
        periodo = _periodo_mediano(por_alvo, cfg["intervalo_normal_s"])
    n_barras = int((t1 - t0) // tam) + 1
    W, alt = 576, 34
    bw = W / n_barras
    fmt_dia = tam >= 6 * 3600
    linhas, contagem = [], Counter()
    for a in alvos:
        med = por_alvo[a["id"]]
        c = Counter(r for _, r, _ in med)
        ok, fal = c["ok"] + c["lento"], sum(c[k] for k in FALHAS)
        disp = 100 * ok / (ok + fal) if ok + fal else None
        lat = [ms for _, r, ms in med if r in OKS]
        rot = _rotulo(disp)
        contagem[rot] += 1
        barras = []
        if fino:  # (posição, largura, cor, latência em s, horário)
            larg = max(1.4, min(24.0, W * 0.8 * periodo / max(t1 - t0, periodo)))
            itens = [((t - t0) / max(t1 - t0, 1) * (W - larg), larg, _cor_da_medicao(r),
                      teto if r in FALHAS else ms / 1000, datetime.fromtimestamp(t).strftime("%d/%m %H:%M:%S"))
                     for t, r, ms in med]
        else:
            itens = []
            for i, (cor, p95s, n, nf, nl) in sorted(_baldes(med, t0, tam, teto).items()):
                ini = datetime.fromtimestamp(t0 + i * tam)
                quando = f"{ini:%d/%m}" if fmt_dia else f"{ini:%d/%m %H:%M}"
                if n > 1:
                    quando += f" ({n} medições: {nf} falha(s), {nl} lenta(s))"
                itens.append((i * bw, bw * 0.8, cor, p95s, quando))
        for x, larg_b, cor, lat_s, quando in itens:
            h = 1.0 if cor == "falha" else (0.08 if cor == "ausente" else max(0.08, math.sqrt(min(lat_s, teto) / teto)))
            dica = f"{quando} — {NOME_BARRA[cor]}" + ("" if cor == "ausente" else f" — {br(lat_s, 1)} s")
            fill = "url(#hach)" if cor == "falha" else COR_BARRA[cor]
            barras.append(f'<rect x="{x:.2f}" y="{alt * (1 - h):.2f}" width="{larg_b:.2f}" height="{alt * h:.2f}" '
                          f'fill="{fill}"><title>{esc(dica)}</title></rect>')
        nums = (f"disp. {br(disp, 1)}% · p95 {br(_pct(lat, 95) / 1000, 1)} s" if disp is not None and lat
                else "sem medições válidas")
        linhas.append(
            f'<div class="row"><div class="nome"><i style="background:{COR_BARRA[rot]}"></i>{esc(a["nome"])}</div>'
            f'<div class="strip"><svg viewBox="0 0 {W} {alt}" width="100%" height="{alt}" preserveAspectRatio="none" '
            f'role="img" aria-label="{esc(a["nome"])}">{"".join(barras)}</svg></div>'
            f'<div class="num"><b style="color:{COR_BARRA[rot]}">{ROTULOS[rot]}</b><span>{nums}</span></div></div>')
    ticks = "".join(
        f"<span>{datetime.fromtimestamp(t0 + (t1 - t0) * k / 4):{'%d/%m' if (t1 - t0) >= 2 * 86400 else '%d/%m %H:%M'}}</span>"
        for k in range(5))
    n_rot = {k: contagem[k] for k in ("ok", "lento", "falha")}
    selo = ("INSTÁVEL", "inst") if n_rot["falha"] else (("COM PROBLEMAS", "deg") if n_rot["lento"] else ("OPERACIONAL", "ok"))
    sem_dados = (f'<b style="color:{COR_BARRA["vazio"]}">{contagem["vazio"]} sem dados</b>' if contagem["vazio"] else "")
    cartao = (f'<div class="card {selo[1]}"><div><div class="t">PNCP — {len(alvos)} serviços monitorados</div>'
              f'<div class="c"><b style="color:{COR_BARRA["ok"]}">{n_rot["ok"]} operacional</b>'
              f'<b style="color:{COR_BARRA["lento"]}">{n_rot["lento"]} com problemas</b>'
              f'<b style="color:{COR_BARRA["falha"]}">{n_rot["falha"]} instável</b>{sem_dados}· no período</div></div>'
              f'<span class="badge">{selo[0]}</span></div>')
    leg = "".join(f'<span><i style="background:{COR_BARRA[k]}"></i>{n}</span>'
                  for k, n in (("ok", "ok"), ("lento", "lenta (acima do limiar)"), ("429", "HTTP 429 (limitação)"),
                               ("ausente", "registro de teste ausente")))
    leg += (f'<span><svg width="11" height="11" style="vertical-align:-1px;margin-right:5px"><rect width="11" height="11" '
            f'fill="url(#hach)" stroke="{COR_BARRA["falha"]}"/></svg>falha (tempo esgotado ou erro)</span>')
    return (f'<svg width="0" height="0" style="position:absolute"><defs><pattern id="hach" width="4" height="4" '
            f'patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="4" height="4" fill="#fecaca"/>'
            f'<rect width="1.6" height="4" fill="{COR_BARRA["falha"]}"/></pattern></defs></svg>{cartao}'
            f'<p class="sub">1 barra = {nome_tam}. Cor = resultado da medição (em intervalos maiores que uma rodada: vermelho se '
            f'≥ 25% falharam; âmbar se houve falha ou ≥ 25% lentas). Altura = latência (0 a {teto} s, escala raiz; '
            f'barra cheia = falha). Rótulo do período: disponibilidade ≥ {LIMIARES_ROTULO[0]:g}% Operacional, '
            f'≥ {LIMIARES_ROTULO[1]:g}% Com problemas, abaixo Instável. Intervalo sem barra = sem medição.</p>'
            f'{"".join(linhas)}<div class="row eixo"><div class="nome"></div><div class="strip ticks">{ticks}</div>'
            f'<div class="num"></div></div><div class="leg">{leg}</div>')

CSS_RESUMO = """
body{font:14px/1.5 Segoe UI,Arial,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#111}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:24px 0 6px;border-bottom:1px solid #999}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{border:1px solid #bbb;padding:3px 6px;text-align:right}
th{background:#eee}
td:first-child,th:first-child{text-align:left}
.id{margin:10px 0}
.id span{display:inline-block;min-width:22em;border-bottom:1px dashed #888;outline:none}
.id span:empty::before{content:attr(data-ph);color:#b00;font-weight:600}
.id small{color:#666;margin-left:8px}
.meta{color:#444}
.nota{font-size:12.5px;color:#333}
.card{border:1px solid #f0c8c8;border-left:5px solid #dc2626;border-radius:12px;padding:10px 16px;display:flex;
justify-content:space-between;align-items:center;background:#fff5f5;margin:8px 0}
.card.deg{border-color:#f3e0b0;border-left-color:#d97706;background:#fffaf0}
.card.ok{border-color:#bbe5c8;border-left-color:#16a34a;background:#f3fbf6}
.card .t{font-size:15px;font-weight:800}
.card .c{font:11.5px Consolas,monospace;color:#555;margin-top:2px}
.card .c b{margin-right:8px}
.badge{font:700 11.5px Consolas,monospace;padding:4px 11px;border-radius:999px;background:#fde2e2;color:#991b1b;
letter-spacing:.04em}
.card.deg .badge{background:#fdebc8;color:#92400e}
.card.ok .badge{background:#d8f3e2;color:#166534}
.row{display:grid;grid-template-columns:180px 1fr 150px;gap:10px;align-items:center;padding:4px 0;
border-bottom:1px dotted #ddd;break-inside:avoid}
.nome{display:flex;align-items:center;gap:7px;font-weight:600;font-size:12.5px}
.nome i{width:9px;height:9px;border-radius:50%;flex:none}
.num{text-align:right;line-height:1.2}
.num b{display:block;font-size:12.5px}
.num span{font-size:10.5px;color:#555}
.eixo{border:0;padding-top:0}
.ticks{display:flex;justify-content:space-between;font-size:10px;color:#555}
.leg{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;margin:8px 0 2px;align-items:center}
.leg i{display:inline-block;width:11px;height:11px;margin-right:5px;vertical-align:-1px}
body{-webkit-print-color-adjust:exact;print-color-adjust:exact}
@page{size:A4;margin:14mm}
@media print{body{margin:0;max-width:none}
.id span{border:0}.id small{display:none}
h2{break-after:avoid}
tr{break-inside:avoid}}
"""


def _resumo_html(out, cfg, ini, fim, sondas, rodadas, janelas, n_lacunas, n_rejeitadas=0):
    """Página única, imprimível, para anexar ao chamado. Só números que o log sustenta."""
    esc = html.escape
    alvos = [a for a in cfg["alvos"] if a["tipo"] != "controle"]
    aviso_log = (f" <b>{n_rejeitadas} linha(s) do log estavam ilegíveis e foram ignoradas</b> "
                 "(queda de energia ou arquivo estragado)." if n_rejeitadas else "")
    primeira = rodadas[0]["ts_local"] if rodadas else ""
    ultima = rodadas[-1]["ts_local"] if rodadas else ""
    # esperadas: da 1ª à última rodada registrada (antes da 1ª a sonda não existia)
    esperadas = (datetime.fromisoformat(ultima) - datetime.fromisoformat(primeira)).total_seconds() \
        / cfg["intervalo_normal_s"] + 1 if rodadas else 0
    linhas = []
    for a in alvos:
        s1 = [s for s in sondas if s["alvo"] == a["id"] and s["tentativa"] == 1]
        c = Counter(s["resultado"] for s in s1)
        fal1 = sum(c[k] for k in FALHAS)
        val = c["ok"] + c["lento"]
        conf = sum(1 for r in rodadas if r["alvos"].get(a["id"]) in FALHAS)
        blips = sum(1 for r in rodadas if r["alvos"].get(a["id"]) == "blip")
        lat = [s["total_ms"] for s in s1 if s["resultado"] in OKS]
        linhas.append(f"<tr><td>{esc(a['nome'])}</td><td>{len(s1)}</td>"
                      f"<td>{br(100 * val / (val + fal1)) if val + fal1 else '-'}</td><td>{fal1}</td>"
                      f"<td>{blips}</td><td>{conf}</td><td>{c['lento']}</td><td>{c['bloqueio_429']}</td>"
                      f"<td>{_pct(lat, 50) if lat else '-'}</td><td>{_pct(lat, 95) if lat else '-'}</td>"
                      f"<td>{max(lat) if lat else '-'}</td>"
                      + "".join(f"<td>{_demora(s1, x) or '-'}</td>" for x in LIMIARES_DEMORA_S)
                      + f"<td>{_disp_ponderada(s1) or '-'}</td></tr>")
    jan = "".join(f"<tr><td>{j['ini']:%d/%m/%Y %H:%M}</td><td>{j['fim']:%H:%M}</td>"
                  f"<td>{round((j['fim'] - j['ini']).total_seconds() / 60, 1)}</td>"
                  f"<td>{esc(', '.join(sorted(j['alvos'])))}</td></tr>" for j in janelas[:MAX_JANELAS_HTML]) \
        or '<tr><td colspan="4">Nenhuma janela de incidente no período.</td></tr>'
    if len(janelas) > MAX_JANELAS_HTML:
        jan += (f'<tr><td colspan="4">e mais {len(janelas) - MAX_JANELAS_HTML} janela(s): '
                'lista completa em 2_janelas_de_incidente.csv</td></tr>')
    ex = [s for s in sondas if s["resultado"] in FALHAS][:8]
    exs = "".join(f"<tr><td>{_fmt(s['ts_local'])}</td><td>{esc(s['alvo'])}</td><td>{s['tentativa']}</td>"
                  f"<td>{esc(s['detalhe'])}{' / HTTP ' + str(s['http']) if s['http'] else ''}</td>"
                  f"<td>{s['total_ms']}</td><td>{esc(s.get('pncp_ts_erro', ''))}</td>"
                  f"<td>{esc(s.get('corpo_trecho', '')[:120])}</td></tr>" for s in ex) \
        or '<tr><td colspan="7">Nenhuma falha registrada.</td></tr>'
    grafico = _grafico(sondas, alvos, cfg)
    (out / "resumo_para_chamado.html").write_text(f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Sonda PNCP - resumo</title><style>{CSS_RESUMO}</style></head><body>
<h1>Disponibilidade do PNCP - medição independente</h1>
<p class="meta">Período: {ini:%d/%m/%Y} a {fim:%d/%m/%Y} · gerado em {datetime.now():%d/%m/%Y %H:%M} ·
Sonda PNCP v{VERSAO}, host {esc(socket.gethostname())}</p>
<p class="id"><b>Solicitante:</b> <span id="solicitante" contenteditable="true" spellcheck="false"
data-ph="[clique aqui e preencha a identificação antes de anexar]"></span>
<small>(campo editável; o navegador lembra o texto nos próximos relatórios)</small></p>
<script>
(function () {{
  var el = document.getElementById("solicitante"), k = "sonda_pncp_solicitante";
  try {{ el.textContent = localStorage.getItem(k) || ""; }} catch (e) {{}}
  el.addEventListener("input", function () {{
    try {{ localStorage.setItem(k, el.textContent.trim()); }} catch (e) {{}}
  }});
}})();
</script>
<h2>Cobertura</h2>
<p>{len(rodadas)} rodadas registradas (de ~{round(esperadas)} esperadas entre a primeira e a última, a cada
{cfg['intervalo_normal_s'] // 60} min), de {_fmt(primeira) or '-'} a {_fmt(ultima) or '-'}; {n_lacunas} lacuna(s) sem registro
(PC desligado ou suspenso, sonda parada ou pausada). Ausência de registro não é contada como disponibilidade.{aviso_log}</p>
<h2>Resultado por serviço (1ª tentativa de cada medição)</h2>
<table><tr><th>Serviço</th><th>Medições</th><th>Disp. %</th><th>Falhas</th><th>Recuperadas na 2ª tentativa</th>
<th>Falhas confirmadas</th><th>Lentas</th><th>HTTP 429</th><th>p50 ms</th><th>p95 ms</th><th>Máx ms</th>
{''.join(f'<th>Demora &gt; {x} s %</th>' for x in LIMIARES_DEMORA_S)}<th>Disp. ponderada por tempo %</th></tr>
{''.join(linhas)}</table>
<p class="nota">Falha = erro HTTP, erro de rede, tempo esgotado ({cfg['timeout_total_s']} s) ou corpo inválido.
Falha repete após {cfg['retry_apos_falha_s']} s; "confirmada" = falhou também na 2ª tentativa. Disponibilidade =
(ok + lentas) / (ok + lentas + falhas); HTTP 429 fica fora (limitação, não queda). Latências (p50/p95/máx) só de
respostas válidas. Disp. ponderada por tempo pesa cada medição pelo tempo até a próxima (não por contagem);
lacuna acima de 15 min não conta pra nenhum lado.
"Demora &gt; X s" = parcela das medições (respostas válidas + tempos esgotados) que levaram mais de X segundos:
mostra o serviço que "responde, mas tarde", que a disponibilidade sozinha esconde.
Controles (Google, Cloudflare) medidos a cada rodada; se ambos caem, a rodada é descartada como "sem rede local".</p>
<h2>Estado e latência ao longo do tempo</h2>
{grafico}
<h2>Janelas de incidente</h2>
<table><tr><th>Início</th><th>Fim</th><th>Duração (min)</th><th>Serviços afetados</th></tr>{jan}</table>
<h2>Exemplos de falha (até 8; todas nos CSVs anexos)</h2>
<table><tr><th>Horário local</th><th>Serviço</th><th>Tent.</th><th>Detalhe</th><th>ms</th>
<th>Horário do erro (PNCP)</th><th>Trecho da resposta</th></tr>{exs}</table>
<h2>Método</h2>
<p class="nota">Medição com curl.exe a cada {cfg['intervalo_normal_s'] // 60} min (a cada
{cfg['intervalo_incidente_s']} s durante incidente), de um único ponto de observação, com horário local e UTC em
cada registro. Consultas: página inicial do portal e 5 endpoints públicos da API, com 10 registros por página.
Logs brutos (JSONL) disponíveis sob solicitação.</p>
</body></html>""", encoding="utf-8")
