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

VERSAO = "1.4.1"
FALHAS = {"erro_http", "erro_rede", "timeout", "corpo_invalido"}  # falha do lado do alvo
OKS = {"ok", "lento"}  # resposta válida (lento = válida, porém acima do limiar)
LIMITE_DESVIO_MS = 2000  # `desvio_relogio_s` só é gravado com resposta abaixo disto (ver _registro_sonda)
FOLGA_VIGIA_S = 900  # rodada mais lenta possível (~8 min com tudo em timeout) + margem, antes do vigia acusar "parada"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # armadilha: sem isso pisca janela

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0 Safari/537.36 SondaPNCP/1.0")
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
            f"&cnpjOrgao={{cnpj}}&pagina=1&tamanhoPagina=10", "limiar_lento_ms": 12000},
    {"id": "api_atas", "nome": "API consulta: atas", "tipo": "api", "validar": "json_data",
     "url": f"{_API}/atas/atualizacao?dataInicial={{d30}}&dataFinal={{hoje}}"
            "&cnpj={cnpj}&pagina=1&tamanhoPagina=10", "limiar_lento_ms": 6000},
    {"id": "api_pca", "nome": "API consulta: PCA", "tipo": "api", "validar": "json_data",
     "url": f"{_API}/pca/atualizacao?dataInicio={{ini_ano}}&dataFim={{hoje}}"
            "&cnpj={cnpj}&pagina=1&tamanhoPagina=10", "limiar_lento_ms": 20000},
    {"id": "api_itens", "nome": "API pncp: itens da compra", "tipo": "api", "validar": "json_lista",
     "url": f"{_PNCP}/orgaos/{{cnpj}}/compras/{{ano_compra}}/{{seq_compra}}/itens?pagina=1&tamanhoPagina=10",
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
    "cnpj_teste": "83102277000152",  # órgão usado nas consultas de contratos, atas, PCA e itens
    "compra_teste": {"ano": 2026, "sequencial": 495},  # compra usada em api_itens (tem de existir no PNCP)
    "alvos": ALVOS_PADRAO,
}

CURL_ERROS = {6: "dns", 7: "conexao_recusada", 18: "resposta_parcial", 28: "timeout", 35: "tls",
              51: "certificado", 52: "resposta_vazia", 55: "envio_falhou", 56: "conexao_derrubada",
              60: "certificado"}


# ───────────────────────── configuração ─────────────────────────

MINIMOS_NUMERICOS = {  # chave -> valor mínimo aceito (0 em intervalo faria a sonda martelar o PNCP)
    "intervalo_normal_s": 1, "intervalo_incidente_s": 1, "espera_entre_alvos_s": 0, "timeout_conexao_s": 1,
    "timeout_total_s": 1, "retry_apos_falha_s": 0, "rodadas_sem_falha_para_sair_incidente": 1,
    "falhas_seguidas_para_vermelho": 1, "limite_lacuna_x_intervalo": 1, "retencao_compactar_dias": 1,
    "porta_instancia": 1}
TIPOS_ALVO = ("controle", "portal", "api")


def _gravar_json(arq, obj):
    """Grava por arquivo temporário + `os.replace`: uma queda no meio não deixa o arquivo pela metade."""
    tmp = arq.with_name(arq.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, arq)


def carregar_config(pasta):
    """Lê `config.json`; se não existir, grava os padrões. Chaves ausentes usam o padrão.
    Valor inválido levanta `ValueError` com a chave e o motivo (em vez de matar o laço horas depois)."""
    arq = Path(pasta) / "config.json"
    cfg = json.loads(json.dumps(CONFIG_PADRAO))
    if arq.exists():
        try:
            lido = json.loads(arq.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"config.json ilegível ({e}); corrija o arquivo ou apague-o para recriar os padrões") from e
        if not isinstance(lido, dict):
            raise ValueError("config.json deve ser um objeto JSON ({...})")
        desconhecidas = sorted(set(lido) - set(CONFIG_PADRAO))
        cfg.update(lido)
    else:
        desconhecidas = []
        _gravar_json(arq, cfg)
    _validar_config(cfg)
    if desconhecidas:  # não derruba (pode ser chave de versão futura), mas erro de digitação não passa calado
        avisar_texto(pasta, "config.json: chave(s) desconhecida(s), ignorada(s): " + ", ".join(desconhecidas))
    return cfg


def avisar_texto(pasta, texto):
    """Linha em logs/sonda-erros.log, sem nunca levantar (é o último recurso de aviso)."""
    try:
        (Path(pasta) / "logs").mkdir(parents=True, exist_ok=True)
        with open(Path(pasta) / "logs" / "sonda-erros.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')}\n{texto}\n")
    except OSError:
        pass


def _validar_config(cfg):
    for chave, minimo in MINIMOS_NUMERICOS.items():
        v = cfg[chave]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < minimo:
            raise ValueError(f"config.json: {chave} deve ser um número >= {minimo} (veio {v!r})")
    if not 1 <= cfg["porta_instancia"] <= 65535:
        raise ValueError(f"config.json: porta_instancia deve estar entre 1 e 65535 (veio {cfg['porta_instancia']!r})")
    for chave in ("notificar", "iniciar_com_windows"):
        if not isinstance(cfg[chave], bool):
            raise ValueError(f"config.json: {chave} deve ser true ou false (veio {cfg[chave]!r})")
    if not isinstance(cfg["user_agent"], str) or not cfg["user_agent"].strip():
        raise ValueError("config.json: user_agent deve ser um texto não vazio")
    _validar_teste(cfg)
    alvos = cfg["alvos"]
    if not isinstance(alvos, list) or not any(isinstance(a, dict) and a.get("tipo") != "controle" for a in alvos):
        raise ValueError("config.json: alvos deve ser uma lista com ao menos 1 alvo que não seja controle")
    ids = set()
    for i, a in enumerate(alvos):
        if not isinstance(a, dict):
            raise ValueError(f"config.json: alvos[{i}] deve ser um objeto")
        for campo in ("id", "nome", "url"):
            if not isinstance(a.get(campo), str) or not a[campo].strip():
                raise ValueError(f"config.json: alvos[{i}].{campo} deve ser um texto não vazio")
        if a["id"] in ids:
            raise ValueError(f"config.json: id de alvo repetido: {a['id']!r}")
        ids.add(a["id"])
        if a.get("tipo") not in TIPOS_ALVO:
            raise ValueError(f"config.json: alvos[{i}] ({a['id']}).tipo deve ser um de {', '.join(TIPOS_ALVO)}")
        lim = a.get("limiar_lento_ms", 5000)
        if isinstance(lim, bool) or not isinstance(lim, (int, float)) or lim <= 0:
            raise ValueError(f"config.json: alvos[{i}] ({a['id']}).limiar_lento_ms deve ser um número > 0")
        try:
            expandir_url(a["url"], cfg=cfg)
        except (KeyError, IndexError, ValueError) as e:
            raise ValueError(f"config.json: url do alvo {a['id']!r} tem marcador inválido ou chave solta ({e!r})") from e


def _validar_teste(cfg):
    """Normaliza `cnpj_teste` (aceita com pontuação) e confere `compra_teste`; erro claro em vez de 404 misterioso."""
    cnpj = re.sub(r"\D", "", str(cfg["cnpj_teste"]))
    if len(cnpj) != 14:
        raise ValueError(f"config.json: cnpj_teste deve ter 14 dígitos (veio {cfg['cnpj_teste']!r})")
    cfg["cnpj_teste"] = cnpj
    try:
        cfg["compra_teste"] = {"ano": int(cfg["compra_teste"]["ano"]), "sequencial": int(cfg["compra_teste"]["sequencial"])}
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError('config.json: compra_teste deve ser {"ano": 2026, "sequencial": 495}') from e


def salvar_config_chave(pasta, chave, valor):
    arq = Path(pasta) / "config.json"
    cfg = json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else json.loads(json.dumps(CONFIG_PADRAO))
    cfg[chave] = valor
    _gravar_json(arq, cfg)


# ───────────────────────── medição ─────────────────────────

def expandir_url(url, agora=None, cfg=None):
    """Troca os marcadores da URL: datas ({hoje}, {ontem}, {d7}, {d30}, {ini_ano}) e o órgão/compra de teste
    ({cnpj}, {ano_compra}, {seq_compra}), lidos de `cnpj_teste`/`compra_teste` do config."""
    a = (agora or datetime.now()).astimezone()
    cfg = cfg or CONFIG_PADRAO
    return url.format(cnpj=cfg["cnpj_teste"], ano_compra=cfg["compra_teste"]["ano"],
                      seq_compra=cfg["compra_teste"]["sequencial"], hoje=a.strftime("%Y%m%d"),
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
    url = expandir_url(alvo["url"], cfg=cfg)
    tmp = tempfile.mkdtemp(prefix="sonda_")
    try:
        return _medir_em(tmp, url, alvo, cfg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # também quando a medição levanta


def _caminho_curl():
    """curl.exe do System32 primeiro: `shutil.which` procura antes no diretório atual (o atalho de autostart
    define WorkingDirectory), e um curl.exe plantado ali seria executado."""
    sistema = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    return str(sistema) if sistema.exists() else (shutil.which("curl.exe") or str(sistema))


def _medir_em(tmp, url, alvo, cfg):
    corpo_p, cab_p = os.path.join(tmp, "corpo"), os.path.join(tmp, "cab")
    cmd = [_caminho_curl(), "-sS", "-L", "--max-redirs", "3", "--proto", "=http,https", "--proto-redir", "=http,https",
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
        m.update(curl_exit=28, curl_erro="curl.exe excedeu o tempo (guarda da sonda)", _guarda=True,
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
            if m.get("_guarda"):  # o curl nem devolveu o JSON: não dá para saber se conectou
                return "timeout", "timeout_guarda"
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
        linha = (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock, open(arq, "ab+") as f:  # fecha a cada linha: queda de energia perde no máximo uma
            f.seek(0, 2)
            if f.tell() > 0:
                f.seek(-1, 2)
                if f.read(1) != b"\n":  # linha anterior cortada: sem isto ela engoliria este registro
                    f.write(b"\n")
            f.write(linha)


def ler_registros(pasta, ini, fim, rejeitadas=None):
    """Itera os registros dos dias [ini, fim] (datas), inclusive arquivos .gz. Tolerante a log estragado:
    linha ilegível, que não seja objeto, byte inválido ou .gz truncado são pulados. Se `rejeitadas` (uma lista)
    for dada, recebe uma entrada por linha ou arquivo ignorado, para o relatório poder dizer quantos foram."""
    d = ini
    while d <= fim:
        puro, comprimido = Path(pasta) / "logs" / f"sonda-{d}.jsonl", Path(pasta) / "logs" / f"sonda-{d}.jsonl.gz"
        # se os dois existem (compactação interrompida), vale o .jsonl: ler os dois contaria o dia em dobro
        for arq, abrir in ((puro, open), (comprimido, gzip.open)) if not puro.exists() else ((puro, open),):
            if not arq.exists():
                continue
            try:
                with abrir(arq, "rt", encoding="utf-8", errors="replace") as f:
                    for linha in f:
                        if not linha.strip():
                            continue
                        try:
                            r = json.loads(linha)
                        except ValueError:
                            r = None  # linha truncada (queda de energia)
                        if isinstance(r, dict) and "tipo" in r:
                            yield r
                        elif rejeitadas is not None:
                            rejeitadas.append(arq.name)
            except (OSError, EOFError):  # .gz truncado, arquivo travado
                if rejeitadas is not None:
                    rejeitadas.append(arq.name + " (arquivo ilegível)")
        d += timedelta(days=1)


def _ts_registro(r):
    try:
        return datetime.fromisoformat(r["ts_utc"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


def ultimo_registro(pasta):
    """O registro de maior horário do log mais recente. O resumo de rodada é gravado no FIM da rodada com o
    horário do INÍCIO, então a última linha do arquivo não é necessariamente a mais recente no tempo."""
    for arq in sorted((Path(pasta) / "logs").glob("sonda-*.jsonl"), reverse=True):
        candidatos = []
        for linha in reversed(arq.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]):
            try:
                r = json.loads(linha)
            except ValueError:
                continue
            if isinstance(r, dict) and "tipo" in r:
                candidatos.append(r)
        if candidatos:
            return max(candidatos, key=_ts_registro)
    return None


def compactar_antigos(pasta, dias):
    """Compacta em .gz os logs mais velhos que `dias` (mantém tudo, só ocupa menos). Arquivo travado por outro
    programa (antivírus, backup) fica para a próxima vez: nunca levanta."""
    corte = datetime.now().date() - timedelta(days=dias)
    n = 0
    for arq in (Path(pasta) / "logs").glob("sonda-*.jsonl"):
        try:
            dia = datetime.strptime(arq.stem[6:], "%Y-%m-%d").date()
        except ValueError:
            continue
        if dia < corte:
            gz = Path(str(arq) + ".gz")
            tmp = Path(str(gz) + ".tmp")
            try:
                with open(arq, "rb") as f, gzip.open(tmp, "wb") as g:
                    shutil.copyfileobj(f, g)
                os.replace(tmp, gz)
                arq.unlink()
                n += 1
            except OSError:
                tmp.unlink(missing_ok=True)
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
        self.thread_laco = None  # a bandeja registra aqui a thread do laço, para o vigia saber se ela morreu
        self.alerta = None  # "morta" | "parada" enquanto o vigia acusar; a bandeja desenha o ícone com isso
        self.batimento = self.agora()  # última prova de vida do laço (cada medição e cada rodada)
        self._bat_mono = time.monotonic()
        self._erros_alvo = {}  # alvo -> quando avisamos pela última vez (evita um erro_interno por rodada)
        self._curl_avisado = False

    # -- registros --
    def _ts(self, a=None):
        a = a or self.agora()
        return {"ts_local": a.astimezone().isoformat(timespec="milliseconds"),
                "ts_utc": a.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")}

    def evento(self, nome, **campos):
        self.log.escrever({"tipo": "evento", **self._ts(), "evento": nome, **campos})

    def registrar_erro(self, exc):
        """Nunca levanta: é chamado de dentro de `except`, e se ele próprio falhasse (log travado, disco cheio)
        levaria o laço junto. O arquivo de erros vai primeiro: é o que sobra quando o log principal é o problema."""
        tb = "".join(traceback.format_exception(exc))[-1500:]
        avisar_texto(self.pasta, tb)
        try:
            self.evento("erro_interno", erro=str(exc)[:300], traceback=tb)
        except Exception:  # noqa: BLE001  # nosec B110
            pass  # já está no sonda-erros.log; não há mais para onde reportar

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
        if cab.get("date") and m["total_ms"] < LIMITE_DESVIO_MS:
            # só com resposta rápida: o erro da estimativa é metade da latência (em 26 s, ±13 s de erro)
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

    def _avisar(self, titulo, msg):
        """Notificação nunca derruba a rodada nem deixa de registrar o estado."""
        try:
            self.notificar(titulo, msg)
        except Exception as e:  # noqa: BLE001
            self.registrar_erro(e)

    def _falha_do_alvo(self, alvo, e):
        """Erro interno ao medir UM alvo (por exemplo, uma URL do config com marcador inválido): registra e segue
        com os demais. Avisa no máximo 1 vez por hora por alvo, para não gerar um erro_interno a cada rodada."""
        agora = time.monotonic()
        if agora - self._erros_alvo.get(alvo["id"], -1e9) > 3600:
            self._erros_alvo[alvo["id"]] = agora
            self.registrar_erro(e)
            if self.cfg["notificar"]:
                self._avisar("Sonda PNCP: alvo não medido", f"{alvo['nome']}: erro interno (ver sonda-erros.log).")

    def _medir_alvo(self, alvo, rid, tentativa):
        """Devolve o resultado, ou None se o alvo não pôde ser medido (erro interno) ou se a sonda está encerrando
        (medição em andamento no encerramento é descartada: nada pode ser gravado depois de `sonda_encerrada`)."""
        try:
            m = self.medir(alvo, self.cfg)
        except Exception as e:  # noqa: BLE001 - um alvo com defeito não pode derrubar a rodada dos outros
            self._falha_do_alvo(alvo, e)
            return None
        if self.parar.is_set():
            return None
        self._pulso()
        resultado, detalhe = classificar(m, alvo, self.cfg)
        self.log.escrever(self._registro_sonda(rid, alvo, tentativa, m, resultado, detalhe))
        if m["curl_exit"] == -2 and not self._curl_avisado:  # curl.exe ausente ou bloqueado (Smart App Control, antivírus)
            self._curl_avisado = True
            self.registrar_erro(RuntimeError(f"curl.exe indisponível: {m['curl_erro']}"))
            if self.cfg["notificar"]:
                self._avisar("Sonda PNCP: curl.exe indisponível", "Sem o curl.exe a sonda não mede nada.")
        elif m["curl_exit"] != -2:
            self._curl_avisado = False
        if m["ip"] and alvo["tipo"] != "controle" and self.ips.get(alvo["id"]) not in (None, m["ip"]):
            # controles (Google, Cloudflare) trocam de IP a cada consulta por balanceamento: seria só ruído
            self.evento("mudanca_ip", alvo=alvo["id"], de=self.ips[alvo["id"]], para=m["ip"])
        if m["ip"]:
            self.ips[alvo["id"]] = m["ip"]
        if resultado == "registro_ausente" and alvo["id"] not in self.ausentes:
            self.ausentes.add(alvo["id"])  # avisa uma vez; não é queda do PNCP, é o teste que precisa de novo registro
            self.evento("registro_ausente", alvo=alvo["id"], url=m["url"])
            if self.cfg["notificar"]:
                self._avisar("Sonda PNCP: registro de teste sumiu",
                             f"{alvo['nome']}: trocar compra_teste no config.json (não conta como falha).")
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
        self._pulso()
        for etapa in (self._carregar_24h, lambda: compactar_antigos(self.pasta, self.cfg["retencao_compactar_dias"])):
            try:  # acessórios da partida: falhar aqui não pode impedir a sonda de medir
                etapa()
            except Exception as e:  # noqa: BLE001
                self.registrar_erro(e)

    def encerrar(self, motivo):
        # `parar` primeiro: com o log falhando o evento levantaria e a sonda continuaria rodando ("Encerrar" sem efeito)
        self.parar.set()
        self.disparar.set()
        try:
            self.evento("sonda_encerrada", motivo=motivo, rodadas=self.n_rodada)
        except Exception as e:  # noqa: BLE001
            self.registrar_erro(e)

    def pausar(self, sim):
        self.pausada = sim
        if sim:
            self.houve_pausa = True
        else:
            self._pulso()  # o vigia não deve estranhar o tempo em que ficou pausada
        try:
            self.evento("pausada" if sim else "retomada_manual")
            self.ao_mudar()
        except Exception as e:  # noqa: BLE001
            self.registrar_erro(e)

    def _carregar_24h(self):
        agora = self.agora()
        hoje = agora.astimezone().date()
        for r in ler_registros(self.pasta, hoje - timedelta(days=1), hoje):
            if r.get("tipo") == "rodada":
                try:
                    t = datetime.fromisoformat(r["ts_utc"])
                    if t >= agora - timedelta(hours=24):
                        self._r24.append((t, r["estado"]))
                except (KeyError, TypeError, ValueError):
                    continue  # rodada estragada no log: ignora só ela

    def _pulso(self):
        self.batimento = self.agora()
        self._bat_mono = time.monotonic()

    def saude(self):
        """Vigia: None se está tudo bem; "morta" se a thread do laço terminou sem ninguém ter pedido;
        "parada" se ela está viva mas sem medir há muito mais do que a rodada mais lenta possível."""
        t = self.thread_laco
        if t is None or self.pausada or self.parar.is_set():
            return None
        if not t.is_alive():
            return "morta"
        limite = self.proximo_esperado_s + FOLGA_VIGIA_S
        # os dois relógios: ao voltar de uma suspensão o de parede salta horas, e isso não é o laço travado
        parada = (self.agora() - self.batimento).total_seconds() > limite and time.monotonic() - self._bat_mono > limite
        return "parada" if parada else None

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
        self._pulso()
        self.n_rodada += 1
        rid = f"{agora.astimezone():%Y%m%dT%H%M%S}-{self.n_rodada}"
        self._checar_lacuna(agora)
        alvos = self.cfg["alvos"]
        controles = [a for a in alvos if a["tipo"] == "controle"]
        pncp = [a for a in alvos if a["tipo"] != "controle"]
        res_ctrl = []
        interrompida = False
        for a in controles:
            if self.parar.is_set():
                interrompida = True
                break
            res_ctrl.append(self._medir_alvo(a, rid, 1))  # None = não medido (erro interno) ou encerrando
            self.dormir(self.cfg["espera_entre_alvos_s"])
        rede_ok = not controles or any(r in OKS for r in res_ctrl)
        finais = {}
        if rede_ok and not interrompida:
            for a in pncp:
                if self.parar.is_set():
                    interrompida = True
                    break
                r = self._medir_alvo(a, rid, 1)
                if r is None:  # encerrando, ou erro interno neste alvo (já registrado): segue com os outros
                    continue
                final = r
                if r in FALHAS:  # o primeiro erro fica registrado; a 2ª tentativa separa soluço de falha
                    self.dormir(self.cfg["retry_apos_falha_s"])
                    r2 = self._medir_alvo(a, rid, 2)
                    final = "blip" if r2 in OKS else (r2 or r)
                finais[a["id"]] = final
                self.dormir(self.cfg["espera_entre_alvos_s"])
        if interrompida or self.parar.is_set():  # encerrando: resumo parcial enganaria e cairia depois do "sonda_encerrada"
            return None
        nao_medidos = rede_ok and any(a["id"] not in finais for a in pncp)
        falhas = sorted(k for k, v in finais.items() if v in FALHAS)
        blips = sorted(k for k, v in finais.items() if v == "blip")
        lentos = sorted(k for k, v in finais.items() if v == "lento")
        bloqueios = sorted(k for k, v in finais.items() if v == "bloqueio_429")
        ausentes = sorted(k for k, v in finais.items() if v == "registro_ausente")
        if not rede_ok:
            estado = "sem_rede"
        elif falhas:
            estado = "falha"
        elif blips or lentos or bloqueios or ausentes or nao_medidos:  # alvo não medido nunca vira "ok"
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
        try:
            self.ao_mudar()  # redesenhar o ícone falhar não pode desfazer a rodada nem a cadência do incidente
        except Exception as e:  # noqa: BLE001
            self.registrar_erro(e)
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
            antes, self.cor = self.cor, cor  # a cor muda mesmo que o log ou a notificação falhem
            self.evento("mudanca_estado", de=antes, para=cor, estado_rodada=estado, falhas=falhas)
            if cfg["notificar"]:
                if cor == "vermelho":
                    self._avisar("Sonda PNCP: falha confirmada", "Falha em: " + ", ".join(falhas))
                elif antes == "vermelho" and cor in ("verde", "amarelo"):
                    self._avisar("Sonda PNCP: PNCP recuperou", "As consultas voltaram a responder.")
        # sem rede: rechecar cedo (só 2 requisições); falha: modo incidente (1 min); senão, normal
        return cfg["intervalo_incidente_s"] if (self.incidente or estado == "sem_rede") \
            else cfg["intervalo_normal_s"]

    # -- laço (roda em thread) --
    def laco(self):
        try:
            self._laco()
        except BaseException as e:  # a thread vai terminar: deixa a causa registrada (registrar_erro não levanta)
            self.registrar_erro(e)
            raise

    @staticmethod
    def _espera_segura(x):
        try:
            return max(0.05, float(x))  # o config validado nunca traz < 1 s; o piso só evita um laço quente
        except (TypeError, ValueError):
            return 300.0

    def _laco(self):
        while not self.parar.is_set():
            espera = None
            try:
                if self.pausada:
                    self.disparar.wait(2)
                    self.disparar.clear()
                    continue
                rec = self.rodada()
                if rec is None:
                    break
                espera = rec["proxima_em_s"]
            except Exception as e:  # noqa: BLE001 - a sonda nunca pode morrer calada
                self.registrar_erro(e)
            if espera is None:  # a rodada falhou: mantém a última cadência conhecida (não volta ao modo normal)
                espera = self.proximo_esperado_s
            self.disparar.wait(self._espera_segura(espera))
            self.disparar.clear()

    # -- textos da bandeja --
    TXT = {"verde": "OK", "amarelo": "atenção", "vermelho": "FALHA", "cinza": "sem rede local"}

    def texto_status(self):
        if self.alerta:
            return f"PARADA - sem medir desde {self.batimento.astimezone():%H:%M}"
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
        if self.alerta:
            return f"Sonda PNCP - PAROU de medir às {self.batimento.astimezone():%H:%M}. Reinicie."[:127]
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
            linhas.append([_fmt(a["ts_local"]), _fmt(b["ts_local"]), round((tb - ta).total_seconds() / 60, 1), motivo])
    _csv(out / "5_lacunas.csv", ["Último registro antes", "Primeiro registro depois", "Duração (min)", "Motivo"], linhas)
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
             "ausente": "#8b5cf6"}
NOME_BARRA = {"ok": "ok", "lento": "lenta", "falha": "falha", "429": "HTTP 429", "ausente": "registro de teste ausente"}
MAX_BARRAS = 300
MAX_JANELAS_HTML = 12  # a lista completa fica no CSV; o HTML precisa caber no A4


def _granularidade(span_s, base_s):
    """Menor largura de barra (5 min → 1 h → 6 h → 1 dia) que mantém o gráfico com até MAX_BARRAS barras."""
    for tam, nome in ((base_s, f"{base_s // 60} min"), (3600, "1 hora"), (6 * 3600, "6 horas")):
        if span_s / tam <= MAX_BARRAS:
            return tam, nome
    return 86400, "1 dia"


def _cor_do_balde(n, nf, nl, n429, naus=0):
    """Cor de um intervalo: vermelho se >= 25% falharam; âmbar se houve falha ou >= 25% lentas; cinza se só 429;
    violeta se só registro de teste ausente (não é queda, e não é "ok" nem "sem dados")."""
    if nf / n >= 0.25:
        return "falha"
    if nf or nl / n >= 0.25:
        return "lento"
    if naus == n:
        return "ausente"
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
        lat = [teto_s if r in FALHAS else ms / 1000 for r, ms in g if r not in ("bloqueio_429", "registro_ausente")]
        out[i] = (_cor_do_balde(len(g), nf, nl, n429, naus), _pct(lat, 95) if lat else 0, len(g), nf, nl)
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
    return {"lento": "lento", "bloqueio_429": "429", "registro_ausente": "ausente"}.get(resultado, "ok")


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
{cfg['intervalo_normal_s'] // 60} min), de {_fmt(primeira) or '-'} a {_fmt(ultima) or '-'}; {n_lacunas} lacuna(s) sem registro
(PC desligado ou suspenso, sonda parada ou pausada). Ausência de registro não é contada como disponibilidade.{aviso_log}</p>
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
