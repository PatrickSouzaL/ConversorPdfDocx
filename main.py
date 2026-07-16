"""
Conversor de PDF para DOCX em lote com OCR híbrido (nível de produção).

Varre um diretório de origem (./pdf), torna o texto pesquisável quando
necessário (via OCR "sandwich") e converte cada arquivo para .docx no diretório
de destino (./docx), preservando o nome original.

Pipeline híbrido (por arquivo):
    Passo 0 (detecção): verifica se TODAS as páginas já possuem texto nativo.
    Passo 1 (pré-processamento, condicional): se alguma página não tem texto
        (documento escaneado / imagem plana), roda o ocrmypdf/Tesseract para
        gerar um "Sandwich PDF" temporário — a camada visual original (prints de
        tela) é preservada intacta e uma camada de texto invisível é sobreposta.
        Páginas que já têm texto nativo NÃO são rasterizadas (--skip-text).
    Passo 2 (conversão): alimenta o PDF (nativo ou sandwich) no pdf2docx para
        gerar o .docx final, editável e pesquisável.

Características de produção:
    * Fail-safe: falha em um arquivo (Tesseract, engine, PDF corrompido) não
      interrompe o lote (isolamento por processo + try/except por arquivo).
    * Idempotência: arquivos já convertidos são ignorados (use --force).
    * Filtro estrito: apenas arquivos com extensão .pdf (case-insensitive).
    * Arquivos temporários seguros (tempfile) com limpeza garantida (finally).
    * Paralelismo via ProcessPoolExecutor e logging para console + arquivo.

Uso:
    python main.py                       # ./pdf -> ./docx (OCR automático)
    python main.py --no-ocr              # desativa o OCR (só conversão direta)
    python main.py --ocr-lang eng        # idioma(s) do Tesseract
    python main.py --force-ocr           # força OCR mesmo com texto nativo
    python main.py --workers 4 --force --verbose

Dependências de sistema (ver README): Tesseract OCR e Ghostscript.

Autor: Engenharia HypeTecnologia
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
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

# Extensões.
SOURCE_SUFFIX = ".pdf"
TARGET_SUFFIX = ".docx"

# OCR: idioma(s) padrão do Tesseract (documentos em português + termos em inglês).
DEFAULT_OCR_LANG = "por+eng"

# Uma página é considerada "com texto nativo" se tiver ao menos este nº de
# caracteres não-espaço. Abaixo disso é tratada como imagem (precisa de OCR).
DEFAULT_MIN_TEXT_CHARS = 10

logger = logging.getLogger("conversor_pdf_docx")


class Status(str, Enum):
    """Resultado possível para a conversão de um único arquivo."""

    CONVERTED = "convertido"
    SKIPPED = "ignorado"
    FAILED = "falha"


@dataclass(frozen=True)
class OcrConfig:
    """Configuração do estágio de OCR (imutável e picklável para os workers)."""

    enabled: bool = True
    lang: str = DEFAULT_OCR_LANG
    min_text_chars: int = DEFAULT_MIN_TEXT_CHARS
    force: bool = False  # força OCR mesmo quando há texto nativo
    available: bool = True  # binários de sistema (Tesseract/Ghostscript) presentes


@dataclass(frozen=True)
class ConversionResult:
    """Resultado imutável da tentativa de conversão de um arquivo."""

    source: Path
    target: Path
    status: Status
    message: str = ""
    elapsed: float = 0.0
    ocr_applied: bool = False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool, log_path: Path) -> None:
    """Configura logging para console e arquivo."""
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Garante acentuação correta no console do Windows (evita mojibake em
    # nomes de arquivo/mensagens com caracteres não-ASCII).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # stdout redirecionado / sem reconfigure
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
    except OSError as exc:  # pragma: no cover - falha rara de I/O de log
        logger.warning("Não foi possível abrir o arquivo de log %s: %s", log_path, exc)


# ---------------------------------------------------------------------------
# Dependências de sistema (binários) do OCR
# ---------------------------------------------------------------------------


def find_ghostscript() -> str | None:
    """Localiza o executável do Ghostscript (nomes variam por plataforma)."""
    for name in ("gswin64c", "gswin32c", "gs"):
        path = shutil.which(name)
        if path:
            return path
    return None


def check_ocr_dependencies() -> tuple[bool, list[str]]:
    """Verifica a presença dos binários de sistema exigidos pelo ocrmypdf.

    Returns:
        (ok, missing) onde ok é True se tudo está disponível e missing lista os
        binários ausentes.
    """
    missing: list[str] = []
    if shutil.which("tesseract") is None:
        missing.append("Tesseract OCR")
    if find_ghostscript() is None:
        missing.append("Ghostscript")
    return (not missing, missing)


# ---------------------------------------------------------------------------
# Passo 0 — detecção de texto nativo
# ---------------------------------------------------------------------------


def pdf_needs_ocr(pdf_path: Path, min_text_chars: int) -> bool:
    """Determina se o PDF precisa de OCR.

    Retorna True se AO MENOS uma página não possuir texto nativo suficiente
    (i.e., é uma imagem/scan). Se todas as páginas já têm texto, o OCR é
    dispensado (fallback do Passo 1) para economizar processamento.

    Em caso de erro na leitura, assume-se que precisa de OCR (comportamento
    conservador em prol da Regra de Ouro: texto pesquisável).
    """
    try:
        import pymupdf  # PyMuPDF (fitz); dependência transitiva do pdf2docx
    except ImportError:  # pragma: no cover - pymupdf sempre presente via pdf2docx
        return True

    try:
        with pymupdf.open(str(pdf_path)) as doc:
            if doc.page_count == 0:
                return False
            for page in doc:
                if len(page.get_text().strip()) < min_text_chars:
                    return True
        return False
    except Exception:  # noqa: BLE001 - leitura defensiva
        return True


# ---------------------------------------------------------------------------
# Passo 1 — pré-processamento OCR (Sandwich PDF)
# ---------------------------------------------------------------------------


def run_ocr(source: Path, sandwich: Path, lang: str) -> None:
    """Gera um "Sandwich PDF" pesquisável a partir do PDF de origem.

    Executa o ocrmypdf num subprocesso isolado (`python -m ocrmypdf`), o que:
        * evita o aninhamento frágil de pools de processos no Windows;
        * isola travamentos do Tesseract do processo worker.

    A flag --skip-text é a chave para a Regra de Ouro: páginas que já contêm
    texto são deixadas intactas (nunca rasterizadas) e apenas as páginas-imagem
    recebem a camada de texto invisível por cima, preservando os prints de tela.

    Levanta RuntimeError com a saída de erro do ocrmypdf em caso de falha.
    """
    cmd = [
        sys.executable,
        "-m",
        "ocrmypdf",
        "--skip-text",          # preserva páginas com texto; NÃO destrói imagens
        "--language",
        lang,
        "--output-type",
        "pdf",                  # sandwich simples (visual original + texto oculto)
        "--optimize",
        "0",                    # sem otimização (dispensa jbig2/pngquant opcionais)
        "--jobs",
        "1",                    # paralelismo é feito em nível de lote (por arquivo)
        "--quiet",
        str(source),
        str(sandwich),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        raise RuntimeError(
            f"ocrmypdf falhou (código {proc.returncode}): {detail[:500]}"
        )


# ---------------------------------------------------------------------------
# Conversão de um único arquivo (executada em processo worker)
# ---------------------------------------------------------------------------


def convert_single(source: Path, target: Path, ocr: OcrConfig) -> ConversionResult:
    """Executa o pipeline híbrido (OCR condicional + conversão) para um arquivo.

    Ponto de entrada de cada processo worker. Captura qualquer exceção e a
    converte em ConversionResult(FAILED), garantindo que uma falha isolada nunca
    derrube o lote. Usa um diretório temporário (tempfile) para o Sandwich PDF,
    removido no finally tanto em caso de sucesso quanto de falha.
    """
    from pdf2docx import Converter  # import local: overhead fica no worker

    start = time.perf_counter()
    tmp_dir: Path | None = None
    converter: "Converter | None" = None
    ocr_applied = False

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        source_for_conversion = source

        # ---- Passo 0 + Passo 1: OCR condicional -------------------------------
        if ocr.enabled and (ocr.force or pdf_needs_ocr(source, ocr.min_text_chars)):
            if not ocr.available:
                raise RuntimeError(
                    "OCR necessário (PDF sem texto nativo), mas Tesseract/"
                    "Ghostscript não estão instalados. Veja o README ou use "
                    "--no-ocr."
                )
            tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_sandwich_"))
            sandwich = tmp_dir / f"{source.stem}.pdf"
            run_ocr(source, sandwich, ocr.lang)
            source_for_conversion = sandwich
            ocr_applied = True

        # ---- Passo 2: conversão PDF -> DOCX -----------------------------------
        converter = Converter(str(source_for_conversion))
        converter.convert(str(target), multi_processing=False)

        elapsed = time.perf_counter() - start
        return ConversionResult(
            source=source,
            target=target,
            status=Status.CONVERTED,
            message="com OCR" if ocr_applied else "texto nativo",
            elapsed=elapsed,
            ocr_applied=ocr_applied,
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
            ocr_applied=ocr_applied,
        )
    finally:
        if converter is not None:
            try:
                converter.close()
            except Exception:  # noqa: BLE001 - close best-effort
                pass
        # Limpeza garantida do Sandwich PDF temporário (sucesso OU falha).
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Descoberta de arquivos e planejamento do lote
# ---------------------------------------------------------------------------


def discover_pdfs(src_dir: Path) -> list[Path]:
    """Retorna a lista de PDFs válidos no diretório de origem (filtro estrito)."""
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
    src_dir: Path,
    dst_dir: Path,
    workers: int,
    force: bool,
    ocr: OcrConfig,
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
        "Encontrados %d PDF(s) | a converter: %d | ignorados: %d | workers: %d | OCR: %s",
        len(pdfs),
        len(jobs),
        len(results),
        workers,
        "ligado" if ocr.enabled else "desligado",
    )

    if not jobs:
        return results

    # ProcessPoolExecutor: isola cada arquivo em seu próprio processo (protege o
    # lote contra crashes de baixo nível da engine/Tesseract) e paraleliza o
    # trabalho CPU-bound. O OCR roda em subprocesso próprio dentro de cada worker.
    completed = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(convert_single, source, target, ocr): source
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
    """Registra o resultado de uma conversão individual no log."""
    progress = f"[{index}/{total}]"
    if result.status is Status.CONVERTED:
        logger.info(
            "%s [OK] %s -> %s (%s, %.2fs)",
            progress,
            result.source.name,
            result.target.name,
            result.message,
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
        description="Converte em lote PDFs para DOCX com OCR híbrido (sandwich).",
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
    parser.add_argument("--no-ocr", dest="ocr", action="store_false",
                        help="Desativa o pré-processamento OCR (só conversão direta).")
    parser.add_argument("--force-ocr", action="store_true",
                        help="Força OCR mesmo em PDFs que já possuem texto nativo.")
    parser.add_argument("--ocr-lang", default=DEFAULT_OCR_LANG,
                        help="Idioma(s) do Tesseract (ex.: 'por', 'eng', 'por+eng').")
    parser.add_argument("--min-text-chars", type=int, default=DEFAULT_MIN_TEXT_CHARS,
                        help="Mín. de caracteres p/ considerar uma página como texto nativo.")
    parser.add_argument("--verbose", action="store_true",
                        help="Habilita logs de nível DEBUG no console.")
    return parser


def resolve_workers(requested: int | None, job_count: int) -> int:
    """Determina o nº efetivo de workers (nunca mais que o nº de arquivos)."""
    import os

    base = requested if (requested and requested > 0) else (os.cpu_count() or 1)
    return max(1, min(base, max(1, job_count)))


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada. Retorna o exit code do processo."""
    args = build_parser().parse_args(argv)

    project_root = Path.cwd()
    setup_logging(args.verbose, project_root / LOG_FILE_NAME)

    logger.info("=" * 70)
    logger.info("Conversor PDF -> DOCX (OCR híbrido) iniciado")
    logger.info("Origem: %s | Destino: %s", args.src.resolve(), args.dst.resolve())

    # Pré-verificação das dependências de sistema do OCR (não-fatal): arquivos
    # que realmente precisarem de OCR falharão individualmente com mensagem clara.
    ocr_available = True
    if args.ocr:
        ocr_available, missing = check_ocr_dependencies()
        if not ocr_available:
            logger.warning(
                "Dependência(s) de sistema ausente(s): %s. PDFs sem texto nativo "
                "irão falhar. Instale-as (ver README) ou rode com --no-ocr.",
                ", ".join(missing),
            )

    ocr = OcrConfig(
        enabled=args.ocr,
        lang=args.ocr_lang,
        min_text_chars=args.min_text_chars,
        force=args.force_ocr,
        available=ocr_available,
    )

    try:
        pdfs_preview = discover_pdfs(args.src)
    except NotADirectoryError as exc:
        logger.error("%s", exc)
        return 2

    workers = resolve_workers(args.workers, len(pdfs_preview))

    start = time.perf_counter()
    results = run_batch(args.src, args.dst, workers=workers, force=args.force, ocr=ocr)
    elapsed = time.perf_counter() - start

    summary = summarize(results)
    ocr_count = sum(1 for r in results if r.ocr_applied)
    logger.info("-" * 70)
    logger.info(
        "Concluído em %.2fs | Convertidos: %d (c/ OCR: %d) | Ignorados: %d | Falhas: %d",
        elapsed,
        summary[Status.CONVERTED],
        ocr_count,
        summary[Status.SKIPPED],
        summary[Status.FAILED],
    )
    logger.info("=" * 70)

    return 1 if summary[Status.FAILED] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
