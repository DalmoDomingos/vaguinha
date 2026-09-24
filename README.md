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

## Vários operadores ao mesmo tempo

O app foi feito para **muitas pessoas registrando entradas e saídas ao mesmo
tempo** (testado com 100 operadores simultâneos: ~0,2 s por clique):

- **Pool de conexões** com o banco (15 por padrão, `DB_MAX_CONEXOES`).
- **Lotação garantida**: dois operadores não conseguem ocupar a mesma última
  vaga — o registro trava a combinação (área, tipo) no Postgres.
- Os **cards se atualizam sozinhos** a cada 5 s (`ATUALIZAR_A_CADA`) para cada
  operador ver o que os outros registraram; os cliques atualizam só os cards.
- O histórico de uma área só é consultado quando alguém liga **Ver histórico**.

> Use a URL do **Transaction pooler** do Supabase (porta 6543).

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

1. Tabelas: o app **cria/migra o banco sozinho** ao conectar (aplica o
   `schema.sql` se o banco estiver vazio ou na versão antiga, sem perder dados —
   as 13 áreas da versão sem eventos viram o evento "Corrida da FAB", com todo o
   histórico). Se preferir, rode o `schema.sql` à mão no **SQL Editor**; ele é
   idempotente.
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
5. Horários: o app grava e mostra tudo no fuso de **Brasília**, qualquer que
   seja o fuso do Supabase (não precisa configurar nada lá).
6. No plano gratuito o projeto **pausa após ~1 semana sem uso**; reative pelo
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
