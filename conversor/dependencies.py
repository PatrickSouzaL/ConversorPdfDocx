"""Resolução das dependências de sistema (Poppler e dados de idioma do Tesseract).

O `pdf2image` precisa dos binários do Poppler e o `pytesseract` do binário do
Tesseract. Para funcionar sem exigir instalação no sistema/PATH, estas funções
detectam automaticamente cópias locais do projeto (`.poppler/` e `.tessdata/`).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# Dependências de IA (Visão Computacional)
# ---------------------------------------------------------------------------


def ml_dependencies_available() -> tuple[bool, list[str]]:
    """Verifica se os pacotes de IA estão instalados, sem importá-los.

    Retorna (ok, ausentes). Usa find_spec para não carregar o torch (pesado) só
    para checar presença. O download dos pesos do modelo é tratado à parte.
    """
    required = ("torch", "doclayout_yolo", "huggingface_hub")
    missing = [mod for mod in required if importlib.util.find_spec(mod) is None]
    return (not missing, missing)


# ---------------------------------------------------------------------------
# Poppler (necessário para o pdf2image)
# ---------------------------------------------------------------------------


def find_poppler(project_root: Path) -> str | None:
    """Localiza o diretório `bin` do Poppler.

    Ordem de busca: PATH do sistema; depois um `.poppler/` local do projeto
    (Poppler portátil). Retorna o diretório que contém `pdftoppm(.exe)` ou None.
    """
    on_path = shutil.which("pdftoppm")
    if on_path:
        return str(Path(on_path).parent)

    local = project_root / ".poppler"
    if local.is_dir():
        for exe in local.rglob("pdftoppm*"):
            if exe.is_file():
                return str(exe.parent)
    return None


# ---------------------------------------------------------------------------
# Tesseract e dados de idioma
# ---------------------------------------------------------------------------


def tesseract_available() -> bool:
    """True se o binário do Tesseract está disponível no PATH."""
    return shutil.which("tesseract") is not None


def list_tesseract_langs(tessdata: str | None = None) -> set[str]:
    """Lista os idiomas disponíveis para o Tesseract (opcionalmente num tessdata)."""
    exe = shutil.which("tesseract")
    if exe is None:
        return set()
    env = os.environ.copy()
    if tessdata:
        env["TESSDATA_PREFIX"] = tessdata
    try:
        proc = subprocess.run(
            [exe, "--list-langs"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env,
        )
    except OSError:
        return set()
    # A 1ª linha é cabeçalho; as demais são os códigos de idioma.
    return {
        line.strip()
        for line in (proc.stdout or "").splitlines()[1:]
        if line.strip()
    }


def resolve_tessdata(lang: str, project_root: Path) -> tuple[str | None, list[str]]:
    """Decide qual diretório de dados de idioma usar para cobrir `lang`.

    Retorna (tessdata_dir, ausentes):
        * (None, [])         -> o Tesseract do sistema já tem todos os idiomas.
        * (caminho, [])      -> usar o `.tessdata` local do projeto.
        * (caminho|None, [x])-> idioma(s) ainda ausente(s).
    """
    requested = {code for code in lang.replace(" ", "").split("+") if code}
    system_langs = list_tesseract_langs()
    missing = sorted(requested - system_langs)
    if not missing:
        return None, []

    local = project_root / ".tessdata"
    if local.is_dir():
        local_langs = list_tesseract_langs(str(local))
        if requested <= local_langs:
            return str(local), []
        return str(local), sorted(requested - local_langs)

    return None, missing
