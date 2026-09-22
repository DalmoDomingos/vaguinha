-- ============================================================
-- Vaguinha - Schema PostgreSQL
-- ============================================================
-- Tabelas de referência (dimensões)
CREATE TABLE IF NOT EXISTS tipo_veiculo (
    id   SMALLINT PRIMARY KEY,       -- 1 = Carro, 2 = Moto
    nome VARCHAR(20) NOT NULL
);

CREATE TABLE IF NOT EXISTS local (
    id       SMALLINT PRIMARY KEY,   -- 1 a 13 (Áreas)
    nome     VARCHAR(60) NOT NULL,
    cap_carro INTEGER NOT NULL DEFAULT 0,
    cap_moto  INTEGER NOT NULL DEFAULT 0
);

-- Tabela de Movimentação (histórico / registros)
CREATE TABLE IF NOT EXISTS movimentacao (
    id              BIGSERIAL PRIMARY KEY,
    tipo_veiculo_id SMALLINT NOT NULL REFERENCES tipo_veiculo(id),  -- 1 Carro, 2 Moto
    local_id        SMALLINT NOT NULL REFERENCES local(id),         -- 1 a 13
    movimentacao    VARCHAR(10) NOT NULL CHECK (movimentacao IN ('entrada','saida')),
    horario         TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mov_local_tipo ON movimentacao (local_id, tipo_veiculo_id);

-- Seed dos tipos
INSERT INTO tipo_veiculo (id, nome) VALUES (1, 'Carro'), (2, 'Moto')
ON CONFLICT (id) DO NOTHING;

-- Seed dos 13 locais (OBRIGATÓRIO: movimentacao.local_id referencia local.id)
INSERT INTO local (id, nome, cap_carro, cap_moto) VALUES
    (1,  'Área 1',  150, 50),
    (2,  'Área 2',  120, 40),
    (3,  'Área 3',  120, 40),
    (4,  'Área 4',  100, 30),
    (5,  'Área 5',  100, 30),
    (6,  'Área 6',   80, 25),
    (7,  'Área 7',   80, 25),
    (8,  'Área 8',   60, 20),
    (9,  'Área 9',   60, 20),
    (10, 'Área 10',  50, 15),
    (11, 'Área 11',  50, 15),
    (12, 'Área 12',  40, 10),
    (13, 'Área 13',  40, 10)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- View de saldo atual (ocupação = entradas - saídas)
-- ============================================================
CREATE OR REPLACE VIEW vw_ocupacao AS
SELECT
    local_id,
    tipo_veiculo_id,
    SUM(CASE WHEN movimentacao = 'entrada' THEN 1 ELSE -1 END) AS ocupados
FROM movimentacao
GROUP BY local_id, tipo_veiculo_id;
