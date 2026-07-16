"""Passo 1 — rasterização das páginas do PDF em imagens de alta resolução.

Usa o pdf2image (Poppler) para converter cada página em uma imagem e a entrega
como array NumPy no formato BGR (convenção do OpenCV).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
from pdf2image import convert_from_path


def render_pages(pdf_path: Path, dpi: int, poppler_path: str | None) -> Iterator[np.ndarray]:
    """Gera, página a página, a imagem BGR (OpenCV) da página rasterizada.

    Renderiza uma página por vez (`first_page`/`last_page`) para manter o uso de
    memória baixo mesmo em PDFs longos e de alta resolução.
    """
    from pdf2image.pdf2image import pdfinfo_from_path

    info = pdfinfo_from_path(str(pdf_path), poppler_path=poppler_path)
    page_count = int(info.get("Pages", 0))

    for page_number in range(1, page_count + 1):
        images = convert_from_path(
            str(pdf_path),
            dpi=dpi,
            first_page=page_number,
            last_page=page_number,
            poppler_path=poppler_path,
        )
        if not images:
            continue
        # PIL (RGB) -> NumPy -> BGR (OpenCV).
        rgb = np.asarray(images[0])
        yield cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
