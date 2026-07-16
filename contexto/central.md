# Arquivo Central — Contexto do Projeto

Índice acumulativo dos handoffs de estado do projeto **Conversor PDF → DOCX**. Cada entrada resume as entregas de um dia e aponta para o documento detalhado correspondente.

---

## Histórico de entregas

**2026-07-16** — Entrega da **v1.0.0** do conversor em lote PDF → DOCX. Implementada a arquitetura modular em `main.py` (descoberta, planejamento, conversão isolada e orquestração), com as regras de negócio de fail-safe (isolamento por processo), idempotência de nomes e filtro estrito de `.pdf`. Stack definida com `pdf2docx` sobre Python 3.14, paralelismo via `ProcessPoolExecutor` e CLI com `argparse`. Conversão validada com os 4 PDFs reais (0 falhas) e o projeto foi publicado no GitHub (commit `23f08fd`). Detalhes completos em [[2026-07-16_estado]].
