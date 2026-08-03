# IVAD Technical Report

This directory publishes the Technical Report **IVAD: Evidence-Constrained and Risk-Controlled Failure Diagnosis for AI Agents** with its reproducible LaTeX source.

- [Read the PDF](IVAD.pdf)
- [Browse the LaTeX source](source/)

## Build the Technical Report

Run the following commands from `paper/source/` with a TeX distribution that includes `acmart`, TikZ, BibTeX, and their dependencies:

```bash
pdflatex -interaction=nonstopmode -halt-on-error main-arxiv.tex
bibtex main-arxiv
pdflatex -interaction=nonstopmode -halt-on-error main-arxiv.tex
pdflatex -interaction=nonstopmode -halt-on-error main-arxiv.tex
```

The generated file is `main-arxiv.pdf`; the checked-in copy is `IVAD.pdf`.

## Evidence scope

The report distinguishes deterministic engineering evidence from formal semantic observations. The public Phase 5 bundle under `../evals/reports/reference/phase5-formal-deepseek-only/` records a DeepSeek-only run with 2,148 scheduled/evaluated cells and zero missing cells. B0-B3 completed; B4 and B5 are policy-skipped with no Qwen results. The observed B3-vs-B2 risk/coverage differences are not causal, cross-model, or certificate claims, and H1-H5 remain unresolved.

## Copyright

Copyright 2026 Hanzhe Liu. The Technical Report, figures, and report source are licensed under
the [Creative Commons Attribution 4.0 International License](https://creativecommons.org/licenses/by/4.0/).
The SpanVouch software remains separately licensed under the repository's MIT License.
