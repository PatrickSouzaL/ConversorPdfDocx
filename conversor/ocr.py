"""OCR e pré-processamento de imagem para leitura de texto.

O OCR roda **por região**: o modelo de layout (detector.py) já isolou os blocos
de TEXTO, então o Tesseract lê apenas o recorte daquela coordenada. O
pré-processamento (escala de cinza + normalização de fundo escuro + Otsu) é
aplicado **somente nessa cópia** do recorte, para aumentar a precisão — a imagem
original colorida é preservada para o recorte dos prints (regiões de IMAGEM).
"""

from __future__ import annotations

import os

import cv2
import numpy as np
import pytesseract

from .config import PipelineConfig


def preprocess_for_ocr(bgr: np.ndarray) -> np.ndarray:
    """Prepara a imagem para o Tesseract: cinza, fundo claro, binarizada.

    Se a página tem fundo escuro (texto claro), inverte para o padrão que o
    Tesseract espera (texto escuro sobre fundo claro). Aplica um filtro que
    preserva bordas e binariza via Otsu.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if gray.mean() < 128:  # fundo escuro -> inverte para fundo claro
        gray = cv2.bitwise_not(gray)
    gray = cv2.bilateralFilter(gray, 5, 60, 60)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    return binary


def _tess_config(psm: int) -> str:
    """Monta a string de configuração do Tesseract (modo de segmentação)."""
    # Obs.: o diretório de idiomas é resolvido via TESSDATA_PREFIX (ver
    # apply_tessdata_env) — passar --tessdata-dir na config quebra no Windows
    # porque o pytesseract não trata aspas/barras invertidas.
    return f"--psm {psm} --oem 1"


def apply_tessdata_env(cfg: PipelineConfig) -> None:
    """Aponta o TESSDATA_PREFIX para o fallback local, se houver (idempotente)."""
    if cfg.tessdata:
        os.environ["TESSDATA_PREFIX"] = cfg.tessdata


def ocr_region_text(crop_bgr: np.ndarray, cfg: PipelineConfig) -> str:
    """OCR de uma única região de TEXTO já recortada; devolve o texto limpo.

    Pré-processa APENAS este recorte (cinza + Otsu) e usa PSM 6 (bloco uniforme
    de texto), adequado a um parágrafo/título já isolado pelo detector. Reaproveita
    a reconstrução de linhas para preservar quebras (listas/passos numerados).
    """
    if crop_bgr is None or crop_bgr.size == 0:
        return ""
    prepped = preprocess_for_ocr(crop_bgr)
    words = _image_to_words(prepped, cfg, psm=6)
    return words_to_text(words, cfg)


def _image_to_words(prepped: np.ndarray, cfg: PipelineConfig, psm: int) -> list[dict]:
    """Roda o OCR na imagem e retorna as palavras confiáveis com bounding boxes.

    Cada palavra é um dict com: text, conf, x, y, w, h (px) e os índices
    hierárquicos do Tesseract (block/par/line) usados para remontar linhas.
    """
    apply_tessdata_env(cfg)
    data = pytesseract.image_to_data(
        prepped,
        lang=cfg.ocr_lang,
        config=_tess_config(psm=psm),
        output_type=pytesseract.Output.DICT,
    )

    words: list[dict] = []
    for i in range(len(data["text"])):
        text = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        if not text or conf < cfg.min_conf:
            continue
        words.append(
            {
                "text": text,
                "conf": conf,
                "x": int(data["left"][i]),
                "y": int(data["top"][i]),
                "w": int(data["width"][i]),
                "h": int(data["height"][i]),
                "block": int(data["block_num"][i]),
                "par": int(data["par_num"][i]),
                "line": int(data["line_num"][i]),
            }
        )
    return words


def words_to_text(words: list[dict], cfg: PipelineConfig) -> str:
    """Remonta as palavras (já filtradas por um bloco) em texto legível.

    Agrupa por (block, par, line) do Tesseract e ordena por posição, preservando
    as quebras de linha do original — importante para listas/passos numerados.
    """
    if not words:
        return ""

    lines: dict[tuple[int, int, int], list[dict]] = {}
    for word in words:
        lines.setdefault((word["block"], word["par"], word["line"]), []).append(word)

    ordered_keys = sorted(
        lines,
        key=lambda k: min(w["y"] for w in lines[k]),  # linha mais acima primeiro
    )

    rendered: list[str] = []
    for key in ordered_keys:
        row = sorted(lines[key], key=lambda w: w["x"])  # esq -> dir
        rendered.append(" ".join(w["text"] for w in row))
    return "\n".join(rendered)
