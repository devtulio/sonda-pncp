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
import subprocess
import sys
import threading
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

import sonda_core as core  # noqa: E402

STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
ATALHO = STARTUP / "Sonda PNCP.lnk"
CORES = {"verde": (34, 166, 79), "amarelo": (240, 180, 0), "vermelho": (214, 48, 49), "cinza": (128, 128, 128)}


def _sem_console():
    """pythonw não tem console: sem isto, qualquer erro some."""
    if sys.stderr is None or sys.stdout is None:
        (RAIZ / "logs").mkdir(exist_ok=True)
        arq = open(RAIZ / "logs" / "sonda-erros.log", "a", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115
        sys.stdout = sys.stderr = arq


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


def _enviar_encerrar(porta):
    try:
        with socket.create_connection(("127.0.0.1", porta), timeout=3) as c:
            c.sendall(b"encerrar\n")
        return True
    except OSError:
        return False


def autostart_ativo():
    return ATALHO.exists()


def definir_autostart(ligar):
    """Atalho na pasta Startup do usuário (sem admin) apontando direto para o pythonw."""
    if not ligar:
        ATALHO.unlink(missing_ok=True)
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    q = lambda p: str(p).replace("'", "''")  # noqa: E731
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(ATALHO)}');"
          f"$s.TargetPath='{q(pythonw)}';$s.Arguments='\"{q(RAIZ / 'sonda_pncp.pyw')}\"';"
          f"$s.WorkingDirectory='{q(RAIZ)}';$s.Description='Sonda PNCP';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, creationflags=core.CREATE_NO_WINDOW, timeout=30)


def _notificar(titulo, msg):
    try:
        from winotify import Notification
        Notification(app_id="Sonda PNCP", title=titulo, msg=msg, duration="short").show()
    except Exception:  # noqa: BLE001 - notificação é acessório, nunca derruba a sonda
        pass


def _desenhar(cor):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=CORES[cor] + (255,), outline=(30, 30, 30, 255), width=3)
    d.text((32, 33), "S", fill=(255, 255, 255, 255), font=ImageFont.load_default(size=34), anchor="mm")
    return img


def bandeja():
    _sem_console()
    cfg = core.carregar_config(RAIZ)
    srv = _tomar_instancia(cfg["porta_instancia"])
    if srv is None:  # já existe uma Sonda PNCP rodando
        return 0
    import pystray

    sonda = core.Sonda(RAIZ, cfg, notificar_fn=_notificar)
    holder = {}

    def atualizar():
        ic = holder.get("icone")
        if ic:
            ic.icon = _desenhar(sonda.cor)
            ic.title = sonda.tooltip()
            ic.update_menu()

    sonda.ao_mudar = atualizar

    def encerrar(motivo="menu"):
        sonda.encerrar(motivo)
        if holder.get("icone"):
            holder["icone"].stop()

    def gerar():
        try:
            pasta = core.gerar_relatorio(RAIZ, 7)
            _notificar("Sonda PNCP", f"Relatório gerado: {pasta.name}")
            os.startfile(pasta)
        except Exception as e:  # noqa: BLE001
            sonda.registrar_erro(e)

    def alternar_autostart(_i, _it):
        novo = not autostart_ativo()
        definir_autostart(novo)
        core.salvar_config_chave(RAIZ, "iniciar_com_windows", novo)

    menu = pystray.Menu(
        pystray.MenuItem(lambda _i: sonda.texto_status(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Verificar agora", lambda _i, _it: sonda.disparar.set(),
                         enabled=lambda _i: not sonda.pausada),
        pystray.MenuItem("Abrir pasta de logs", lambda _i, _it: os.startfile(RAIZ / "logs")),
        pystray.MenuItem("Gerar relatório (últimos 7 dias)",
                         lambda _i, _it: threading.Thread(target=gerar, daemon=True).start()),
        pystray.MenuItem("Pausar sonda", lambda _i, _it: sonda.pausar(not sonda.pausada),
                         checked=lambda _i: sonda.pausada),
        pystray.MenuItem("Iniciar com o Windows", alternar_autostart, checked=lambda _i: autostart_ativo()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Encerrar", lambda _i, _it: encerrar()),
    )

    def ouvir():  # atende `--encerrar` vindo de outra execução
        srv.settimeout(1)
        while not sonda.parar.is_set():
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with c:
                c.settimeout(2)
                try:
                    msg = c.recv(64).decode("utf-8", "replace").strip()
                except OSError:
                    msg = ""
            if msg == "encerrar":
                encerrar(motivo="comando_encerrar")

    def preparar(icone):
        icone.visible = True
        if cfg["iniciar_com_windows"] and not autostart_ativo():
            definir_autostart(True)
        sonda.iniciar()
        threading.Thread(target=sonda.laco, daemon=True).start()
        threading.Thread(target=ouvir, daemon=True).start()

    holder["icone"] = pystray.Icon("sonda_pncp", _desenhar("cinza"), "Sonda PNCP - iniciando", menu)
    try:
        holder["icone"].run(setup=preparar)
    finally:
        if not sonda.parar.is_set():
            sonda.encerrar("saida_inesperada")
        srv.close()
    return 0


def main(argv):
    if "--encerrar" in argv:
        return 0 if _enviar_encerrar(core.carregar_config(RAIZ)["porta_instancia"]) else 1
    if "--uma-rodada" in argv:
        rec = core.Sonda(RAIZ).rodada()
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0
    if "--relatorio" in argv:
        i = argv.index("--relatorio")
        dias = int(argv[i + 1]) if len(argv) > i + 1 and argv[i + 1].isdigit() else 7
        print(core.gerar_relatorio(RAIZ, dias))
        return 0
    return bandeja()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
