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

O app escolhe a persistência **automaticamente**:

1. Se existir `DATABASE_URL` (em `.streamlit/secrets.toml` ou variável de
   ambiente) → usa **PostgreSQL**.
2. Senão → **SQLite em memória** (protótipo; zera ao reiniciar).

O rodapé do título mostra qual banco está ativo (`PostgreSQL ✅` / `SQLite ⚠️`).

### Ligar no PostgreSQL

1. Crie o banco e aplique o schema:
   ```bash
   psql "postgresql://usuario:senha@host:5432/nome_do_banco" -f schema.sql
   ```
   (isso cria as tabelas e **semeia os 13 locais** — obrigatório por causa da
   chave estrangeira `movimentacao.local_id → local.id`.)
2. Copie o modelo de segredos e preencha com a conexão real:
   ```bash
   cp .streamlit/secrets.toml.example .streamlit/secrets.toml
   ```
3. Rode o app. Se conectar, o rodapé mostra **PostgreSQL ✅**.

> ⚠️ **Nunca** versione `.streamlit/secrets.toml` — ele tem a senha e já está no
> `.gitignore`.

## Tecnologias

Python · Streamlit · SQLite · PostgreSQL
