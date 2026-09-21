"""Sonda PNCP - ícone na bandeja que mede a disponibilidade do PNCP e grava um log por dia.

Uso:   pythonw sonda_pncp.pyw               sobe a sonda na bandeja (instância única)
       python  sonda_pncp.pyw --uma-rodada  faz uma verificação e imprime o resultado
       pythonw sonda_pncp.pyw --encerrar    pede para a instância em execução encerrar
       python  sonda_pncp.pyw --relatorio 7 gera o relatório dos últimos N dias e sai

O ícone na bandeja é a forma normal de encerrar (menu "Encerrar"); `--encerrar` cobre o
caso de o ícone estar escondido na área de "ícones ocultos".
"""

import json
import os
import socket
import subprocess  # nosec B404
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

import sonda_core as core  # noqa: E402

STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
ATALHO = STARTUP / "Sonda PNCP.lnk"
CORES = {"verde": (34, 166, 79), "amarelo": (240, 180, 0), "vermelho": (214, 48, 49), "cinza": (128, 128, 128)}
FLAGS = ("--encerrar", "--uma-rodada", "--relatorio")
USO = ("uso: sonda_pncp.pyw [--uma-rodada | --encerrar | --relatorio [DIAS 1-365]]\n"
       "     (sem argumentos: sobe a sonda na bandeja)")
INTERVALO_VIGIA_S = 30


def _sem_console():
    """pythonw não tem console: sem isto, qualquer erro some."""
    if sys.stderr is None or sys.stdout is None:
        (RAIZ / "logs").mkdir(exist_ok=True)
        arq = open(RAIZ / "logs" / "sonda-erros.log", "a", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115
        sys.stdout = sys.stderr = arq


def _caixa(titulo, texto):
    """Caixa de mensagem do Windows: erro de partida não pode ficar só num arquivo que ninguém abre."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, texto, titulo, 0x10)  # 0x10 = ícone de erro
    except Exception:  # noqa: BLE001  # nosec B110
        pass  # sem interface (CI, sessão sem desktop): o texto já foi para o sonda-erros.log


def _falha_de_partida(texto):
    core.avisar_texto(RAIZ, texto)
    _caixa("Sonda PNCP não iniciou", texto)


def _tomar_instancia(porta):
    s = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", porta))
        s.listen(2)
        return s
    except OSError:
        s.close()
        return None


def _pingar(porta, tentativas=3, timeout=2.0):
    """True só se quem escuta na porta É uma Sonda (responde ao ping). Porta ocupada por outro programa dá False.
    Tenta mais de uma vez: uma sonda que acabou de abrir ainda pode estar terminando a partida."""
    for _ in range(tentativas):
        try:
            with socket.create_connection(("127.0.0.1", porta), timeout=timeout) as c:
                c.settimeout(timeout)
                c.sendall(b"ping\n")
                if c.recv(64).strip() == b"sonda-pncp":
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def _enviar_encerrar(porta, timeout=5.0):
    """True só se a Sonda confirmou ("ok"). Antes bastava conseguir conectar, e um outro programa na porta
    respondia "encerrada" sem que nada tivesse acontecido."""
    try:
        with socket.create_connection(("127.0.0.1", porta), timeout=timeout) as c:
            c.settimeout(timeout)
            c.sendall(b"encerrar\n")
            return c.recv(16).strip() == b"ok"
    except OSError:
        return False


def atender(srv, parar, encerrar):
    """Atende `ping` e `encerrar` vindos de outras execuções. Erro no accept não pode calar o servidor."""
    srv.settimeout(1)
    seguidas = 0
    while not parar.is_set():
        try:
            c, _ = srv.accept()
            seguidas = 0
        except TimeoutError:
            continue
        except OSError as e:
            seguidas += 1
            if seguidas >= 20:  # erro persistente: desiste, mas deixa dito
                core.avisar_texto(RAIZ, f"servidor local da instância única parou: {e}")
                return
            time.sleep(0.5)
            continue
        with c:
            c.settimeout(2)
            try:
                msg = c.recv(64).decode("utf-8", "replace").strip()
                if msg == "ping":
                    c.sendall(b"sonda-pncp\n")
                elif msg == "encerrar":
                    c.sendall(b"ok\n")  # confirma antes: encerrar() derruba o processo
            except OSError:
                msg = ""
        if msg == "encerrar":
            encerrar()


def autostart_ativo():
    return ATALHO.exists()


def _powershell():
    p = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(p) if p.exists() else "powershell.exe"


def definir_autostart(ligar):
    """Atalho na pasta Startup do usuário (sem admin) apontando direto para o pythonw. Devolve se deu certo.
    Os caminhos vão por variáveis de ambiente, não dentro do texto do script: um caminho com aspas tipográficas
    ou `$` não vira código do PowerShell."""
    if not ligar:
        ATALHO.unlink(missing_ok=True)
        return True
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SONDA_LNK);"
          "$s.TargetPath=$env:SONDA_EXE;$s.Arguments='\"'+$env:SONDA_ARQ+'\"';"
          "$s.WorkingDirectory=$env:SONDA_DIR;$s.Description='Sonda PNCP';$s.Save()")
    env = {**os.environ, "SONDA_LNK": str(ATALHO), "SONDA_EXE": str(Path(sys.executable).with_name("pythonw.exe")),
           "SONDA_ARQ": str(RAIZ / "sonda_pncp.pyw"), "SONDA_DIR": str(RAIZ)}
    # só o PowerShell do atalho; script fixo, caminho absoluto do System32
    p = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, env=env, creationflags=core.CREATE_NO_WINDOW, timeout=30)  # nosec B603
    return p.returncode == 0 and ATALHO.exists()


def _notificar(titulo, msg):
    try:
        from winotify import Notification
        Notification(app_id="Sonda PNCP", title=titulo, msg=msg, duration="short").show()
    except Exception as e:  # noqa: BLE001
        core.avisar_texto(RAIZ, f"notificação não exibida ({titulo}: {msg}): {e!r}")  # acessório: nunca derruba a sonda


def _desenhar(cor):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=CORES[cor] + (255,), outline=(30, 30, 30, 255), width=3)
    d.text((32, 33), "S", fill=(255, 255, 255, 255), font=ImageFont.load_default(size=34), anchor="mm")
    return img


def bandeja():
    _sem_console()
    try:
        cfg = core.carregar_config(RAIZ)
    except (ValueError, OSError) as e:
        _falha_de_partida(f"Não foi possível ler o config.json:\n\n{e}")
        return 1
    porta = cfg["porta_instancia"]
    srv = _tomar_instancia(porta)
    if srv is None:
        if _pingar(porta):  # já existe uma Sonda PNCP rodando: nada a fazer
            return 0
        _falha_de_partida(f"A porta {porta} está em uso por outro programa, então a Sonda PNCP não pode iniciar.\n\n"
                          "Troque 'porta_instancia' no config.json ou encerre o programa que a usa.")
        return 1
    import pystray

    sonda = core.Sonda(RAIZ, cfg, notificar_fn=_notificar)
    holder = {}

    def atualizar():
        ic = holder.get("icone")
        if ic:
            ic.icon = _desenhar("vermelho" if sonda.alerta else sonda.cor)
            ic.title = sonda.tooltip()
            ic.update_menu()

    sonda.ao_mudar = atualizar

    def encerrar(motivo="menu"):
        sonda.encerrar(motivo)  # não levanta e marca `parar` primeiro
        try:
            if holder.get("icone"):
                holder["icone"].stop()
        except Exception as e:  # noqa: BLE001
            sonda.registrar_erro(e)

    def gerar():
        try:
            pasta = core.gerar_relatorio(RAIZ, 7)
            _notificar("Sonda PNCP", f"Relatório gerado: {pasta.name}")
            os.startfile(pasta)  # nosec
        except Exception as e:  # noqa: BLE001
            sonda.registrar_erro(e)
            _notificar("Sonda PNCP: relatório não gerado", str(e)[:200])

    def alternar_autostart(_i, _it):
        try:
            novo = not autostart_ativo()
            if definir_autostart(novo):
                core.salvar_config_chave(RAIZ, "iniciar_com_windows", novo)
            else:
                _notificar("Sonda PNCP", "Não foi possível criar o atalho de início automático.")
        except Exception as e:  # noqa: BLE001
            sonda.registrar_erro(e)
            _notificar("Sonda PNCP", f"Início automático não alterado: {str(e)[:150]}")

    menu = pystray.Menu(
        pystray.MenuItem(lambda _i: sonda.texto_status(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Verificar agora", lambda _i, _it: sonda.disparar.set(),
                         enabled=lambda _i: not sonda.pausada),
        # os.startfile só abre pastas da própria sonda (logs/relatórios)
        pystray.MenuItem("Abrir pasta de logs", lambda _i, _it: os.startfile(RAIZ / "logs")),  # nosec
        pystray.MenuItem("Gerar relatório (últimos 7 dias)",
                         lambda _i, _it: threading.Thread(target=gerar, daemon=True).start()),
        pystray.MenuItem("Pausar sonda", lambda _i, _it: sonda.pausar(not sonda.pausada),
                         checked=lambda _i: sonda.pausada),
        pystray.MenuItem("Iniciar com o Windows", alternar_autostart, checked=lambda _i: autostart_ativo()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Encerrar", lambda _i, _it: encerrar()),
    )

    def vigiar():
        """Sem isto o ícone fica verde para sempre com o laço morto. A thread que morreu é prova imediata; laço vivo
        mas sem medir precisa de duas verificações seguidas (evita alarme falso ao voltar de uma suspensão)."""
        suspeitas = 0
        while not sonda.parar.wait(INTERVALO_VIGIA_S):
            estado = sonda.saude()
            suspeitas = suspeitas + 1 if estado == "parada" else 0
            novo = "morta" if estado == "morta" else ("parada" if suspeitas >= 2 else None)
            if novo != sonda.alerta:
                sonda.alerta = novo
                if novo:
                    sonda.registrar_erro(RuntimeError(f"vigia: o laço de medição está {novo}"))
                    _notificar("Sonda PNCP: parou de medir", "O laço de medição não está mais rodando. Reinicie a sonda.")
                try:
                    atualizar()
                except Exception as e:  # noqa: BLE001
                    sonda.registrar_erro(e)

    def preparar(icone):
        icone.visible = True
        etapas = [sonda.iniciar]
        if cfg["iniciar_com_windows"] and not autostart_ativo():
            etapas.insert(0, lambda: definir_autostart(True))
        for etapa in etapas:  # cada etapa protegida: falhar numa não pode impedir o laço de subir
            try:
                etapa()
            except Exception as e:  # noqa: BLE001
                sonda.registrar_erro(e)
                _notificar("Sonda PNCP: problema na partida", str(e)[:200])
        laco = threading.Thread(target=sonda.laco, daemon=True, name="laco")
        sonda.thread_laco = laco
        laco.start()
        threading.Thread(target=atender, args=(srv, sonda.parar, lambda: encerrar("comando_encerrar")), daemon=True).start()
        threading.Thread(target=vigiar, daemon=True).start()

    holder["icone"] = pystray.Icon("sonda_pncp", _desenhar("cinza"), "Sonda PNCP - iniciando", menu)
    try:
        holder["icone"].run(setup=preparar)
    finally:
        if not sonda.parar.is_set():
            sonda.encerrar("saida_inesperada")
        srv.close()
    return 0


def _dias(argv):
    """Argumento de --relatorio: ausente = 7; senão um inteiro de 1 a 365 (None = inválido)."""
    i = argv.index("--relatorio")
    if len(argv) <= i + 1 or argv[i + 1].startswith("--"):
        return 7
    txt = argv[i + 1]
    return int(txt) if txt.isascii() and txt.isdigit() and 1 <= int(txt) <= 365 else None


def main(argv):
    if any(a.startswith("--") and a not in FLAGS for a in argv):
        print(USO, file=sys.stderr)  # antes, um erro de digitação subia a sonda na bandeja sem avisar
        return 2
    try:
        if "--encerrar" in argv:
            return 0 if _enviar_encerrar(core.carregar_config(RAIZ)["porta_instancia"]) else 1
        if "--uma-rodada" in argv:
            rec = core.Sonda(RAIZ).rodada()
            print(json.dumps(rec, ensure_ascii=False, indent=2))
            return 0
        if "--relatorio" in argv:
            dias = _dias(argv)
            if dias is None:
                print(USO, file=sys.stderr)
                return 2
            print(core.gerar_relatorio(RAIZ, dias))
            return 0
    except ValueError as e:  # config.json inválido
        print(f"erro: {e}", file=sys.stderr)
        return 1
    return bandeja()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
