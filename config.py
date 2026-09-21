"""Configuração de domínio: tipos de veículo e capacidades por área (local)."""

# tipo_veiculo_id -> nome
TIPO_CARRO = 1
TIPO_MOTO = 2

TIPOS_VEICULO = {
    TIPO_CARRO: "Carro",
    TIPO_MOTO: "Moto",
}

# local_id (1 a 13) -> {nome, cap_carro, cap_moto}
# Ajuste as capacidades conforme a realidade de cada área.
# A Área 1 segue o desenho: 150 carros / 50 motos.
AREAS = {
    1:  {"nome": "Área 1",  "cap_carro": 150, "cap_moto": 50},
    2:  {"nome": "Área 2",  "cap_carro": 120, "cap_moto": 40},
    3:  {"nome": "Área 3",  "cap_carro": 120, "cap_moto": 40},
    4:  {"nome": "Área 4",  "cap_carro": 100, "cap_moto": 30},
    5:  {"nome": "Área 5",  "cap_carro": 100, "cap_moto": 30},
    6:  {"nome": "Área 6",  "cap_carro": 80,  "cap_moto": 25},
    7:  {"nome": "Área 7",  "cap_carro": 80,  "cap_moto": 25},
    8:  {"nome": "Área 8",  "cap_carro": 60,  "cap_moto": 20},
    9:  {"nome": "Área 9",  "cap_carro": 60,  "cap_moto": 20},
    10: {"nome": "Área 10", "cap_carro": 50,  "cap_moto": 15},
    11: {"nome": "Área 11", "cap_carro": 50,  "cap_moto": 15},
    12: {"nome": "Área 12", "cap_carro": 40,  "cap_moto": 10},
    13: {"nome": "Área 13", "cap_carro": 40,  "cap_moto": 10},
}

# Cores dos cards (visual do Excalidraw: azul, verde, roxo em blocos)
CORES_AREA = ["#1f6f8b", "#1f6f8b", "#1f6f8b", "#1f6f8b",
              "#2e6b3e", "#2e6b3e", "#2e6b3e", "#2e6b3e",
              "#5b4b8a", "#5b4b8a", "#5b4b8a", "#5b4b8a", "#5b4b8a"]


def cor_da_area(local_id: int) -> str:
    return CORES_AREA[(local_id - 1) % len(CORES_AREA)]
