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
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

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
            # inclui QUAL cópia do módulo está carregada: o Streamlit também o
            # recarrega quando só o config.py muda, e o repositório do cache
            # (feito com a cópia antiga) levantaria erros que o app não
            # reconhece (ex.: "Não há veículos..." virava "Erro ao registrar")
            versao_banco = f"{digital}-{id(modulo)}"
    return versao_banco


VERSAO_BANCO = _recarregar_modulos_alterados()

from config import COR_PADRAO, PALETA, TIPO_CARRO, TIPO_MOTO, rotulo_da_cor
from repository import (ACESSO_HORAS, SENHA_MAX, SENHA_MIN, OperacaoInvalida,
                        PostgresRepository, SQLiteRepository)

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

      /* Esconde a dica em inglês dentro dos campos ("Press Enter to apply" /
         "Press Enter to submit form" e o contador de letras) */
      [data-testid="InputInstructions"] { display: none; }

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
        repo = PostgresRepository(dsn, max_conexoes=int(ler_config("DB_MAX_CONEXOES") or 15))
    else:
        repo = SQLiteRepository(":memory:")
    # Eventos criados antes de existir senha (ex.: Corrida da FAB) recebem a
    # SENHA_EVENTO_INICIAL dos secrets — nunca no código: o repositório é público.
    senha_inicial = ler_config("SENHA_EVENTO_INICIAL")
    if senha_inicial:
        try:
            repo.aplicar_senha_inicial(str(senha_inicial))
        except OperacaoInvalida:
            pass  # senha curta demais nos secrets: os eventos antigos seguem fechados
    return repo


repo = get_repo(VERSAO_BANCO)


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


def texto_puro(texto: str) -> str:
    """
    Nome digitado pelo usuário para exibir em texto com formatação (markdown):
    símbolos como * _ [ ] # viram texto normal em vez de negrito, link, título...
    """
    texto = re.sub(r"([\\`*_\[\]<>#|~$])", r"\\\1", str(texto))
    # "1. Área" / "- Área" no começo virariam lista
    return re.sub(r"^(\d+)([.)])|^([-+])", lambda m: (f"{m[1]}\\{m[2]}" if m[1] else f"\\{m[3]}"), texto)


# ------------------------------------------------------------------
# Senha dos eventos
# ------------------------------------------------------------------
class Tentativas:
    """
    Senhas erradas por (evento, aparelho): depois de LIVRES erros seguidos, o
    login daquele aparelho naquele evento fica bloqueado por um tempo que dobra
    a cada novo erro (até BLOQUEIO_MAX). Acertar zera a contagem.
    """
    LIVRES, BLOQUEIO_INICIAL, BLOQUEIO_MAX = 5, 30, 300

    def __init__(self):
        self._lock = threading.Lock()
        self._erros = {}  # chave -> (erros seguidos, bloqueado até, último erro)

    def espera(self, chave) -> int:
        """Segundos que ainda faltam de bloqueio (0 = pode tentar)."""
        with self._lock:
            _, ate, _ = self._erros.get(chave, (0, 0.0, 0.0))
        return max(0, int(ate - time.monotonic() + 0.999))

    def errou(self, chave) -> None:
        agora = time.monotonic()
        with self._lock:
            if len(self._erros) > 10_000:  # limpa aparelhos parados há mais de 1 h
                self._erros = {k: v for k, v in self._erros.items() if agora - v[2] < 3600}
            n = self._erros.get(chave, (0, 0.0, 0.0))[0] + 1
            ate = 0.0
            if n >= self.LIVRES:
                ate = agora + min(self.BLOQUEIO_MAX, self.BLOQUEIO_INICIAL * 2 ** (n - self.LIVRES))
            self._erros[chave] = (n, ate, agora)

    def acertou(self, chave) -> None:
        with self._lock:
            self._erros.pop(chave, None)


@st.cache_resource
def tentativas() -> Tentativas:
    return Tentativas()  # uma só para o servidor: vale para todas as sessões


def _liberados() -> dict:
    """Eventos que esta sessão abriu com a senha: {evento_id: marca da senha}."""
    return st.session_state.setdefault("eventos_liberados", {})


def evento_liberado(evento: dict) -> bool:
    """
    Esta sessão digitou a senha ATUAL do evento? Se a senha foi trocada
    depois, a marca não bate mais e a senha nova é pedida.
    """
    return bool(evento["tem_senha"]) and _liberados().get(evento["id"]) == evento["senha_marca"]


# ---- aparelho lembrado: cookie com um código aleatório (ver repo.criar_acesso) ----
def _nome_cookie(evento_id: int) -> str:
    return f"vg_acesso_{int(evento_id)}"


def _cookie(nome: str) -> Optional[str]:
    """Cookie que o aparelho mandou ao abrir a página (None se não tem)."""
    try:
        valor = st.context.cookies.get(nome)
    except Exception:
        return None
    return valor if isinstance(valor, str) else None


def _gravar_cookie(nome: str, valor: str, segundos: int):
    """O cookie é gravado no aparelho pelo script de cookies_js() desta sessão."""
    st.session_state.setdefault("cookies_pendentes", {})[nome] = (valor, int(segundos))


def cookies_js():
    """Grava no aparelho os cookies pendentes desta sessão (via script na página)."""
    pendentes = st.session_state.get("cookies_pendentes")
    if not pendentes:
        return
    comandos = "".join(
        f'document.cookie = "{nome}={valor}; Max-Age={segundos}; Path=/; SameSite=Lax"'
        f' + (location.protocol === "https:" ? "; Secure" : "");'
        for nome, (valor, segundos) in pendentes.items()
        if re.fullmatch(r"[A-Za-z0-9_\-]*", nome + valor)  # só caracteres seguros
    )
    st.html(f"<script>{comandos}</script>", unsafe_allow_javascript=True)


def liberar_aparelho(evento_id: int, marca: str):
    """Libera o evento nesta sessão e lembra o aparelho por ACESSO_HORAS."""
    _liberados()[evento_id] = marca
    codigo = repo.criar_acesso(evento_id, marca)
    st.session_state.setdefault("codigos_acesso", {})[evento_id] = codigo
    _gravar_cookie(_nome_cookie(evento_id), codigo, ACESSO_HORAS * 3600)


def lembrar_aparelho(evento: dict) -> bool:
    """O aparelho tem um código válido para o evento (de antes de recarregar/bloquear)?"""
    marca = repo.conferir_acesso(evento["id"], _cookie(_nome_cookie(evento["id"])))
    if marca and marca == evento["senha_marca"]:
        _liberados()[evento["id"]] = marca
        return True
    return False


def sair_do_evento(evento_id: int):
    """Fecha o evento neste aparelho e esquece o código dele."""
    _liberados().pop(evento_id, None)
    codigo = st.session_state.get("codigos_acesso", {}).pop(evento_id, None)
    repo.apagar_acesso(codigo or _cookie(_nome_cookie(evento_id)))
    _gravar_cookie(_nome_cookie(evento_id), "", 0)
    ir_para()


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
# Toque no MESMO botão chegando logo depois do anterior é tratado como toque
# repetido (internet lenta: o operador tocou de novo achando que não tinha ido).
# O prazo conta a partir do ÚLTIMO toque, inclusive dos ignorados: uma rajada
# de toques vira 1 registro. Um segundo veículo de verdade vem depois de uma
# pausa maior que isso (a trava na tela só solta depois da resposta).
TOQUE_REPETIDO_SEGUNDOS = 0.3
# Duração dos pop-ups de problema (o de sucesso usa AVISO_OK_SEGUNDOS)
AVISO_CURTO_SEGUNDOS = 4
AVISO_LONGO_SEGUNDOS = 10


def registrar(evento_id: int, tipo_id: int, local_id: int, mov: str, nome_area: str):
    """
    Registra o evento. A validação de saldo usa a capacidade e a ocupação
    ATUAIS do banco (não os valores da última renderização, que podem estar
    defasados quando há mais de um operador).
    """
    # Os avisos são mostrados pelo próprio painel (exibir elementos dentro de
    # um callback de fragmento não é suportado pelo Streamlit) e só aparecem
    # para quem registrou: cada operador tem a sua sessão.
    # senha trocada enquanto o evento estava aberto: não grava (o painel pede a nova)
    evento = repo.obter_evento(evento_id)
    if evento is None:
        st.session_state["aviso_painel"] = (
            "Este evento foi excluído. Nada foi registrado.", "⚠️", AVISO_CURTO_SEGUNDOS)
        return
    if not evento_liberado(evento):
        st.session_state["aviso_painel"] = (
            "A senha do evento mudou. Digite a senha nova para continuar.", "🔒", AVISO_CURTO_SEGUNDOS)
        return
    chave = (tipo_id, local_id, mov)
    anterior = st.session_state.get("ultimo_registro")  # (chave, quando foi o último toque)
    if anterior and anterior[0] == chave and time.monotonic() - anterior[1] < TOQUE_REPETIDO_SEGUNDOS:
        st.session_state["ultimo_registro"] = (chave, time.monotonic())
        st.session_state["aviso_painel"] = (
            "Toque repetido ignorado. Se era outro veículo, toque de novo.", "⏳", AVISO_CURTO_SEGUNDOS)
        return
    # marca já, antes de ir ao banco: toques repetidos podem rodar ao mesmo
    # tempo que este (internet lenta) e precisam enxergar a marca
    st.session_state["ultimo_registro"] = (chave, time.monotonic())
    try:
        r = repo.registrar(tipo_id, local_id, mov)
    except OperacaoInvalida as e:
        st.session_state["aviso_painel"] = (str(e), "⚠️", AVISO_CURTO_SEGUNDOS)
        return
    except Exception as e:
        st.session_state["aviso_painel"] = (f"Erro ao registrar movimento: {e}", "❌", AVISO_LONGO_SEGUNDOS)
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
    st.caption("Controle de vagas por evento")

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
            info.markdown(f"#### 🔒 {texto_puro(e['nome'])}")
            # ocupação e vagas só aparecem dentro do evento, para quem tem a senha
            info.caption(f"{e['n_areas']} área(s) · criado em {fmt_data(e['criado_em'])}")
            acao.button(
                "Abrir", key=f"abrir_{e['id']}", type="primary", width="stretch",
                on_click=ir_para, kwargs={"evento": e["id"]},
            )


# ------------------------------------------------------------------
# Tela de criação de evento
# ------------------------------------------------------------------
def _abrir_evento_criado(pedido: dict):
    """Abre o evento que esta sessão acabou de criar (quem criou já entra)."""
    evento_id = pedido["id"]
    if evento_id not in _liberados():
        liberar_aparelho(evento_id, repo.obter_evento(evento_id)["senha_marca"])
    if not pedido.get("avisado"):
        pedido["avisado"] = True
        avisar(f"Evento “{texto_puro(pedido['nome'])}” criado!")
    ir_para(evento=evento_id)
    st.rerun()


def tela_novo_evento():
    # Toque repetido em "Criar evento" que chega depois do evento criado traz
    # de volta esta tela: leva para o evento que acabou de ser criado.
    pedido = st.session_state.get("criando_evento")
    if pedido and pedido["id"] and time.monotonic() - pedido["quando"] < 10:
        _abrir_evento_criado(pedido)
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

    # Senha e botão num formulário: as duas senhas chegam juntas no toque do
    # botão. Fora dele, a senha digitada às vezes ainda não tinha chegado ao
    # servidor (ex.: Tab leva ao "olho" de mostrar a senha, dentro do campo).
    with st.form("novo_evento_senha", border=False):
        st.markdown("**Senha do evento**")
        st.caption(f"Quem for registrar entradas e saídas vai precisar dela para abrir o "
                   f"evento. Mínimo de {SENHA_MIN} caracteres.")
        c_senha, c_repete = st.columns(2)
        senha = c_senha.text_input("Senha", type="password", max_chars=SENHA_MAX,
                                   key="novo_senha", autocomplete="new-password")
        senha2 = c_repete.text_input("Repita a senha", type="password", max_chars=SENHA_MAX,
                                     key="novo_senha2", autocomplete="new-password")
        criar = st.form_submit_button("Criar evento", type="primary", width="stretch")

    if criar:
        # Toque repetido (internet lenta): cada toque roda ao mesmo tempo que o
        # primeiro, que ainda está criando o evento — e criaria uma cópia dele.
        # O pedido é marcado ANTES de criar; os repetidos esperam e só abrem o evento.
        pedido = st.session_state.get("criando_evento")  # {"nome", "id", "quando", "falhou"}
        if pedido and pedido["nome"] == nome.strip() and time.monotonic() - pedido["quando"] < 60:
            for _ in range(150):
                if pedido["id"] or pedido["falhou"]:
                    break
                time.sleep(0.1)
            if pedido["id"]:
                _abrir_evento_criado(pedido)
        if senha != senha2:
            st.error("As duas senhas não são iguais. Digite a mesma senha nos dois campos.")
            return
        pedido = {"nome": nome.strip(), "id": None, "quando": time.monotonic(), "falhou": False}
        st.session_state["criando_evento"] = pedido
        try:
            evento_id = repo.criar_evento(nome, areas, senha)
        except OperacaoInvalida as e:
            pedido["falhou"] = True
            st.error(str(e))
            return
        except Exception:
            pedido["falhou"] = True
            raise
        # anota JÁ (antes de qualquer comando do Streamlit): um toque repetido
        # pode interromper esta execução daqui em diante, e a próxima termina o serviço
        pedido["id"] = evento_id
        st.session_state.pop("novo_areas_linhas", None)  # descarta o rascunho
        for chave in ("novo_senha", "novo_senha2"):  # a senha não fica guardada na sessão
            st.session_state.pop(chave, None)
        _abrir_evento_criado(pedido)


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

        secao_trocar_senha(evento_id)
        secao_zerar_evento(evento, versao)
        secao_excluir_evento(evento)


def secao_trocar_senha(evento_id: int):
    st.divider()
    st.markdown("**🔑 Trocar a senha do evento**")
    st.caption("Quem estiver com o evento aberto em outro aparelho vai precisar "
               "digitar a senha nova.")
    with st.form(f"trocar_senha_{evento_id}", clear_on_submit=True, border=False):
        c_nova, c_repete = st.columns(2)
        nova = c_nova.text_input("Nova senha", type="password", max_chars=SENHA_MAX,
                                 autocomplete="new-password")
        nova2 = c_repete.text_input("Repita a nova senha", type="password", max_chars=SENHA_MAX,
                                    autocomplete="new-password")
        if not st.form_submit_button("Trocar senha"):
            return
    if nova != nova2:
        st.error("As duas senhas não são iguais. Digite a mesma senha nos dois campos.")
        return
    try:
        marca = repo.trocar_senha(evento_id, nova)
    except OperacaoInvalida as e:
        st.error(str(e))
        return
    liberar_aparelho(evento_id, marca)  # quem trocou continua dentro (e lembrado)
    avisar("Senha trocada. Os outros aparelhos vão pedir a senha nova.")
    st.rerun()


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
        f"Para confirmar, digite o nome do evento: **{texto_puro(evento['nome'])}**",
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


def secao_excluir_evento(evento: dict):
    """Exclui o evento e tudo dele. Pede o nome do evento e a senha de novo."""
    evento_id = evento["id"]
    st.divider()
    st.markdown("**❌ Excluir evento**")
    st.caption(
        "Apaga o evento **e tudo dele**: áreas, vagas e todos os registros de "
        "entrada e saída, inclusive os arquivados. **Não dá para desfazer.**"
    )
    # formulário: nome e senha chegam juntos no toque do botão
    with st.form(f"excluir_{evento_id}", clear_on_submit=True, border=False):
        nome = st.text_input(
            f"Digite o nome do evento: **{texto_puro(evento['nome'])}**", max_chars=80,
        )
        senha = st.text_input("Digite a senha do evento", type="password",
                              max_chars=SENHA_MAX, autocomplete="current-password")
        excluir = st.form_submit_button("Excluir evento para sempre", icon="❌")
    if not excluir:
        return
    if nome.strip().casefold() != evento["nome"].strip().casefold():
        st.error("O nome digitado não é o nome do evento. Nada foi excluído.")
        return
    chave = (evento_id, _aparelho())
    espera = tentativas().espera(chave)
    if espera:
        st.error(f"Muitas tentativas com a senha errada. Tente de novo em {espera} s.")
        return
    if not repo.conferir_senha(evento_id, senha):
        tentativas().errou(chave)
        st.error("Senha incorreta. Nada foi excluído.")
        return
    tentativas().acertou(chave)
    repo.excluir_evento(evento_id)
    _liberados().pop(evento_id, None)
    avisar(f"Evento “{texto_puro(evento['nome'])}” excluído.", "❌")
    ir_para()
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
  const FAIXAS = '[class*="st-key-mov_"]', BOTOES = FAIXAS + ' button';
  const MINIMO_MS = 250, MAXIMO_MS = 12000;
  let travadoEm = 0, ultimoToque = 0, viuRodando = false, observador = null;

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
  // é emendada com a do registro e conta também. Toques bloqueados renovam o
  // prazo mínimo: numa rajada de toques, só destrava depois que o dedo para.
  setInterval(() => {
    if (!travadoEm) return;
    const passou = Date.now() - travadoEm;
    if (estado() === "running") { viuRodando = true; }
    else if (viuRodando && passou >= MINIMO_MS && Date.now() - ultimoToque >= MINIMO_MS) { destravar(); }
    if (travadoEm && passou >= MAXIMO_MS) destravar();
  }, 100);

  document.addEventListener("click", (ev) => {
    if (!ev.target.closest) return;
    // Travado, o botão não recebe o toque (pointer-events: none): o toque cai
    // na faixa de botões atrás dele — e também conta como toque repetido.
    if (travadoEm) {
      if (ev.target.closest(FAIXAS)) { ultimoToque = Date.now(); ev.preventDefault(); ev.stopImmediatePropagation(); }
      return;
    }
    const botao = ev.target.closest(BOTOES);
    if (!botao) return;
    observar();
    travadoEm = ultimoToque = Date.now();
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


def _aparelho() -> str:
    """Identifica o aparelho (IP) para contar as senhas erradas."""
    try:
        ip = st.context.ip_address
    except Exception:
        ip = None
    return ip if isinstance(ip, str) and ip else "?"  # sem IP: todos contam juntos


def tela_senha(evento: dict):
    """Pede a senha do evento (e controla as tentativas erradas)."""
    evento_id = evento["id"]
    st.caption("🔒 Este evento é protegido por senha.")
    if not evento["tem_senha"]:
        st.warning("Este evento ainda não tem senha, por isso não pode ser aberto. "
                   "Avise o responsável pelo sistema.")
        return
    chave = (evento_id, _aparelho())
    with st.form(f"entrar_{evento_id}", clear_on_submit=True):
        senha = st.text_input("Senha do evento", type="password", max_chars=SENHA_MAX,
                              autocomplete="current-password")
        entrar = st.form_submit_button("Entrar", type="primary", width="stretch")
    if not entrar:
        return
    espera = tentativas().espera(chave)
    if espera:
        st.error(f"Muitas tentativas com a senha errada. Tente de novo em {espera} s.")
        return
    marca = repo.conferir_senha(evento_id, senha)
    if not marca:
        tentativas().errou(chave)
        st.error("Senha incorreta.")
        return
    tentativas().acertou(chave)
    liberar_aparelho(evento_id, marca)
    st.rerun()


def tela_evento(evento_id: int):
    evento = repo.obter_evento(evento_id)
    st.button("← Eventos", on_click=ir_para)
    if evento is None:
        st.warning("Evento não encontrado.")
        return

    st.markdown(f"<h1 style='text-align:center'>{html.escape(evento['nome'])}</h1>", unsafe_allow_html=True)
    if not evento_liberado(evento) and not lembrar_aparelho(evento):
        tela_senha(evento)
        return
    st.caption("Controle de vagas por área")

    st.html(TRAVA_JS, unsafe_allow_javascript=True)
    st.html(f"<style>{CSS_CARDS}{CSS_AVISO}</style>")
    painel_areas(evento_id)
    # no fim: operadores quase não usam, e o "zerar" fica longe de toques acidentais
    st.divider()
    secao_editar_evento(evento, repo.listar_areas(evento_id))
    st.button("🔒 Sair deste evento", key=f"sair_{evento_id}",
              on_click=sair_do_evento, args=(evento_id,),
              help="Fecha o evento neste aparelho. Para abrir de novo, digite a senha.")


# ---- Cor de cada área no card todo ----
# Cada card recebe a sua cor em --cor (ver painel_areas): cabeçalho na cor,
# conteúdo num tom claro dela, barras na cor. Entrada = botão cheio na cor;
# Saída = só contorno (formas diferentes evitam o toque errado com pressa).
_CARD = '[class*="st-key-area_"]'
CSS_CARDS = f"""
  {_CARD} details {{ border: 2px solid var(--cor) !important; overflow: hidden;
                     background: color-mix(in srgb, var(--cor) 13%, transparent); }}
  {_CARD} summary, {_CARD} summary:hover, {_CARD} summary:focus, {_CARD} summary:focus-visible {{
      background: var(--cor) !important; border-radius: 0 !important; }}
  {_CARD} summary, {_CARD} summary:hover, {_CARD} summary p {{ color: var(--texto-cor, #fff) !important; }}
  {_CARD} summary svg, {_CARD} summary [data-testid="stIconMaterial"] {{ color: var(--texto-cor, #fff) !important; }}
  {_CARD} [data-testid="stProgressBarTrack"] > div {{ background: var(--cor) !important; }}
  {_CARD} [data-testid="stBaseButton-primary"] {{
      background: var(--cor) !important; border: 2px solid var(--cor) !important; color: var(--texto-cor, #fff) !important; }}
  {_CARD} [data-testid="stBaseButton-secondary"] {{
      background: transparent !important; border: 2px solid var(--cor) !important; color: var(--cor) !important; }}
  {_CARD} button p, {_CARD} button [data-testid="stIconMaterial"] {{ color: inherit !important; }}
"""
# Área lotada: cabeçalho em faixa amarela com texto escuro
CSS_LOTADA = "--cor:#F2B705;--texto-cor:#2F3438;"


# Pop-up do painel (confirmação de registro). Não usa st.toast: com um pop-up
# ainda na tela, o Streamlit não mostrava o seguinte — e com dois registros
# seguidos o operador ficava sem a confirmação do segundo. Este é substituído
# na hora por cada aviso novo e fica por cima da tela (não empurra os botões).
CSS_AVISO = """
  /* fora do fluxo da página (inclusive a caixa que o Streamlit põe em volta),
     para o pop-up não empurrar os botões quando aparece */
  .st-key-aviso, [data-testid="stLayoutWrapper"]:has(> .st-key-aviso) {
      position: absolute !important; height: 0 !important; margin: 0 !important; overflow: visible; }
  .vg-aviso {
      position: fixed; z-index: 1000; left: 50%; top: 4.2rem;
      transform: translateX(-50%);
      max-width: min(30rem, calc(100vw - 2rem)); width: max-content;
      padding: 0.7rem 1.1rem; border-radius: 10px;
      background: #2F3438; color: #fff; font-size: 1.05rem; line-height: 1.35;
      box-shadow: 0 6px 24px rgba(0, 0, 0, 0.25);
      pointer-events: none;
      animation: vg-aviso var(--duracao) ease-in-out forwards;
  }
  @keyframes vg-aviso {
      0% { opacity: 0; transform: translate(-50%, -0.5rem); }
      6%, 88% { opacity: 1; transform: translate(-50%, 0); }
      100% { opacity: 0; visibility: hidden; transform: translate(-50%, 0); }
  }
  @media (prefers-reduced-motion: reduce) {
      @keyframes vg-aviso { 0%, 90% { opacity: 1; } 100% { opacity: 0; visibility: hidden; } }
  }
"""


def mostrar_aviso_painel():
    """Mostra o aviso pendente (e o mantém enquanto dura, nas atualizações do painel)."""
    agora = time.monotonic()
    if "aviso_painel" in st.session_state:
        msg, icon, segundos = st.session_state.pop("aviso_painel")
        n = st.session_state["avisos_mostrados"] = st.session_state.get("avisos_mostrados", 0) + 1
        st.session_state["aviso_ativo"] = (n, msg, icon, segundos, agora + segundos)
    with st.container(key="aviso"):
        ativo = st.session_state.get("aviso_ativo")
        if ativo and agora < ativo[4]:
            n, msg, icon, segundos, fim = ativo
            # atualização automática no meio: a animação continua de onde estava
            decorrido = max(0.0, segundos - (fim - agora))
            # texto vem do banco (nome da área): escapa e só então aplica o **negrito**
            partes = html.escape(msg).split("**")
            texto = "".join(f"<b>{p}</b>" if i % 2 else p for i, p in enumerate(partes))
            st.html(f'<div class="vg-aviso" role="status" data-n="{n}" '
                    f'style="--duracao:{segundos}s;animation-delay:-{decorrido:.1f}s">'
                    f'{icon}&nbsp; {texto}</div>')


@st.fragment(run_every=ATUALIZAR_A_CADA)
def painel_areas(evento_id: int):
    """
    Resumo + cards das áreas. É um fragmento: os cliques de entrada/saída e a
    atualização automática refazem só este trecho (1 consulta), não a página.
    """
    # senha trocada em outro aparelho: volta para a tela de senha
    evento = repo.obter_evento(evento_id)
    if evento is None or not evento_liberado(evento):
        st.rerun(scope="app")

    mostrar_aviso_painel()

    areas = repo.painel_evento(evento_id)  # áreas + ocupação, numa consulta só

    # ---- Resumo geral ----
    tot_carros = sum(a["ocup_carro"] for a in areas)
    tot_motos = sum(a["ocup_moto"] for a in areas)
    # vagas livres por tipo (área com mais veículos que vagas não conta negativo)
    livres_carro = sum(max(0, a["cap_carro"] - a["ocup_carro"]) for a in areas)
    livres_moto = sum(max(0, a["cap_moto"] - a["ocup_moto"]) for a in areas)
    cap_total = sum(a["cap_carro"] + a["cap_moto"] for a in areas)
    ocup_total = tot_carros + tot_motos

    with st.container(key="resumo"):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🚗 Carros", tot_carros)
        c2.metric("🏍️ Motos", tot_motos)
        c3.metric("Livres 🚗", livres_carro)
        c4.metric("Livres 🏍️", livres_moto)

    st.caption(
        f"Capacidade total configurada: **{cap_total}** vagas · "
        f"atualizado às {datetime.now(FUSO_BR):%H:%M:%S}"
    )

    # Ocupação geral do evento (todas as áreas somadas)
    ocup_geral = pct(ocup_total, cap_total)
    st.progress(ocup_geral, text=f"Ocupação geral — {ocup_geral*100:.1f}% ({ocup_total}/{cap_total})")

    st.divider()

    # ---- Áreas como cards expansíveis ----
    # Cor de cada área no card todo: cada card recebe a sua cor em --cor e o
    # CSS_CARDS usa essa variável. Área lotada: cabeçalho amarelo.
    cores = "".join(
        f".st-key-area_{a['id']}{{--cor:{a['cor'] or COR_PADRAO}}}" for a in areas
    ) + "".join(
        f".st-key-area_{a['id']}{{{CSS_LOTADA}}}" for a in areas
        if a["cap_carro"] + a["cap_moto"] - a["ocup_carro"] - a["ocup_moto"] <= 0
    )
    st.html(f"<style>{cores}</style>")

    for i, area in enumerate(areas):
        local_id = area["id"]
        cap_carro, cap_moto = area["cap_carro"], area["cap_moto"]
        ocup_carro, ocup_moto = area["ocup_carro"], area["ocup_moto"]

        cap_area = cap_carro + cap_moto
        ocup_area = ocup_carro + ocup_moto
        sobrando = cap_area - ocup_area

        nome = texto_puro(area["nome"])
        titulo = f"{nome}      🚗 {ocup_carro}/{cap_carro}    🏍️ {ocup_moto}/{cap_moto}"
        if sobrando <= 0:
            titulo = f"⛔ {nome} lotada      🚗 {ocup_carro}/{cap_carro}    🏍️ {ocup_moto}/{cap_moto}"

        # key fixa: sem ela o card fecha a cada clique, pois o título (contagem) muda
        with st.expander(titulo, expanded=(i == 0), key=f"area_{local_id}"):
            # ---- Motos ----
            st.markdown(f"🏍️ Motos **{ocup_moto}**/{cap_moto}")
            with st.container(key=f"mov_moto_{local_id}"):
                m1, m2 = st.columns(2)
                m1.button(
                    "Entrada", key=f"mot_in_{local_id}", width="stretch",
                    type="primary", icon=":material/add:",
                    on_click=registrar, args=(evento_id, TIPO_MOTO, local_id, "entrada", area["nome"]),
                )
                m2.button(
                    "Saída", key=f"mot_out_{local_id}", width="stretch",
                    icon=":material/remove:",
                    on_click=registrar, args=(evento_id, TIPO_MOTO, local_id, "saida", area["nome"]),
                )

            # ---- Carros ----
            st.markdown(f"🚗 Carros **{ocup_carro}**/{cap_carro}")
            with st.container(key=f"mov_carro_{local_id}"):
                c1b, c2b = st.columns(2)
                c1b.button(
                    "Entrada", key=f"car_in_{local_id}", width="stretch",
                    type="primary", icon=":material/add:",
                    on_click=registrar, args=(evento_id, TIPO_CARRO, local_id, "entrada", area["nome"]),
                )
                c2b.button(
                    "Saída", key=f"car_out_{local_id}", width="stretch",
                    icon=":material/remove:",
                    on_click=registrar, args=(evento_id, TIPO_CARRO, local_id, "saida", area["nome"]),
                )

            st.markdown(
                f"Vagas livres: 🚗 **{max(0, cap_carro - ocup_carro)}** · "
                f"🏍️ **{max(0, cap_moto - ocup_moto)}**"
            )

            # ---- Barras de ocupação ----
            st.progress(pct(ocup_carro, cap_carro), text=f"Carros: {pct(ocup_carro, cap_carro)*100:.0f}% ocupado")
            st.progress(pct(ocup_moto, cap_moto), text=f"Motos: {pct(ocup_moto, cap_moto)*100:.0f}% ocupado")
            st.progress(pct(ocup_area, cap_area), text=f"Área: {pct(ocup_area, cap_area)*100:.0f}% ocupada")

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
cookies_js()

if "aviso" in st.session_state:
    msg, icon = st.session_state.pop("aviso")
    st.toast(msg, icon=icon)

params = st.query_params
if "evento" in params:
    evento_param = params["evento"]
    # só números comuns e de tamanho razoável (ex.: "²" ou um número gigante
    # vindos de um link quebrado derrubavam a página)
    if re.fullmatch(r"[0-9]{1,9}", evento_param):
        tela_evento(int(evento_param))
    else:
        ir_para()
        st.rerun()
elif params.get("tela") == "novo":
    tela_novo_evento()
else:
    tela_inicial()
