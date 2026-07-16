"""Pipeline de um único arquivo (ponto de entrada de cada worker).

Orquestra os três passos — rasterizar (rendering) → detectar layout + OCR
(layout/detector/ocr) → remontar o DOCX (docx_builder) — com fail-safe: qualquer
exceção vira um ConversionResult(FAILED), para que uma falha isolada nunca
derrube o lote. O modelo de IA é carregado uma única vez por worker (ver
detector._get_model), na primeira página processada por este processo.
"""

from __future__ import annotations

import time
from pathlib import Path

from .config import (
    JSON_ERROR_PREFIX,
    BlockKind,
    ConversionResult,
    PipelineConfig,
    Status,
)
from .docx_builder import DocxBuilder
from .layout import analyze_page
from .rendering import render_pages


def convert_single(source: Path, target: Path, cfg: PipelineConfig) -> ConversionResult:
    """Converte um PDF em DOCX editável reconstruído por Document Layout Analysis."""
    start = time.perf_counter()
    try:
        builder = DocxBuilder(cfg)
        pages = 0
        for bgr in render_pages(source, cfg.dpi, cfg.poppler_path):
            pages += 1
            blocks = analyze_page(bgr, cfg)
            builder.add_page(bgr, blocks)

        if pages == 0:
            raise RuntimeError("PDF sem páginas renderizáveis.")

        builder.save(target)
        elapsed = time.perf_counter() - start
        return ConversionResult(
            source=source,
            target=target,
            status=Status.CONVERTED,
            message=f"{builder.text_blocks} bloco(s) de texto, {builder.image_blocks} imagem(ns)",
            elapsed=elapsed,
            pages=pages,
            text_blocks=builder.text_blocks,
            image_blocks=builder.image_blocks,
        )
    except Exception as exc:  # noqa: BLE001 - fail-safe intencional por arquivo
        elapsed = time.perf_counter() - start
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        return ConversionResult(
            source=source,
            target=target,
            status=Status.FAILED,
            message=f"{type(exc).__name__}: {exc}",
            elapsed=elapsed,
        )


def extract_single_json(source: Path, cfg: PipelineConfig) -> dict:
    """Extrai o texto (OCR) de um PDF e devolve uma entrada da base de conhecimento.

    Diferente de `convert_single`, este worker NÃO grava nada em disco: ele apenas
    reaproveita os Passos 1 e 2 (rasterização + Document Layout Analysis) e devolve
    um dicionário para o orquestrador agregar. As regiões classificadas como IMAGEM
    são ignoradas sumariamente (sem recortes); só as regiões de TEXTO passam pelo
    OCR e têm o texto concatenado na ordem de leitura.

    Fail-safe: qualquer exceção vira uma entrada com o erro no campo "Resolucao"
    (prefixado por JSON_ERROR_PREFIX), para que uma falha isolada nunca derrube o
    lote nem impeça a gravação do arquivo agregado.
    """
    title = source.stem
    try:
        parts: list[str] = []
        pages = 0
        for bgr in render_pages(source, cfg.dpi, cfg.poppler_path):
            pages += 1
            for block in analyze_page(bgr, cfg):
                if block.kind is BlockKind.TEXT and block.text.strip():
                    parts.append(block.text.strip())

        if pages == 0:
            raise RuntimeError("PDF sem páginas renderizáveis.")

        return {"Titulo": title, "Resolucao": "\n".join(parts)}
    except Exception as exc:  # noqa: BLE001 - fail-safe intencional por arquivo
        return {
            "Titulo": title,
            "Resolucao": f"{JSON_ERROR_PREFIX}{type(exc).__name__}: {exc}",
        }
