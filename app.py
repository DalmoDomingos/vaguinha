"""
Lotação - gestão de entrada/saída de veículos em estacionamento.

Rodar:
    pip install -r requirements.txt
    streamlit run app.py

Persistência: SQLite em memória por padrão (protótipo). Para usar PostgreSQL,
veja o bloco `get_repo()` abaixo — basta trocar a implementação do Repository.
"""

import os

import streamlit as st

from config import AREAS, TIPO_CARRO, TIPO_MOTO, cor_da_area
from repository import PostgresRepository, SQLiteRepository

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
# Repositório (único, mantido entre reruns do Streamlit)
# ------------------------------------------------------------------
@st.cache_resource
def get_repo():
    """
    Escolhe a persistência automaticamente, sem senha no código:
      1. DATABASE_URL em .streamlit/secrets.toml   (recomendado — arquivo NÃO versionado)
      2. variável de ambiente DATABASE_URL          (ex.: no servidor de deploy)
      3. fallback: SQLite em memória                (protótipo; zera ao reiniciar)
    """
    dsn = None
    try:
        dsn = st.secrets.get("DATABASE_URL")  # lê .streamlit/secrets.toml, se existir
    except Exception:
        dsn = None
    dsn = dsn or os.environ.get("DATABASE_URL")

    if dsn:
        return PostgresRepository(dsn)
    return SQLiteRepository(":memory:")


repo = get_repo()
usando_pg = isinstance(repo, PostgresRepository)


# ------------------------------------------------------------------
# Capacidades editáveis: começam do config.py e ficam na sessão,
# para o usuário poder ajustar as vagas de cada local pela tela.
# ------------------------------------------------------------------
if "caps" not in st.session_state:
    st.session_state.caps = {
        lid: {"cap_carro": a["cap_carro"], "cap_moto": a["cap_moto"]}
        for lid, a in AREAS.items()
    }


# ------------------------------------------------------------------
# Helpers de exibição
# ------------------------------------------------------------------
def pct(ocupados: int, capacidade: int) -> float:
    if capacidade <= 0:
        return 0.0
    return max(0.0, min(1.0, ocupados / capacidade))


def registrar(tipo_id: int, local_id: int, mov: str, cap: int, ocup: int):
    """Registra o evento com validações simples de saldo."""
    if mov == "entrada" and ocup >= cap:
        st.toast("Área lotada para este tipo de veículo.", icon="⚠️")
        return
    if mov == "saida" and ocup <= 0:
        st.toast("Não há veículos deste tipo para dar saída.", icon="⚠️")
        return
    repo.registrar(tipo_id, local_id, mov)


# ------------------------------------------------------------------
# Cabeçalho
# ------------------------------------------------------------------
st.markdown("<h1 style='text-align:center'>Lotação</h1>", unsafe_allow_html=True)
st.caption(
    "Corrida da FAB — controle de vagas por área · "
    f"Banco: {'PostgreSQL ✅' if usando_pg else 'SQLite (memória) ⚠️'}"
)


# ------------------------------------------------------------------
# Configuração de vagas por local (editável pelo usuário)
# ------------------------------------------------------------------
with st.expander("⚙️ Configurar vagas por local", expanded=False):
    st.write("Edite a capacidade de **carros** e **motos** de cada área. "
             "O total é recalculado automaticamente.")
    linhas = [
        {"Área": AREAS[lid]["nome"], "Carros": c["cap_carro"], "Motos": c["cap_moto"]}
        for lid, c in st.session_state.caps.items()
    ]
    editado = st.data_editor(
        linhas,
        hide_index=True,
        use_container_width=True,
        disabled=["Área"],
        column_config={
            "Carros": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
            "Motos": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
        },
        key="editor_caps",
    )
    # Grava os valores editados de volta na sessão (mesma ordem das linhas)
    for lid, linha in zip(st.session_state.caps.keys(), editado):
        st.session_state.caps[lid]["cap_carro"] = int(linha["Carros"] or 0)
        st.session_state.caps[lid]["cap_moto"] = int(linha["Motos"] or 0)

caps = st.session_state.caps


# ------------------------------------------------------------------
# Resumo geral
# ------------------------------------------------------------------
saldos = repo.saldos()  # {(local_id, tipo_veiculo_id): ocupados}

tot_carros = sum(v for (lid, t), v in saldos.items() if t == TIPO_CARRO)
tot_motos = sum(v for (lid, t), v in saldos.items() if t == TIPO_MOTO)
cap_total = sum(c["cap_carro"] + c["cap_moto"] for c in caps.values())
ocup_total = tot_carros + tot_motos

c1, c2, c3 = st.columns(3)
c1.metric("Carros", tot_carros)
c2.metric("Motos", tot_motos)
c3.metric("Vagas livres", cap_total - ocup_total)

st.caption(f"Capacidade total configurada: **{cap_total}** vagas")

# Ocupação geral do estacionamento (todas as áreas somadas)
ocup_geral = pct(ocup_total, cap_total)
st.progress(ocup_geral, text=f"Ocupação geral — {ocup_geral*100:.1f}% ({ocup_total}/{cap_total})")

st.divider()

# ------------------------------------------------------------------
# Lista de áreas (local_id 1..13) como cards expansíveis
# ------------------------------------------------------------------
for local_id, area in AREAS.items():
    cap_carro = caps[local_id]["cap_carro"]
    cap_moto = caps[local_id]["cap_moto"]
    ocup_carro = saldos.get((local_id, TIPO_CARRO), 0)
    ocup_moto = saldos.get((local_id, TIPO_MOTO), 0)

    cap_area = cap_carro + cap_moto
    ocup_area = ocup_carro + ocup_moto
    sobrando = cap_area - ocup_area

    # Barra de cor + rótulo no topo do expander
    cor = cor_da_area(local_id)
    titulo = f"{area['nome']}  —  🚗 {ocup_carro}/{cap_carro}   🏍️ {ocup_moto}/{cap_moto}"

    with st.expander(titulo, expanded=(local_id == 1)):
        st.markdown(
            f"<div style='height:6px;border-radius:3px;background:{cor};margin:-4px 0 10px'></div>",
            unsafe_allow_html=True,
        )

        # ---- Motos ----
        st.markdown(f"**🏍️ {ocup_moto:02d}/{cap_moto} motos**")
        m1, m2, _ = st.columns([1, 1, 3])
        m1.button(
            "➕ Entrada", key=f"mot_in_{local_id}",
            on_click=registrar, args=(TIPO_MOTO, local_id, "entrada", cap_moto, ocup_moto),
        )
        m2.button(
            "➖ Saída", key=f"mot_out_{local_id}",
            on_click=registrar, args=(TIPO_MOTO, local_id, "saida", cap_moto, ocup_moto),
        )

        # ---- Carros ----
        st.markdown(f"**🚗 {ocup_carro:02d}/{cap_carro} carros**")
        c1b, c2b, _ = st.columns([1, 1, 3])
        c1b.button(
            "➕ Entrada", key=f"car_in_{local_id}",
            on_click=registrar, args=(TIPO_CARRO, local_id, "entrada", cap_carro, ocup_carro),
        )
        c2b.button(
            "➖ Saída", key=f"car_out_{local_id}",
            on_click=registrar, args=(TIPO_CARRO, local_id, "saida", cap_carro, ocup_carro),
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
                            "horário": h["horario"],
                            "veículo": "Carro" if h["tipo_veiculo_id"] == TIPO_CARRO else "Moto",
                            "movimento": h["movimentacao"],
                        }
                        for h in hist
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
