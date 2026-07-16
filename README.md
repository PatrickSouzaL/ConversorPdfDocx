# Conversor PDF → DOCX (em lote, com OCR híbrido)

Rotina de backend, de nível de produção, para converter em lote arquivos **PDF**
para **DOCX**. Quando o PDF é uma documentação "engessada" (imagens planas /
scans, `Words count: 0`), o script aplica **OCR** para tornar o texto
editável/pesquisável **preservando as imagens** (prints de tela) intactas.

---

## Índice

- [Como funciona (pipeline híbrido)](#como-funciona-pipeline-híbrido)
- [Arquitetura](#arquitetura)
- [Requisitos](#requisitos)
- [Instalação](#instalação)
  - [1. Dependências Python](#1-dependências-python)
  - [2. Binários de sistema (Windows)](#2-binários-de-sistema-windows)
- [Uso](#uso)
- [Regras de negócio](#regras-de-negócio)
- [Estrutura de pastas](#estrutura-de-pastas)
- [Decisões técnicas](#decisões-técnicas)
- [Histórico de versões](#histórico-de-versões)

---

## Como funciona (pipeline híbrido)

Cada arquivo passa por um pipeline de duas etapas com *fallback* inteligente:

```
             ┌─────────────────────────────────────────────┐
 PDF origem ─► Passo 0: todas as páginas têm texto nativo?  │
             └───────────────┬──────────────┬──────────────┘
                    não / força │            │ sim (economiza processamento)
                                ▼            │
             ┌──────────────────────────┐   │
             │ Passo 1: OCR (ocrmypdf)   │   │
             │ gera "Sandwich PDF" temp: │   │
             │ imagem original intacta + │   │
             │ camada de texto invisível │   │
             │ (--skip-text)             │   │
             └───────────────┬──────────┘   │
                             ▼               ▼
             ┌─────────────────────────────────────────────┐
             │ Passo 2: pdf2docx → arquivo .docx final       │
             └─────────────────────────────────────────────┘
```

**Regra de Ouro:** o OCR **nunca** substitui a imagem por texto desformatado.
A flag `--skip-text` do ocrmypdf garante que páginas já com texto não sejam
tocadas e que as páginas-imagem recebam apenas uma **camada de texto invisível
por cima** — o print de tela é preservado como imagem no Word.

## Arquitetura

Código modular, com funções de propósito único (`main.py`):

| Função | Responsabilidade |
| --- | --- |
| `check_ocr_dependencies()` | Pré-verifica os binários de sistema (Tesseract/Ghostscript). |
| `discover_pdfs()` | Varre a origem e aplica o **filtro estrito** de `.pdf`. |
| `pdf_needs_ocr()` | **Passo 0**: detecta se há páginas sem texto nativo (via PyMuPDF). |
| `run_ocr()` | **Passo 1**: gera o *Sandwich PDF* (ocrmypdf em subprocesso isolado). |
| `convert_single()` | Orquestra o pipeline de **um** arquivo (worker isolado + `tempfile`). |
| `_plan_jobs()` | Aplica a **idempotência** (o que converter × o que ignorar). |
| `run_batch()` | **Orquestrador** do lote: fila, pool de processos e progresso. |
| `build_parser()` / `main()` | Camada de CLI e ponto de entrada. |

## Requisitos

- **Python 3.10+** (validado em Python 3.14)
- Dependências Python: `pdf2docx`, `ocrmypdf` (ver `requirements.txt`)
- **Binários de sistema:** **Tesseract OCR** e **Ghostscript** (ver abaixo)

## Instalação

### 1. Dependências Python

```powershell
# (opcional, recomendado) ambiente virtual
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Binários de sistema (Windows)

O `ocrmypdf` é um "wrapper" — ele **precisa** dos binários do Tesseract e do
Ghostscript instalados no sistema operacional (não são pacotes pip).

**Opção A — via `winget` (mais rápido):**

```powershell
winget install UB-Mannheim.TesseractOCR
winget install ArtifexSoftware.GhostScript
```

**Opção B — instaladores oficiais:**

- **Tesseract OCR:** baixe o instalador em
  <https://github.com/UB-Mannheim/tesseract/wiki>. Durante a instalação, em
  *"Additional language data"*, **marque o idioma Portuguese (`por`)**.
- **Ghostscript:** baixe a versão *Windows (64 bit)* em
  <https://ghostscript.com/releases/gsdnld.html> e instale.

**Depois de instalar (importante):**

1. **Feche e reabra o terminal** para o `PATH` ser recarregado.
2. Se os comandos não forem reconhecidos, adicione ao `PATH` manualmente:
   - Tesseract: `C:\Program Files\Tesseract-OCR`
   - Ghostscript: `C:\Program Files\gs\gs<versão>\bin`
3. Verifique a instalação:

   ```powershell
   tesseract --version
   gswin64c --version
   ```

> **Idioma português:** se você instalou o Tesseract sem marcar o `por`, baixe o
> arquivo `por.traineddata` de <https://github.com/tesseract-ocr/tessdata> e
> copie-o para a pasta `tessdata` do Tesseract
> (`C:\Program Files\Tesseract-OCR\tessdata`). Ou rode com `--ocr-lang eng`.

## Uso

Execute a partir da **raiz do projeto**:

```powershell
python main.py                     # ./pdf -> ./docx, OCR automático (por+eng)
python main.py --no-ocr            # desativa o OCR (só conversão direta)
python main.py --ocr-lang eng      # muda o idioma do Tesseract
python main.py --force-ocr         # força OCR mesmo com texto nativo
python main.py --workers 4 --force --verbose
```

| Flag | Descrição | Padrão |
| --- | --- | --- |
| `--src` | Diretório de origem dos PDFs. | `pdf` |
| `--dst` | Diretório de destino dos DOCX. | `docx` |
| `--workers` | Nº de processos paralelos. | nº de CPUs (limitado ao nº de arquivos) |
| `--force` | Reprocessa/sobrescreve arquivos já convertidos. | desativado |
| `--no-ocr` | Desativa o pré-processamento OCR. | OCR ativado |
| `--force-ocr` | Força OCR mesmo em PDFs com texto nativo. | desativado |
| `--ocr-lang` | Idioma(s) do Tesseract (`por`, `eng`, `por+eng`). | `por+eng` |
| `--min-text-chars` | Mín. de caracteres p/ uma página contar como texto nativo. | `10` |
| `--verbose` | Ativa logs `DEBUG` no console. | desativado |

Log persistente em `conversao.log`. **Exit code** `1` se houve qualquer falha
(útil para CI), `0` caso contrário.

> Se o OCR estiver ligado mas os binários faltarem, o script **avisa no início**
> e faz cada PDF que precise de OCR falhar individualmente (sem quebrar o lote).

## Regras de negócio

- **Fail-safe:** falha em um PDF (Tesseract, engine, arquivo corrompido) é
  capturada, registrada e **não interrompe** o lote — segue para o próximo. Cada
  arquivo roda em processo isolado, então nem um crash de baixo nível derruba o
  processo principal.
- **Idempotência de nomes:** o `.docx` mantém o nome de origem, trocando só a
  extensão (`arquivo_a.pdf` → `arquivo_a.docx`). Execuções repetidas ignoram
  arquivos já convertidos (salvo `--force`).
- **Filtro estrito:** apenas `.pdf` (case-insensitive); demais arquivos ignorados.
- **Temporários seguros:** o *Sandwich PDF* vai para um diretório do `tempfile`,
  removido no `finally` (sucesso **ou** falha).

## Estrutura de pastas

```
ConversorPdfDocx/
├── main.py            # script principal (pipeline híbrido, modular)
├── requirements.txt   # dependências Python
├── README.md          # este arquivo
├── .gitignore
├── contexto/          # handoffs de estado do projeto
├── pdf/               # ORIGEM — coloque aqui os PDFs a converter
└── docx/              # DESTINO — os .docx convertidos são gravados aqui
```

## Decisões técnicas

- **OCR "sandwich" com `--skip-text`:** cumpre a Regra de Ouro — texto
  pesquisável sem destruir as imagens; páginas com texto nativo ficam intocadas.
- **Fallback por detecção (`pdf_needs_ocr`):** se todas as páginas já têm texto,
  pula o OCR e economiza processamento; conservador em caso de erro de leitura
  (assume que precisa de OCR).
- **`ocrmypdf` via subprocesso (`python -m ocrmypdf`):** isola travamentos do
  Tesseract e evita o aninhamento frágil de pools de processos no Windows.
- **`ProcessPoolExecutor`:** paralelismo real para trabalho CPU-bound + isolamento
  de crashes; o paralelismo é em nível de lote (um arquivo por processo, OCR com
  `--jobs 1`).
- **`pathlib` + `tempfile`:** caminhos multiplataforma e temporários seguros.
- **stdout em UTF-8:** acentuação correta no console do Windows.

## Histórico de versões

### v1.1.0
- **Pipeline híbrido com OCR** (`ocrmypdf`/Tesseract): torna PDFs escaneados
  pesquisáveis preservando as imagens (*Sandwich PDF* com `--skip-text`).
- Detecção de texto nativo (`pdf_needs_ocr`) com *fallback* que pula o OCR.
- OCR em subprocesso isolado + limpeza garantida de temporários (`tempfile`).
- Pré-verificação de dependências de sistema com aviso claro.
- Novas flags: `--no-ocr`, `--force-ocr`, `--ocr-lang`, `--min-text-chars`.
- Correção de encoding do console (UTF-8) no Windows.

### v1.0.0
- Conversão em lote PDF → DOCX com `pdf2docx`.
- Arquitetura modular; fail-safe por processo; idempotência; filtro estrito.
- Paralelismo via `ProcessPoolExecutor`; CLI `argparse`; logging console+arquivo.
