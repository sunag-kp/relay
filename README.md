# Relay

[![CI](https://github.com/sunag-kp/relay/actions/workflows/ci.yml/badge.svg)](https://github.com/sunag-kp/relay/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**A self-hostable LLM gateway: apps call one OpenAI-compatible endpoint; Relay handles routing, reliability, cost and limits across providers.**

> Work in progress. The README is completed with the live demo, architecture, benchmarks and design notes as the milestones land.

## Run it locally

```bash
make up      # 3 gateway replicas behind nginx + Postgres + Redis
make test    # unit + integration tests
make lint    # ruff, mypy (strict), import-linter architecture contracts
```
