"""Conversor PDF -> DOCX por Document Layout Analysis (DLA) com Visão Computacional.

Pipeline: renderiza cada página como imagem de alta resolução, detecta as regiões
de layout com um modelo de ML (DocLayout-YOLO), classifica cada região como TEXTO
ou IMAGEM/print, aplica OCR apenas às regiões de texto e recorta as regiões de
imagem, e por fim remonta um DOCX nativo e editável (python-docx) preservando o
fluxo visual de leitura (top-to-bottom).
"""

__all__ = [
    "config",
    "dependencies",
    "rendering",
    "detector",
    "layout",
    "ocr",
    "docx_builder",
    "pipeline",
]
