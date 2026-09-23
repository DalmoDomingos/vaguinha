# Vaguinha 🅿️

Sistema simples de gestão de **entrada e saída de veículos** em estacionamento,
organizado por **eventos** e suas **áreas**. Feito em Python + Streamlit.

A ocupação de cada área é **derivada do saldo de movimentações**:

```
ocupados = Σ entradas − Σ saídas   (por área e por tipo de veículo)
```

## Funcionalidades

- **Tela inicial** com a lista de eventos (ocupação de cada um) e o botão
  **Criar novo evento**. O evento "Corrida da FAB" já vem criado.
- Em cada evento: **quantas áreas quiser**, com **nome**, **cor** e vagas de
  **carros** e **motos** — dá para renomear, incluir e excluir áreas e renomear
  o evento em "⚙️ Editar evento". (Área com veículos estacionados não pode ser
  excluída: dê saída antes.)
- Botões de **entrada** e **saída** por tipo de veículo, respeitando a lotação.
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
| `config.py`      | Tipos de veículo, paleta de cores e evento inicial         |
| `schema.sql`     | Schema PostgreSQL (cria **e migra** o banco)               |

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

### Usando o Supabase

1. No painel do projeto: **SQL Editor** → cole o conteúdo de `schema.sql` → **Run**.
   Rode de novo sempre que o `schema.sql` mudar: ele é idempotente e **migra**
   bancos antigos sem perder dados (ex.: as 13 áreas da versão sem eventos
   viram o evento "Corrida da FAB", com todo o histórico).
2. Em **Connect**, copie a URL do **Transaction pooler** (porta `6543`, host
   `aws-0-<região>.pooler.supabase.com`). Evite a "Direct connection": ela usa
   só IPv6 e costuma falhar no Streamlit Cloud e em várias redes.
3. No `secrets.toml` (ou nos *Secrets* do Streamlit Cloud):
   ```toml
   DATABASE_URL = "postgresql://postgres.<ref-do-projeto>:<SENHA>@aws-0-<região>.pooler.supabase.com:6543/postgres?sslmode=require"
   ```
   Se a senha tiver caracteres especiais (`@ # / : ?`), codifique-os
   (ex.: `@` → `%40`) ou gere uma senha só com letras e números.
4. O `schema.sql` ativa RLS nas tabelas para que elas **não** fiquem acessíveis
   pela API pública do Supabase — o app não é afetado.
5. No plano gratuito o projeto **pausa após ~1 semana sem uso**; reative pelo
   painel antes do evento.

## Ambientes (teste × produção)

| Branch    | Ambiente              | Banco Supabase   | Secrets do app                          |
|-----------|-----------------------|------------------|-----------------------------------------|
| `develop` | **Teste** (homologação) | projeto de teste | `DATABASE_URL` do teste + `AMBIENTE = "teste"` |
| `main`    | **Produção**          | projeto real     | `DATABASE_URL` real (sem `AMBIENTE`)    |

Fluxo de trabalho:

1. Crie uma branch a partir da `develop` (ex.: `feat/minha-mudanca`).
2. Abra o PR para a **`develop`** → o app de teste atualiza → testem lá.
3. Tudo certo? Abra um PR de **`develop` → `main`** para levar à produção.

Com `AMBIENTE = "teste"` o app mostra uma faixa **⚠️ AMBIENTE DE TESTE** no topo,
para ninguém registrar veículos reais no app errado.

## Tecnologias

Python · Streamlit · SQLite · PostgreSQL
