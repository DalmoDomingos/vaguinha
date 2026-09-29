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

import hashlib
import html
import importlib
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st


def _recarregar_modulos_alterados() -> str:
    """
    Depois de uma atualização (git pull / deploy no Streamlit Cloud) o Streamlit
    roda o app.py novo, mas pode continuar com config.py/repository.py ANTIGOS
    na memória do Python — e o app quebra misturando as duas versões. Aqui
    comparamos o conteúdo dos arquivos com o que foi carregado e recarregamos
    o que mudou. Retorna uma "impressão digital" do código do banco.
    """
    pasta = Path(__file__).parent
    versao_banco = ""
    for nome, arquivos in (("config", ["config.py"]), ("repository", ["repository.py", "schema.sql"])):
        h = hashlib.sha1()
        for arq in arquivos:
            h.update((pasta / arq).read_bytes())
        digital = h.hexdigest()
        modulo = sys.modules.get(nome)
        if modulo is not None and getattr(modulo, "_VG_VERSAO", None) != digital:
            modulo = importlib.reload(modulo)
        if modulo is None:
            modulo = importlib.import_module(nome)
        modulo._VG_VERSAO = digital
        if nome == "repository":
            versao_banco = digital
    return versao_banco


VERSAO_BANCO = _recarregar_modulos_alterados()

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

      /* Trava dos botões de entrada/saída (ver TRAVA_JS): enquanto um registro
         é processado, os botões não aceitam toque e o tocado mostra um círculo. */
      body.vg-travado [class*="st-key-mov_"] button {
          pointer-events: none;
          opacity: 0.6;
      }
      [class*="st-key-mov_"] button.vg-carregando {
          position: relative;
          opacity: 1;
      }
      [class*="st-key-mov_"] button.vg-carregando > * { visibility: hidden; }
      [class*="st-key-mov_"] button.vg-carregando::after {
          content: "";
          position: absolute;
          top: 50%; left: 50%;
          width: 1.1rem; height: 1.1rem;
          margin: -0.55rem 0 0 -0.55rem;
          border: 2.5px solid currentColor;
          border-top-color: transparent;
          border-radius: 50%;
          animation: vg-gira 0.6s linear infinite;
      }
      @keyframes vg-gira { to { transform: rotate(360deg); } }

      /* Celular: o Streamlit empilha colunas em telas estreitas. O resumo e os
         botões de entrada/saída ficam lado a lado, para o operador achar os
         botões sem rolar e acertar o dedo com facilidade. */
      @media (max-width: 640px) {
          .st-key-resumo [data-testid="stHorizontalBlock"],
          [class*="st-key-mov_"] [data-testid="stHorizontalBlock"] {
              flex-wrap: nowrap !important;
              gap: 0.5rem !important;
          }
          .st-key-resumo [data-testid="stColumn"],
          [class*="st-key-mov_"] [data-testid="stColumn"] {
              min-width: 0 !important;
              width: auto !important;
              flex: 1 1 0 !important;
          }
          .st-key-resumo [data-testid="stMetricValue"] { font-size: 1.5rem; }
          .st-key-resumo [data-testid="stMetricLabel"] p { font-size: 0.85rem; }
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
@st.cache_resource(max_entries=1)
def get_repo(versao: str):
    """
    Escolhe a persistência automaticamente:
      1. DATABASE_URL (secrets ou variável de ambiente) → PostgreSQL
      2. fallback: SQLite em memória                    (protótipo; zera ao reiniciar)

    `versao` muda quando repository.py/schema.sql mudam: assim, depois de uma
    atualização (git pull / deploy no Streamlit Cloud), o app cria um
    repositório novo em vez de continuar usando o objeto antigo do cache.
    """
    dsn = ler_config("DATABASE_URL")
    if dsn:
        # DB_MAX_CONEXOES (opcional): conexões simultâneas com o banco (padrão 15)
        return PostgresRepository(dsn, max_conexoes=int(ler_config("DB_MAX_CONEXOES") or 15))
    return SQLiteRepository(":memory:")


repo = get_repo(VERSAO_BANCO)
usando_pg = isinstance(repo, PostgresRepository)
BANCO = "PostgreSQL ✅" if usando_pg else "SQLite (memória) ⚠️"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def pct(ocupados: int, capacidade: int) -> float:
    if capacidade <= 0:
        return 0.0
    return max(0.0, min(1.0, ocupados / capacidade))


# Brasília (UTC-3; sem horário de verão desde 2019)
FUSO_BR = timezone(timedelta(hours=-3))


def fmt_data(valor, segundos: bool = False) -> str:
    """
    Data/hora do banco → dd/mm/aaaa hh:mm[:ss] em Brasília.
    Aceita datetime com fuso (coluna TIMESTAMPTZ), sem fuso (TIMESTAMP, já em
    Brasília) e texto (SQLite).
    """
    try:
        dt = valor if isinstance(valor, datetime) else datetime.fromisoformat(str(valor))
    except ValueError:
        return str(valor)
    if dt.tzinfo is not None:
        dt = dt.astimezone(FUSO_BR)
    return dt.strftime("%d/%m/%Y %H:%M:%S" if segundos else "%d/%m/%Y %H:%M")


def num(valor) -> int:
    """Número digitado (vazio/NaN conta como 0)."""
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


# Pop-up de confirmação: some rápido para não atrapalhar o próximo registro.
AVISO_OK_SEGUNDOS = 2
# Toque no MESMO botão chegando logo depois do anterior ter sido processado é
# tratado como toque repetido (internet lenta: o operador tocou de novo achando
# que não tinha ido): esses chegam colados, a menos de 0,1 s. Um segundo veículo
# de verdade leva mais (a trava na tela só solta depois da resposta).
TOQUE_REPETIDO_SEGUNDOS = 0.3


def registrar(tipo_id: int, local_id: int, mov: str, nome_area: str):
    """
    Registra o evento. A validação de saldo usa a capacidade e a ocupação
    ATUAIS do banco (não os valores da última renderização, que podem estar
    defasados quando há mais de um operador).
    """
    # Os avisos são mostrados pelo próprio painel (exibir elementos dentro de
    # um callback de fragmento não é suportado pelo Streamlit) e só aparecem
    # para quem registrou: cada operador tem a sua sessão.
    chave = (tipo_id, local_id, mov)
    anterior = st.session_state.get("ultimo_registro")  # (chave, quando terminou)
    if anterior and anterior[0] == chave and time.monotonic() - anterior[1] < TOQUE_REPETIDO_SEGUNDOS:
        st.session_state["aviso_painel"] = (
            "Toque repetido ignorado. Se era outro veículo, toque de novo.", "⏳", "short")
        return
    try:
        r = repo.registrar(tipo_id, local_id, mov)
    except OperacaoInvalida as e:
        st.session_state["aviso_painel"] = (str(e), "⚠️", "short")
        return
    except Exception as e:
        st.session_state["aviso_painel"] = (f"Erro ao registrar movimento: {e}", "❌", "long")
        return
    finally:
        st.session_state["ultimo_registro"] = (chave, time.monotonic())

    veiculo = "🚗 Carro" if tipo_id == TIPO_CARRO else "🏍️ Moto"
    verbo = "entrou" if mov == "entrada" else "saiu"
    st.session_state["aviso_painel"] = (
        f"{veiculo} **{verbo}** — {nome_area} · {r['ocupados']}/{r['capacidade']}",
        "✅" if mov == "entrada" else "↩️",
        AVISO_OK_SEGUNDOS,
    )


def _linha(area: dict) -> dict:
    """Uma linha do editor de áreas (uid identifica os campos dela na tela)."""
    return {
        "uid": uuid.uuid4().hex[:8],
        "id": area.get("id"),
        "nome": area["nome"],
        "cor": rotulo_da_cor(area["cor"]),
        "cap_carro": int(area["cap_carro"]),
        "cap_moto": int(area["cap_moto"]),
    }


def _adicionar_linha(chave_linhas: str):
    linhas = st.session_state[chave_linhas]
    linhas.append(_linha({"nome": f"Área {len(linhas) + 1}", "cor": COR_PADRAO,
                          "cap_carro": 0, "cap_moto": 0}))


def _remover_linha(chave_linhas: str, uid: str):
    st.session_state[chave_linhas] = [l for l in st.session_state[chave_linhas] if l["uid"] != uid]


def editor_areas(areas: list, key: str) -> list:
    """
    Lista editável de áreas — nome, cor e vagas de carros/motos — com botões
    para adicionar (➕) e excluir (🗑️). Devolve a lista no formato do
    repositório (com o id das áreas que já existem).

    Usa campos comuns em vez de st.data_editor: a tabela do Streamlit (1.64)
    descarta o valor digitado quando a célula é aberta com duplo clique depois
    da primeira edição, e é difícil de usar no celular.
    """
    chave_linhas = f"{key}_linhas"
    if chave_linhas not in st.session_state:
        st.session_state[chave_linhas] = [_linha(a) for a in areas]
    linhas = st.session_state[chave_linhas]

    # cores fora da paleta (ex.: definidas direto no banco) continuam selecionáveis
    opcoes_cor = list(PALETA) + sorted({l["cor"] for l in linhas} - set(PALETA))

    for l in linhas:
        uid = l["uid"]
        with st.container(border=True):
            c_nome, c_cor, c_car, c_mot, c_del = st.columns(
                [3, 2.2, 1.5, 1.5, 0.8], vertical_alignment="bottom",
            )
            l["nome"] = c_nome.text_input(
                "Nome da área", value=l["nome"], max_chars=60, key=f"{key}_{uid}_nome",
                placeholder="Ex.: Portão Norte",
            )
            l["cor"] = c_cor.selectbox(
                "Cor", opcoes_cor, index=opcoes_cor.index(l["cor"]), key=f"{key}_{uid}_cor",
            )
            l["cap_carro"] = c_car.number_input(
                "🚗 Carros", min_value=0, step=1, value=l["cap_carro"], key=f"{key}_{uid}_carros",
            )
            l["cap_moto"] = c_mot.number_input(
                "🏍️ Motos", min_value=0, step=1, value=l["cap_moto"], key=f"{key}_{uid}_motos",
            )
            c_del.button(
                "🗑️", key=f"{key}_{uid}_excluir", help="Excluir esta área", width="stretch",
                on_click=_remover_linha, args=(chave_linhas, uid),
            )

    st.button("➕ Adicionar área", key=f"{key}_adicionar",
              on_click=_adicionar_linha, args=(chave_linhas,))

    return [
        {
            "id": l["id"],
            "nome": l["nome"],
            "cor": PALETA.get(l["cor"], l["cor"]) or COR_PADRAO,
            "cap_carro": l["cap_carro"],
            "cap_moto": l["cap_moto"],
        }
        for l in linhas
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
        st.session_state.pop("novo_areas_linhas", None)  # descarta o rascunho
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

        secao_zerar_evento(evento, versao)


def secao_zerar_evento(evento: dict, versao: int):
    """Zera entradas/saídas de todas as áreas (mantém áreas e vagas)."""
    evento_id = evento["id"]
    st.divider()
    st.markdown("**🗑️ Zerar entradas e saídas**")
    st.caption(
        "Volta a ocupação de **todas as áreas** para zero. As áreas e o número de "
        "vagas continuam iguais, e os registros antigos ficam arquivados (não são "
        "apagados). Use antes de começar o evento, para limpar os testes."
    )
    confirmacao = st.text_input(
        f"Para confirmar, digite o nome do evento: **{evento['nome']}**",
        key=f"zerar_{evento_id}_{versao}",
    )
    confirmado = confirmacao.strip().casefold() == evento["nome"].strip().casefold()
    if st.button(
        "Zerar ocupação do evento", key=f"btn_zerar_{evento_id}",
        disabled=not confirmado, icon="⚠️",
    ):
        arquivados = repo.zerar_evento(evento_id)
        st.session_state[f"versao_{evento_id}"] = versao + 1  # limpa a confirmação
        avisar(f"Evento zerado — {arquivados} registro(s) arquivado(s).", "🗑️")
        st.rerun()


# Trava dos botões no navegador: ao tocar em Entrada/Saída, todos os botões de
# movimento ficam bloqueados (e o tocado mostra um círculo girando) até o
# Streamlit terminar de processar o registro — assim um segundo toque, com a
# internet lenta, não vira um segundo veículo. Destrava sozinho após 12 s.
TRAVA_JS = """
<script>
(() => {
  if (window.__vgTrava) return;
  window.__vgTrava = true;
  const BOTOES = '[class*="st-key-mov_"] button';
  const MINIMO_MS = 250, MAXIMO_MS = 12000;
  let travadoEm = 0, viuRodando = false, observador = null;

  const estado = () => {
    const app = document.querySelector('[data-testid="stApp"]');
    return app && app.getAttribute("data-test-script-state");
  };
  const destravar = () => {
    travadoEm = 0; viuRodando = false;
    document.body.classList.remove("vg-travado");
    document.querySelectorAll(".vg-carregando").forEach(b => b.classList.remove("vg-carregando"));
  };
  // Marca se o Streamlit rodou depois do toque (pega execuções curtíssimas).
  const observar = () => {
    const app = document.querySelector('[data-testid="stApp"]');
    if (!app || observador) return;
    observador = new MutationObserver(() => {
      if (travadoEm && estado() === "running") viuRodando = true;
    });
    observador.observe(app, {attributes: true, attributeFilter: ["data-test-script-state"]});
  };
  // Destrava quando o Streamlit parar de rodar depois do toque. Se o toque
  // aconteceu durante uma atualização automática (já "running"), essa execução
  // é emendada com a do registro e conta também.
  setInterval(() => {
    if (!travadoEm) return;
    const passou = Date.now() - travadoEm;
    if (estado() === "running") { viuRodando = true; }
    else if (viuRodando && passou >= MINIMO_MS) { destravar(); }
    if (travadoEm && passou >= MAXIMO_MS) destravar();
  }, 100);

  document.addEventListener("click", (ev) => {
    const botao = ev.target.closest && ev.target.closest(BOTOES);
    if (!botao) return;
    if (travadoEm) { ev.preventDefault(); ev.stopImmediatePropagation(); return; }
    observar();
    travadoEm = Date.now();
    viuRodando = estado() === "running";
    document.body.classList.add("vg-travado");
    botao.classList.add("vg-carregando");
  }, true);
})();
</script>
"""


# Com vários operadores ao mesmo tempo, os cards se atualizam sozinhos para
# mostrar o que os outros registraram (só os cards, com 1 consulta ao banco).
# ATUALIZAR_A_CADA (segundos, opcional nos secrets): aumente (ex.: 10) se houver
# muitos operadores e o servidor ficar lento.
ATUALIZAR_A_CADA = f"{int(ler_config('ATUALIZAR_A_CADA') or 5)}s"


def tela_evento(evento_id: int):
    evento = repo.obter_evento(evento_id)
    st.button("← Eventos", on_click=ir_para)
    if evento is None:
        st.warning("Evento não encontrado.")
        return

    st.markdown(f"<h1 style='text-align:center'>{html.escape(evento['nome'])}</h1>", unsafe_allow_html=True)
    st.caption(f"Controle de vagas por área · Banco: {BANCO}")

    st.html(TRAVA_JS, unsafe_allow_javascript=True)
    painel_areas(evento_id)
    # no fim: operadores quase não usam, e o "zerar" fica longe de toques acidentais
    st.divider()
    secao_editar_evento(evento, repo.listar_areas(evento_id))


@st.fragment(run_every=ATUALIZAR_A_CADA)
def painel_areas(evento_id: int):
    """
    Resumo + cards das áreas. É um fragmento: os cliques de entrada/saída e a
    atualização automática refazem só este trecho (1 consulta), não a página.
    """
    if "aviso_painel" in st.session_state:
        msg, icon, duracao = st.session_state.pop("aviso_painel")
        # O Streamlit identifica cada pop-up pela posição em que foi criado: no
        # mesmo lugar, o 2º pop-up (dois veículos seguidos) era tratado como o 1º,
        # ainda visível, e não aparecia. Marcadores vazios antes dele mudam a
        # posição a cada aviso (até 5 avisos seguidos na tela, cada um no seu lugar).
        n = st.session_state["avisos_mostrados"] = st.session_state.get("avisos_mostrados", 0) + 1
        for _ in range(n % 5):
            st.empty()
        st.toast(msg, icon=icon, duration=duracao)

    areas = repo.painel_evento(evento_id)  # áreas + ocupação, numa consulta só

    # ---- Resumo geral ----
    tot_carros = sum(a["ocup_carro"] for a in areas)
    tot_motos = sum(a["ocup_moto"] for a in areas)
    cap_total = sum(a["cap_carro"] + a["cap_moto"] for a in areas)
    ocup_total = tot_carros + tot_motos

    with st.container(key="resumo"):
        c1, c2, c3 = st.columns(3)
        c1.metric("Carros", tot_carros)
        c2.metric("Motos", tot_motos)
        c3.metric("Vagas livres", cap_total - ocup_total)

    st.caption(
        f"Capacidade total configurada: **{cap_total}** vagas · "
        f"atualizado às {datetime.now(FUSO_BR):%H:%M:%S}"
    )

    # Ocupação geral do evento (todas as áreas somadas)
    ocup_geral = pct(ocup_total, cap_total)
    st.progress(ocup_geral, text=f"Ocupação geral — {ocup_geral*100:.1f}% ({ocup_total}/{cap_total})")

    st.divider()

    # ---- Áreas como cards expansíveis ----
    for i, area in enumerate(areas):
        local_id = area["id"]
        cap_carro, cap_moto = area["cap_carro"], area["cap_moto"]
        ocup_carro, ocup_moto = area["ocup_carro"], area["ocup_moto"]

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
            with st.container(key=f"mov_moto_{local_id}"):
                m1, m2 = st.columns(2)
                m1.button(
                    "➕ Entrada", key=f"mot_in_{local_id}", width="stretch",
                    on_click=registrar, args=(TIPO_MOTO, local_id, "entrada", area["nome"]),
                )
                m2.button(
                    "➖ Saída", key=f"mot_out_{local_id}", width="stretch",
                    on_click=registrar, args=(TIPO_MOTO, local_id, "saida", area["nome"]),
                )

            # ---- Carros ----
            st.markdown(f"**🚗 {ocup_carro:02d}/{cap_carro} carros**")
            with st.container(key=f"mov_carro_{local_id}"):
                c1b, c2b = st.columns(2)
                c1b.button(
                    "➕ Entrada", key=f"car_in_{local_id}", width="stretch",
                    on_click=registrar, args=(TIPO_CARRO, local_id, "entrada", area["nome"]),
                )
                c2b.button(
                    "➖ Saída", key=f"car_out_{local_id}", width="stretch",
                    on_click=registrar, args=(TIPO_CARRO, local_id, "saida", area["nome"]),
                )

            st.write(f"**Vagas sobrando: {sobrando}**")

            # ---- Barras de ocupação ----
            st.progress(pct(ocup_carro, cap_carro), text=f"% carros — {pct(ocup_carro, cap_carro)*100:.0f}%")
            st.progress(pct(ocup_moto, cap_moto), text=f"% motos — {pct(ocup_moto, cap_moto)*100:.0f}%")
            st.progress(pct(ocup_area, cap_area), text=f"% total — {pct(ocup_area, cap_area)*100:.0f}%")

            # ---- Histórico recente da área (só consulta o banco se ligado) ----
            if st.toggle("Ver histórico", key=f"hist_{local_id}"):
                hist = repo.historico(local_id=local_id, limite=15)
                if not hist:
                    st.write("Sem movimentos ainda.")
                else:
                    st.dataframe(
                        [
                            {
                                "horário": fmt_data(h["horario"], segundos=True),
                                "veículo": "Carro" if h["tipo_veiculo_id"] == TIPO_CARRO else "Moto",
                                "movimento": "Entrada" if h["movimentacao"] == "entrada" else "Saída",
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
