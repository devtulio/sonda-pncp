"""Sonda PNCP - núcleo: medição, classificação, log diário, estado e relatório.

Sem interface: a bandeja fica em `sonda_pncp.pyw`. Tudo aqui é testável sem Windows
além do `curl.exe` (que a medição usa para obter DNS/TCP/TLS/TTFB e o código de erro
de rede de graça).
"""

import csv
import gzip
import hashlib
import html
import json
import math
import os
import re
import shutil
import socket
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, UTC
from email.utils import parsedate_to_datetime
from pathlib import Path

VERSAO = "1.3.0"
FALHAS = {"erro_http", "erro_rede", "timeout", "corpo_invalido"}  # falha do lado do alvo
OKS = {"ok", "lento"}  # resposta válida (lento = válida, porém acima do limiar)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # armadilha: sem isso pisca janela

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0 Safari/537.36 SondaPNCP/1.0")
CNPJ_TESTE = "83102277000152"
_API = "https://pncp.gov.br/api/consulta/v1"
_PNCP = "https://pncp.gov.br/api/pncp/v1"

# Limiares de "lento" iniciais são generosos: a linha de base é desconhecida.
# Recalibrar pelo p95 do relatório após ~7 dias de dados.
ALVOS_PADRAO = [
    {"id": "ctrl_google", "nome": "Controle: Google", "tipo": "controle", "validar": "status",
     "url": "https://www.google.com/generate_204", "limiar_lento_ms": 4000, "accept": "*/*"},
    {"id": "ctrl_cloudflare", "nome": "Controle: Cloudflare", "tipo": "controle", "validar": "status",
     "url": "https://www.cloudflare.com/cdn-cgi/trace", "limiar_lento_ms": 4000, "accept": "*/*"},
    {"id": "portal", "nome": "Portal PNCP", "tipo": "portal", "validar": "html",
     "url": "https://pncp.gov.br/app/", "limiar_lento_ms": 4000, "accept": "text/html"},
    {"id": "api_contratacoes", "nome": "API consulta: contratações", "tipo": "api", "validar": "json_data",
     "url": f"{_API}/contratacoes/publicacao?dataInicial={{ontem}}&dataFinal={{hoje}}"
            "&codigoModalidadeContratacao=6&pagina=1&tamanhoPagina=10", "limiar_lento_ms": 5000},
    {"id": "api_contratos", "nome": "API consulta: contratos", "tipo": "api", "validar": "json_data",
     "url": f"{_API}/contratos/atualizacao?dataInicial={{d30}}&dataFinal={{hoje}}"
            f"&cnpjOrgao={CNPJ_TESTE}&pagina=1&tamanhoPagina=10", "limiar_lento_ms": 12000},
    {"id": "api_atas", "nome": "API consulta: atas", "tipo": "api", "validar": "json_data",
     "url": f"{_API}/atas/atualizacao?dataInicial={{d30}}&dataFinal={{hoje}}"
            f"&cnpj={CNPJ_TESTE}&pagina=1&tamanhoPagina=10", "limiar_lento_ms": 6000},
    {"id": "api_pca", "nome": "API consulta: PCA", "tipo": "api", "validar": "json_data",
     "url": f"{_API}/pca/atualizacao?dataInicio={{ini_ano}}&dataFim={{hoje}}"
            f"&cnpj={CNPJ_TESTE}&pagina=1&tamanhoPagina=10", "limiar_lento_ms": 20000},
    {"id": "api_itens", "nome": "API pncp: itens da compra", "tipo": "api", "validar": "json_lista",
     "url": f"{_PNCP}/orgaos/{CNPJ_TESTE}/compras/2026/495/itens?pagina=1&tamanhoPagina=10",
     "limiar_lento_ms": 20000, "ausencia_404": "Contratação não cadastrada"},
]

CONFIG_PADRAO = {
    "intervalo_normal_s": 300,
    "intervalo_incidente_s": 60,
    "espera_entre_alvos_s": 1.5,
    "timeout_conexao_s": 10,
    "timeout_total_s": 30,
    "retry_apos_falha_s": 10,
    "rodadas_sem_falha_para_sair_incidente": 3,
    "falhas_seguidas_para_vermelho": 2,
    "limite_lacuna_x_intervalo": 2.5,
    "retencao_compactar_dias": 30,
    "notificar": True,
    "iniciar_com_windows": True,
    "porta_instancia": 48650,
    "user_agent": UA,
    "alvos": ALVOS_PADRAO,
}

CURL_ERROS = {6: "dns", 7: "conexao_recusada", 18: "resposta_parcial", 28: "timeout", 35: "tls",
              51: "certificado", 52: "resposta_vazia", 55: "envio_falhou", 56: "conexao_derrubada",
              60: "certificado"}


# ───────────────────────── configuração ─────────────────────────

def carregar_config(pasta):
    """Lê `config.json`; se não existir, grava os padrões. Chaves ausentes usam o padrão."""
    arq = Path(pasta) / "config.json"
    cfg = json.loads(json.dumps(CONFIG_PADRAO))
    if arq.exists():
        cfg.update(json.loads(arq.read_text(encoding="utf-8")))
    else:
        arq.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def salvar_config_chave(pasta, chave, valor):
    arq = Path(pasta) / "config.json"
    cfg = json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else json.loads(json.dumps(CONFIG_PADRAO))
    cfg[chave] = valor
    arq.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ───────────────────────── medição ─────────────────────────

def expandir_url(url, agora=None):
    a = (agora or datetime.now()).astimezone()
    return url.format(hoje=a.strftime("%Y%m%d"),
                      ontem=(a - timedelta(days=1)).strftime("%Y%m%d"),
                      d7=(a - timedelta(days=7)).strftime("%Y%m%d"),
                      d30=(a - timedelta(days=30)).strftime("%Y%m%d"),
                      ini_ano=f"{a.year}0101")


def _ms(segundos):
    return round(float(segundos) * 1000) if segundos else 0


def _parse_cabecalhos(texto):
    blocos = [b for b in re.split(r"\r?\n\r?\n", texto.strip()) if b.strip().upper().startswith("HTTP/")]
    if not blocos:
        return {}
    d = {}
    for linha in blocos[-1].splitlines()[1:]:
        if ":" in linha:
            k, v = linha.split(":", 1)
            d[k.strip().lower()] = v.strip()
    return d


def medir(alvo, cfg):
    """Uma medição via `curl.exe`. Devolve o dicionário bruto (ainda sem classificar)."""
    url = expandir_url(alvo["url"])
    tmp = tempfile.mkdtemp(prefix="sonda_")
    corpo_p, cab_p = os.path.join(tmp, "corpo"), os.path.join(tmp, "cab")
    curl = shutil.which("curl.exe") or r"C:\Windows\System32\curl.exe"
    cmd = [curl, "-sS", "-L", "--max-redirs", "3",
           "--connect-timeout", str(cfg["timeout_conexao_s"]), "--max-time", str(cfg["timeout_total_s"]),
           "-A", cfg["user_agent"], "-H", f"Accept: {alvo.get('accept', 'application/json')}",
           "--compressed", "-o", corpo_p, "-D", cab_p, "-w", "%{json}", url]
    m = {"url": url, "curl_exit": -1, "curl_erro": "", "http": 0, "http_versao": "", "ip": "",
         "dns_ms": 0, "tcp_ms": 0, "tls_ms": 0, "ttfb_ms": 0, "total_ms": 0, "bytes": 0,
         "_conectou": False, "_corpo": b"", "_cab": {}, "_inicio": datetime.now(UTC)}
    try:
        # sem shell; argv montado pela sonda, a URL vem do config.json do próprio usuário
        p = subprocess.run(cmd,  # nosec B603
                           capture_output=True, timeout=cfg["timeout_total_s"] + 10,
                           creationflags=CREATE_NO_WINDOW)
        j = {}
        try:
            j = json.loads(p.stdout.decode("utf-8", "replace"))
        except ValueError:
            pass
        stderr = p.stderr.decode("utf-8", "replace").strip()
        dns, con = j.get("time_namelookup") or 0, j.get("time_connect") or 0
        app = j.get("time_appconnect") or 0
        m.update(curl_exit=j.get("exitcode", p.returncode), curl_erro=(j.get("errormsg") or stderr)[:200],
                 http=int(j.get("response_code") or 0), http_versao=str(j.get("http_version", "")),
                 ip=j.get("remote_ip", ""), dns_ms=_ms(dns), tcp_ms=_ms(con - dns) if con else 0,
                 tls_ms=_ms(app - con) if app else 0, ttfb_ms=_ms(j.get("time_starttransfer")),
                 total_ms=_ms(j.get("time_total")), bytes=int(j.get("size_download") or 0),
                 _conectou=bool(con))
    except subprocess.TimeoutExpired:
        m.update(curl_exit=28, curl_erro="curl.exe excedeu o tempo (guarda da sonda)",
                 total_ms=(cfg["timeout_total_s"] + 10) * 1000)
    except OSError as e:  # curl.exe ausente
        m.update(curl_exit=-2, curl_erro=str(e)[:200])
    m["_fim"] = datetime.now(UTC)
    try:
        with open(corpo_p, "rb") as f:
            m["_corpo"] = f.read(2_000_000)
        with open(cab_p, encoding="utf-8", errors="replace") as f:
            m["_cab"] = _parse_cabecalhos(f.read())
    except OSError:
        pass
    shutil.rmtree(tmp, ignore_errors=True)
    return m


def validar(tipo, corpo):
    if tipo == "html":
        return b"<html" in corpo[:4000].lower() and len(corpo) > 2000
    if tipo in ("json_data", "json_lista"):
        try:
            j = json.loads(corpo.decode("utf-8"))
        except ValueError:
            return False
        if tipo == "json_data":
            return isinstance(j, dict) and isinstance(j.get("data"), list)
        return isinstance(j, list)
    return True


def classificar(m, alvo, cfg):
    """→ (resultado, detalhe). Resultados: ok, lento, erro_http, erro_rede, timeout,
    corpo_invalido, bloqueio_429, registro_ausente."""
    if m["curl_exit"] != 0:
        nome = CURL_ERROS.get(m["curl_exit"], f"curl_{m['curl_exit']}")
        if m["curl_exit"] == 28:
            return "timeout", "timeout_resposta" if m["_conectou"] else "timeout_conexao"
        return "erro_rede", nome
    h = m["http"]
    if h == 429:
        return "bloqueio_429", "http_429"
    marca = alvo.get("ausencia_404")  # registro fixo de teste que sumiu: o PNCP responde 404 explícito
    if h == 404 and marca and marca.encode() in m["_corpo"]:
        return "registro_ausente", "http_404_registro_ausente"
    if not 200 <= h < 300:
        return "erro_http", f"http_{h}"
    if alvo["tipo"] != "controle" and not validar(alvo.get("validar", "status"), m["_corpo"]):
        return "corpo_invalido", "corpo_vazio" if not m["_corpo"] else "corpo_inesperado"
    return ("lento" if m["total_ms"] > alvo.get("limiar_lento_ms", 5000) else "ok"), ""


# ───────────────────────── log ─────────────────────────

class Log:
    """Um arquivo JSONL por dia (data LOCAL do registro → vira sozinho à meia-noite)."""

    def __init__(self, pasta):
        self.dir = Path(pasta) / "logs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def escrever(self, rec):
        arq = self.dir / f"sonda-{rec['ts_local'][:10]}.jsonl"
        linha = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, open(arq, "a", encoding="utf-8") as f:
            f.write(linha)  # fecha a cada linha: queda de energia perde no máximo uma


def ler_registros(pasta, ini, fim):
    """Itera os registros dos dias [ini, fim] (datas), inclusive arquivos .gz."""
    d = ini
    while d <= fim:
        for nome, abrir in ((f"sonda-{d}.jsonl", open), (f"sonda-{d}.jsonl.gz", gzip.open)):
            arq = Path(pasta) / "logs" / nome
            if arq.exists():
                with abrir(arq, "rt", encoding="utf-8") as f:
                    for linha in f:
                        try:
                            yield json.loads(linha)
                        except ValueError:
                            continue  # linha truncada (queda de energia)
        d += timedelta(days=1)


def ultimo_registro(pasta):
    for arq in sorted((Path(pasta) / "logs").glob("sonda-*.jsonl"), reverse=True):
        linhas = [x for x in arq.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
        for linha in reversed(linhas):
            try:
                return json.loads(linha)
            except ValueError:
                continue
    return None


def compactar_antigos(pasta, dias):
    """Compacta em .gz os logs mais velhos que `dias` (mantém tudo, só ocupa menos)."""
    corte = datetime.now().date() - timedelta(days=dias)
    n = 0
    for arq in (Path(pasta) / "logs").glob("sonda-*.jsonl"):
        try:
            dia = datetime.strptime(arq.stem[6:], "%Y-%m-%d").date()
        except ValueError:
            continue
        if dia < corte:
            with open(arq, "rb") as f, gzip.open(str(arq) + ".gz", "wb") as g:
                shutil.copyfileobj(f, g)
            arq.unlink()
            n += 1
    return n


# ───────────────────────── a sonda ─────────────────────────

class Sonda:
    def __init__(self, pasta, cfg=None, medir_fn=None, agora_fn=None, dormir_fn=None, notificar_fn=None):
        self.pasta = Path(pasta)
        self.cfg = cfg or carregar_config(pasta)
        self.log = Log(pasta)
        self.medir = medir_fn or medir
        self.agora = agora_fn or (lambda: datetime.now(UTC))
        self.parar = threading.Event()
        self.disparar = threading.Event()
        self.dormir = dormir_fn or (lambda s: self.parar.wait(s))
        self.notificar = notificar_fn or (lambda titulo, msg: None)
        self.ao_mudar = lambda: None  # a bandeja pluga aqui para redesenhar ícone/tooltip
        self.cor = "cinza"
        self.streak = 0
        self.incidente = False
        self.sem_falha = 0
        self.pausada = False
        self.houve_pausa = False
        self.n_rodada = 0
        self.ultima = None
        self.ultimo_wall = None
        self.proximo_esperado_s = self.cfg["intervalo_normal_s"]
        self.ips = {}
        self.ausentes = set()
        self._r24 = deque()
        self._dia_compactado = None

    # -- registros --
    def _ts(self, a=None):
        a = a or self.agora()
        return {"ts_local": a.astimezone().isoformat(timespec="milliseconds"),
                "ts_utc": a.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")}

    def evento(self, nome, **campos):
        self.log.escrever({"tipo": "evento", **self._ts(), "evento": nome, **campos})

    def registrar_erro(self, exc):
        tb = "".join(traceback.format_exception(exc))[-1500:]
        self.evento("erro_interno", erro=str(exc)[:300], traceback=tb)
        with open(self.pasta / "logs" / "sonda-erros.log", "a", encoding="utf-8") as f:
            f.write(f"{self._ts()['ts_local']}\n{tb}\n")

    def _registro_sonda(self, rid, alvo, tentativa, m, resultado, detalhe):
        a = self._ts(m["_fim"])
        cab = m["_cab"]
        rec = {"tipo": "sonda", **a, "rodada": rid, "alvo": alvo["id"], "categoria": alvo["tipo"],
               "url": m["url"], "tentativa": tentativa, "resultado": resultado, "detalhe": detalhe,
               "http": m["http"], "http_versao": m["http_versao"], "ip": m["ip"],
               "curl_exit": m["curl_exit"], "curl_erro": m["curl_erro"],
               "dns_ms": m["dns_ms"], "tcp_ms": m["tcp_ms"], "tls_ms": m["tls_ms"],
               "ttfb_ms": m["ttfb_ms"], "total_ms": m["total_ms"], "bytes": m["bytes"],
               "cabecalhos": {k: cab[k] for k in ("date", "server", "via", "retry-after", "content-type",
                                                  "content-length") if k in cab}}
        if cab.get("date"):
            try:  # estimativa: relógio local no meio da requisição menos o Date do servidor (±latência)
                meio = m["_inicio"] + (m["_fim"] - m["_inicio"]) / 2
                rec["desvio_relogio_s"] = round((meio - parsedate_to_datetime(cab["date"])).total_seconds(), 1)
            except (TypeError, ValueError):
                pass
        if resultado in FALHAS or resultado == "bloqueio_429":
            trecho = m["_corpo"][:400].decode("utf-8", "replace")
            rec["corpo_trecho"] = trecho
            rec["cabecalhos_completos"] = {k: v for k, v in cab.items()
                                           if k not in ("set-cookie", "cookie", "authorization")}
            ts_pncp = re.search(r'"timestamp"\s*:\s*"([^"]+)"', m["_corpo"][:2000].decode("utf-8", "replace"))
            if ts_pncp:
                rec["pncp_ts_erro"] = ts_pncp.group(1)  # horário do erro segundo o próprio PNCP
        elif m["_corpo"]:
            rec["corpo_sha256"] = hashlib.sha256(m["_corpo"]).hexdigest()[:16]
        return rec

    def _medir_alvo(self, alvo, rid, tentativa):
        m = self.medir(alvo, self.cfg)
        resultado, detalhe = classificar(m, alvo, self.cfg)
        self.log.escrever(self._registro_sonda(rid, alvo, tentativa, m, resultado, detalhe))
        if m["ip"] and self.ips.get(alvo["id"]) not in (None, m["ip"]):
            self.evento("mudanca_ip", alvo=alvo["id"], de=self.ips[alvo["id"]], para=m["ip"])
        if m["ip"]:
            self.ips[alvo["id"]] = m["ip"]
        if resultado == "registro_ausente" and alvo["id"] not in self.ausentes:
            self.ausentes.add(alvo["id"])  # avisa uma vez; não é queda do PNCP, é o teste que precisa de novo registro
            self.evento("registro_ausente", alvo=alvo["id"], url=m["url"])
            if self.cfg["notificar"]:
                self.notificar("Sonda PNCP: registro de teste sumiu",
                               f"{alvo['nome']}: trocar o registro fixo no config.json (não conta como falha).")
        elif resultado != "registro_ausente":
            self.ausentes.discard(alvo["id"])
        return resultado

    # -- ciclo de vida --
    def iniciar(self):
        ult = ultimo_registro(self.pasta)
        campos = {"versao": VERSAO, "host": socket.gethostname(), "python": sys.version.split()[0],
                  "intervalo_normal_s": self.cfg["intervalo_normal_s"]}
        if ult:
            campos["ultimo_registro_anterior"] = ult.get("ts_local")
            try:
                gap = (self.agora() - datetime.fromisoformat(ult["ts_utc"])).total_seconds()
                campos["gap_desde_anterior_s"] = round(gap)
            except (KeyError, ValueError):
                pass
            campos["encerramento_anterior_limpo"] = ult.get("evento") == "sonda_encerrada"
        self.evento("sonda_iniciada", **campos)
        self._carregar_24h()
        compactar_antigos(self.pasta, self.cfg["retencao_compactar_dias"])

    def encerrar(self, motivo):
        self.evento("sonda_encerrada", motivo=motivo, rodadas=self.n_rodada)
        self.parar.set()
        self.disparar.set()

    def pausar(self, sim):
        self.pausada = sim
        if sim:
            self.houve_pausa = True
        self.evento("pausada" if sim else "retomada_manual")
        self.ao_mudar()

    def _carregar_24h(self):
        agora = self.agora()
        hoje = agora.astimezone().date()
        for r in ler_registros(self.pasta, hoje - timedelta(days=1), hoje):
            if r.get("tipo") == "rodada":
                t = datetime.fromisoformat(r["ts_utc"])
                if t >= agora - timedelta(hours=24):
                    self._r24.append((t, r["estado"]))

    def disponibilidade_24h(self):
        corte = self.agora() - timedelta(hours=24)
        while self._r24 and self._r24[0][0] < corte:
            self._r24.popleft()
        validas = [e for _, e in self._r24 if e in ("ok", "degradado", "falha")]
        return None if not validas else 100.0 * sum(e != "falha" for e in validas) / len(validas)

    def _checar_lacuna(self, agora):
        if self.ultimo_wall is None:
            return
        gap = (agora - self.ultimo_wall).total_seconds()
        esperado = self.proximo_esperado_s
        if gap > esperado * self.cfg["limite_lacuna_x_intervalo"] + self.cfg["timeout_total_s"]:
            motivo = "pausa_manual" if self.houve_pausa else "suspensao_ou_indisponibilidade"
            self.evento("retomada_apos_lacuna", lacuna_s=round(gap - esperado),
                        desde=self._ts(self.ultimo_wall)["ts_local"], motivo=motivo)
        self.houve_pausa = False

    # -- uma rodada --
    def rodada(self):
        t0 = time.monotonic()
        agora = self.agora()
        self.n_rodada += 1
        rid = f"{agora.astimezone():%Y%m%dT%H%M%S}-{self.n_rodada}"
        self._checar_lacuna(agora)
        alvos = self.cfg["alvos"]
        controles = [a for a in alvos if a["tipo"] == "controle"]
        pncp = [a for a in alvos if a["tipo"] != "controle"]
        res_ctrl = []
        for a in controles:
            res_ctrl.append(self._medir_alvo(a, rid, 1))
            self.dormir(self.cfg["espera_entre_alvos_s"])
        rede_ok = not controles or any(r in OKS for r in res_ctrl)
        finais = {}
        interrompida = False
        if rede_ok:
            for a in pncp:
                if self.parar.is_set():
                    interrompida = True
                    break
                r = self._medir_alvo(a, rid, 1)
                final = r
                if r in FALHAS:  # o primeiro erro fica registrado; a 2ª tentativa separa soluço de falha
                    self.dormir(self.cfg["retry_apos_falha_s"])
                    r2 = self._medir_alvo(a, rid, 2)
                    final = "blip" if r2 in OKS else r2
                finais[a["id"]] = final
                self.dormir(self.cfg["espera_entre_alvos_s"])
        if interrompida:  # encerrando no meio: resumo parcial enganaria e cairia depois do "sonda_encerrada"
            return None
        falhas = sorted(k for k, v in finais.items() if v in FALHAS)
        blips = sorted(k for k, v in finais.items() if v == "blip")
        lentos = sorted(k for k, v in finais.items() if v == "lento")
        bloqueios = sorted(k for k, v in finais.items() if v == "bloqueio_429")
        ausentes = sorted(k for k, v in finais.items() if v == "registro_ausente")
        if not rede_ok:
            estado = "sem_rede"
        elif falhas:
            estado = "falha"
        elif blips or lentos or bloqueios or ausentes:
            estado = "degradado"
        else:
            estado = "ok"
        res = self._atualizar(estado, falhas)
        fim = self.agora()
        rec = {"tipo": "rodada", **self._ts(agora), "rodada": rid, "estado": estado, "cor": self.cor,
               "rede_local_ok": rede_ok, "alvos": finais, "falhas": falhas, "blips": blips,
               "lentos": lentos, "bloqueios_429": bloqueios,
               "registros_ausentes": ausentes,
               "modo": "incidente" if self.incidente else "normal", "streak_falha": self.streak,
               "duracao_ms": round((time.monotonic() - t0) * 1000), "proxima_em_s": res}
        self.log.escrever(rec)
        self._r24.append((agora.astimezone(UTC), estado))
        self.ultima = rec
        self.ultimo_wall = fim
        self.proximo_esperado_s = res
        dia = agora.astimezone().date()
        if self._dia_compactado != dia:
            self._dia_compactado = dia
            compactar_antigos(self.pasta, self.cfg["retencao_compactar_dias"])
        self.ao_mudar()
        return rec

    def _atualizar(self, estado, falhas):
        cfg = self.cfg
        if estado == "falha":
            self.streak += 1
            self.sem_falha = 0
            self.incidente = True
        elif estado == "sem_rede":
            self.streak = 0
        else:
            self.streak = 0
            if self.incidente:
                self.sem_falha += 1
                if self.sem_falha >= cfg["rodadas_sem_falha_para_sair_incidente"]:
                    self.incidente = False
                    self.sem_falha = 0
        cor = {"ok": "verde", "degradado": "amarelo", "sem_rede": "cinza",
               "falha": "vermelho" if self.streak >= cfg["falhas_seguidas_para_vermelho"] else "amarelo"}[estado]
        if cor != self.cor:
            self.evento("mudanca_estado", de=self.cor, para=cor, estado_rodada=estado, falhas=falhas)
            if cfg["notificar"]:
                if cor == "vermelho":
                    self.notificar("Sonda PNCP: falha confirmada", "Falha em: " + ", ".join(falhas))
                elif self.cor == "vermelho" and cor in ("verde", "amarelo"):
                    self.notificar("Sonda PNCP: PNCP recuperou", "As consultas voltaram a responder.")
            self.cor = cor
        # sem rede: rechecar cedo (só 2 requisições); falha: modo incidente (1 min); senão, normal
        return cfg["intervalo_incidente_s"] if (self.incidente or estado == "sem_rede") \
            else cfg["intervalo_normal_s"]

    # -- laço (roda em thread) --
    def laco(self):
        while not self.parar.is_set():
            if self.pausada:
                self.disparar.wait(2)
                self.disparar.clear()
                continue
            try:
                rec = self.rodada()
                if rec is None:
                    break
                espera = rec["proxima_em_s"]
            except Exception as e:  # noqa: BLE001 - a sonda nunca pode morrer calada
                self.registrar_erro(e)
                espera = self.cfg["intervalo_normal_s"]
            self.disparar.wait(espera)
            self.disparar.clear()

    # -- textos da bandeja --
    TXT = {"verde": "OK", "amarelo": "atenção", "vermelho": "FALHA", "cinza": "sem rede local"}

    def texto_status(self):
        if self.pausada:
            return "Sonda PNCP - pausada"
        if not self.ultima:
            return "Sonda PNCP - aguardando 1ª verificação"
        hora = datetime.fromisoformat(self.ultima["ts_local"]).strftime("%H:%M")
        extra = f" ({', '.join(self.ultima['falhas'])})" if self.ultima["falhas"] else ""
        return f"{self.TXT[self.cor]} - última {hora}{extra}"

    def tooltip(self):
        d = self.disponibilidade_24h()
        disp = "n/d" if d is None else f"{d:.1f}%".replace(".", ",")
        if self.pausada:
            return f"Sonda PNCP - pausada · 24 h: {disp}"[:127]
        hora = "--:--" if not self.ultima else datetime.fromisoformat(self.ultima["ts_local"]).strftime("%H:%M")
        return f"Sonda PNCP - {self.TXT[self.cor]} · última {hora} · 24 h: {disp}"[:127]


# ───────────────────────── relatório ─────────────────────────

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


def _fmt(iso):
    return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M:%S") if iso else ""


def _csv(caminho, cabecalho, linhas):
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:  # ';' e BOM: Excel em português
        w = csv.writer(f, delimiter=";")
        w.writerow(cabecalho)
        w.writerows(linhas)


MARCAS_PNCP = (("banco de dados", "Erro na comunicação com o banco de dados"),
               ("JDBC", "Failed to obtain JDBC Connection"), ("Bad gateway", "Bad gateway"))


def gerar_relatorio(pasta, dias=7, agora=None):
    """Gera 5 CSVs + resumo HTML em `relatorios/relatorio-AAAAMMDD/` (sobrescreve o do dia) e devolve a pasta."""
    cfg = carregar_config(pasta)
    agora = (agora or datetime.now()).astimezone()
    ini, fim = (agora - timedelta(days=dias - 1)).date(), agora.date()
    regs = list(ler_registros(pasta, ini, fim))
    sondas = [r for r in regs if r["tipo"] == "sonda"]
    rodadas = sorted((r for r in regs if r["tipo"] == "rodada"), key=lambda r: r["ts_utc"])
    eventos = [r for r in regs if r["tipo"] == "evento"]
    out = Path(pasta) / "relatorios" / f"relatorio-{agora:%Y%m%d}"  # 1 por dia: gerar de novo sobrescreve
    out.mkdir(parents=True, exist_ok=True)

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
                       *(_demora(lst, x) for x in LIMIARES_DEMORA_S)])
    _csv(out / "1_resumo_diario.csv", ["Data", "Alvo", "Categoria", "Sondas", "OK", "Lentas", "Falhas",
                                       "Bloqueios 429", "Disponibilidade %", "Latência p50 (ms)",
                                       "Latência p95 (ms)", "Falhas por tipo",
                                       *(f"Demora > {x} s %" for x in LIMIARES_DEMORA_S)], linhas)

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
    esperadas_dia = 86400 / cfg["intervalo_normal_s"]
    por_dia = Counter(r["ts_local"][:10] for r in rodadas)
    linhas = []
    d = ini
    while d <= fim:
        esp = esperadas_dia
        if d == agora.date():
            esp = max(1, (agora - agora.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
                      / cfg["intervalo_normal_s"])
        n = por_dia.get(str(d), 0)
        linhas.append([d.strftime("%d/%m/%Y"), n, round(esp), f"{min(100, 100 * n / esp):.1f}".replace(".", ",")])
        d += timedelta(days=1)
    _csv(out / "4_cobertura_diaria.csv", ["Data", "Rodadas registradas", "Rodadas esperadas", "Cobertura %"], linhas)
    linhas = []
    limite = timedelta(seconds=cfg["intervalo_normal_s"] * cfg["limite_lacuna_x_intervalo"] + cfg["timeout_total_s"])
    for a, b in zip(rodadas, rodadas[1:], strict=False):
        ta, tb = datetime.fromisoformat(a["ts_utc"]), datetime.fromisoformat(b["ts_utc"])
        if tb - ta > limite:
            ev = [e for e in eventos if e["evento"] in ("retomada_apos_lacuna", "sonda_iniciada", "pausada")
                  and ta <= datetime.fromisoformat(e["ts_utc"]) <= tb]
            motivo = ev[-1].get("motivo") or ev[-1]["evento"] if ev else "sem registro (PC desligado/suspenso?)"
            linhas.append([_fmt(a["ts_local"]), _fmt(b["ts_local"]), round((tb - ta).total_seconds() / 60, 1), motivo])
    _csv(out / "5_lacunas.csv", ["Último registro antes", "Primeiro registro depois", "Duração (min)", "Motivo"], linhas)
    _resumo_html(out, cfg, ini, fim, sondas, rodadas, jan, len(linhas))
    return out


def br(x, casas=2):
    return f"{x:.{casas}f}".replace(".", ",")


LIMIARES_ROTULO = (99.0, 95.0)  # disponibilidade % do período: >= 99 Operacional, >= 95 Com problemas, senão Instável
ROTULOS = {"ok": "Operacional", "lento": "Com problemas", "falha": "Instável", "vazio": "Sem dados"}
COR_BARRA = {"ok": "#16a34a", "lento": "#d97706", "falha": "#dc2626", "429": "#64748b", "vazio": "#94a3b8"}
NOME_BARRA = {"ok": "ok", "lento": "lenta", "falha": "falha", "429": "HTTP 429"}
MAX_BARRAS = 300
MAX_JANELAS_HTML = 12  # a lista completa fica no CSV; o HTML precisa caber no A4


def _granularidade(span_s, base_s):
    """Menor largura de barra (5 min → 1 h → 6 h → 1 dia) que mantém o gráfico com até MAX_BARRAS barras."""
    for tam, nome in ((base_s, f"{base_s // 60} min"), (3600, "1 hora"), (6 * 3600, "6 horas")):
        if span_s / tam <= MAX_BARRAS:
            return tam, nome
    return 86400, "1 dia"


def _cor_do_balde(n, nf, nl, n429):
    """Cor de um intervalo: vermelho se >= 25% falharam; âmbar se houve falha ou >= 25% lentas; cinza se só 429."""
    if nf / n >= 0.25:
        return "falha"
    if nf or nl / n >= 0.25:
        return "lento"
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
        lat = [teto_s if r in FALHAS else ms / 1000 for r, ms in g if r != "bloqueio_429"]
        out[i] = (_cor_do_balde(len(g), nf, nl, n429), _pct(lat, 95) if lat else 0, len(g), nf, nl)
    return out


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
        for i, (cor, p95s, n, nf, nl) in sorted(_baldes(med, t0, tam, teto).items()):
            h = 1.0 if cor == "falha" else max(0.08, math.sqrt(min(p95s, teto) / teto))
            ini = datetime.fromtimestamp(t0 + i * tam)
            quando = f"{ini:%d/%m}" if fmt_dia else f"{ini:%d/%m %H:%M}"
            dica = (f"{quando} — {NOME_BARRA[cor]} — {br(p95s, 1)} s" if n == 1 else
                    f"{quando} — {n} medições: {nf} falha(s), {nl} lenta(s) — p95 {br(p95s, 1)} s")
            fill = "url(#hach)" if cor == "falha" else COR_BARRA[cor]
            barras.append(f'<rect x="{i * bw:.2f}" y="{alt * (1 - h):.2f}" width="{bw * 0.8:.2f}" height="{alt * h:.2f}" '
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
                  for k, n in (("ok", "ok"), ("lento", "lenta (acima do limiar)"), ("429", "HTTP 429 (limitação)")))
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


def _resumo_html(out, cfg, ini, fim, sondas, rodadas, janelas, n_lacunas):
    """Página única, imprimível, para anexar ao chamado. Só números que o log sustenta."""
    esc = html.escape
    alvos = [a for a in cfg["alvos"] if a["tipo"] != "controle"]
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
                      + "".join(f"<td>{_demora(s1, x) or '-'}</td>" for x in LIMIARES_DEMORA_S) + "</tr>")
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
{cfg['intervalo_normal_s'] // 60} min), de {_fmt(primeira) or '-'} a {_fmt(ultima) or '-'}; {n_lacunas} lacuna(s) por PC
desligado ou suspenso. Ausência de registro não é contada como disponibilidade.</p>
<h2>Resultado por serviço (1ª tentativa de cada medição)</h2>
<table><tr><th>Serviço</th><th>Medições</th><th>Disp. %</th><th>Falhas</th><th>Recuperadas na 2ª tentativa</th>
<th>Falhas confirmadas</th><th>Lentas</th><th>HTTP 429</th><th>p50 ms</th><th>p95 ms</th><th>Máx ms</th>
{''.join(f'<th>Demora &gt; {x} s %</th>' for x in LIMIARES_DEMORA_S)}</tr>
{''.join(linhas)}</table>
<p class="nota">Falha = erro HTTP, erro de rede, tempo esgotado ({cfg['timeout_total_s']} s) ou corpo inválido.
Falha repete após {cfg['retry_apos_falha_s']} s; "confirmada" = falhou também na 2ª tentativa. Disponibilidade =
(ok + lentas) / (ok + lentas + falhas); HTTP 429 fica fora (limitação, não queda). Latências (p50/p95/máx) só de
respostas válidas.
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
