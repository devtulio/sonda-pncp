"""Testes da Sonda PNCP. Rodar:  .venv\\Scripts\\python -m unittest discover -s tests -v"""

import csv
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from datetime import datetime, timedelta, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sonda_core as core  # noqa: E402
import sonda_relatorio as rel  # noqa: E402

HTML = "<html>" + "x" * 3000 + "</html>"
PORT = None
SERVIDOR = None


class Falso(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _enviar(self, codigo, corpo=b"", tipo="application/json", extra=None):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/ok":
            self._enviar(200, b'{"data":[],"totalRegistros":0}')
        elif p == "/lista":
            self._enviar(200, b"[]")
        elif p == "/html":
            self._enviar(200, HTML.encode(), "text/html")
        elif p == "/204":
            self._enviar(204)
        elif p == "/503":
            self._enviar(503, b'{"timestamp":"2026-09-20T05:06:46.370+00:00","status":503,'
                              b'"message":"Failed to obtain JDBC Connection; nested exception"}')
        elif p == "/404":
            self._enviar(404, '{"status":"404","message":"Contratação não cadastrada."}'.encode())
        elif p == "/429":
            self._enviar(429, b"", extra={"Retry-After": "30"})
        elif p == "/lento":
            time.sleep(1.2)
            self._enviar(200, b'{"data":[]}')
        elif p == "/travado":
            time.sleep(6)
            self._enviar(200, b'{"data":[]}')
        elif p == "/ruim":
            self._enviar(200, b"oi", "text/plain")
        elif p == "/vazio":  # aceita e fecha sem responder
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)


def setUpModule():
    global PORT, SERVIDOR
    SERVIDOR = ThreadingHTTPServer(("127.0.0.1", 0), Falso)
    PORT = SERVIDOR.server_address[1]
    threading.Thread(target=SERVIDOR.serve_forever, daemon=True).start()


def tearDownModule():
    SERVIDOR.shutdown()


def cfg_teste(**extra):
    c = json.loads(json.dumps(core.CONFIG_PADRAO))
    c.update({"timeout_total_s": 3, "timeout_conexao_s": 2, "notificar": True, **extra})
    return c


def alvo(caminho, tipo="api", validar="json_data", limiar=500, id="t"):
    return {"id": id, "nome": id, "tipo": tipo, "validar": validar, "limiar_lento_ms": limiar,
            "url": f"http://127.0.0.1:{PORT}{caminho}"}


def medir_e_classificar(a, cfg=None):
    cfg = cfg or cfg_teste()
    m = core.medir(a, cfg)
    return core.classificar(m, a, cfg), m


class TestMedicaoReal(unittest.TestCase):
    """curl.exe de verdade contra o servidor falso: prova a taxonomia de erros."""

    def test_ok_e_campos_de_diagnostico(self):
        (res, det), m = medir_e_classificar(alvo("/ok"))
        self.assertEqual((res, det), ("ok", ""))
        self.assertEqual(m["http"], 200)
        self.assertEqual(m["ip"], "127.0.0.1")
        self.assertGreaterEqual(m["total_ms"], m["ttfb_ms"])
        self.assertIn("content-type", m["_cab"])

    def test_lista_html_controle(self):
        self.assertEqual(medir_e_classificar(alvo("/lista", validar="json_lista"))[0], ("ok", ""))
        self.assertEqual(medir_e_classificar(alvo("/html", tipo="portal", validar="html"))[0], ("ok", ""))
        self.assertEqual(medir_e_classificar(alvo("/204", tipo="controle", validar="status"))[0], ("ok", ""))

    def test_http_5xx_e_429(self):
        self.assertEqual(medir_e_classificar(alvo("/503"))[0], ("erro_http", "http_503"))
        (res, det), m = medir_e_classificar(alvo("/429"))
        self.assertEqual((res, det), ("bloqueio_429", "http_429"))
        self.assertEqual(m["_cab"]["retry-after"], "30")

    def test_lento_timeout_corpo_invalido(self):
        self.assertEqual(medir_e_classificar(alvo("/lento", limiar=500))[0], ("lento", ""))
        self.assertEqual(medir_e_classificar(alvo("/travado"), cfg_teste(timeout_total_s=2))[0],
                         ("timeout", "timeout_resposta"))
        self.assertEqual(medir_e_classificar(alvo("/ruim"))[0], ("corpo_invalido", "corpo_inesperado"))

    def test_conexao_derrubada_recusada_dns(self):
        res, det = medir_e_classificar(alvo("/vazio"))[0]
        self.assertEqual(res, "erro_rede")
        self.assertIn(det, ("resposta_vazia", "conexao_derrubada"))
        with socket.socket() as s:  # porta que acabou de ser liberada: ninguém escutando
            s.bind(("127.0.0.1", 0))
            livre = s.getsockname()[1]
        a = alvo("/ok")
        a["url"] = f"http://127.0.0.1:{livre}/"
        # Windows retenta o SYN recusado por ~2 s; timeout de conexão folgado para ver o "recusada"
        self.assertEqual(medir_e_classificar(a, cfg_teste(timeout_conexao_s=8, timeout_total_s=10))[0],
                         ("erro_rede", "conexao_recusada"))
        b = alvo("/ok")
        b["url"] = "http://nao-existe.invalid/"
        self.assertEqual(medir_e_classificar(b)[0][0], "erro_rede")


class TestRegistroAusente(unittest.TestCase):
    def test_404_com_a_mensagem_do_pncp_nao_e_falha(self):
        a = alvo("/404")
        a["ausencia_404"] = "Contratação não cadastrada"
        self.assertEqual(medir_e_classificar(a)[0], ("registro_ausente", "http_404_registro_ausente"))
        self.assertNotIn("registro_ausente", core.FALHAS | core.OKS)
        self.assertEqual(medir_e_classificar(alvo("/404"))[0], ("erro_http", "http_404"))  # sem a marca: falha normal

    def test_alvo_padrao_declara_a_marca(self):
        self.assertTrue([a for a in core.ALVOS_PADRAO if a["id"] == "api_itens"][0]["ausencia_404"])


class Relogio:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def avancar(self, **kw):
        self.t += timedelta(**kw)


class Roteiro:
    """medir falso: cada alvo consome uma lista de resultados; o que sobra é 'ok'."""

    def __init__(self, relogio, **script):
        self.relogio, self.script, self.chamadas = relogio, {k: list(v) for k, v in script.items()}, []

    def __call__(self, a, cfg):
        tipo = (self.script.get(a["id"]) or ["ok"]).pop(0)
        self.chamadas.append((a["id"], tipo))
        corpo = {"json_data": b'{"data":[]}', "json_lista": b"[]", "html": HTML.encode()}.get(a["validar"], b"")
        m = {"url": "https://exemplo/x", "curl_exit": 0, "curl_erro": "", "http": 200, "http_versao": "2",
             "ip": "1.2.3.4", "dns_ms": 5, "tcp_ms": 10, "tls_ms": 20, "ttfb_ms": 100, "total_ms": 150,
             "bytes": len(corpo), "_conectou": True, "_corpo": corpo, "_cab": {"date": "Sun, 20 Sep 2026 12:00:00 GMT"},
             "_inicio": self.relogio.t, "_fim": self.relogio.t}
        if tipo == "503":
            m.update(http=503, _corpo='{"message":"Erro na comunicação com o banco de dados."}'.encode())
        elif tipo == "429":
            m.update(http=429, _corpo=b"")
        elif tipo == "ausente":
            m.update(http=404, _corpo="Contratação não cadastrada.".encode())
        elif tipo == "rede":
            m.update(curl_exit=56, http=0, _corpo=b"", curl_erro="Recv failure")
        elif tipo == "lento":
            m.update(total_ms=a["limiar_lento_ms"] + 1000)
        return m


def cfg_rodada():
    c = cfg_teste()
    c["alvos"] = [
        {"id": "c1", "nome": "c1", "tipo": "controle", "validar": "status", "url": "x", "limiar_lento_ms": 4000},
        {"id": "c2", "nome": "c2", "tipo": "controle", "validar": "status", "url": "x", "limiar_lento_ms": 4000},
        {"id": "portal", "nome": "portal", "tipo": "portal", "validar": "html", "url": "x", "limiar_lento_ms": 4000},
        {"id": "api", "nome": "api", "tipo": "api", "validar": "json_data", "url": "x", "limiar_lento_ms": 5000,
         "ausencia_404": "Contratação não cadastrada"},
    ]
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self._tmp.name)
        self.relogio = Relogio(datetime(2026, 9, 18, 12, 0, tzinfo=UTC))
        self.avisos = []
        # relógio monotônico falso: a duração de uma rodada de teste é 0, a menos que o teste peça `self.avancando()`
        self.mono = [1000.0]
        relogio_falso = mock.patch.object(core, "time", SimpleNamespace(monotonic=lambda: self.mono[0]))
        relogio_falso.start()
        self.addCleanup(relogio_falso.stop)

    def avancando(self):
        """dormir_fn que faz o tempo (monotônico falso) passar: a rodada dura a soma das suas esperas."""
        return lambda s: self.mono.__setitem__(0, self.mono[0] + s)

    def tearDown(self):
        self._tmp.cleanup()

    def sonda(self, **script):
        self.roteiro = Roteiro(self.relogio, **script)
        return core.Sonda(self.pasta, cfg_rodada(), medir_fn=self.roteiro, agora_fn=self.relogio,
                          dormir_fn=lambda s: None, notificar_fn=lambda t, m: self.avisos.append(t))

    def registros(self, tipo=None):
        regs = list(core.ler_registros(self.pasta, datetime(2026, 9, 1).date(), datetime(2026, 12, 31).date()))
        return [r for r in regs if tipo is None or r["tipo"] == tipo]


class TestEstados(Base):
    def test_tudo_ok(self):
        s = self.sonda()
        rec = s.rodada()
        self.assertEqual((rec["estado"], s.cor, rec["proxima_em_s"]), ("ok", "verde", 300))
        self.assertEqual(len(self.registros("sonda")), 4)
        self.assertEqual(len(self.registros("rodada")), 1)
        self.assertTrue(list((self.pasta / "logs").glob("sonda-2026-09-1*.jsonl")))
        self.assertIn("OK", s.tooltip())
        self.assertIn("100,0%", s.tooltip())
        self.assertLessEqual(len(s.tooltip()), 127)

    def test_falha_confirmada_incidente_e_recuperacao(self):
        s = self.sonda(portal=["503", "503", "503", "503"])
        r1 = s.rodada()  # 1ª falha: amarelo, entra em modo incidente
        self.assertEqual((r1["estado"], s.cor, r1["proxima_em_s"], s.incidente), ("falha", "amarelo", 60, True))
        r2 = s.rodada()  # 2 falhas seguidas: vermelho + notificação
        self.assertEqual(s.cor, "vermelho")
        self.assertEqual(self.avisos, ["Sonda PNCP: falha confirmada"])
        self.assertEqual(r2["streak_falha"], 2)
        r3 = s.rodada()  # 1ª rodada boa: notifica recuperação, mas segue em modo incidente
        self.assertEqual((s.cor, r3["proxima_em_s"]), ("verde", 60))
        self.assertEqual(self.avisos[-1], "Sonda PNCP: PNCP recuperou")
        s.rodada()
        r5 = s.rodada()  # 3ª rodada seguida sem falha: sai do modo incidente
        self.assertEqual((s.incidente, r5["proxima_em_s"]), (False, 300))

    def test_soluco_vira_degradado_e_guarda_as_duas_tentativas(self):
        s = self.sonda(portal=["503", "ok"])
        rec = s.rodada()
        self.assertEqual((rec["estado"], s.cor, s.incidente, rec["blips"]), ("degradado", "amarelo", False, ["portal"]))
        tent = [r["tentativa"] for r in self.registros("sonda") if r["alvo"] == "portal"]
        self.assertEqual(tent, [1, 2])  # o primeiro erro nunca é escondido

    def test_sem_rede_local_nao_conta_como_falha_do_pncp(self):
        s = self.sonda(c1=["rede"], c2=["rede"])
        rec = s.rodada()
        self.assertEqual((rec["estado"], s.cor, rec["proxima_em_s"], s.streak), ("sem_rede", "cinza", 60, 0))
        self.assertEqual(rec["alvos"], {})
        self.assertFalse(any(r["alvo"] in ("portal", "api") for r in self.registros("sonda")))

    def test_um_controle_fora_ainda_conta_rede_ok(self):
        s = self.sonda(c1=["rede"])
        self.assertEqual(s.rodada()["estado"], "ok")

    def test_429_e_lento_sao_degradado_sem_retry_e_sem_acelerar(self):
        s = self.sonda(portal=["429"], api=["lento"])
        rec = s.rodada()
        self.assertEqual((rec["estado"], rec["proxima_em_s"], s.incidente), ("degradado", 300, False))
        self.assertEqual([r["tentativa"] for r in self.registros("sonda") if r["alvo"] == "portal"], [1])
        self.assertEqual((rec["bloqueios_429"], rec["lentos"]), (["portal"], ["api"]))


    def test_registro_ausente_e_degradado_sem_retry_e_avisa_uma_vez(self):
        s = self.sonda(api=["ausente", "ausente"])
        rec = s.rodada()
        self.assertEqual((rec["estado"], rec["registros_ausentes"], s.incidente), ("degradado", ["api"], False))
        self.assertEqual([r["tentativa"] for r in self.registros("sonda") if r["alvo"] == "api"], [1])
        s.rodada()
        self.assertEqual(len([e for e in self.registros("evento") if e["evento"] == "registro_ausente"]), 1)
        self.assertEqual(sum("registro de teste" in a for a in self.avisos), 1)


class TestRegistroDiagnostico(Base):
    def test_campos_e_horario_do_pncp(self):
        s = self.sonda(portal=["503", "503"])
        s.rodada()
        f = [r for r in self.registros("sonda") if r["alvo"] == "portal"][0]
        for campo in ("ts_local", "ts_utc", "rodada", "http", "ip", "dns_ms", "tcp_ms", "tls_ms", "ttfb_ms",
                      "total_ms", "cabecalhos", "corpo_trecho", "cabecalhos_completos", "curl_exit"):
            self.assertIn(campo, f)
        self.assertTrue(f["ts_utc"].endswith("Z"))
        self.assertRegex(f["ts_local"], r"[+-]\d\d:\d\d$")
        ok = [r for r in self.registros("sonda") if r["alvo"] == "api"][0]
        self.assertIn("corpo_sha256", ok)
        self.assertNotIn("corpo_trecho", ok)

    def test_extrai_timestamp_do_erro_do_proprio_pncp(self):
        cfg = cfg_teste()
        a = alvo("/503")
        m = core.medir(a, cfg)
        s = core.Sonda(self.pasta, cfg_rodada())
        rec = s._registro_sonda("r1", a, 1, m, *core.classificar(m, a, cfg))
        self.assertEqual(rec["pncp_ts_erro"], "2026-09-20T05:06:46.370+00:00")
        self.assertIn("JDBC", rec["corpo_trecho"])

    def test_mudanca_de_ip_vira_evento(self):
        s = self.sonda()
        s.rodada()
        s.ips["portal"] = "9.9.9.9"
        s.rodada()
        ev = [e for e in self.registros("evento") if e["evento"] == "mudanca_ip"]
        self.assertEqual((len(ev), ev[0]["de"], ev[0]["para"]), (1, "9.9.9.9", "1.2.3.4"))


class TestLacunasEInicio(Base):
    def test_lacuna_por_suspensao_do_pc(self):
        s = self.sonda()
        s.rodada()
        self.relogio.avancar(hours=2)
        s.rodada()
        ev = [e for e in self.registros("evento") if e["evento"] == "retomada_apos_lacuna"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["motivo"], "suspensao_ou_indisponibilidade")
        self.assertGreater(ev[0]["lacuna_s"], 3600)

    def test_pausa_manual_nao_e_confundida_com_suspensao(self):
        s = self.sonda()
        s.rodada()
        s.pausar(True)
        self.relogio.avancar(hours=1)
        s.pausar(False)
        s.rodada()
        ev = [e for e in self.registros("evento") if e["evento"] == "retomada_apos_lacuna"]
        self.assertEqual(ev[0]["motivo"], "pausa_manual")

    def test_intervalo_normal_nao_gera_lacuna(self):
        s = self.sonda()
        s.rodada()
        self.relogio.avancar(seconds=305)
        s.rodada()
        self.assertFalse([e for e in self.registros("evento") if e["evento"] == "retomada_apos_lacuna"])

    def test_inicio_detecta_encerramento_sujo_e_limpo(self):
        s = self.sonda()
        s.rodada()
        self.relogio.avancar(hours=3)
        s2 = self.sonda()
        s2.iniciar()
        ini = [e for e in self.registros("evento") if e["evento"] == "sonda_iniciada"][-1]
        self.assertFalse(ini["encerramento_anterior_limpo"])
        self.assertAlmostEqual(ini["gap_desde_anterior_s"], 3 * 3600, delta=5)
        s2.encerrar("menu")
        self.relogio.avancar(minutes=1)
        s3 = self.sonda()
        s3.iniciar()
        ini = [e for e in self.registros("evento") if e["evento"] == "sonda_iniciada"][-1]
        self.assertTrue(ini["encerramento_anterior_limpo"])

    def test_inicio_grava_uptime_do_pc(self):
        with mock.patch.object(core, "uptime_pc_s", return_value=1234):
            self.sonda().iniciar()
        ini = [e for e in self.registros("evento") if e["evento"] == "sonda_iniciada"][-1]
        self.assertEqual(ini["uptime_pc_s"], 1234)

    def test_inicio_sem_uptime_omite_o_campo(self):
        with mock.patch.object(core, "uptime_pc_s", return_value=None):
            self.sonda().iniciar()
        ini = [e for e in self.registros("evento") if e["evento"] == "sonda_iniciada"][-1]
        self.assertNotIn("uptime_pc_s", ini)

    @unittest.skipUnless(sys.platform == "win32", "GetTickCount64 é do Windows")
    def test_uptime_real_do_windows_e_plausivel(self):
        up = core.uptime_pc_s()
        self.assertIsInstance(up, int)
        self.assertGreater(up, 0)

    def test_encerrar_no_meio_da_rodada_nao_grava_resumo_e_o_proximo_inicio_ve_encerramento_limpo(self):
        s = self.sonda()
        s.dormir = lambda seg: s.encerrar("menu")  # o usuário encerra logo após o 1º controle
        self.assertIsNone(s.rodada())
        self.assertEqual(self.registros("rodada"), [])
        self.relogio.avancar(seconds=30)
        s2 = self.sonda()
        s2.iniciar()
        ini = [e for e in self.registros("evento") if e["evento"] == "sonda_iniciada"][-1]
        self.assertTrue(ini["encerramento_anterior_limpo"])

    def test_disponibilidade_24h_reconstruida_do_log(self):
        s = self.sonda(portal=["503", "503"])
        s.rodada()
        s.rodada()
        s.rodada()  # 1 falha em 3 rodadas
        s2 = self.sonda()
        s2.iniciar()
        self.assertAlmostEqual(s2.disponibilidade_24h(), 100 * 2 / 3, places=1)


class TestLogEConfig(Base):
    def test_rotacao_a_meia_noite_local(self):
        log = core.Log(self.pasta)
        log.escrever({"ts_local": "2026-09-20T23:59:59.000-03:00", "tipo": "evento"})
        log.escrever({"ts_local": "2026-09-21T00:00:01.000-03:00", "tipo": "evento"})
        nomes = sorted(p.name for p in (self.pasta / "logs").glob("sonda-*.jsonl"))
        self.assertEqual(nomes, ["sonda-2026-09-20.jsonl", "sonda-2026-09-21.jsonl"])

    def test_linha_truncada_e_ignorada_na_leitura(self):
        (self.pasta / "logs").mkdir()
        (self.pasta / "logs" / "sonda-2026-09-20.jsonl").write_text(
            '{"tipo":"evento","ts_local":"2026-09-20T10:00:00-03:00"}\n{"tipo":"even', encoding="utf-8")
        regs = list(core.ler_registros(self.pasta, datetime(2026, 9, 20).date(), datetime(2026, 9, 20).date()))
        self.assertEqual(len(regs), 1)

    def test_compacta_logs_antigos_e_continua_legivel(self):
        (self.pasta / "logs").mkdir()
        arq = self.pasta / "logs" / "sonda-2020-01-01.jsonl"
        arq.write_text('{"tipo":"evento","ts_local":"2020-01-01T10:00:00-03:00"}\n', encoding="utf-8")
        self.assertEqual(core.compactar_antigos(self.pasta, 30), 1)
        self.assertFalse(arq.exists())
        self.assertTrue(Path(str(arq) + ".gz").exists())
        self.assertEqual(len(list(core.ler_registros(self.pasta, datetime(2020, 1, 1).date(),
                                                     datetime(2020, 1, 1).date()))), 1)

    def test_config_padrao_gravada_e_mesclada(self):
        cfg = core.carregar_config(self.pasta)
        self.assertEqual(cfg["intervalo_normal_s"], 300)
        self.assertTrue((self.pasta / "config.json").exists())
        core.salvar_config_chave(self.pasta, "intervalo_normal_s", 120)
        (self.pasta / "config.json").write_text(json.dumps({"intervalo_normal_s": 120}), encoding="utf-8")
        cfg = core.carregar_config(self.pasta)
        self.assertEqual((cfg["intervalo_normal_s"], cfg["intervalo_incidente_s"]), (120, 60))
        self.assertEqual(len(cfg["alvos"]), len(core.ALVOS_PADRAO))

    def test_alvos_padrao_cobrem_o_que_foi_aprovado(self):
        ids = {a["id"] for a in core.ALVOS_PADRAO}
        self.assertEqual(ids, {"ctrl_google", "ctrl_cloudflare", "portal", "api_contratacoes", "api_contratos",
                               "api_atas", "api_pca", "api_itens"})
        self.assertEqual(sum(a["tipo"] == "controle" for a in core.ALVOS_PADRAO), 2)
        for a in core.ALVOS_PADRAO:  # placeholders de data resolvem sem erro
            self.assertNotIn("{", core.expandir_url(a["url"]))


class TestDemora(unittest.TestCase):
    def test_conta_resposta_lenta_e_timeout_mas_ignora_erro_rapido(self):
        def x(res, ms):
            return {"resultado": res, "total_ms": ms}
        s1 = [x("ok", 500), x("lento", 12000), x("timeout", 30000), x("erro_http", 50), x("bloqueio_429", 40)]
        self.assertEqual(rel._demora(s1, 10), "66,7")  # 2 de 3 (erro rápido e 429 fora da base)
        self.assertEqual(rel._demora(s1, 20), "33,3")
        self.assertEqual(rel._demora([x("erro_http", 50)], 10), "")


class TestGraficoBarras(unittest.TestCase):
    def test_granularidade_acompanha_o_periodo(self):
        self.assertEqual(rel._granularidade(3600, 300)[0], 300)  # 1 h: 12 barras de 5 min
        self.assertEqual(rel._granularidade(24 * 3600, 300)[0], 300)  # 1 dia: 288 barras
        self.assertEqual(rel._granularidade(7 * 86400, 300)[0], 3600)  # 7 dias: 168 barras de 1 h
        self.assertEqual(rel._granularidade(30 * 86400, 300)[0], 6 * 3600)  # 30 dias: 120 barras de 6 h
        self.assertEqual(rel._granularidade(200 * 86400, 300)[0], 86400)

    def test_cor_do_balde(self):
        self.assertEqual(rel._cor_do_balde(12, 3, 0, 0), "falha")  # 25% falhou
        self.assertEqual(rel._cor_do_balde(12, 2, 0, 0), "lento")  # alguma falha, menos de 25%
        self.assertEqual(rel._cor_do_balde(12, 0, 3, 0), "lento")  # 25% lentas
        self.assertEqual(rel._cor_do_balde(12, 0, 2, 0), "ok")
        self.assertEqual(rel._cor_do_balde(2, 0, 0, 2), "429")
        self.assertEqual(rel._cor_do_balde(1, 1, 0, 0), "falha")  # 1 medição = a própria cor

    def test_baldes_falha_vira_teto_e_429_fica_fora_da_latencia(self):
        med = [(0, "ok", 500), (10, "timeout", 30000), (400, "ok", 200), (410, "bloqueio_429", 50)]
        b = rel._baldes(med, 0, 300, 30)
        self.assertEqual(sorted(b), [0, 1])  # 2 baldes de 5 min; só existe balde com dado
        self.assertEqual((b[0][0], b[0][1], b[0][2:]), ("falha", 30, (2, 1, 0)))  # p95 = teto por causa do timeout
        self.assertEqual((b[1][0], b[1][1]), ("ok", 0.2))  # o 429 não entra na latência

    def test_rotulo_do_periodo(self):
        self.assertEqual([rel._rotulo(x) for x in (100, 99, 98.9, 95, 94.9, None)],
                         ["ok", "ok", "lento", "lento", "falha", "vazio"])


class TestOrgaoDeTeste(Base):
    def test_padrao_gera_as_mesmas_urls_de_sempre(self):
        itens = [x for x in core.ALVOS_PADRAO if x["id"] == "api_itens"][0]
        self.assertEqual(core.expandir_url(itens["url"]),
                         "https://pncp.gov.br/api/pncp/v1/orgaos/83102277000152/compras/2026/495/itens"
                         "?pagina=1&tamanhoPagina=10")
        atas = [x for x in core.ALVOS_PADRAO if x["id"] == "api_atas"][0]
        self.assertIn("&cnpj=83102277000152&", core.expandir_url(atas["url"]))

    def test_trocar_orgao_e_compra_muda_todas_as_urls_que_usam_marcador(self):
        cfg = cfg_teste(cnpj_teste="11222333000181", compra_teste={"ano": 2025, "sequencial": 7})
        urls = {x["id"]: core.expandir_url(x["url"], cfg=cfg) for x in core.ALVOS_PADRAO}
        for alvo_id in ("api_contratos", "api_atas", "api_pca", "api_itens"):
            self.assertIn("11222333000181", urls[alvo_id])
            self.assertNotIn("83102277000152", urls[alvo_id])
        self.assertIn("/compras/2025/7/itens", urls["api_itens"])

    def test_url_fixa_de_config_antigo_continua_valendo(self):
        fixa = "https://pncp.gov.br/api/consulta/v1/atas/atualizacao?cnpj=99999999000191&pagina=1"
        self.assertEqual(core.expandir_url(fixa, cfg=cfg_teste()), fixa)

    def test_config_normaliza_cnpj_com_pontuacao_e_recusa_valor_ruim(self):
        (self.pasta / "config.json").write_text(json.dumps(
            {"cnpj_teste": "11.222.333/0001-81", "compra_teste": {"ano": "2025", "sequencial": "7"}}), encoding="utf-8")
        cfg = core.carregar_config(self.pasta)
        self.assertEqual((cfg["cnpj_teste"], cfg["compra_teste"]), ("11222333000181", {"ano": 2025, "sequencial": 7}))
        (self.pasta / "config.json").write_text(json.dumps({"cnpj_teste": "123"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "14 dígitos"):
            core.carregar_config(self.pasta)
        (self.pasta / "config.json").write_text(json.dumps({"compra_teste": {"ano": 2026}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "compra_teste"):
            core.carregar_config(self.pasta)


class TestRelatorio(Base):
    def test_disponibilidade_janelas_ocorrencias_e_lacunas(self):
        # portal, 1ª tentativa nas 5 rodadas: ok, ok, 503 (+retry 503), ok, ok → 4/5 = 80%
        s = self.sonda(portal=["ok", "ok", "503", "503", "ok"], api=["lento"])
        for _ in range(4):
            s.rodada()
            self.relogio.avancar(minutes=5)
        self.relogio.avancar(hours=3)  # lacuna
        s.rodada()
        out = rel.gerar_relatorio(self.pasta, dias=3, agora=self.relogio.t + timedelta(minutes=1))

        def ler(nome):
            with open(out / nome, encoding="utf-8-sig", newline="") as f:
                return list(csv.reader(f, delimiter=";"))

        resumo = {(linha[1]): linha for linha in ler("1_resumo_diario.csv")[1:]}
        self.assertEqual(resumo["portal"][3:9], ["5", "4", "0", "1", "0", "80,00"])  # 5 rodadas: 1 falha
        self.assertEqual(resumo["api"][4:6], ["4", "1"])  # api: 4 ok + 1 lenta (só na 1ª rodada)
        janelas = ler("2_janelas_de_incidente.csv")[1:]
        self.assertEqual(len(janelas), 1)
        self.assertEqual(janelas[0][3:5], ["1", "portal"])
        self.assertIn("banco de dados", janelas[0][6])
        ocorr = ler("3_ocorrencias.csv")[1:]
        self.assertEqual(sorted({(o[2], o[3]) for o in ocorr if o[4] != "ok"} & {("portal", "1"), ("portal", "2")}),
                         [("portal", "1"), ("portal", "2")])
        lacunas = ler("5_lacunas.csv")[1:]
        self.assertEqual(len(lacunas), 1)
        self.assertGreater(float(lacunas[0][2].replace(",", ".")), 170)
        self.assertEqual(len(ler("4_cobertura_diaria.csv")) - 1, 1)  # só o dia do 1º registro: antes dele a sonda não existia
        agora = self.relogio.t + timedelta(minutes=1)
        self.assertEqual(rel.gerar_relatorio(self.pasta, dias=3, agora=agora + timedelta(hours=2)), out)  # mesmo dia
        self.assertEqual(len(list((self.pasta / "relatorios").iterdir())), 1)
        html_ = (out / "resumo_para_chamado.html").read_text(encoding="utf-8")
        self.assertIn("PNCP — 6 serviços monitorados", html_)  # o relatório lê os alvos do config da pasta
        self.assertIn("5 sem dados", html_)  # só o portal tem medição neste teste
        self.assertIn("1 barra = 1 medição", html_)  # período curto: uma barra por medição, sem baldes vazios
        self.assertIn("url(#hach)", html_)  # falha hachurada
        self.assertIn("Operacional", html_)
        self.assertNotIn("e mais", html_)  # 1 janela: nada truncado
        pg = (out / "resumo_para_chamado.html").read_text(encoding="utf-8")
        self.assertIn('id="solicitante" contenteditable="true"', pg)  # campo editável direto no HTML
        self.assertIn("preencha a identificação", pg)  # aviso vermelho enquanto estiver vazio
        self.assertIn("<td>80,00</td>", pg)  # disponibilidade do portal, igual ao CSV
        self.assertIn("1 lacuna", pg)
        self.assertIn("banco de dados", pg)  # trecho da resposta do PNCP nos exemplos


if __name__ == "__main__":
    unittest.main()
