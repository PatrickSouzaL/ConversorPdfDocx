"""Estruturas de dados imutáveis e picláveis compartilhadas pelo pipeline.

Ficam isoladas aqui (sem dependências pesadas) para poderem ser importadas tanto
pelo processo principal quanto pelos workers do ProcessPoolExecutor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# --- Padrões de configuração ------------------------------------------------

DEFAULT_SRC_DIR = "pdf"
DEFAULT_DST_DIR = "docx"
LOG_FILE_NAME = "conversao.log"
SOURCE_SUFFIX = ".pdf"
TARGET_SUFFIX = ".docx"

# --- Modos de operação ------------------------------------------------------
# DOCX_MODE: reconstrói um .docx nativo por arquivo (comportamento original).
# JSON_MODE: extrai apenas o texto (OCR) de cada PDF e agrega tudo em um único
#            arquivo JSON (lista de dicionários) para ingestão em etapas futuras.
DOCX_MODE = "docx"
JSON_MODE = "json"
DEFAULT_JSON_FILE = "base_conhecimento.json"
# Prefixo que marca um erro no campo "Resolucao" de uma entrada JSON. Permite ao
# orquestrador contar falhas e definir o exit code sem alterar o formato da saída.
JSON_ERROR_PREFIX = "ERRO: "

DEFAULT_OCR_LANG = "por+eng"
DEFAULT_DPI = 300  # resolução de rasterização das páginas

# --- Modelo de Visão Computacional (DocLayout-YOLO / YOLOv10) ----------------
# Pesos hospedados no HuggingFace; baixados automaticamente no primeiro uso e
# cacheados (o download é feito no processo principal — ver detector.py).
MODEL_REPO_ID = "juliozhao/DocLayout-YOLO-DocStructBench"
MODEL_FILENAME = "doclayout_yolo_docstructbench_imgsz1024.pt"

DEFAULT_YOLO_CONF = 0.2   # confiança mínima de detecção do YOLO
DEFAULT_YOLO_IMGSZ = 1024  # tamanho de inferência (o modelo foi treinado em 1024)

# Mapeamento das classes do modelo para os nossos dois domínios essenciais.
# As comparações são feitas em minúsculas. Cobre a taxonomia do DocStructBench
# e também nomes comuns do DocLayNet/PubLayNet, para robustez a outros pesos.
TEXT_CLASSES = frozenset({
    "title", "plain text", "text", "paragraph", "list", "list-item", "list_item",
    "section-header", "section_header", "caption", "figure_caption",
    "table_caption", "table_footnote", "formula_caption", "footnote",
    "page-header", "page-footer",
})
IMAGE_CLASSES = frozenset({
    "figure", "picture", "image", "table", "isolate_formula", "formula",
})
# Regiões descartadas (ruído tipográfico: números de página, cabeçalhos soltos).
DISCARD_CLASSES = frozenset({"abandon"})


class Status(str, Enum):
    """Resultado possível para a conversão de um único arquivo."""

    CONVERTED = "convertido"
    SKIPPED = "ignorado"
    FAILED = "falha"


class BlockKind(str, Enum):
    """Classificação de uma região da página."""

    TEXT = "texto"
    IMAGE = "imagem"


@dataclass(frozen=True)
class PipelineConfig:
    """Parâmetros do pipeline (imutável e piclável para os workers)."""

    dpi: int = DEFAULT_DPI
    ocr_lang: str = DEFAULT_OCR_LANG
    tessdata: str | None = None       # --tessdata-dir para o Tesseract (fallback local)
    poppler_path: str | None = None   # diretório bin do Poppler (pdf2image)

    # --- Detecção de layout (DocLayout-YOLO) ---
    device: str = "cpu"               # device de inferência ("cuda:0" ou "cpu"), resolvido no main
    model_path: str | None = None     # caminho local dos pesos (pré-baixados no main)
    yolo_conf: float = DEFAULT_YOLO_CONF
    yolo_imgsz: int = DEFAULT_YOLO_IMGSZ
    cpu_threads: int | None = None    # threads do torch por worker em CPU (None = padrão do torch)

    # --- OCR das regiões de texto ---
    min_conf: int = 40                # confiança mínima (0-100) p/ aceitar palavra do OCR

    # --- Reconstrução do DOCX ---
    line_tolerance_frac: float = 0.6  # tolerância vertical (x altura da palavra) p/ agrupar linhas


@dataclass
class Block:
    """Uma região da página, já classificada e (se texto) transcrita."""

    kind: BlockKind
    bbox: tuple[int, int, int, int]   # (x, y, w, h) em pixels da página rasterizada
    text: str = ""                    # preenchido apenas quando kind == TEXT

    @property
    def top(self) -> int:
        return self.bbox[1]

    @property
    def left(self) -> int:
        return self.bbox[0]


@dataclass(frozen=True)
class ConversionResult:
    """Resultado imutável da tentativa de conversão de um arquivo."""

    source: Path
    target: Path
    status: Status
    message: str = ""
    elapsed: float = 0.0
    pages: int = 0
    text_blocks: int = 0
    image_blocks: int = 0
