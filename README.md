# Conversor PDF → DOCX (em lote)

Rotina de backend, de nível de produção, para converter em lote arquivos **PDF**
para **DOCX**, preservando layout, tabelas e imagens. Projetado para processar
documentações de TI de forma resiliente e automatizável.

---

## Índice

- [Arquitetura](#arquitetura)
- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Uso](#uso)
- [Regras de negócio](#regras-de-negócio)
- [Estrutura de pastas](#estrutura-de-pastas)
- [Decisões técnicas](#decisões-técnicas)
- [Histórico de versões](#histórico-de-versões)

---

## Arquitetura

O código é modular e separa responsabilidades em funções de propósito único:

| Função | Responsabilidade |
| --- | --- |
| `discover_pdfs()` | Varre o diretório de origem e aplica o **filtro estrito** de `.pdf`. |
| `target_path_for()` | Resolve o caminho de destino mantendo o nome de origem. |
| `convert_single()` | Converte **um** arquivo (executa em processo worker isolado). |
| `_plan_jobs()` | Aplica a **idempotência** separando o que converter do que ignorar. |
| `run_batch()` | **Orquestrador**: gerencia a fila, o pool de processos e o progresso. |
| `summarize()` / `_log_result()` | Agregação e log dos resultados. |
| `main()` / `build_parser()` | Camada de CLI e ponto de entrada. |

```
CLI (argparse) ─► run_batch ─┬─► _plan_jobs (idempotência / filtro)
                             └─► ProcessPoolExecutor ─► convert_single (fail-safe)
```

## Requisitos

- **Python 3.10+** (validado em Python 3.14)
- Dependência principal: [`pdf2docx`](https://pypi.org/project/pdf2docx/) `0.5.13`
  (traz o `PyMuPDF` como dependência transitiva)

## Instalação

```powershell
# (opcional, recomendado) criar e ativar um ambiente virtual
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# instalar dependências
pip install -r requirements.txt
```

Ou instalar diretamente:

```powershell
pip install pdf2docx==0.5.13
```

## Uso

Execute a partir da **raiz do projeto**:

```powershell
# Conversão padrão: ./pdf -> ./docx
python main.py

# Definir número de processos paralelos
python main.py --workers 4

# Reprocessar mesmo que o .docx já exista (desativa a idempotência)
python main.py --force

# Origem/destino customizados + logs detalhados
python main.py --src pdf --dst docx --verbose
```

| Flag | Descrição | Padrão |
| --- | --- | --- |
| `--src` | Diretório de origem dos PDFs. | `pdf` |
| `--dst` | Diretório de destino dos DOCX. | `docx` |
| `--workers` | Número de processos paralelos. | nº de CPUs (limitado ao nº de arquivos) |
| `--force` | Reprocessa/sobrescreve arquivos já convertidos. | desativado |
| `--verbose` | Ativa logs de nível `DEBUG` no console. | desativado |

O script também grava um log persistente em `conversao.log` na raiz do projeto.
O **exit code** é `1` quando houve qualquer falha (útil para CI/pipelines) e `0`
quando tudo ocorreu bem.

## Regras de negócio

- **Fail-safe:** uma falha em um PDF (corrompido, protegido, erro da engine) é
  capturada, registrada e **não interrompe** o lote. O processamento segue para
  o próximo arquivo. A conversão roda em processos isolados, de modo que até um
  crash de baixo nível da engine não derruba o processo principal.
- **Idempotência de nomes:** o `.docx` de saída mantém exatamente o nome de
  origem, trocando apenas a extensão (`arquivo_a.pdf` → `arquivo_a.docx`). Além
  disso, execuções repetidas ignoram arquivos já convertidos (salvo `--force`).
- **Filtro estrito:** apenas arquivos com extensão `.pdf` (case-insensitive) são
  processados; qualquer outro arquivo solto na pasta é ignorado.

## Estrutura de pastas

```
ConversorPdfDocx/
├── main.py            # script principal (modular)
├── requirements.txt   # dependências
├── README.md          # este arquivo
├── .gitignore
├── pdf/               # ORIGEM — coloque aqui os PDFs a converter
└── docx/              # DESTINO — os .docx convertidos são gravados aqui
```

## Decisões técnicas

- **Engine `pdf2docx`:** convenção sobre configuração — reconstrói parágrafos,
  tabelas e imagens a partir do layout do PDF, entregando um DOCX editável com
  fidelidade superior a extrações de texto puro.
- **`ProcessPoolExecutor` (e não threads):** a conversão é CPU-bound e a engine
  libera pouco o GIL; processos separados dão paralelismo real **e** isolam
  falhas da engine (um segfault não contamina o lote).
- **`pathlib`:** resolução de caminhos robusta e multiplataforma.
- **Limpeza de artefatos parciais:** se a conversão falha no meio, um `.docx`
  incompleto é removido para não deixar saída corrompida.

## Histórico de versões

### v1.0.0
- Conversão em lote PDF → DOCX com `pdf2docx`.
- Arquitetura modular (descoberta, planejamento, conversão, orquestração).
- Fail-safe por processo, idempotência, filtro estrito de `.pdf`.
- Paralelismo via `ProcessPoolExecutor` e CLI com `argparse`.
- Logging para console e arquivo (`conversao.log`).
