"""
Vaguinha - gestão de entrada/saída de veículos em estacionamento.

Rodar:
    pip install -r requirements.txt
    streamlit run app.py

Persistência: SQLite em memória por padrão (protótipo). Para usar PostgreSQL,
veja o bloco `get_repo()` abaixo — basta trocar a implementação do Repository.
"""

import streamlit as st

from config import AREAS, TIPO_CARRO, TIPO_MOTO, cor_da_area
from repository import SQLiteRepository  # , PostgresRepository

st.set_page_config(page_title="Vaguinha", page_icon="🅿️", layout="centered")


# ------------------------------------------------------------------
# Repositório (único, mantido entre reruns do Streamlit)
# ------------------------------------------------------------------
@st.cache_resource
def get_repo():
    # --- Protótipo (SQLite em memória) ---
    return SQLiteRepository(":memory:")
    # --- Produção (PostgreSQL) — troque pelo seu DSN e comente a linha acima ---
    # return PostgresRepository("postgresql://user:senha@host:5432/vaguinha")


repo = get_repo()


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
st.markdown("<h1 style='text-align:center'>Vaguinha</h1>", unsafe_allow_html=True)
st.caption("Corrida da FAB — controle de vagas por área")

saldos = repo.saldos()  # {(local_id, tipo_veiculo_id): ocupados}

# Resumo geral
tot_carros = sum(v for (lid, t), v in saldos.items() if t == TIPO_CARRO)
tot_motos = sum(v for (lid, t), v in saldos.items() if t == TIPO_MOTO)
cap_total = sum(a["cap_carro"] + a["cap_moto"] for a in AREAS.values())
ocup_total = tot_carros + tot_motos

c1, c2, c3 = st.columns(3)
c1.metric("Carros", tot_carros)
c2.metric("Motos", tot_motos)
c3.metric("Vagas livres", cap_total - ocup_total)

st.divider()

# ------------------------------------------------------------------
# Lista de áreas (local_id 1..13) como cards expansíveis
# ------------------------------------------------------------------
for local_id, area in AREAS.items():
    cap_carro, cap_moto = area["cap_carro"], area["cap_moto"]
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
