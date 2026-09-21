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
