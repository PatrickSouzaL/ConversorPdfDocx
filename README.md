# Conversor PDF → DOCX (Document Layout Analysis)

Rotina de backend, de nível de produção, para converter em lote **PDFs "chapados"**
(páginas exportadas inteiramente como imagem, misturando texto instrucional e
prints de tela) em **DOCX nativo e editável**.

Em vez de extrair texto ou sobrepor uma camada invisível, o pipeline **fatia cada
página e reconstrói o documento do zero**. A partir da **v3.0.0**, o *"o que é
texto x o que é print"* deixou de ser adivinhado por heurísticas e passou a ser
decidido por um **modelo de Visão Computacional** (DocLayout-YOLO): a rede detecta
as regiões da página, aplicamos OCR apenas nas regiões de texto e recortamos as
regiões de imagem, remontando o fluxo original — mas agora com o texto 100%
editável no Word e os prints preservados como imagem.

---

## Índice

- [Como funciona (pipeline DLA)](#como-funciona-pipeline-dla)
- [O modelo de IA](#o-modelo-de-ia)
- [Arquitetura](#arquitetura)
- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Uso](#uso)
- [Ajuste da detecção](#ajuste-da-detecção)
- [Regras de negócio](#regras-de-negócio)
- [Estrutura de pastas](#estrutura-de-pastas)
- [Limitações conhecidas](#limitações-conhecidas)
- [Histórico de versões](#histórico-de-versões)

---

## Como funciona (pipeline DLA)

Cada arquivo passa por três passos:

```
                ┌──────────────────────────────────────────────┐
 PDF (imagem) ─►│ Passo 1 — RASTERIZAÇÃO (pdf2image/Poppler)    │
                │ cada página vira uma imagem em alta resolução │
                └───────────────────────┬──────────────────────┘
                                         ▼
                ┌──────────────────────────────────────────────┐
                │ Passo 2 — LAYOUT ANALYSIS (DocLayout-YOLO)    │
                │  a) o modelo detecta as regiões da página     │
                │  b) mapeia cada classe: TEXTO x IMAGEM         │
                │  c) TEXTO  -> OCR lê a string limpa no recorte │
                │     IMAGEM -> recorta o print da imagem original│
                └───────────────────────┬──────────────────────┘
                                         ▼
                ┌──────────────────────────────────────────────┐
                │ Passo 3 — RECONSTRUÇÃO (python-docx)          │
                │ remonta o DOCX na ordem de leitura (top->down):│
                │ parágrafos de texto nativo + imagens recortadas│
                └──────────────────────────────────────────────┘
```

### Pré-processamento para o OCR

O tratamento com OpenCV (escala de cinza, **inversão de fundo escuro** e
*thresholding* Otsu) é aplicado **somente na cópia** do recorte de texto enviada
ao Tesseract, para elevar a precisão em PDFs com fundo escuro/cinza. A imagem
original colorida é preservada intacta para o recorte dos prints.

## O modelo de IA

O Passo 2 usa o **DocLayout-YOLO** (`DocStructBench`), um YOLOv10 ajustado para
análise de layout de documentos. Os pesos são **baixados automaticamente** do
HuggingFace no primeiro uso e ficam em cache.

**Hardware (fallback automático):** na inicialização, o programa usa `torch` para
detectar **GPU (CUDA)**; havendo uma, a inferência roda nela, senão cai para
**CPU**. O hardware escolhido é registrado no log (`Hardware de inferência: ...`).

**Mapeamento de classes → domínios:** a rede devolve classes de layout que
reduzimos aos nossos dois domínios essenciais (ver `conversor/config.py`):

| Classe do modelo | Domínio | Ação |
| --- | --- | --- |
| `title`, `plain text`, `*_caption`, `table_footnote` | **TEXTO** | OCR no recorte |
| `figure`, `table`, `isolate_formula` | **IMAGEM** | recorta o print original |
| `abandon` (nº de página, cabeçalho solto) | — | descartada |
| *(classe desconhecida)* | **IMAGEM** | recortada (não perde conteúdo) |

> **Carregamento seguro para multiprocessamento:** os pesos são baixados **uma vez**
> no processo principal (sem corrida entre workers) e cada worker carrega o modelo
> em memória **uma única vez** (singleton lazy por processo), nunca por arquivo.

## Arquitetura

Código modular, no pacote `conversor/`, com responsabilidades isoladas:

| Módulo | Responsabilidade |
| --- | --- |
| `config.py` | Dataclasses imutáveis/picláveis (`PipelineConfig`, `Block`) + mapeamento de classes. |
| `dependencies.py` | Detecção de Poppler, dos dados de idioma do Tesseract e das dependências de IA. |
| `rendering.py` | **Passo 1**: rasteriza as páginas (pdf2image/Poppler), uma a uma. |
| `detector.py` | **Passo 2 (IA)**: DocLayout-YOLO — device, download dos pesos, inferência, mapeamento. |
| `ocr.py` | Pré-processamento de imagem e OCR por região (pytesseract). |
| `layout.py` | **Passo 2**: orquestra detecção + OCR das regiões + ordem de leitura. |
| `docx_builder.py` | **Passo 3**: reconstrução do DOCX nativo (python-docx). |
| `pipeline.py` | Orquestra os 3 passos de **um** arquivo (worker, fail-safe). |
| `main.py` | CLI, descoberta, planejamento e orquestração do lote. |

## Requisitos

- **Python 3.10–3.12** recomendado (é a faixa com wheels estáveis de `torch` /
  `doclayout-yolo`; em Python 3.13/3.14 pode não haver wheel disponível ainda).
- Dependências Python: ver `requirements.txt` (inclui `torch` + `doclayout-yolo`).
- **Binários de sistema:** **Poppler** (para o pdf2image) e **Tesseract OCR**
  (para o pytesseract).
- **GPU (opcional):** uma placa **NVIDIA/CUDA** acelera a inferência. Sem GPU, o
  pipeline roda em CPU automaticamente (mais lento). Para GPU, instale o wheel
  CUDA do PyTorch **antes** do `requirements.txt` (ver <https://pytorch.org/get-started>).
- **Acesso à internet** no primeiro uso, para baixar os pesos do modelo (cacheados
  depois).

## Instalação

### 1. Dependências Python

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Binários de sistema (Windows)

**Tesseract OCR** — instale via `winget install UB-Mannheim.TesseractOCR` ou pelo
instalador de <https://github.com/UB-Mannheim/tesseract/wiki> (marque o idioma
**Portuguese** durante a instalação). Garanta que `tesseract` esteja no `PATH`.

**Poppler** — não tem instalador tradicional; baixe o pacote de
<https://github.com/oschwartz10612/poppler-windows/releases> e:

- **Opção A (sistema):** extraia e adicione a pasta `.../Library/bin` ao `PATH`.
- **Opção B (fallback local, sem PATH):** extraia dentro de uma pasta `.poppler/`
  na raiz do projeto. O programa procura o `pdftoppm.exe` automaticamente ali.

Verifique:

```powershell
tesseract --version
pdftoppm -v
```

> **Idioma português (fallback local):** se o Tesseract do sistema não tiver o
> `por`, crie uma pasta `.tessdata/` na raiz com os arquivos `<idioma>.traineddata`
> (de <https://github.com/tesseract-ocr/tessdata_fast>) **e** as subpastas de
> suporte `configs/`, `tessconfigs/`, `script/` + `pdf.ttf` copiadas do `tessdata`
> do sistema. O `main.py` detecta e usa esse `.tessdata` automaticamente quando o
> idioma pedido não está instalado — sem precisar de variável de ambiente.

## Uso

Execute a partir da **raiz do projeto**:

```powershell
python main.py                     # ./pdf -> ./docx (GPU se houver, senão CPU)
python main.py --dpi 400 --verbose # mais resolução (OCR melhor, mais lento)
python main.py --ocr-lang eng      # idioma do Tesseract
python main.py --workers 4 --force # paraleliza e reprocessa tudo
python main.py --device cpu        # força CPU mesmo com GPU disponível
```

| Flag | Descrição | Padrão |
| --- | --- | --- |
| `--src` | Diretório de origem dos PDFs. | `pdf` |
| `--dst` | Diretório de destino dos DOCX. | `docx` |
| `--workers` | Nº de processos paralelos. | nº de CPUs (em GPU: `1`, p/ não estourar a VRAM) |
| `--force` | Reprocessa/sobrescreve arquivos já convertidos. | desativado |
| `--dpi` | Resolução de rasterização das páginas. | `300` |
| `--ocr-lang` | Idioma(s) do Tesseract (`por`, `eng`, `por+eng`). | `por+eng` |
| `--min-conf` | Confiança mínima (0-100) para aceitar uma palavra do OCR. | `40` |
| `--device` | Hardware de inferência: `auto`, `cpu` ou `cuda`. | `auto` |
| `--yolo-conf` | Confiança mínima de detecção do modelo (0-1). | `0.2` |
| `--yolo-imgsz` | Tamanho de inferência do modelo (px). | `1024` |
| `--verbose` | Ativa logs `DEBUG` no console. | desativado |

Log persistente em `conversao.log`. **Exit code** `1` se houve qualquer falha,
`0` caso contrário. Se Poppler, Tesseract ou as dependências de IA não forem
encontrados, o programa avisa e encerra com código `2`.

## Ajuste da detecção

Se prints estiverem virando texto (ou vice-versa), ajuste a detecção do modelo:

- **Muitas regiões espúrias / falsos positivos** → **aumente** `--yolo-conf`.
- **Regiões deixando de ser detectadas** → **reduza** `--yolo-conf`.
- **Texto pequeno não detectado** → **aumente** `--yolo-imgsz` (ex.: `1280`) e/ou
  o `--dpi`.
- O mapeamento de cada classe do modelo para **TEXTO/IMAGEM** (e o que é descartado)
  está em `conversor/config.py` (`TEXT_CLASSES`, `IMAGE_CLASSES`, `DISCARD_CLASSES`).

## Regras de negócio

- **Fail-safe:** falha em um PDF (arquivo bloqueado, corrompido, engine) é
  capturada e **não interrompe** o lote — cada arquivo roda em processo isolado.
- **Idempotência de nomes:** o `.docx` mantém o nome de origem (`.pdf`→`.docx`) e
  execuções repetidas ignoram os já convertidos (salvo `--force`).
- **Filtro estrito:** apenas `.pdf` (case-insensitive).
- **Texto nativo:** o conteúdo textual é reconstruído como texto real do Word
  (editável e pesquisável) — não é imagem nem camada invisível.

## Estrutura de pastas

```
ConversorPdfDocx/
├── main.py             # CLI + orquestração do lote
├── conversor/          # pacote com o pipeline DLA (módulos por passo)
├── requirements.txt    # dependências Python
├── README.md           # este arquivo
├── .gitignore
├── contexto/           # handoffs de estado do projeto
├── pdf/                # ORIGEM — coloque aqui os PDFs a converter
└── docx/               # DESTINO — os .docx convertidos são gravados aqui
```

## Limitações conhecidas

- A **detecção é probabilística**: o modelo não acerta 100% dos casos. Ajuste
  `--yolo-conf`/`--yolo-imgsz` (ver acima); regiões não detectadas não entram no
  DOCX. O pipeline privilegia preservar conteúdo (classe desconhecida vira imagem).
- **Primeiro uso** baixa os pesos do modelo (requer internet) e, em CPU, a
  inferência é bem mais lenta que em GPU.
- **Ordem de leitura** assume fluxo de coluna única (top-to-bottom, esquerda→
  direita por faixa). Layouts multicoluna complexos podem sair fora de ordem.
- A **qualidade do texto** depende da resolução do print original; scans muito
  ruidosos podem gerar erros de OCR (tente `--dpi 400`).
- Arquivos abertos no Word ficam **bloqueados** e falham na sobrescrita (fail-safe):
  feche-os antes de reprocessar com `--force`.

## Histórico de versões

### v3.0.0 — Visão Computacional (DocLayout-YOLO)
- **Substitui a heurística por ML:** a segmentação/classificação de blocos por
  projeção e limiares (colorfulness/densidade/morfologia OpenCV) foi removida e
  trocada por um modelo **DocLayout-YOLO** (`ultralytics`/YOLOv10) que detecta as
  regiões da página.
- **Fallback de hardware:** usa GPU (CUDA) via `torch` quando disponível, senão CPU;
  o hardware é registrado no log.
- **OCR por região:** o Tesseract passa a ler apenas o recorte de cada região de
  TEXTO (pré-processamento Otsu só nessa cópia); regiões de IMAGEM são recortadas
  da imagem original colorida — sem OCR.
- **Carregamento seguro em multiprocessamento:** pesos baixados uma vez no processo
  principal; modelo carregado uma vez por worker (singleton lazy). Em GPU, o padrão
  é 1 worker para evitar OOM de VRAM.
- Preservados: pacote modular `conversor/`, fail-safe por arquivo e idempotência.

### v2.0.0 — Document Layout Analysis (reescrita)
- **Nova arquitetura DLA:** substitui `ocrmypdf`+`pdf2docx` por um pipeline que
  fatia a página (pdf2image), classifica blocos com OpenCV+Tesseract (texto ×
  print) e remonta um DOCX **nativo/editável** com python-docx.
- Texto reconstruído como texto real do Word; prints recortados e reinseridos na
  ordem de leitura.
- Pré-processamento OpenCV (cinza + inversão de fundo escuro + Otsu) só na cópia
  enviada ao OCR, para maior precisão em fundos escuros.
- Detecção automática de Poppler (`.poppler/`) e dos dados de idioma (`.tessdata/`).

### v1.x — Pipeline OCR "sandwich" (descontinuado)
Abordagem anterior baseada em `ocrmypdf` (Sandwich PDF) + `pdf2docx`. Descontinuada
porque mantinha a página como imagem (texto não editável) — ver histórico no Git.
