"""Passo 2 (núcleo de IA) — detecção de layout com DocLayout-YOLO.

Substitui a antiga análise por heurísticas (projeção por bandas, limiares de
"colorfulness"/densidade de tinta, morfologia OpenCV) por um modelo de Visão
Computacional real: DocLayout-YOLO (YOLOv10 ajustado em documentos). O modelo
devolve, por página, bounding boxes com classes de layout (title, plain text,
figure, table, ...), que mapeamos para os dois domínios do nosso pipeline —
TEXTO e IMAGEM.

Carregamento seguro para multiprocessamento:
    * O download dos pesos acontece UMA vez no processo principal (ensure_weights),
      antes de abrir o pool, evitando corrida de download entre workers.
    * Cada worker do ProcessPoolExecutor carrega o modelo em memória UMA única vez
      (singleton lazy por processo, _MODEL), nunca por arquivo/página. O objeto do
      modelo nunca cruza a fronteira de processo (não é piclável); apenas o caminho
      dos pesos (str) viaja no PipelineConfig.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from .config import (
    DISCARD_CLASSES,
    IMAGE_CLASSES,
    MODEL_FILENAME,
    MODEL_REPO_ID,
    TEXT_CLASSES,
    BlockKind,
    PipelineConfig,
)

logger = logging.getLogger("conversor_pdf_docx")

# Singleton lazy POR PROCESSO. Cada worker preenche o seu na 1ª inferência.
_MODEL = None
_MODEL_PATH: str | None = None


@dataclass
class Detection:
    """Uma região detectada pelo modelo, já mapeada para TEXTO/IMAGEM."""

    bbox: tuple[int, int, int, int]  # (x, y, w, h) em pixels da página rasterizada
    kind: BlockKind
    label: str                       # classe original do modelo (para depuração)
    conf: float


# ---------------------------------------------------------------------------
# Hardware e pesos (executados no processo principal)
# ---------------------------------------------------------------------------


def resolve_device(prefer: str = "auto") -> tuple[str, str]:
    """Resolve o device de inferência via torch. Retorna (device, descrição legível).

    `prefer`: "auto" (GPU se houver, senão CPU), "cpu" (força CPU) ou "cuda"/"gpu".
    """
    try:
        import torch
    except ImportError:
        return "cpu", "CPU (PyTorch indisponível)"

    if prefer == "cpu":
        return "cpu", "CPU (forçado por --device cpu)"

    if prefer in ("auto", "cuda", "gpu") and torch.cuda.is_available():
        try:
            name = torch.cuda.get_device_name(0)
        except Exception:  # noqa: BLE001 - nome é apenas cosmético
            name = "desconhecida"
        return "cuda:0", f"GPU CUDA disponível: {name}"

    if prefer in ("cuda", "gpu"):
        return "cpu", "CPU (--device cuda pedido, mas nenhuma GPU CUDA disponível)"
    return "cpu", "CPU (nenhuma GPU CUDA disponível)"


def ensure_weights() -> str:
    """Baixa (ou localiza no cache) os pesos do modelo e retorna o caminho local.

    Deve ser chamado no processo principal, antes de abrir o pool, para que o
    download ocorra uma única vez (sem corrida entre workers).
    """
    from huggingface_hub import hf_hub_download

    return hf_hub_download(repo_id=MODEL_REPO_ID, filename=MODEL_FILENAME)


# ---------------------------------------------------------------------------
# Modelo (carregado dentro de cada worker, sob demanda)
# ---------------------------------------------------------------------------


def _get_model(cfg: PipelineConfig):
    """Carrega (uma vez por processo) e devolve o modelo DocLayout-YOLO."""
    global _MODEL, _MODEL_PATH

    path = cfg.model_path or ensure_weights()
    if _MODEL is not None and _MODEL_PATH == path:
        return _MODEL

    # Otimização de CPU: fixa o nº de threads do torch DESTE worker. Com o
    # ProcessPoolExecutor, sem isso cada worker tentaria usar todos os núcleos
    # (N workers × N threads = oversubscription e thrashing). Só se aplica em CPU;
    # em GPU o torch é irrelevante para o throughput da inferência.
    if cfg.device == "cpu" and cfg.cpu_threads:
        try:
            import torch

            torch.set_num_threads(cfg.cpu_threads)
            logger.debug("torch.set_num_threads(%d) neste worker (CPU)", cfg.cpu_threads)
        except Exception:  # noqa: BLE001 - ajuste de threads é best-effort
            pass

    from doclayout_yolo import YOLOv10

    _MODEL = YOLOv10(path)
    _MODEL_PATH = path
    logger.debug("Modelo DocLayout-YOLO carregado neste worker (device=%s): %s", cfg.device, path)
    return _MODEL


def _map_kind(label: str) -> BlockKind | None:
    """Mapeia a classe do modelo para TEXTO/IMAGEM (ou None para descartar)."""
    key = label.strip().lower()
    if key in DISCARD_CLASSES:
        return None
    if key in IMAGE_CLASSES:
        return BlockKind.IMAGE
    if key in TEXT_CLASSES:
        return BlockKind.TEXT
    # Classe desconhecida: preserva como IMAGEM (não arrisca OCR de lixo nem
    # perde conteúdo) — o recorte colorido garante que nada some do documento.
    logger.debug("Classe de layout desconhecida '%s' -> tratada como IMAGEM", label)
    return BlockKind.IMAGE


def detect_regions(bgr: np.ndarray, cfg: PipelineConfig) -> list[Detection]:
    """Roda a inferência do YOLO na página e devolve as regiões mapeadas.

    Recebe a imagem BGR (convenção OpenCV, aceita diretamente pelo predict do
    ultralytics/doclayout-yolo). As coordenadas voltam na resolução original da
    página rasterizada — os recortes usam, portanto, a imagem em alta resolução.
    """
    model = _get_model(cfg)
    results = model.predict(
        bgr,
        imgsz=cfg.yolo_imgsz,
        conf=cfg.yolo_conf,
        device=cfg.device,
        verbose=False,
    )
    if not results:
        return []

    res = results[0]
    boxes = getattr(res, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return []

    names = getattr(res, "names", None) or getattr(model, "names", {})
    height, width = bgr.shape[:2]

    xyxy = boxes.xyxy.cpu().numpy()
    class_ids = boxes.cls.cpu().numpy().astype(int)
    confs = boxes.conf.cpu().numpy()

    detections: list[Detection] = []
    for (x1, y1, x2, y2), class_id, conf in zip(xyxy, class_ids, confs):
        label = str(names.get(int(class_id), str(class_id)))
        kind = _map_kind(label)
        if kind is None:
            continue

        # Clampa às bordas e converte para (x, y, w, h) inteiros.
        left = max(0, int(round(float(x1))))
        top = max(0, int(round(float(y1))))
        right = min(width, int(round(float(x2))))
        bottom = min(height, int(round(float(y2))))
        w, h = right - left, bottom - top
        if w <= 1 or h <= 1:
            continue

        detections.append(
            Detection(bbox=(left, top, w, h), kind=kind, label=label, conf=float(conf))
        )
    return detections
