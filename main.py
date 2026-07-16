"""
Conversor de PDF para DOCX em lote por Document Layout Analysis (DLA).

Ao contrário de uma extração/sanduíche simples, este pipeline "fatia" cada página
e reconstrói o documento do zero:

    Passo 1 (rendering):  pdf2image/Poppler rasteriza cada página em alta resolução.
    Passo 2 (layout+ocr): um modelo de Visão Computacional (DocLayout-YOLO) detecta
        as regiões da página e as classifica como TEXTO (instrução) ou IMAGEM
        (print de tela). Nas regiões de texto, o Tesseract lê a string limpa;
        as regiões de imagem são recortadas da imagem original colorida.
    Passo 3 (docx):       python-docx remonta um DOCX nativo, inserindo texto e
        imagens na ordem de leitura — texto 100% editável, prints preservados.

Características de produção:
    * IA com fallback de hardware: usa GPU (CUDA) quando disponível, senão CPU.
    * Fail-safe: falha em um arquivo não interrompe o lote (isolamento por processo).
    * Idempotência: arquivos já convertidos são ignorados (use --force).
    * Filtro estrito: apenas arquivos .pdf (case-insensitive).
    * Paralelismo via ProcessPoolExecutor; logging para console + arquivo.
    * Modelo carregado uma vez por worker; pesos baixados uma vez no processo principal.
    * Dependências (Poppler/Tesseract) detectadas automaticamente, com fallback
      local do projeto (.poppler/ e .tessdata/).

Uso:
    python main.py                       # ./pdf -> ./docx
    python main.py --dpi 400 --verbose
    python main.py --ocr-lang eng --workers 4 --force
    python main.py --device cpu          # força CPU mesmo com GPU disponível

Dependências de sistema (ver README): Poppler e Tesseract OCR.
Dependências de IA (ver requirements.txt): doclayout-yolo, torch, huggingface_hub.

Autor: Engenharia HypeTecnologia
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from conversor.config import (
    DEFAULT_DPI,
    DEFAULT_DST_DIR,
    DEFAULT_OCR_LANG,
    DEFAULT_SRC_DIR,
    DEFAULT_YOLO_CONF,
    DEFAULT_YOLO_IMGSZ,
    LOG_FILE_NAME,
    SOURCE_SUFFIX,
    TARGET_SUFFIX,
    ConversionResult,
    PipelineConfig,
    Status,
)
from conversor.dependencies import (
    find_poppler,
    ml_dependencies_available,
    resolve_tessdata,
    tesseract_available,
)
from conversor.detector import ensure_weights, resolve_device
from conversor.pipeline import convert_single

logger = logging.getLogger("conversor_pdf_docx")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool, log_path: Path) -> None:
    """Configura logging para console (UTF-8) e arquivo."""
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover
        logger.warning("Não foi possível abrir o arquivo de log %s: %s", log_path, exc)


# ---------------------------------------------------------------------------
# Descoberta de arquivos e planejamento do lote
# ---------------------------------------------------------------------------


def discover_pdfs(src_dir: Path) -> list[Path]:
    """Retorna os PDFs válidos no diretório de origem (filtro estrito)."""
    if not src_dir.is_dir():
        raise NotADirectoryError(f"Diretório de origem não encontrado: {src_dir}")
    pdfs = [
        item
        for item in src_dir.iterdir()
        if item.is_file() and item.suffix.lower() == SOURCE_SUFFIX
    ]
    return sorted(pdfs, key=lambda p: p.name.lower())


def target_path_for(pdf_path: Path, dst_dir: Path) -> Path:
    """Resolve o destino .docx mantendo o nome de origem (idempotência de nome)."""
    return dst_dir / (pdf_path.stem + TARGET_SUFFIX)


def _plan_jobs(
    pdfs: Iterable[Path], dst_dir: Path, force: bool
) -> tuple[list[tuple[Path, Path]], list[ConversionResult]]:
    """Separa PDFs entre trabalhos a executar e já ignorados (idempotência)."""
    jobs: list[tuple[Path, Path]] = []
    skipped: list[ConversionResult] = []
    for pdf in pdfs:
        target = target_path_for(pdf, dst_dir)
        if target.exists() and not force:
            skipped.append(
                ConversionResult(
                    source=pdf,
                    target=target,
                    status=Status.SKIPPED,
                    message="Destino já existe (use --force para reprocessar).",
                )
            )
        else:
            jobs.append((pdf, target))
    return jobs, skipped


# ---------------------------------------------------------------------------
# Orquestração do lote
# ---------------------------------------------------------------------------


def run_batch(
    src_dir: Path, dst_dir: Path, workers: int, force: bool, cfg: PipelineConfig
) -> list[ConversionResult]:
    """Orquestra a conversão em lote de todos os PDFs do diretório de origem."""
    pdfs = discover_pdfs(src_dir)
    if not pdfs:
        logger.warning("Nenhum arquivo .pdf encontrado em: %s", src_dir)
        return []

    dst_dir.mkdir(parents=True, exist_ok=True)
    jobs, results = _plan_jobs(pdfs, dst_dir, force)

    for skipped in results:
        logger.info("[IGNORADO] %s (destino já existe)", skipped.source.name)

    logger.info(
        "Encontrados %d PDF(s) | a converter: %d | ignorados: %d | workers: %d | DPI: %d",
        len(pdfs), len(jobs), len(results), workers, cfg.dpi,
    )
    if not jobs:
        return results

    completed = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(convert_single, source, target, cfg): source
            for source, target in jobs
        }
        for future in as_completed(future_map):
            source = future_map[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - falha do próprio worker
                result = ConversionResult(
                    source=source,
                    target=target_path_for(source, dst_dir),
                    status=Status.FAILED,
                    message=f"Processo worker falhou: {type(exc).__name__}: {exc}",
                )
            completed += 1
            _log_result(result, completed, len(jobs))
            results.append(result)

    return results


def _log_result(result: ConversionResult, index: int, total: int) -> None:
    """Registra o resultado de uma conversão individual."""
    progress = f"[{index}/{total}]"
    if result.status is Status.CONVERTED:
        logger.info(
            "%s [OK] %s -> %s (%d pág., %s, %.2fs)",
            progress, result.source.name, result.target.name,
            result.pages, result.message, result.elapsed,
        )
    elif result.status is Status.FAILED:
        logger.error("%s [FALHA] %s :: %s", progress, result.source.name, result.message)


def summarize(results: list[ConversionResult]) -> dict[Status, int]:
    """Agrega os resultados por status."""
    summary = {status: 0 for status in Status}
    for result in results:
        summary[result.status] += 1
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Converte em lote PDFs para DOCX editável por Document Layout Analysis.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--src", type=Path, default=Path(DEFAULT_SRC_DIR),
                        help="Diretório de origem contendo os PDFs.")
    parser.add_argument("--dst", type=Path, default=Path(DEFAULT_DST_DIR),
                        help="Diretório de destino para os DOCX.")
    parser.add_argument("--workers", type=int, default=None,
                        help="Nº de processos paralelos (padrão: nº de CPUs).")
    parser.add_argument("--force", action="store_true",
                        help="Reprocessa mesmo que o .docx de destino já exista.")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                        help="Resolução de rasterização das páginas.")
    parser.add_argument("--ocr-lang", default=DEFAULT_OCR_LANG,
                        help="Idioma(s) do Tesseract (ex.: 'por', 'eng', 'por+eng').")
    parser.add_argument("--min-conf", type=int, default=40,
                        help="Confiança mínima (0-100) para aceitar uma palavra do OCR.")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto",
                        help="Hardware de inferência: auto (GPU se houver), cpu ou cuda.")
    parser.add_argument("--yolo-conf", type=float, default=DEFAULT_YOLO_CONF,
                        help="Confiança mínima de detecção do YOLO (0-1).")
    parser.add_argument("--yolo-imgsz", type=int, default=DEFAULT_YOLO_IMGSZ,
                        help="Tamanho de inferência do modelo (px).")
    parser.add_argument("--verbose", action="store_true",
                        help="Habilita logs de nível DEBUG no console.")
    return parser


def resolve_workers(requested: int | None, job_count: int, device: str) -> int:
    """Determina o nº efetivo de workers (nunca mais que o nº de arquivos).

    Em GPU, cada worker carrega uma cópia do modelo na mesma VRAM; por isso, se o
    usuário não pedir explicitamente, limitamos a 1 worker para evitar OOM de VRAM.
    """
    if requested and requested > 0:
        base = requested
    elif device.startswith("cuda"):
        base = 1
    else:
        base = os.cpu_count() or 1
    return max(1, min(base, max(1, job_count)))


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada. Retorna o exit code do processo."""
    args = build_parser().parse_args(argv)

    project_root = Path.cwd()
    setup_logging(args.verbose, project_root / LOG_FILE_NAME)

    logger.info("=" * 70)
    logger.info("Conversor PDF -> DOCX (Document Layout Analysis) iniciado")
    logger.info("Origem: %s | Destino: %s", args.src.resolve(), args.dst.resolve())

    # --- Dependências de sistema -------------------------------------------
    poppler_path = find_poppler(project_root)
    if poppler_path is None:
        logger.error(
            "Poppler não encontrado (necessário para o pdf2image). Instale-o e "
            "adicione ao PATH, ou coloque uma cópia portátil em .poppler/ (ver README)."
        )
        return 2
    logger.info("Poppler: %s", poppler_path)

    if not tesseract_available():
        logger.error(
            "Tesseract OCR não encontrado no PATH. Instale-o (ver README) — é "
            "obrigatório para a leitura do texto."
        )
        return 2

    tessdata, lang_missing = resolve_tessdata(args.ocr_lang, project_root)
    if tessdata:
        logger.info("Dados de idioma do sistema incompletos; usando fallback local: %s", tessdata)
    if lang_missing:
        logger.warning(
            "Idioma(s) de OCR ausente(s): %s. Instale o(s) traineddata "
            "correspondente(s) (ver README) ou ajuste --ocr-lang.",
            ", ".join(lang_missing),
        )

    # --- Dependências de IA (Visão Computacional) --------------------------
    ml_ok, ml_missing = ml_dependencies_available()
    if not ml_ok:
        logger.error(
            "Dependências de IA ausentes: %s. Instale-as com "
            "'pip install -r requirements.txt' (ver README).",
            ", ".join(ml_missing),
        )
        return 2

    # Resolve e registra o hardware de inferência UMA vez (os workers herdam via cfg).
    device, device_desc = resolve_device(args.device)
    logger.info("Hardware de inferência: %s", device_desc)

    # Baixa os pesos do modelo no processo principal (uma vez; sem corrida entre workers).
    try:
        logger.info("Preparando modelo DocLayout-YOLO (download no 1º uso pode demorar)...")
        model_path = ensure_weights()
        logger.info("Pesos do modelo: %s", model_path)
    except Exception as exc:  # noqa: BLE001 - erro de rede/HF é fatal para o lote
        logger.error("Falha ao obter os pesos do modelo: %s: %s", type(exc).__name__, exc)
        return 2

    cfg = PipelineConfig(
        dpi=args.dpi,
        ocr_lang=args.ocr_lang,
        tessdata=tessdata,
        poppler_path=poppler_path,
        min_conf=args.min_conf,
        device=device,
        model_path=model_path,
        yolo_conf=args.yolo_conf,
        yolo_imgsz=args.yolo_imgsz,
    )

    try:
        pdfs_preview = discover_pdfs(args.src)
    except NotADirectoryError as exc:
        logger.error("%s", exc)
        return 2

    workers = resolve_workers(args.workers, len(pdfs_preview), device)

    start = time.perf_counter()
    results = run_batch(args.src, args.dst, workers=workers, force=args.force, cfg=cfg)
    elapsed = time.perf_counter() - start

    summary = summarize(results)
    total_text = sum(r.text_blocks for r in results)
    total_images = sum(r.image_blocks for r in results)
    logger.info("-" * 70)
    logger.info(
        "Concluído em %.2fs | Convertidos: %d | Ignorados: %d | Falhas: %d | "
        "Blocos de texto: %d | Imagens: %d",
        elapsed, summary[Status.CONVERTED], summary[Status.SKIPPED],
        summary[Status.FAILED], total_text, total_images,
    )
    logger.info("=" * 70)

    return 1 if summary[Status.FAILED] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
