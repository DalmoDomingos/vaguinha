"""
Lotação - gestão de entrada/saída de veículos em estacionamento, por evento.

Rodar:
    pip install -r requirements.txt
    streamlit run app.py

Telas (navegação pela URL, então dá para recarregar/compartilhar o link):
    /                 → tela inicial: lista de eventos + "Criar novo evento"
    /?tela=novo       → criação de evento (nome + áreas, cores e vagas)
    /?evento=<id>     → controle de entrada/saída do evento

Persistência: SQLite em memória por padrão (protótipo); PostgreSQL/Supabase
quando houver DATABASE_URL (veja `get_repo()`).
"""

import html
import os
from datetime import datetime

import pandas as pd
import streamlit as st

from config import COR_PADRAO, PALETA, TIPO_CARRO, TIPO_MOTO, emoji_da_cor, rotulo_da_cor
from repository import OperacaoInvalida, PostgresRepository, SQLiteRepository

st.set_page_config(page_title="Lotação", page_icon="🅿️", layout="centered")

# ------------------------------------------------------------------
# Estilo: cards de área maiores e fonte um pouco maior
# ------------------------------------------------------------------
st.markdown(
    """
    <style>
      /* Aumenta um pouco a fonte geral do app */
      html, body, [data-testid="stAppViewContainer"] { font-size: 17.5px; }

      /* Deixa os cards (expanders) das áreas maiores e com título maior */
      [data-testid="stExpander"] details {
          border-radius: 12px;
      }
      [data-testid="stExpander"] summary {
          padding: 1rem 1.2rem;
      }
      [data-testid="stExpander"] summary p {
          font-size: 1.25rem;
          font-weight: 600;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------
# Configuração externa (sem senha no código)
# ------------------------------------------------------------------
def ler_config(nome: str):
    """
    Lê uma configuração de:
      1. .streamlit/secrets.toml (ou Secrets do Streamlit Cloud)  — arquivo NÃO versionado
      2. variável de ambiente                                      (ex.: no servidor de deploy)
    """
    valor = None
    try:
        valor = st.secrets.get(nome)
    except Exception:
        valor = None
    return valor or os.environ.get(nome)


# "teste" no app de homologação (branch develop); ausente/"producao" no app real.
AMBIENTE = (ler_config("AMBIENTE") or "producao").strip().lower()


# ------------------------------------------------------------------
# Repositório (único, mantido entre reruns do Streamlit)
# ------------------------------------------------------------------
@st.cache_resource
def get_repo():
    """
    Escolhe a persistência automaticamente:
      1. DATABASE_URL (secrets ou variável de ambiente) → PostgreSQL
      2. fallback: SQLite em memória                    (protótipo; zera ao reiniciar)
    """
    dsn = ler_config("DATABASE_URL")
    if dsn:
        return PostgresRepository(dsn)
    return SQLiteRepository(":memory:")


repo = get_repo()
usando_pg = isinstance(repo, PostgresRepository)
BANCO = "PostgreSQL ✅" if usando_pg else "SQLite (memória) ⚠️"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def pct(ocupados: int, capacidade: int) -> float:
    if capacidade <= 0:
        return 0.0
    return max(0.0, min(1.0, ocupados / capacidade))


def fmt_data(valor) -> str:
    """Data/hora do banco (datetime no Postgres, texto no SQLite) → dd/mm/aaaa hh:mm."""
    try:
        dt = valor if isinstance(valor, datetime) else datetime.fromisoformat(str(valor))
        return dt.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(valor)


def num(valor) -> int:
    """Número de uma célula da tabela (vazia/NaN conta como 0)."""
    return 0 if valor is None or valor != valor else int(valor)


def avisar(msg: str, icon: str = "✅"):
    """Guarda um aviso para mostrar depois do st.rerun()."""
    st.session_state["aviso"] = (msg, icon)


def ir_para(evento=None, tela=None):
    """Navega trocando a URL (?evento=<id> / ?tela=novo / tela inicial)."""
    st.query_params.clear()
    if evento is not None:
        st.query_params["evento"] = str(evento)
    elif tela:
        st.query_params["tela"] = tela


def registrar(tipo_id: int, local_id: int, mov: str):
    """
    Registra o evento. A validação de saldo usa a capacidade e a ocupação
    ATUAIS do banco (não os valores da última renderização, que podem estar
    defasados quando há mais de um operador).
    """
    try:
        repo.registrar(tipo_id, local_id, mov)
    except OperacaoInvalida as e:
        st.toast(str(e), icon="⚠️")
    except Exception as e:
        st.toast(f"Erro ao registrar movimento: {e}", icon="❌")


def editor_areas(areas: list, key: str) -> list:
    """
    Tabela editável de áreas (nome, cor, vagas), com linhas para adicionar e
    excluir. Devolve a lista no formato do repositório (com o id das existentes).
    """
    df = pd.DataFrame(
        [
            {
                "id": a.get("id"),
                "Área": a["nome"],
                "Cor": rotulo_da_cor(a["cor"]),
                "Carros": int(a["cap_carro"]),
                "Motos": int(a["cap_moto"]),
            }
            for a in areas
        ],
        columns=["id", "Área", "Cor", "Carros", "Motos"],
    )
    # cores fora da paleta (ex.: definidas direto no banco) continuam selecionáveis
    opcoes_cor = list(PALETA) + sorted({c for c in df["Cor"] if c not in PALETA})

    st.caption(
        "✏️ Clique numa célula para editar · ➕ adicione áreas na última linha · "
        "🗑️ para excluir, marque a linha à esquerda e clique na lixeira."
    )
    editado = st.data_editor(
        df,
        key=key,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_order=["Área", "Cor", "Carros", "Motos"],
        column_config={
            "Área": st.column_config.TextColumn(required=True, default="Nova área", max_chars=60),
            "Cor": st.column_config.SelectboxColumn(
                options=opcoes_cor, required=True, default=rotulo_da_cor(COR_PADRAO),
            ),
            "Carros": st.column_config.NumberColumn(
                min_value=0, step=1, format="%d", default=0, required=True,
            ),
            "Motos": st.column_config.NumberColumn(
                min_value=0, step=1, format="%d", default=0, required=True,
            ),
        },
    )

    def texto(v):
        return v if isinstance(v, str) else ""

    return [
        {
            "id": r["id"],
            "nome": texto(r["Área"]),
            "cor": PALETA.get(texto(r["Cor"]), texto(r["Cor"])) or COR_PADRAO,
            "cap_carro": r["Carros"],
            "cap_moto": r["Motos"],
        }
        for r in editado.to_dict("records")
    ]


# ------------------------------------------------------------------
# Tela inicial: lista de eventos
# ------------------------------------------------------------------
def tela_inicial():
    st.markdown("<h1 style='text-align:center'>Lotação</h1>", unsafe_allow_html=True)
    st.caption(f"Controle de vagas por evento · Banco: {BANCO}")

    st.button(
        "➕ Criar novo evento", type="primary", width="stretch",
        on_click=ir_para, kwargs={"tela": "novo"},
    )

    st.subheader("Eventos")
    eventos = repo.listar_eventos()
    if not eventos:
        st.info("Nenhum evento ainda. Crie o primeiro no botão acima.")
    for e in eventos:
        with st.container(border=True):
            info, acao = st.columns([4, 1], vertical_alignment="center")
            info.markdown(f"#### {e['nome']}")
            info.caption(
                f"{e['n_areas']} área(s) · {e['capacidade']} vagas · "
                f"criado em {fmt_data(e['criado_em'])}"
            )
            acao.button(
                "Abrir →", key=f"abrir_{e['id']}", type="primary", width="stretch",
                on_click=ir_para, kwargs={"evento": e["id"]},
            )
            p = pct(e["ocupados"], e["capacidade"])
            st.progress(p, text=f"Ocupação — {p*100:.1f}% ({e['ocupados']}/{e['capacidade']})")


# ------------------------------------------------------------------
# Tela de criação de evento
# ------------------------------------------------------------------
def tela_novo_evento():
    st.button("← Voltar", on_click=ir_para)
    st.title("Novo evento")

    nome = st.text_input(
        "Nome do evento", max_chars=80, key="novo_nome",
        placeholder="Ex.: Show de aniversário da cidade",
    )
    st.markdown("**Áreas do evento**")
    areas = editor_areas(
        [
            {"nome": f"Área {i}", "cor": COR_PADRAO, "cap_carro": 50, "cap_moto": 20}
            for i in range(1, 4)
        ],
        key="novo_areas",
    )
    total = sum(num(a["cap_carro"]) + num(a["cap_moto"]) for a in areas)
    st.caption(f"{len(areas)} área(s) · capacidade total: **{total}** vagas")

    if st.button("Criar evento", type="primary", width="stretch"):
        try:
            evento_id = repo.criar_evento(nome, areas)
        except OperacaoInvalida as e:
            st.error(str(e))
            return
        avisar(f"Evento “{nome.strip()}” criado!")
        ir_para(evento=evento_id)
        st.rerun()


# ------------------------------------------------------------------
# Tela do evento: entrada/saída por área
# ------------------------------------------------------------------
def secao_editar_evento(evento: dict, areas: list):
    """Renomear o evento e editar áreas (nome, cor, vagas, incluir/excluir)."""
    evento_id = evento["id"]
    # Trocar a versão recria os widgets com os dados salvos (descarta o rascunho).
    versao = st.session_state.get(f"versao_{evento_id}", 0)

    with st.expander("⚙️ Editar evento — nome, áreas, cores e vagas", key=f"editar_{evento_id}"):
        novo_nome = st.text_input(
            "Nome do evento", value=evento["nome"], max_chars=80,
            key=f"nome_{evento_id}_{versao}",
        )
        editadas = editor_areas(areas, key=f"areas_{evento_id}_{versao}")

        if st.button("💾 Salvar alterações", type="primary", key=f"salvar_{evento_id}"):
            try:
                repo.salvar_areas(evento_id, editadas)
                if novo_nome.strip() != evento["nome"]:
                    repo.renomear_evento(evento_id, novo_nome)
            except OperacaoInvalida as e:
                st.error(str(e))
                return
            st.session_state[f"versao_{evento_id}"] = versao + 1
            avisar("Alterações salvas.")
            st.rerun()


def tela_evento(evento_id: int):
    evento = repo.obter_evento(evento_id)
    st.button("← Eventos", on_click=ir_para)
    if evento is None:
        st.warning("Evento não encontrado.")
        return

    st.markdown(f"<h1 style='text-align:center'>{html.escape(evento['nome'])}</h1>", unsafe_allow_html=True)
    st.caption(f"Controle de vagas por área · Banco: {BANCO}")

    areas = repo.listar_areas(evento_id)
    secao_editar_evento(evento, areas)

    # ---- Resumo geral ----
    saldos = repo.saldos(evento_id)  # {(local_id, tipo_veiculo_id): ocupados}
    tot_carros = sum(v for (_, t), v in saldos.items() if t == TIPO_CARRO)
    tot_motos = sum(v for (_, t), v in saldos.items() if t == TIPO_MOTO)
    cap_total = sum(a["cap_carro"] + a["cap_moto"] for a in areas)
    ocup_total = tot_carros + tot_motos

    c1, c2, c3 = st.columns(3)
    c1.metric("Carros", tot_carros)
    c2.metric("Motos", tot_motos)
    c3.metric("Vagas livres", cap_total - ocup_total)

    st.caption(f"Capacidade total configurada: **{cap_total}** vagas")

    # Ocupação geral do evento (todas as áreas somadas)
    ocup_geral = pct(ocup_total, cap_total)
    st.progress(ocup_geral, text=f"Ocupação geral — {ocup_geral*100:.1f}% ({ocup_total}/{cap_total})")

    st.divider()

    # ---- Áreas como cards expansíveis ----
    for i, area in enumerate(areas):
        local_id = area["id"]
        cap_carro, cap_moto = area["cap_carro"], area["cap_moto"]
        ocup_carro = saldos.get((local_id, TIPO_CARRO), 0)
        ocup_moto = saldos.get((local_id, TIPO_MOTO), 0)

        cap_area = cap_carro + cap_moto
        ocup_area = ocup_carro + ocup_moto
        sobrando = cap_area - ocup_area

        # Barra de cor + bolinha da cor no título
        cor = area["cor"] or COR_PADRAO
        bolinha = emoji_da_cor(cor)
        titulo = (f"{bolinha} {area['nome']}".strip()
                  + f"  —  🚗 {ocup_carro}/{cap_carro}   🏍️ {ocup_moto}/{cap_moto}")

        # key fixa: sem ela o card fecha a cada clique, pois o título (contagem) muda
        with st.expander(titulo, expanded=(i == 0), key=f"area_{local_id}"):
            st.markdown(
                f"<div style='height:6px;border-radius:3px;background:{cor};margin:-4px 0 10px'></div>",
                unsafe_allow_html=True,
            )

            # ---- Motos ----
            st.markdown(f"**🏍️ {ocup_moto:02d}/{cap_moto} motos**")
            m1, m2, _ = st.columns([1, 1, 3])
            m1.button(
                "➕ Entrada", key=f"mot_in_{local_id}",
                on_click=registrar, args=(TIPO_MOTO, local_id, "entrada"),
            )
            m2.button(
                "➖ Saída", key=f"mot_out_{local_id}",
                on_click=registrar, args=(TIPO_MOTO, local_id, "saida"),
            )

            # ---- Carros ----
            st.markdown(f"**🚗 {ocup_carro:02d}/{cap_carro} carros**")
            c1b, c2b, _ = st.columns([1, 1, 3])
            c1b.button(
                "➕ Entrada", key=f"car_in_{local_id}",
                on_click=registrar, args=(TIPO_CARRO, local_id, "entrada"),
            )
            c2b.button(
                "➖ Saída", key=f"car_out_{local_id}",
                on_click=registrar, args=(TIPO_CARRO, local_id, "saida"),
            )

            st.write(f"**Vagas sobrando: {sobrando}**")

            # ---- Barras de ocupação ----
            st.progress(pct(ocup_carro, cap_carro), text=f"% carros — {pct(ocup_carro, cap_carro)*100:.0f}%")
            st.progress(pct(ocup_moto, cap_moto), text=f"% motos — {pct(ocup_moto, cap_moto)*100:.0f}%")
            st.progress(pct(ocup_area, cap_area), text=f"% total — {pct(ocup_area, cap_area)*100:.0f}%")

            # ---- Histórico recente da área ----
            with st.popover("Ver histórico"):
                hist = repo.historico(local_id=local_id, limite=15)
                if not hist:
                    st.write("Sem movimentos ainda.")
                else:
                    st.dataframe(
                        [
                            {
                                "horário": fmt_data(h["horario"]),
                                "veículo": "Carro" if h["tipo_veiculo_id"] == TIPO_CARRO else "Moto",
                                "movimento": h["movimentacao"],
                            }
                            for h in hist
                        ],
                        hide_index=True,
                        width="stretch",
                    )


# ------------------------------------------------------------------
# Roteamento
# ------------------------------------------------------------------
if AMBIENTE == "teste":
    st.warning(
        "**⚠️ AMBIENTE DE TESTE** — os dados daqui não são reais. "
        "Não use este app para registrar veículos no evento.",
    )

if "aviso" in st.session_state:
    msg, icon = st.session_state.pop("aviso")
    st.toast(msg, icon=icon)

params = st.query_params
if "evento" in params:
    evento_param = params["evento"]
    if evento_param.isdigit():
        tela_evento(int(evento_param))
    else:
        ir_para()
        st.rerun()
elif params.get("tela") == "novo":
    tela_novo_evento()
else:
    tela_inicial()
