# IQ Option Bridge API

Backend Python que conecta na IQ Option e expõe dados via REST API.

## Deploy no Railway (5 passos)

### Passo 1 — Criar repositório no GitHub
1. Acesse https://github.com/new
2. Nome: `iqoption-bridge`
3. Marque "Public" e clique em "Create repository"
4. Clique em "uploading an existing file"
5. Arraste os 4 arquivos desta pasta (main.py, requirements.txt, railway.toml, README.md)
6. Clique em "Commit changes"

### Passo 2 — Deploy no Railway
1. Acesse https://railway.app
2. Clique em "New Project"
3. Escolha "Deploy from GitHub repo"
4. Selecione o repo `iqoption-bridge`
5. Clique em "Deploy Now"

### Passo 3 — Configurar variáveis de ambiente
No painel do Railway, clique no serviço → aba "Variables" → Add Variable:

| Variável | Valor |
|---|---|
| `IQOPTION_EMAIL` | seu-email@gmail.com (conta DEMO) |
| `IQOPTION_PASSWORD` | sua-senha-iqoption |

### Passo 4 — Pegar a URL pública
1. Clique na aba "Settings"
2. Em "Domains" clique em "Generate Domain"
3. Copie a URL (ex: https://iqoption-bridge-production.up.railway.app)

### Passo 5 — Colar no Artifact React
Cole a URL no campo "URL do Backend" no dashboard.

## Endpoints disponíveis

- `GET /health` — status da conexão
- `GET /candles/{asset}/{duracao_seg}/{qtd}` — velas OHLC
- `GET /price/{asset}` — preço atual
- `GET /payout/{asset}` — payout real do ativo
- `GET /balance` — saldo conta demo
- `GET /analyze/{asset}/{duracao_seg}` — análise técnica completa
- `GET /assets/binary` — todos os ativos binários abertos

## Assets (exemplos)
- Forex: `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`
- Crypto: `BTCUSD`, `ETHUSD`
- Duração: `60`=M1, `300`=M5, `900`=M15, `3600`=H1

## Segurança
- Usa SEMPRE a conta PRACTICE (demo) por padrão
- Nunca expõe credenciais na API
- Conexão com reconexão automática
