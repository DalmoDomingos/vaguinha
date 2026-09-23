"""Configuração de domínio: tipos de veículo, paleta de cores e evento inicial."""

# tipo_veiculo_id -> nome
TIPO_CARRO = 1
TIPO_MOTO = 2

TIPOS_VEICULO = {
    TIPO_CARRO: "Carro",
    TIPO_MOTO: "Moto",
}

# Paleta oferecida ao escolher a cor de uma área: rótulo (com bolinha colorida,
# que também aparece no título do card) -> cor em hex.
PALETA = {
    "🔵 Azul":     "#1f6f8b",
    "🟢 Verde":    "#2e6b3e",
    "🟣 Roxo":     "#5b4b8a",
    "🔴 Vermelho": "#b3261e",
    "🟠 Laranja":  "#c75b12",
    "🟡 Amarelo":  "#b8860b",
    "🟤 Marrom":   "#6d4c41",
    "⚫ Preto":    "#2b2b2b",
    "⚪ Cinza":    "#8a8a8a",
}
COR_PADRAO = PALETA["🔵 Azul"]


def rotulo_da_cor(cor: str) -> str:
    """Rótulo da paleta para um hex; cores fora da paleta aparecem como o próprio hex."""
    for rotulo, hexa in PALETA.items():
        if hexa.lower() == (cor or "").lower():
            return rotulo
    return cor or COR_PADRAO


def emoji_da_cor(cor: str) -> str:
    """Bolinha colorida para o título do card ('' se a cor não for da paleta)."""
    rotulo = rotulo_da_cor(cor)
    return rotulo.split(" ", 1)[0] if rotulo in PALETA else ""


# Evento criado automaticamente quando o banco está vazio (e usado pelo
# schema.sql para migrar as 13 áreas que existiam antes dos eventos).
EVENTO_INICIAL = "Corrida da FAB"
AREAS_EVENTO_INICIAL = [
    # nome, cap_carro, cap_moto, cor
    ("Área 1",  150, 50, PALETA["🔵 Azul"]),
    ("Área 2",  120, 40, PALETA["🔵 Azul"]),
    ("Área 3",  120, 40, PALETA["🔵 Azul"]),
    ("Área 4",  100, 30, PALETA["🔵 Azul"]),
    ("Área 5",  100, 30, PALETA["🟢 Verde"]),
    ("Área 6",   80, 25, PALETA["🟢 Verde"]),
    ("Área 7",   80, 25, PALETA["🟢 Verde"]),
    ("Área 8",   60, 20, PALETA["🟢 Verde"]),
    ("Área 9",   60, 20, PALETA["🟣 Roxo"]),
    ("Área 10",  50, 15, PALETA["🟣 Roxo"]),
    ("Área 11",  50, 15, PALETA["🟣 Roxo"]),
    ("Área 12",  40, 10, PALETA["🟣 Roxo"]),
    ("Área 13",  40, 10, PALETA["🟣 Roxo"]),
]
