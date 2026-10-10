> **HISTORICAL** — kept as a record. It does not describe the current Binance USD-M runtime; see `README.md` and `docs/audit/`.

# BGX Capital — Guia de Migração Bybit → KuCoin

## 1. Arquivos alterados

| Arquivo | Mudança |
|---|---|
| `bot/kucoin.py` | **NOVO** — cliente KuCoin (substitui `bot/bybit.py`) |
| `bot/config.py` | Credenciais: `KUCOIN_API_KEY/SECRET/PASSPHRASE` (era `BYBIT_`) |
| `main.py` | Import `KuCoinClient` em vez de `BybitClient` (1 linha) |
| `requirements.txt` | Removido `pybit` (não necessário) |

## 2. Variáveis de ambiente no Railway

### Remover (Bybit — não mais necessárias):
```
BYBIT_API_KEY
BYBIT_API_SECRET
```

### Adicionar (KuCoin):
```
KUCOIN_API_KEY=<sua_api_key>
KUCOIN_API_SECRET=<seu_api_secret>
KUCOIN_API_PASSPHRASE=<sua_passphrase>
```

### Manter iguais:
```
BOT_API_SECRET=<token_interno>
ALLOWED_ORIGINS=https://seu-dominio.railway.app
LEVERAGE=10
MAX_RISK_PCT=0.01
MAX_DRAWDOWN=0.10
DAILY_TARGET_PCT=0.02
DAILY_STOP_LOSS_PCT=0.01
MAX_CORRELATION=0.70
TRAILING_LOCK_R_MULT=1.0
TELEGRAM_TOKEN=...
TELEGRAM_CHAT=...
```

## 3. Criar API Key na KuCoin

1. Acesse kucoin.com → Conta → API Management
2. Crie nova API Key com permissões:
   - ✅ Futures Trading
   - ✅ Futures Read (para posições e saldo)
   - ❌ Withdrawal (NÃO marcar)
3. Defina **IP Whitelist** com o IP do Railway (recomendado)
4. Copie: API Key, Secret Key e **Passphrase** (obrigatória na KuCoin)

## 4. Diferenças KuCoin vs Bybit

| Aspecto | Bybit | KuCoin |
|---|---|---|
| Símbolo BTC | `BTCUSDT` | `XBTUSDTM` (conversão automática) |
| Autenticação | KEY + SECRET | KEY + SECRET + **PASSPHRASE** |
| WebSocket token | Direto | Obtido via REST `/api/v1/bullet-private` |
| Unidade de ordem | Quantidade base (BTC) | **Contratos** (inteiros) |
| Taxa taker | 0.055% | 0.060% |
| Funding | A cada 8h | A cada 8h |
| Leverage máx | 100x | 100x |

## 5. Nota sobre contratos

A KuCoin Futures opera em **contratos** (lotes inteiros), não em quantidade base.
Cada contrato representa um `multiplier` em USDT (ex: XBTUSDTM = 0.001 BTC por contrato).

O `KuCoinClient._round_qty()` faz a conversão automaticamente:
```
contratos = round(qty_base / multiplier)
```
O engine não precisa saber disso — recebe e envia quantidades normalmente.
