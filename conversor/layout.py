"""Passo 2 — Document Layout Analysis por Visão Computacional (DocLayout-YOLO).

Substitui a antiga segmentação por projeção/limiares matemáticos (OpenCV) por um
modelo de detecção treinado em documentos. Para cada página:

    * o modelo (ver detector.py) devolve as regiões com suas classes de layout;
    * mapeamos cada classe para TEXTO ou IMAGEM;
    * regiões de TEXTO  -> OCR (Tesseract) aplicado APENAS no recorte, sobre uma
      cópia pré-processada (cinza + Otsu);
    * regiões de IMAGEM -> recorte da imagem ORIGINAL colorida (sem OCR);
    * os blocos saem ordenados no fluxo de leitura (cima -> baixo, esq -> dir).
"""

from __future__ import annotations

import numpy as np

from .config import Block, BlockKind, PipelineConfig
from .detector import detect_regions
from .ocr import ocr_region_text


def analyze_page(bgr: np.ndarray, cfg: PipelineConfig) -> list[Block]:
    """Detecta, transcreve e ordena os blocos de uma página.

    A imagem colorida original (`bgr`) é preservada: o pré-processamento para OCR
    ocorre só na cópia do recorte de texto (ver ocr.ocr_region_text), enquanto os
    recortes de IMAGEM usam o `bgr` intacto no docx_builder.
    """
    height = bgr.shape[0]
    blocks: list[Block] = []

    for det in detect_regions(bgr, cfg):
        x, y, w, h = det.bbox
        if det.kind is BlockKind.TEXT:
            crop = bgr[y : y + h, x : x + w]
            text = ocr_region_text(crop, cfg)
            if text.strip():
                blocks.append(Block(kind=BlockKind.TEXT, bbox=det.bbox, text=text))
            else:
                # OCR não leu nada aproveitável: preserva a região como imagem
                # (melhor manter o conteúdo visível do que descartá-lo).
                blocks.append(Block(kind=BlockKind.IMAGE, bbox=det.bbox))
        else:
            blocks.append(Block(kind=BlockKind.IMAGE, bbox=det.bbox))

    # Ordena por faixa vertical (tolerância = 1,5% da altura) e depois horizontal,
    # reproduzindo o fluxo de leitura de um manual (inclusive múltiplas colunas).
    band = max(1, int(height * 0.015))
    blocks.sort(key=lambda b: (b.top // band, b.left))
    return blocks
