"""
Conversor de PDF para DOCX em lote (nível de produção).

Varre um diretório de origem (./pdf) em busca de arquivos .pdf, converte cada
um para .docx preservando o nome original e grava o resultado no diretório de
destino (./docx).

Características de produção:
    * Fail-safe: falha em um arquivo não interrompe o lote (isolamento por processo).
    * Idempotência: por padrão, arquivos já convertidos são ignorados (use --force
      para reprocessar).
    * Filtro estrito: apenas arquivos com extensão .pdf (case-insensitive).
    * Paralelismo: conversão distribuída em múltiplos processos (pdf2docx é
      CPU-bound; processos separados também isolam eventuais crashes da engine).
    * Logging robusto: saída no terminal e em arquivo de log rotativo.

Uso:
    python main.py                      # usa ./pdf -> ./docx com defaults
    python main.py --workers 4          # define o número de processos
    python main.py --force              # reprocessa mesmo que o .docx já exista
    python main.py --src pdf --dst docx --verbose

Autor: Engenharia HypeTecnologia
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Configuração e constantes
# ---------------------------------------------------------------------------

# Diretórios padrão relativos à raiz do projeto (onde este script é executado).
DEFAULT_SRC_DIR = "pdf"
DEFAULT_DST_DIR = "docx"

# Nome do arquivo de log gerado na raiz do projeto.
LOG_FILE_NAME = "conversao.log"

# Extensão de origem aceita (comparada em lower-case).
SOURCE_SUFFIX = ".pdf"
TARGET_SUFFIX = ".docx"

logger = logging.getLogger("conversor_pdf_docx")


class Status(str, Enum):
    """Resultado possível para a conversão de um único arquivo."""

    CONVERTED = "convertido"
    SKIPPED = "ignorado"
    FAILED = "falha"


@dataclass(frozen=True)
class ConversionResult:
    """Resultado imutável da tentativa de conversão de um arquivo."""

    source: Path
    target: Path
    status: Status
    message: str = ""
    elapsed: float = 0.0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool, log_path: Path) -> None:
    """Configura logging para console e arquivo.

    Args:
        verbose: quando True, eleva o nível do console para DEBUG.
        log_path: caminho do arquivo de log persistente.
    """
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

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
    except OSError as exc:  # pragma: no cover - falha rara de I/O de log
        logger.warning("Não foi possível abrir o arquivo de log %s: %s", log_path, exc)


# ---------------------------------------------------------------------------
# Descoberta de arquivos
# ---------------------------------------------------------------------------


def discover_pdfs(src_dir: Path) -> list[Path]:
    """Retorna a lista de PDFs válidos no diretório de origem.

    Filtro estrito: apenas arquivos regulares cuja extensão seja .pdf
    (comparação case-insensitive). Qualquer outro item é ignorado.

    Args:
        src_dir: diretório de origem a ser varrido (não recursivo).

    Returns:
        Lista ordenada de caminhos de PDFs.
    """
    if not src_dir.is_dir():
        raise NotADirectoryError(f"Diretório de origem não encontrado: {src_dir}")

    pdfs = [
        item
        for item in src_dir.iterdir()
        if item.is_file() and item.suffix.lower() == SOURCE_SUFFIX
    ]
    return sorted(pdfs, key=lambda p: p.name.lower())


def target_path_for(pdf_path: Path, dst_dir: Path) -> Path:
    """Resolve o caminho de destino .docx mantendo o nome de origem.

    Ex.: pdf/arquivo_a.pdf -> docx/arquivo_a.docx
    """
    return dst_dir / (pdf_path.stem + TARGET_SUFFIX)


# ---------------------------------------------------------------------------
# Conversão de um único arquivo (executada em processo worker)
# ---------------------------------------------------------------------------


def convert_single(source: Path, target: Path) -> ConversionResult:
    """Converte um único PDF em DOCX.

    Esta função é o ponto de entrada de cada processo worker. Ela captura
    qualquer exceção da engine e a converte em um ConversionResult com status
    FAILED, garantindo que uma falha isolada nunca derrube o lote.

    Args:
        source: caminho do PDF de origem.
        target: caminho do DOCX de destino.

    Returns:
        ConversionResult descrevendo o desfecho.
    """
    # Import local: mantém o overhead de import dentro do processo worker e
    # evita carregar a engine no processo pai desnecessariamente.
    from pdf2docx import Converter

    start = time.perf_counter()
    converter: "Converter | None" = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)

        converter = Converter(str(source))
        # Conversão de todas as páginas; single-process por arquivo pois o
        # paralelismo é feito em nível de lote (um processo por arquivo).
        converter.convert(str(target), multi_processing=False)

        elapsed = time.perf_counter() - start
        return ConversionResult(
            source=source,
            target=target,
            status=Status.CONVERTED,
            elapsed=elapsed,
        )
    except Exception as exc:  # noqa: BLE001 - fail-safe intencional por arquivo
        elapsed = time.perf_counter() - start
        # Remove um .docx parcial/corrompido que possa ter sido criado.
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
    finally:
        if converter is not None:
            try:
                converter.close()
            except Exception:  # noqa: BLE001 - close best-effort
                pass


# ---------------------------------------------------------------------------
# Orquestração do lote
# ---------------------------------------------------------------------------


def _plan_jobs(
    pdfs: Iterable[Path], dst_dir: Path, force: bool
) -> tuple[list[tuple[Path, Path]], list[ConversionResult]]:
    """Separa os PDFs entre trabalhos a executar e os já ignorados (idempotência).

    Returns:
        (jobs, skipped) onde jobs é a lista de pares (source, target) a
        converter e skipped são os resultados já resolvidos como SKIPPED.
    """
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


def run_batch(
    src_dir: Path,
    dst_dir: Path,
    workers: int,
    force: bool,
) -> list[ConversionResult]:
    """Orquestra a conversão em lote de todos os PDFs do diretório de origem.

    Args:
        src_dir: diretório de origem contendo os PDFs.
        dst_dir: diretório de destino dos DOCX.
        workers: número de processos paralelos.
        force: quando True, reprocessa mesmo que o destino já exista.

    Returns:
        Lista de ConversionResult para todos os arquivos considerados.
    """
    pdfs = discover_pdfs(src_dir)
    if not pdfs:
        logger.warning("Nenhum arquivo .pdf encontrado em: %s", src_dir)
        return []

    dst_dir.mkdir(parents=True, exist_ok=True)

    jobs, results = _plan_jobs(pdfs, dst_dir, force)

    for skipped in results:
        logger.info("[IGNORADO] %s (destino já existe)", skipped.source.name)

    total = len(pdfs)
    logger.info(
        "Encontrados %d PDF(s) | a converter: %d | ignorados: %d | workers: %d",
        total,
        len(jobs),
        len(results),
        workers,
    )

    if not jobs:
        return results

    # ProcessPoolExecutor: isola cada conversão em seu próprio processo, o que
    # protege o lote inteiro contra crashes de baixo nível da engine (segfault,
    # travamento) além de paralelizar trabalho CPU-bound.
    completed = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(convert_single, source, target): source
            for source, target in jobs
        }
        for future in as_completed(future_map):
            source = future_map[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - falha do próprio worker/processo
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
    """Registra o resultado de uma conversão individual no log."""
    progress = f"[{index}/{total}]"
    if result.status is Status.CONVERTED:
        logger.info(
            "%s [OK] %s -> %s (%.2fs)",
            progress,
            result.source.name,
            result.target.name,
            result.elapsed,
        )
    elif result.status is Status.FAILED:
        logger.error(
            "%s [FALHA] %s :: %s",
            progress,
            result.source.name,
            result.message,
        )


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
    """Constrói o parser de argumentos de linha de comando."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Converte em lote arquivos PDF para DOCX.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--src",
        type=Path,
        default=Path(DEFAULT_SRC_DIR),
        help="Diretório de origem contendo os PDFs.",
    )
    parser.add_argument(
        "--dst",
        type=Path,
        default=Path(DEFAULT_DST_DIR),
        help="Diretório de destino para os DOCX.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Número de processos paralelos (padrão: nº de CPUs, máx. de arquivos).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocessa e sobrescreve mesmo que o .docx de destino já exista.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Habilita logs de nível DEBUG no console.",
    )
    return parser


def resolve_workers(requested: int | None, job_count: int) -> int:
    """Determina o número efetivo de workers.

    Nunca cria mais processos do que arquivos a converter, nem menos que 1.
    """
    import os

    if requested is not None and requested > 0:
        base = requested
    else:
        base = os.cpu_count() or 1
    return max(1, min(base, max(1, job_count)))


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada. Retorna o exit code do processo."""
    args = build_parser().parse_args(argv)

    project_root = Path.cwd()
    setup_logging(args.verbose, project_root / LOG_FILE_NAME)

    logger.info("=" * 70)
    logger.info("Conversor PDF -> DOCX iniciado")
    logger.info("Origem: %s | Destino: %s", args.src.resolve(), args.dst.resolve())

    try:
        pdfs_preview = discover_pdfs(args.src)
    except NotADirectoryError as exc:
        logger.error("%s", exc)
        return 2

    workers = resolve_workers(args.workers, len(pdfs_preview))

    start = time.perf_counter()
    results = run_batch(args.src, args.dst, workers=workers, force=args.force)
    elapsed = time.perf_counter() - start

    summary = summarize(results)
    logger.info("-" * 70)
    logger.info(
        "Concluído em %.2fs | Convertidos: %d | Ignorados: %d | Falhas: %d",
        elapsed,
        summary[Status.CONVERTED],
        summary[Status.SKIPPED],
        summary[Status.FAILED],
    )
    logger.info("=" * 70)

    # Exit code 1 se houve qualquer falha (útil para pipelines/CI).
    return 1 if summary[Status.FAILED] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
