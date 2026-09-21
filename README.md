# Vaguinha 🅿️

Sistema simples de gestão de **entrada e saída de veículos** em estacionamento,
organizado por áreas (locais). Feito em Python + Streamlit.

A ocupação de cada área é **derivada do saldo de movimentações**:

```
ocupados = Σ entradas − Σ saídas   (por área e por tipo de veículo)
```

## Funcionalidades

- Visualização das 13 áreas com carros e motos ocupados vs. capacidade.
- Botões de **entrada** e **saída** por tipo de veículo.
- Barras de ocupação (% carros, % motos, % total) e vagas restantes.
- Histórico de movimentações por área.

## Como rodar

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Estrutura

| Arquivo          | Papel                                                        |
|------------------|-------------------------------------------------------------|
| `app.py`         | Interface Streamlit                                         |
| `repository.py`  | Persistência (SQLite p/ protótipo, PostgreSQL p/ produção) |
| `config.py`      | Tipos de veículo e capacidades das áreas                   |
| `schema.sql`     | Schema PostgreSQL                                          |

## Banco de dados

Por padrão usa **SQLite em memória** (protótipo). Para PostgreSQL, aplique o
`schema.sql` e troque a implementação em `get_repo()` (`app.py`) para
`PostgresRepository` com seu DSN.

## Tecnologias

Python · Streamlit · SQLite · PostgreSQL
