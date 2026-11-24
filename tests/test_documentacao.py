"""Guarda de documentação: o que dá para medir, mede. O resto está no checklist do RELEASING.md (seção 5.1)."""
import importlib.machinery
import importlib.util
import re
import unicodedata
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
INTERNAS = {"--aguardar"}  # flags fora do contrato (RELEASING.md, seção 1)


def ler(nome):
    return (RAIZ / nome).read_text(encoding="utf-8")


def sem_acento(txt):
    return "".join(c for c in unicodedata.normalize("NFD", txt) if unicodedata.category(c) != "Mn").lower()


def carregar_bandeja():
    loader = importlib.machinery.SourceFileLoader("sonda_pncp_doc", str(RAIZ / "sonda_pncp.pyw"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("sonda_pncp_doc", loader))
    loader.exec_module(mod)
    return mod


class TestDocumentacao(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = carregar_bandeja()
        cls.pyw = ler("sonda_pncp.pyw")

    def test_versao_do_codigo_e_a_ultima_do_changelog(self):
        numeradas = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", ler("CHANGELOG.md"), re.M)
        self.assertEqual(self.mod.core.VERSAO, numeradas[0],
                         "sonda_core.VERSAO deve ser a 1ª seção numerada do CHANGELOG (a de cima é `Não publicado`)")

    def test_versao_citada_no_leia_me(self):
        m = re.match(r"SONDA PNCP v(\d+\.\d+\.\d+)", ler("LEIA-ME.txt"))
        self.assertIsNotNone(m, "LEIA-ME.txt deve começar com «SONDA PNCP vX.Y.Z»")
        self.assertEqual(m[1], self.mod.core.VERSAO, "versão do cabeçalho do LEIA-ME diferente da do código")

    def test_toda_flag_esta_no_manual_e_as_do_contrato_no_releasing(self):
        manual, rel = ler("MANUAL.md"), ler("RELEASING.md")
        for f in self.mod.FLAGS:
            self.assertTrue(f in manual, f"flag {f} não está no MANUAL")
            self.assertTrue(f in rel, f"flag {f} não está no RELEASING (contrato ou «interna»)")
        for f in set(self.mod.FLAGS) - INTERNAS:
            self.assertTrue(f in (self.mod.USO if f != "--relatorio-mensal" else rel), f"flag {f} fora do USO")

    def test_todo_item_do_menu_esta_nos_tres_documentos(self):
        itens = re.findall(r'MenuItem\("([^"]+)"', self.pyw)
        self.assertGreaterEqual(len(itens), 6)  # se o regex parar de achar o menu, falha em vez de passar vazio
        docs = {n: sem_acento(ler(n)) for n in ("MANUAL.md", "README.md", "LEIA-ME.txt")}
        for item in itens:
            for nome, txt in docs.items():
                self.assertTrue(sem_acento(item.split("(")[0].strip()) in txt, f"menu «{item}» não está em {nome}")

    def test_todo_motivo_de_encerramento_esta_no_manual(self):
        motivos = set(re.findall(r'encerrar\("(\w+)"\)', self.pyw)) | set(re.findall(r'motivo="(\w+)"', self.pyw))
        motivos |= set(re.findall(r'sonda\.encerrar\("(\w+)"\)', self.pyw))
        self.assertGreaterEqual(len(motivos), 3)
        manual = ler("MANUAL.md")
        for m in motivos:
            self.assertTrue(f"`{m}`" in manual, f"motivo de sonda_encerrada «{m}» não está no MANUAL")

    def test_contagem_de_testes_do_manual(self):
        n = unittest.defaultTestLoader.discover(str(RAIZ / "tests")).countTestCases()
        self.assertTrue(f"{n} testes" in ler("MANUAL.md"), f"o MANUAL deve dizer «{n} testes»")

    def test_documentos_da_raiz_estao_no_readme(self):
        readme = ler("README.md")
        for nome in ("MANUAL.md", "CHANGELOG.md", "RELEASING.md"):
            self.assertTrue(nome in readme, f"{nome} não está listado no README")
        for alvo in re.findall(r"\]\((docs/[^)#]+)\)", readme):
            self.assertTrue((RAIZ / alvo).exists(), f"README aponta para {alvo}, que não existe")


if __name__ == "__main__":
    unittest.main()
