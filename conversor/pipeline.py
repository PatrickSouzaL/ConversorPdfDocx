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

from .config import ConversionResult, PipelineConfig, Status
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
