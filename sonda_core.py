"""Sonda PNCP - núcleo: medição, classificação, log diário, estado e relatório.

Sem interface: a bandeja fica em `sonda_pncp.pyw`. Tudo aqui é testável sem Windows
além do `curl.exe` (que a medição usa para obter DNS/TCP/TLS/TTFB e o código de erro
de rede de graça).
"""

import gzip
import hashlib
import json
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
from collections import deque
from datetime import datetime, timedelta, UTC
from email.utils import parsedate_to_datetime
from pathlib import Path

VERSAO = "1.6.1"
FALHAS = {"erro_http", "erro_rede", "timeout", "corpo_invalido"}  # falha do lado do alvo
OKS = {"ok", "lento"}  # resposta válida (lento = válida, porém acima do limiar)
# erro_local: problema do LADO da sonda (disco cheio, permissão), não do alvo — fora de FALHAS e OKS,
# não conta nem como disponibilidade nem como falha confirmada.
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
    {"id": "ctrl_google", "nome": "Controle: Google", "tipo": "controle", "validar": "google_204",
     "url": "https://www.google.com/generate_204", "limiar_lento_ms": 4000, "accept": "*/*"},
    {"id": "ctrl_cloudflare", "nome": "Controle: Cloudflare", "tipo": "controle", "validar": "cloudflare_trace",
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
        if m["bytes"] > 0:  # curl baixou dados, mas a sonda não conseguiu reler o corpo (disco/permissão): problema local
            m["_corpo_ilegivel"] = True
    return m


def validar(tipo, corpo):
    if tipo == "google_204":
        return corpo == b""  # generate_204 sempre responde corpo vazio
    if tipo == "cloudflare_trace":
        return b"\nip=" in corpo or corpo.startswith(b"ip=")
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
    corpo_invalido, bloqueio_429, registro_ausente, erro_local."""
    if m["curl_exit"] == 23:  # falha ao ESCREVER o corpo em disco: não é o alvo que falhou, é a sonda
        return "erro_local", "curl_23_escrita_falhou"
    if m["curl_exit"] != 0:
        nome = CURL_ERROS.get(m["curl_exit"], f"curl_{m['curl_exit']}")
        if m["curl_exit"] == 28:
            if m.get("_guarda"):  # o curl nem devolveu o JSON: não dá para saber se conectou
                return "timeout", "timeout_guarda"
            return "timeout", "timeout_resposta" if m["_conectou"] else "timeout_conexao"
        return "erro_rede", nome
    if m.get("_corpo_ilegivel"):
        return "erro_local", "leitura_corpo_falhou"
    h = m["http"]
    if h == 429:
        return "bloqueio_429", "http_429"
    marca = alvo.get("ausencia_404")  # registro fixo de teste que sumiu: o PNCP responde 404 explícito
    if h == 404 and marca and marca.encode() in m["_corpo"]:
        return "registro_ausente", "http_404_registro_ausente"
    if not 200 <= h < 300:
        return "erro_http", f"http_{h}"
    if not validar(alvo.get("validar", "status"), m["_corpo"]):
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

def ativo_s():
    """Segundos em que o PC esteve LIGADO desde o último boot do zero (QueryUnbiasedInterruptTime): não conta
    suspensão nem hibernação, e o "Desligar" com a Inicialização Rápida do Windows é uma hibernação. None fora do
    Windows ou se a chamada falhar."""
    try:
        import ctypes
        v = ctypes.c_ulonglong()
        if not ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(v)):  # type: ignore[attr-defined]
            return None
        return round(v.value / 1e7, 1)  # unidades de 100 ns
    except (AttributeError, OSError, ValueError):
        return None


def _pc_na_lacuna(gap_s, ativo_antes, ativo_agora):
    """Campos de `sonda_iniciada` sobre o PC na lacuna. Contador ativo andou menos que o relógio = PC parado
    (desligado ou suspenso) a diferença; andou para trás = houve boot do zero no meio. {} se faltar dado."""
    if gap_s is None or not isinstance(ativo_antes, (int, float)) or ativo_agora is None:
        return {}
    if ativo_agora < ativo_antes:
        return {"pc_reiniciou": True}
    return {"pc_reiniciou": False, "pc_parado_s": round(max(0.0, gap_s - (ativo_agora - ativo_antes)))}


def uptime_pc_s():
    """Segundos desde o boot do Windows (GetTickCount64); None fora do Windows ou se a chamada falhar.
    CONTA o tempo desligado com a Inicialização Rápida (é hibernação): para saber se o PC ficou parado numa
    lacuna, use `ativo_s`."""
    try:
        import ctypes
        f = ctypes.windll.kernel32.GetTickCount64  # type: ignore[attr-defined]
        f.restype = ctypes.c_ulonglong  # o padrão (int de 32 bits) truncaria/negativaria após ~24 dias ligado
        return int(f() // 1000)
    except (AttributeError, OSError, ValueError):
        return None


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
        r = {"ts_local": a.astimezone().isoformat(timespec="milliseconds"),
             "ts_utc": a.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        at = ativo_s()  # no momento da gravação: a próxima partida compara com ele para saber se o PC ficou parado
        if at is not None:
            r["ativo_s"] = at
        return r

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
            gap = None
            try:
                gap = (self.agora() - datetime.fromisoformat(ult["ts_utc"])).total_seconds()
                campos["gap_desde_anterior_s"] = round(gap)
            except (KeyError, ValueError):
                pass
            campos["encerramento_anterior_limpo"] = ult.get("evento") == "sonda_encerrada"
            campos.update(_pc_na_lacuna(gap, ult.get("ativo_s"), ativo_s()))
        up = uptime_pc_s()
        if up is not None:
            campos["uptime_pc_s"] = up
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
        esperado = max(self.proximo_esperado_s, self.cfg["intervalo_incidente_s"])
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
        intervalo = self._atualizar(estado, falhas)
        duracao_s = time.monotonic() - t0
        # o intervalo vale de INÍCIO a INÍCIO: a rodada já gastou parte dele. Rodada mais longa que o intervalo
        # (PNCP em timeout: ~5 min) emenda na seguinte, sem pausa; `espera_entre_alvos_s` segue espaçando as requisições
        espera = max(0.0, intervalo - duracao_s)
        fim = self.agora()
        rec = {"tipo": "rodada", **self._ts(agora), "rodada": rid, "estado": estado, "cor": self.cor,
               "rede_local_ok": rede_ok, "alvos": finais, "falhas": falhas, "blips": blips,
               "lentos": lentos, "bloqueios_429": bloqueios,
               "registros_ausentes": ausentes,
               "modo": "incidente" if self.incidente else "normal", "streak_falha": self.streak,
               "duracao_ms": round(duracao_s * 1000), "proxima_em_s": round(espera, 1)}
        self.log.escrever(rec)
        self._r24.append((agora.astimezone(UTC), estado))
        self.ultima = rec
        self.ultimo_wall = fim
        self.proximo_esperado_s = espera
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

    def _intervalo_atual(self):
        return self.cfg["intervalo_incidente_s"] if self.incidente else self.cfg["intervalo_normal_s"]

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
            if espera is None:  # a rodada falhou: a espera de uma rodada que não terminou não existe; usa o intervalo do modo
                espera = self._intervalo_atual()
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
