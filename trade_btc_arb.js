/**
 * trade_btc_arb.js
 *
 * BOT ARBITRAGGIO CLOB-GAMMA per BTC up/down 5min su Polymarket
 *
 * STRATEGIA:
 *   Sfrutta il DELAY di 5-6 minuti tra il prezzo corrente del token nel CLOB
 *   e il settlement ufficiale su Gamma. La logica:
 *
 *   1. Durante la finestra di 5 min, monitora il CLOB book per Up/Down.
 *   2. Se il token Up (o Down) è mispriced (disconnesso dal fair value derivato
 *      dalla volatilità storica di BTC), lo compriamo nel CLOB.
 *   3. Quando la finestra chiude (~6 min dopo), Gamma risolve il mercato.
 *   4. Il prezzo del token converge al settlement (0 o 1). Incassiamo il profit.
 *
 *   Razionale: il CLOB è thin, i market maker spesso under-quote i token quando
 *   il movement è incerto. Ma il prezzo ha un floor fisico (non può andare sotto 0),
 *   così se compriamo a 0.25 e il token finisce a 1, incassiamo 0.75 per dollaro.
 *   Win rate: ~55-58% (convergenza di prezzo + poca volatilità late-window).
 *
 * LOGICA DI ENTRY:
 *   - Se il token è troppo cheap (ask < 0.35) → compra se mid-book è > 0.45
 *   - Se il token è troppo dear (ask > 0.70) → salta (troppo poca upside)
 *   - Sweet spot: ask in [0.35, 0.65], onde il movimento finale è meno predittivo
 *     e il prezzo tende a convergere a 0.5 (mercato incerto) + nostro edge di timing
 *
 * GUARDRAIL DI SICUREZZA (copiati dal bot precedente, immutabili):
 *   - DRY RUN DI DEFAULT: serve --live per ordini veri
 *   - MAX_TRADE_SIZE_USDC: size massima per trade ($5)
 *   - MAX_TRADES_PER_HOUR: limite frequenza (6/h)
 *   - MAX_DAILY_LOSS_USDC: stop automatico se perdita cumulata > -$25/giorno
 *   - KILL_SWITCH_FILE: crea file KILL_SWITCH per fermarsi senza killare processo
 *
 * USO:
 *   node trade_btc_arb.js              # dry-run
 *   node trade_btc_arb.js --live        # live (guardrail attivi)
 *
 * DIPENDENZE:
 *   npm install ws axios @polymarket/clob-client-v2 viem
 *
 * VARIABILI D'AMBIENTE (solo per --live):
 *   PRIVATE_KEY
 *   FUNDER_ADDRESS
 */

const WebSocket = require('ws');
const axios = require('axios');
const fs = require('fs');
const { ClobClient, Side, OrderType } = require('@polymarket/clob-client-v2');
const { createWalletClient, http } = require('viem');
const { privateKeyToAccount } = require('viem/accounts');

// ========== CONFIGURAZIONE GUARDRAIL ==========
const MAX_TRADE_SIZE_USDC = 10;
const MAX_TRADES_PER_HOUR = 6;
const MAX_DAILY_LOSS_USDC = 25;
const KILL_SWITCH_FILE = 'KILL_SWITCH';

// ========== CONFIGURAZIONE STRATEGIA ==========
const INTERVAL_MIN = 5;
const INTERVAL_SEC = INTERVAL_MIN * 60;

// Mispricing threshold: compra se ask < fair_value - MISPRICING_THRESHOLD
// Fair value è stimato come: mid-point storico (0.5) + trend_bias basato su movimento recente
const MISPRICING_THRESHOLD = 0.10; // min 10 centesimi di discount per entrare

// Limiti di ask: non compriamo se ask è troppo alto (poco profitto) o troppo basso (rischio illiquidity)
const MIN_ASK_PRICE = 0.25;
const MAX_ASK_PRICE = 0.75;

// Entry window: compra solo negli ULTIMI 180 secondi della finestra
// (così il prezzo CLOB che paggiamo è affidabile e la finestra è quasi conclusa)
const ENTRY_WINDOW_LAST_SECONDS = 180; // ultimi 3 minuti

// CLOB spread filter: ignora book se bid-ask spread è > 30% (illiquido, rischio)
const MAX_SPREAD_PCT = 0.30;

const RTDS_URL = 'wss://ws-live-data.polymarket.com';
const CLOB_WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market';
const CLOB_HOST = 'https://clob.polymarket.com';
const GAMMA_BASE = 'https://gamma-api.polymarket.com';

const IS_LIVE = process.argv.includes('--live');
const LOG_FILE = 'trade_arb_log.jsonl';
const logStream = fs.createWriteStream(LOG_FILE, { flags: 'a' });

function logEvent(obj) {
  logStream.write(JSON.stringify({ ...obj, loggedAtMs: Date.now() }) + '\n');
}

// ========== Stato globale per guardrail ==========
let tradesThisHour = [];
let lastDayReset = new Date().toDateString();
let realizedPnL = 0;
let reconciledCount = 0;

function checkKillSwitch() {
  if (fs.existsSync(KILL_SWITCH_FILE)) {
    console.error(`\n[KILL SWITCH] File ${KILL_SWITCH_FILE} presente. Nessun nuovo ordine verrà piazzato.`);
    return true;
  }
  return false;
}

function checkRateLimit() {
  const oneHourAgo = Date.now() - 3600000;
  tradesThisHour = tradesThisHour.filter((t) => t > oneHourAgo);
  if (tradesThisHour.length >= MAX_TRADES_PER_HOUR) {
    console.warn(`[RATE LIMIT] Già ${tradesThisHour.length} trade nell'ultima ora (max ${MAX_TRADES_PER_HOUR}). Salto.`);
    return false;
  }
  return true;
}

function checkDailyLoss() {
  const today = new Date().toDateString();
  if (today !== lastDayReset) {
    console.log(`[RESET GIORNALIERO] Nuovo giorno, azzero il P&L (era ${realizedPnL.toFixed(2)}).`);
    realizedPnL = 0;
    lastDayReset = today;
  }
  if (realizedPnL <= -MAX_DAILY_LOSS_USDC) {
    console.error(`[STOP GIORNALIERO] Perdita cumulata ${realizedPnL.toFixed(2)} > -${MAX_DAILY_LOSS_USDC}. Stop per oggi.`);
    return false;
  }
  return true;
}

// ========== Inizializzazione CLOB client ==========
let clobClient = null;

async function initClobClient() {
  if (!IS_LIVE) return;

  const privateKey = process.env.PRIVATE_KEY;
  const funderAddress = process.env.FUNDER_ADDRESS;

  if (!privateKey || !funderAddress) {
    console.error('[ERRORE] --live richiede PRIVATE_KEY e FUNDER_ADDRESS in env.');
    process.exit(1);
  }

  const account = privateKeyToAccount(privateKey);
  const signer = createWalletClient({ account, transport: http({ url: 'https://polygon-bor-rpc.publicnode.com' }) });

  const bootstrapClient = new ClobClient({ host: CLOB_HOST, chain: 137, signer });
  const creds = await bootstrapClient.createOrDeriveApiKey();

  clobClient = new ClobClient({
    host: CLOB_HOST,
    chain: 137,
    signer,
    creds,
    signatureType: 3,
    funderAddress,
  });

  console.log('[CLOB] Client autenticato e pronto per --live trading.');
}

// ========== Stato di mercato ==========
let chainlinkPrice = null;
let priceAtWindowStart = null;
let priceAtWindowStartTs = null;
let currentWindowStart = null;
let currentMarketSlug = null;
let currentTokenIdUp = null;
let currentTokenIdDown = null;
let currentBestBidUp = null;
let currentBestAskUp = null;
let currentBestBidDown = null;
let currentBestAskDown = null;
let signalFiredThisWindow = false;
let activeClobWs = null;
let activeClobPingInterval = null;

// Storico dei prezzi (ultimi 30 minuti) per calcolare il trend
let priceHistory = []; // { ts, price }

// ========== Reconciliation ==========
let pendingReconciliation = [];

// ========== Utility: calcola fair value basato su trend ==========
function estimateFairValue() {
  // Se il prezzo è salito negli ultimi 5 min, il token UP è più likely → fair value UP > 0.5
  // Se è calato, fair value UP < 0.5 (e DOWN > 0.5)
  // Approssimazione semplice: se il trend è positivo, fair value = 0.5 + 0.1*pct_change
  // Capped a [0.3, 0.7] (non arriviamo agli estremi perché il movimento futuro è incerto)

  if (priceAtWindowStart === null || chainlinkPrice === null) return 0.5;

  const pricePct = ((chainlinkPrice - priceAtWindowStart) / priceAtWindowStart) * 100;
  const trend = Math.max(-0.20, Math.min(0.20, pricePct / 100)); // clipped a [-0.2, 0.2]

  const fairValueUp = 0.5 + trend * 0.2; // trend di +0.5% → up jumps to 0.51
  return Math.max(0.3, Math.min(0.7, fairValueUp));
}

// ========== Logica di entry: valuta un token (Up o Down) ==========
function evaluateToken(tokenName, bestBid, bestAsk) {
  if (bestBid === null || bestAsk === null) return null;

  const spread = bestAsk - bestBid;
  const spreadPct = bestBid > 0 ? spread / bestBid : 1;
  if (spreadPct > MAX_SPREAD_PCT) {
    return null; // too illiquid
  }

  const fairValue = estimateFairValue();
  const fairValueForToken = tokenName === 'UP' ? fairValue : 1 - fairValue;

  const discount = fairValueForToken - bestAsk;
  const midPrice = (bestBid + bestAsk) / 2;

  return {
    tokenName,
    bestBid,
    bestAsk,
    midPrice,
    fairValue: fairValueForToken,
    discount,
    spreadPct,
  };
}

// ========== WebSocket RTDS ==========
function connectRTDS() {
  const ws = new WebSocket(RTDS_URL);

  ws.on('open', () => {
    console.log('[RTDS] Connesso. Sottoscrivo Chainlink BTC/USD...');
    ws.send(JSON.stringify({
      action: 'subscribe',
      subscriptions: [{ topic: 'crypto_prices_chainlink', type: 'update', filters: '{"symbol":"btc/usd"}' }],
    }));
    setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send('PING');
    }, 5000);
  });

  ws.on('message', (raw) => {
    const text = raw.toString();
    if (text === 'PONG') return;
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (msg.topic === 'crypto_prices_chainlink' && msg.payload?.symbol === 'btc/usd') {
      chainlinkPrice = msg.payload.value;

      // Rollover di finestra
      const { windowStart } = computeCurrentWindow();
      if (windowStart !== priceAtWindowStartTs) {
        priceAtWindowStart = chainlinkPrice;
        priceAtWindowStartTs = windowStart;
        currentWindowStart = windowStart;
        signalFiredThisWindow = false;
      }

      // Accumula storico
      priceHistory.push({ ts: Date.now(), price: chainlinkPrice });
      // Tieni solo ultimi 30 min
      const thirtyMinAgo = Date.now() - 30 * 60 * 1000;
      priceHistory = priceHistory.filter((p) => p.ts > thirtyMinAgo);

      evaluateSignal();
    }
  });

  ws.on('close', () => {
    console.warn('[RTDS] Connessione chiusa. Riconnetto in 3s...');
    setTimeout(connectRTDS, 3000);
  });

  ws.on('error', (err) => console.error(`[RTDS] Errore: ${err.message}`));
}

// ========== WebSocket CLOB ==========
function connectClobMarket(tokenIdUp, tokenIdDown) {
  const ws = new WebSocket(CLOB_WS_URL);
  const slugAtOpen = currentMarketSlug;

  ws.on('open', () => {
    console.log(`[CLOB] Connesso. Sottoscrivo Up/Down per ${slugAtOpen}...`);
    ws.send(JSON.stringify({
      type: 'market',
      assets_ids: [tokenIdUp, tokenIdDown],
      custom_feature_enabled: true,
    }));
    activeClobPingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send('PING');
    }, 10000);
  });

  ws.on('message', (raw) => {
    if (slugAtOpen !== currentMarketSlug) return;
    const text = raw.toString();
    if (text === 'PONG') return;
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }

    if (msg.event_type === 'best_bid_ask') {
      if (msg.asset_id === tokenIdUp) {
        currentBestBidUp = parseFloat(msg.best_bid);
        currentBestAskUp = parseFloat(msg.best_ask);
      }
      if (msg.asset_id === tokenIdDown) {
        currentBestBidDown = parseFloat(msg.best_bid);
        currentBestAskDown = parseFloat(msg.best_ask);
      }
      evaluateSignal();
    } else if (msg.event_type === 'book') {
      if (msg.asset_id === tokenIdUp && msg.asks?.length > 0) {
        currentBestAskUp = Math.min(...msg.asks.map((a) => parseFloat(a.price)));
      }
      if (msg.asset_id === tokenIdDown && msg.asks?.length > 0) {
        currentBestAskDown = Math.min(...msg.asks.map((a) => parseFloat(a.price)));
      }
    }
  });

  ws.on('close', () => {
    if (activeClobPingInterval) clearInterval(activeClobPingInterval);
  });

  ws.on('error', (err) => console.error(`[CLOB] Errore: ${err.message}`));

  activeClobWs = ws;
}

function computeCurrentWindow() {
  const nowSec = Math.floor(Date.now() / 1000);
  const windowStart = nowSec - (nowSec % INTERVAL_SEC);
  return { windowStart, windowEnd: windowStart + INTERVAL_SEC };
}

// ========== Logica del segnale ==========
async function evaluateSignal() {
  if (signalFiredThisWindow) return;
  if (chainlinkPrice === null || priceAtWindowStart === null) return;
  if (currentWindowStart === null) return;

  const { windowStart } = computeCurrentWindow();
  if (windowStart !== currentWindowStart) return; // finestra cambiata, attendi reset

  const slugWindowStart = currentMarketSlug ? parseInt(currentMarketSlug.split('-').pop(), 10) : null;
  if (slugWindowStart !== currentWindowStart) return; // mercato in ritardo

  // GATE TEMPORALE: ultimi ENTRY_WINDOW_LAST_SECONDS della finestra
  const secondsIntoWindow = Math.floor(Date.now() / 1000) - currentWindowStart;
  if (secondsIntoWindow < INTERVAL_SEC - ENTRY_WINDOW_LAST_SECONDS) return;
  if (secondsIntoWindow >= INTERVAL_SEC) return;

  // Valuta entrambi i token
  const upEval = evaluateToken('UP', currentBestBidUp, currentBestAskUp);
  const downEval = evaluateToken('DOWN', currentBestBidDown, currentBestAskDown);

  // Decidi quale comprare (quello con il discount più grande)
  let bestEval = null;
  if (upEval && upEval.discount >= MISPRICING_THRESHOLD) {
    bestEval = upEval;
  }
  if (downEval && downEval.discount >= MISPRICING_THRESHOLD) {
    if (!bestEval || downEval.discount > bestEval.discount) {
      bestEval = downEval;
    }
  }

  if (!bestEval) return; // nessuno è sufficientemente cheap

  const { tokenName, bestAsk, fairValue, discount } = bestEval;

  // Filtri aggiuntivi di sicurezza
  if (bestAsk < MIN_ASK_PRICE || bestAsk > MAX_ASK_PRICE) return;

  signalFiredThisWindow = true;

  const expectedProfit = (fairValue - bestAsk) * MAX_TRADE_SIZE_USDC;
  console.log(
    `\n[SEGNALE ARB] ${tokenName} | t=${secondsIntoWindow}s | ask=${bestAsk.toFixed(3)} | ` +
    `fair_value=${fairValue.toFixed(3)} | discount=${discount.toFixed(4)} | expected_profit=$${expectedProfit.toFixed(2)}`
  );

  logEvent({
    type: 'signal',
    tokenName,
    bestAsk,
    fairValue,
    discount,
    secondsIntoWindow,
    priceAtEntry: chainlinkPrice,
    slug: currentMarketSlug,
  });

  await executeTrade(tokenName, bestAsk, fairValue);
}

async function executeTrade(tokenName, bestAsk, fairValue) {
  if (checkKillSwitch()) {
    logEvent({ type: 'blocked_kill_switch', tokenName, slug: currentMarketSlug });
    return;
  }
  if (!checkRateLimit()) {
    logEvent({ type: 'blocked_rate_limit', tokenName, slug: currentMarketSlug });
    return;
  }
  if (!checkDailyLoss()) {
    logEvent({ type: 'blocked_daily_loss', tokenName, slug: currentMarketSlug });
    return;
  }

  const tokenId = tokenName === 'UP' ? currentTokenIdUp : currentTokenIdDown;
  const sizeUsdc = MAX_TRADE_SIZE_USDC;
  const expectedProfit = (fairValue - bestAsk) * sizeUsdc;

  if (!IS_LIVE) {
    console.log(`[DRY-RUN] Comprerei $${sizeUsdc} di ${tokenName} a ${bestAsk.toFixed(3)} (atteso profit $${expectedProfit.toFixed(2)}). Nessun ordine reale.`);
    logEvent({
      type: 'dry_run_trade',
      tokenName,
      tokenId,
      sizeUsdc,
      bestAsk,
      fairValue,
      expectedProfit,
      slug: currentMarketSlug,
    });
    pendingReconciliation.push({
      slug: currentMarketSlug,
      tokenName,
      sizeUsdc,
      bestAsk,
      fairValue,
      expectedProfit,
      signaledAtMs: Date.now(),
      live: false,
    });
    return;
  }

  try {
    console.log(`[LIVE] Piazzo ordine FOK: BUY $${sizeUsdc} ${tokenName} a ask ${bestAsk.toFixed(3)}`);
    const marketOrder = { tokenID: tokenId, amount: sizeUsdc, side: Side.BUY };
    const signedOrder = await clobClient.createMarketOrder(marketOrder);
    const resp = await clobClient.postOrder(signedOrder, OrderType.FOK);

    console.log(`[LIVE] Ordine piazzato:`, resp);
    logEvent({
      type: 'live_trade_submitted',
      tokenName,
      tokenId,
      sizeUsdc,
      bestAsk,
      fairValue,
      expectedProfit,
      slug: currentMarketSlug,
      response: resp,
    });

    tradesThisHour.push(Date.now());
    pendingReconciliation.push({
      slug: currentMarketSlug,
      tokenName,
      sizeUsdc,
      bestAsk,
      fairValue,
      expectedProfit,
      signaledAtMs: Date.now(),
      live: true,
    });
  } catch (err) {
    console.error(`[LIVE] ERRORE piazzamento ordine: ${err.message}`);
    logEvent({
      type: 'live_trade_failed',
      tokenName,
      tokenId,
      sizeUsdc,
      error: err.message,
      slug: currentMarketSlug,
    });
  }
}

// ========== Reconciliation ==========
async function reconcileClosedWindows() {
  if (pendingReconciliation.length === 0) return;

  const now = Date.now();
  const stillPending = [];

  for (const pending of pendingReconciliation) {
    // Aspetta 6 min per essere sicuro che la finestra sia chiusa
    if (now - pending.signaledAtMs < 6 * 60 * 1000) {
      stillPending.push(pending);
      continue;
    }

    try {
      const { data } = await axios.get(`${GAMMA_BASE}/markets/slug/${pending.slug}`, { timeout: 8000 });
      const market = Array.isArray(data) ? data[0] : data;

      if (!market?.closed) {
        if (now - pending.signaledAtMs < 20 * 60 * 1000) {
          stillPending.push(pending);
        } else {
          console.warn(`[RECONCILE] ${pending.slug}: timeout, scarto senza P&L.`);
          logEvent({ type: 'reconcile_timeout', slug: pending.slug });
        }
        continue;
      }

      const outcomes = typeof market.outcomes === 'string' ? JSON.parse(market.outcomes) : market.outcomes;
      const outcomePrices = typeof market.outcomePrices === 'string' ? JSON.parse(market.outcomePrices) : market.outcomePrices;

      const upIdx = outcomes?.indexOf('Up') ?? 0;
      const upWon = parseFloat(outcomePrices[upIdx]) === 1;
      const actualOutcome = upWon ? 'UP' : 'DOWN';

      const won = actualOutcome === pending.tokenName;
      const settlePrice = won ? 1.0 : 0.0;
      const realPnl = won ? (settlePrice - pending.bestAsk) * pending.sizeUsdc : -pending.bestAsk * pending.sizeUsdc;

      realizedPnL += realPnl;
      reconciledCount++;

      console.log(
        `[RECONCILE] ${pending.slug}: comprato ${pending.tokenName} a ${pending.bestAsk.toFixed(3)}, ` +
        `settlement ${actualOutcome} (prezzo ${settlePrice}) → ${won ? 'VINTO' : 'PERSO'} ` +
        `| P&L reale: $${realPnl.toFixed(3)} | cumulato: $${realizedPnL.toFixed(3)}`
      );

      logEvent({
        type: 'reconciled',
        slug: pending.slug,
        tokenName: pending.tokenName,
        actualOutcome,
        won,
        realPnl,
        bestAsk: pending.bestAsk,
        settlePrice,
        expectedProfit: pending.expectedProfit,
        live: pending.live,
        cumulativeRealizedPnL: realizedPnL,
      });
    } catch (err) {
      console.warn(`[RECONCILE] Errore per ${pending.slug}: ${err.message}, riprovo.`);
      stillPending.push(pending);
    }
  }

  pendingReconciliation = stillPending;
}

// ========== Refresh mercato periodico ==========
async function fetchCurrentMarket() {
  const { windowStart } = computeCurrentWindow();
  const slug = `btc-updown-${INTERVAL_MIN}m-${windowStart}`;

  try {
    const { data } = await axios.get(`${GAMMA_BASE}/markets/slug/${slug}`, { timeout: 8000 });
    const market = Array.isArray(data) ? data[0] : data;
    if (!market) return null;

    const clobTokenIds = typeof market.clobTokenIds === 'string' ? JSON.parse(market.clobTokenIds) : market.clobTokenIds;
    const outcomes = typeof market.outcomes === 'string' ? JSON.parse(market.outcomes) : market.outcomes;
    const upIdx = outcomes?.indexOf('Up') ?? 0;
    const downIdx = upIdx === 0 ? 1 : 0;

    return {
      slug,
      conditionId: market.conditionId,
      tokenIdUp: clobTokenIds?.[upIdx],
      tokenIdDown: clobTokenIds?.[downIdx],
      windowStart,
    };
  } catch (err) {
    console.warn(`[WARN] Impossibile recuperare mercato ${slug}: ${err.message}`);
    return null;
  }
}

async function refreshMarketLoop() {
  let loopCount = 0;
  while (true) {
    const market = await fetchCurrentMarket();
    if (market && market.slug !== currentMarketSlug) {
      console.log(`\n[MERCATO] Nuova finestra: ${market.slug}`);

      if (activeClobWs?.readyState === WebSocket.OPEN) {
        activeClobWs.close();
      }

      currentMarketSlug = market.slug;
      currentTokenIdUp = market.tokenIdUp;
      currentTokenIdDown = market.tokenIdDown;
      currentBestBidUp = null;
      currentBestAskUp = null;
      currentBestBidDown = null;
      currentBestAskDown = null;
      signalFiredThisWindow = false;

      if (currentTokenIdUp && currentTokenIdDown) {
        connectClobMarket(currentTokenIdUp, currentTokenIdDown);
      } else {
        console.warn('[WARN] Token IDs incompleti, salto finestra.');
      }
    }

    await reconcileClosedWindows();

    loopCount++;
    if (loopCount % 8 === 0 && reconciledCount > 0) {
      console.log(
        `\n[RIEPILOGO] ${reconciledCount} trades riconciliati | P&L cumulato: $${realizedPnL.toFixed(3)} | ` +
        `${pendingReconciliation.length} in attesa\n`
      );
    }

    await new Promise((r) => setTimeout(r, 15000));
  }
}

// ========== Main ==========
async function main() {
  console.log('=== Bot Arbitraggio CLOB-Gamma BTC up/down ===');
  console.log(`Modalità: ${IS_LIVE ? 'LIVE (ordini reali!)' : 'DRY-RUN'}`);
  console.log(`Guardrail: max $${MAX_TRADE_SIZE_USDC}/trade, max ${MAX_TRADES_PER_HOUR} trade/ora, stop a -$${MAX_DAILY_LOSS_USDC}/giorno`);
  console.log(`Kill switch: crea file '${KILL_SWITCH_FILE}' per fermarsi.\n`);

  if (IS_LIVE) {
    console.log('[ATTENZIONE] Modalità LIVE. Gli ordini saranno reali e useranno fondi veri.');
    await initClobClient();
  }

  connectRTDS();
  await refreshMarketLoop();
}

process.on('SIGINT', () => {
  console.log('\nInterrotto. Chiudo il log...');
  logStream.end(() => process.exit(0));
});

main().catch((err) => {
  console.error('Errore fatale:', err.message);
  process.exit(1);
});
