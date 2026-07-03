/**
 * trade_btc_momentum.js
 *
 * SCRIPT OPERATIVO - PIAZZA ORDINI REALI SU POLYMARKET (in modalità --live).
 *
 * Strategia: momentum a soglia EV-based, basata sull'analisi storica validata
 * su un mese di dati (7502 mercati BTC up/down 5min, 1-27 giugno 2026):
 *   - L'accordo direzionale tra il movimento netto BTC (body_pct, da Chainlink)
 *     e l'esito finale del mercato Polymarket aumenta monotonicamente con
 *     l'ampiezza del movimento: 68.4% a 0%, fino a 83.1% sopra lo 0.2%.
 *   - NON abbiamo trovato un "lag" sfruttabile stabile (testato e scartato
 *     dopo analisi su 309 trigger/199 reazioni). Questa è quindi una strategia
 *     di momentum/probabilità, non di arbitraggio di latenza.
 *
 * LOGICA:
 *   1. Monitora Chainlink BTC/USD (RTDS) durante ogni finestra di 5 minuti.
 *   2. Calcola body_pct cumulato rispetto al prezzo di apertura della finestra
 *      (Price to Beat, preso da Gamma al momento dell'apertura).
 *   3. Mappa body_pct -> probabilità stimata di accordo direzionale, usando
 *      la tabella empirica validata (PROB_TABLE sotto).
 *   4. Legge il prezzo ask corrente del token coerente con la direzione
 *      (Up se BTC sale, Down se scende) dal CLOB.
 *   5. Calcola EV = prob*(1-ask) - (1-prob)*ask. Se EV > EV_MIN_THRESHOLD,
 *      genera un segnale di acquisto.
 *   6. In modalità --live, piazza un ordine FOK (fill-or-kill) di size fissa.
 *      In modalità dry-run (default), LOGGA SOLO cosa farebbe.
 *
 * GUARDRAIL DI SICUREZZA (non opzionali, fanno parte della logica):
 *   - DRY RUN DI DEFAULT: serve il flag esplicito --live per piazzare ordini veri.
 *   - MAX_TRADE_SIZE_USDC: size massima per singolo ordine (default $5).
 *   - MAX_TRADES_PER_HOUR: limite di frequenza, per evitare loop impazziti.
 *   - MAX_DAILY_LOSS_USDC: stop automatico se la perdita cumulata giornaliera
 *     (stimata sui trade piazzati) supera questa soglia.
 *   - KILL_SWITCH_FILE: se questo file esiste nella working directory, lo
 *     script si ferma prima di piazzare qualsiasi nuovo ordine. Crea il file
 *     da un altro terminale per fermarlo senza killare il processo:
 *         touch KILL_SWITCH
 *
 * USO:
 *   node trade_btc_momentum.js              # dry-run, nessun ordine reale
 *   node trade_btc_momentum.js --live        # esecuzione reale, guardrail attivi
 *
 * DIPENDENZE:
 *   npm install ws axios @polymarket/clob-client-v2 viem
 *
 * VARIABILI D'AMBIENTE RICHIESTE (solo per --live):
 *   PRIVATE_KEY            chiave privata del wallet di trading (NON il seed principale)
 *   FUNDER_ADDRESS         indirizzo del proxy wallet che detiene i fondi
 *
 * BUG FIX (dopo il primo run dry-run): quando il movimento Chainlink rispetto
 * al priceToBeat era zero o quasi-zero, il codice assegnava comunque una
 * direzione (DOWN, per default del ternario) e applicava la probabilità base
 * della tabella come se fosse un segnale reale. Risultato osservato: 52/82
 * segnali nel primo run erano di questo tipo, con win rate 42.3% (peggio del
 * coin-flip) e P&L -$20.95 simulati — un bias sistematico senza alcuna
 * informazione direzionale dietro. Corretto aggiungendo MIN_MOVE_FOR_DIRECTION_PCT:
 * sotto questa soglia non si genera nessun segnale.
 *
 * RECONCILIATION: ogni segnale (dry-run o live) viene tracciato in
 * pendingReconciliation. Circa 6 minuti dopo, lo script controlla l'esito
 * UFFICIALE del mercato su Gamma e calcola il P&L REALE (non più solo l'EV
 * teorico). Il guardrail MAX_DAILY_LOSS_USDC si basa su questo P&L reale.
 * Questo permette anche di confrontare, dopo un po' di raccolta, la
 * probabilità stimata (PROB_TABLE) con il tasso di vittoria osservato davvero
 * — un test di calibrazione del modello, non solo un conto della cassa.
 *
 * LIMITI ONESTI DI QUESTA STRATEGIA (da rileggere prima di aumentare i size):
 *   - La tabella di probabilità è stimata su UN mese di dati storici (giugno 2026,
 *     mercato BTC in un regime specifico). Non garantisce risultati futuri.
 *   - L'EV usa l'ask CORRENTE al momento del segnale, ma tra il calcolo e
 *     l'esecuzione effettiva dell'ordine il prezzo può muoversi (slippage).
 *     L'ordine è FOK (fill-or-kill) per evitare fill parziali a prezzo diverso,
 *     ma può semplicemente fallire se il book si è già mosso.
 *   - Fee Polymarket e gas Polygon NON sono ancora sottratti dall'EV in modo
 *     preciso: EV_MIN_THRESHOLD deve includere un margine per questo (vedi commento).
 */

const WebSocket = require('ws');
const axios = require('axios');
const fs = require('fs');
const { ClobClient, Side, OrderType } = require('@polymarket/clob-client-v2');
const { createWalletClient, http } = require('viem');
const { privateKeyToAccount } = require('viem/accounts');

// ========== CONFIGURAZIONE GUARDRAIL (non bypassabili da riga di comando) ==========
const MAX_TRADE_SIZE_USDC = 5; // size massima per singolo ordine, in USDC
const MAX_TRADES_PER_HOUR = 6; // limite di frequenza
const MAX_DAILY_LOSS_USDC = 25; // stop automatico se la perdita stimata cumulata supera questo
const KILL_SWITCH_FILE = 'KILL_SWITCH';

// ========== CONFIGURAZIONE STRATEGIA ==========
const INTERVAL_MIN = 5;
const INTERVAL_SEC = INTERVAL_MIN * 60;

// --- FILTRI RENDIMENTO (aggiunti dopo l'analisi del trade_log: il bot perdeva
// perché scommetteva sul rumore. 195/215 trade partivano entro 30s con
// body_pct<0.02% -> win rate reale 51% (coin flip), P&L -$31.75. I pochi
// ingressi su movimento vero e in finestra avanzata erano profittevoli.) ---

// 1) GATE TEMPORALE: valuta segnali solo negli ULTIMI N secondi della finestra,
//    quando il movimento netto è predittivo dell'esito (l'inizio finestra è rumore
//    e non è coerente con come il backtest misurava body_pct, cioè a fine corsa).
const ENTRY_WINDOW_LAST_SECONDS = 120; // entra solo negli ultimi 120s dei 300

// 2) SOGLIA DI MOVIMENTO REALE: sotto questo body_pct nessun segnale. Alzata da
//    0.005% (che lasciava passare il rumore e attivava la riga "0.0 -> 0.684"
//    della PROB_TABLE, irreale dal vivo) a 0.03%. Di fatto neutralizza le due
//    righe più basse della tabella, che i dati reali non confermano.
const MIN_MOVE_FOR_DIRECTION_PCT = 0.03;

// 3) FILTRO ASK: niente acquisti dove il payoff è minimo (ask alto). Nei dati,
//    ask>=0.70 perdeva (winrate 0.40 vs breakeven 0.72). L'EV>soglia copre già
//    il breakeven (EV = prob - ask), questo è un tetto di sicurezza aggiuntivo.
const MAX_ASK_PRICE = 0.85;


// Tabella empirica validata sul mese di giugno 2026 (n=7502, vedi analisi).
// body_pct minimo -> probabilità stimata di accordo direzionale.
// Interpolazione lineare a step tra i punti noti; sotto 0% si usa il primo valore.
const PROB_TABLE = [
  { bodyPct: 0.0, prob: 0.684 },
  { bodyPct: 0.02, prob: 0.707 },
  { bodyPct: 0.05, prob: 0.746 },
  { bodyPct: 0.10, prob: 0.783 },
  { bodyPct: 0.15, prob: 0.807 },
  { bodyPct: 0.20, prob: 0.831 },
];

// Margine di sicurezza minimo sull'EV per coprire fee/gas/slippage non modellati
// esplicitamente. 0.03 = 3 centesimi di EV per dollaro scommesso, soglia
// conservativa scelta perché la stima di probabilità stessa ha un margine di
// errore non quantificato (intervallo di confidenza non calcolato qui).
const EV_MIN_THRESHOLD = 0.03;

const RTDS_URL = 'wss://ws-live-data.polymarket.com';
const CLOB_WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market';
const CLOB_HOST = 'https://clob.polymarket.com';
const GAMMA_BASE = 'https://gamma-api.polymarket.com';

const IS_LIVE = process.argv.includes('--live');

const LOG_FILE = 'trade_log.jsonl';
const logStream = fs.createWriteStream(LOG_FILE, { flags: 'a' });

function logEvent(obj) {
  logStream.write(JSON.stringify({ ...obj, loggedAtMs: Date.now() }) + '\n');
}

function estimateProb(bodyPct) {
  if (bodyPct <= PROB_TABLE[0].bodyPct) return PROB_TABLE[0].prob;
  for (let i = 1; i < PROB_TABLE.length; i++) {
    if (bodyPct <= PROB_TABLE[i].bodyPct) {
      const prev = PROB_TABLE[i - 1];
      const curr = PROB_TABLE[i];
      const frac = (bodyPct - prev.bodyPct) / (curr.bodyPct - prev.bodyPct);
      return prev.prob + frac * (curr.prob - prev.prob);
    }
  }
  return PROB_TABLE[PROB_TABLE.length - 1].prob; // sopra il massimo osservato, uso l'ultimo valore (no extrapolazione oltre)
}

function computeEV(prob, askPrice) {
  // EV per 1 unità di token comprata all'ask: se vince, incasso (1 - ask); se perde, perdo ask.
  return prob * (1 - askPrice) - (1 - prob) * askPrice;
}

// ========== Stato globale per i guardrail ==========
let tradesThisHour = [];
let lastDayReset = new Date().toDateString();

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
    console.log(`[RESET GIORNALIERO] Nuovo giorno, azzero il P&L realizzato (era ${realizedPnL.toFixed(2)}).`);
    realizedPnL = 0;
    lastDayReset = today;
  }
  if (realizedPnL <= -MAX_DAILY_LOSS_USDC) {
    console.error(`[STOP GIORNALIERO] Perdita REALIZZATA cumulata (${realizedPnL.toFixed(2)}) ha superato il limite di -${MAX_DAILY_LOSS_USDC}. Stop trading per oggi.`);
    return false;
  }
  return true;
}

// ========== Inizializzazione client CLOB (solo se --live) ==========
let clobClient = null;

async function initClobClient() {
  if (!IS_LIVE) return;

  const privateKey = process.env.PRIVATE_KEY;
  const funderAddress = process.env.FUNDER_ADDRESS;

  if (!privateKey || !funderAddress) {
    console.error('[ERRORE FATALE] Modalità --live richiede PRIVATE_KEY e FUNDER_ADDRESS nelle variabili d\'ambiente.');
    process.exit(1);
  }

  const account = privateKeyToAccount(privateKey);
  const signer = createWalletClient({ account, transport: http() });

  const bootstrapClient = new ClobClient({ host: CLOB_HOST, chain: 137, signer });
  const creds = await bootstrapClient.createOrDeriveApiKey();

  clobClient = new ClobClient({
    host: CLOB_HOST,
    chain: 137,
    signer,
    creds,
    signatureType: 3, // POLY_1271, per proxy wallet (vedi documentazione)
    funderAddress,
  });

  console.log('[CLOB] Client autenticato e pronto per --live trading.');
}

// ========== Stato di mercato/segnale ==========
let chainlinkPrice = null;
let priceToBeat = null; // prezzo Chainlink all'apertura della finestra corrente
let priceToBeatWindowStart = null; // window-start a cui si riferisce priceToBeat
let currentWindowStart = null; // finestra attiva secondo l'orologio (driven da RTDS)
let currentMarketSlug = null;
let currentTokenIdUp = null;
let currentTokenIdDown = null;
let currentBestAskUp = null;
let currentBestAskDown = null;
let signalFiredThisWindow = false;
let activeClobWs = null;
let activeClobPingInterval = null;

// ========== Reconciliation: traccia i segnali in attesa di esito reale ==========
// Ogni segnale generato viene aggiunto qui con lo slug della sua finestra.
// Quando la finestra si chiude, recuperiamo l'esito ufficiale da Gamma e
// calcoliamo il P&L REALE, confrontandolo con l'EV teorico stimato al momento.
let pendingReconciliation = []; // [{ slug, direction, sizeUsdc, ask, evEstimated, prob, signaledAtMs }]
let reconciledCount = 0;
let realizedPnL = 0;

// ========== Gestione finestra di mercato ==========
function computeCurrentWindow() {
  const nowSec = Math.floor(Date.now() / 1000);
  const windowStart = nowSec - (nowSec % INTERVAL_SEC);
  return { windowStart, windowEnd: windowStart + INTERVAL_SEC };
}

async function fetchCurrentMarket() {
  const { windowStart } = computeCurrentWindow();
  const slug = `btc-updown-${INTERVAL_MIN}m-${windowStart}`;

  try {
    const { data } = await axios.get(`${GAMMA_BASE}/markets/slug/${slug}`, { timeout: 8000 });
    const market = Array.isArray(data) ? data[0] : data;
    if (!market) return null;

    const clobTokenIds = typeof market.clobTokenIds === 'string' ? JSON.parse(market.clobTokenIds) : market.clobTokenIds;
    const outcomes = typeof market.outcomes === 'string' ? JSON.parse(market.outcomes) : market.outcomes;
    const upIdx = outcomes ? outcomes.indexOf('Up') : 0;
    const downIdx = upIdx === 0 ? 1 : 0;

    return {
      slug,
      conditionId: market.conditionId,
      tokenIdUp: clobTokenIds ? clobTokenIds[upIdx] : null,
      tokenIdDown: clobTokenIds ? clobTokenIds[downIdx] : null,
      windowStart,
    };
  } catch (err) {
    console.warn(`[WARN] Impossibile recuperare il mercato corrente (${slug}): ${err.message}`);
    return null;
  }
}

// ========== WebSocket RTDS (Chainlink) ==========
function connectRTDS() {
  const ws = new WebSocket(RTDS_URL);

  ws.on('open', () => {
    console.log('[RTDS] Connesso. Sottoscrivo Chainlink btc/usd...');
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
    if (msg.topic === 'crypto_prices_chainlink' && msg.payload && msg.payload.symbol === 'btc/usd') {
      chainlinkPrice = msg.payload.value;

      // Rollover di finestra guidato dallo stream Chainlink (veloce, ~1s) invece
      // che dal loop a 15s: così priceToBeat è il prezzo all'APERTURA reale della
      // finestra, com'è definita la risoluzione del mercato (Chainlink a inizio
      // range). Questo rende affidabile il calcolo del movimento netto su cui si
      // reggono il gate temporale e la soglia di movimento.
      const { windowStart } = computeCurrentWindow();
      if (windowStart !== priceToBeatWindowStart) {
        priceToBeat = chainlinkPrice;
        priceToBeatWindowStart = windowStart;
        currentWindowStart = windowStart;
        signalFiredThisWindow = false; // nuova finestra: un solo segnale per finestra
      }

      evaluateSignal();
    }
  });

  ws.on('close', () => {
    console.warn('[RTDS] Connessione chiusa. Riconnetto in 3s...');
    setTimeout(connectRTDS, 3000);
  });

  ws.on('error', (err) => console.error(`[RTDS] Errore: ${err.message}`));
}

// ========== WebSocket CLOB (book per entrambi i token) ==========
function connectClobMarket(tokenIdUp, tokenIdDown) {
  const ws = new WebSocket(CLOB_WS_URL);
  const subscribedSlugAtOpen = currentMarketSlug;

  ws.on('open', () => {
    console.log(`[CLOB] Connesso. Sottoscrivo Up/Down per ${subscribedSlugAtOpen}...`);
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
    if (subscribedSlugAtOpen !== currentMarketSlug) return;
    const text = raw.toString();
    if (text === 'PONG') return;
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }

    if (msg.event_type === 'best_bid_ask') {
      const ask = parseFloat(msg.best_ask);
      if (msg.asset_id === tokenIdUp) currentBestAskUp = ask;
      if (msg.asset_id === tokenIdDown) currentBestAskDown = ask;
      evaluateSignal();
    } else if (msg.event_type === 'book') {
      if (msg.asset_id === tokenIdUp && msg.asks && msg.asks.length > 0) {
        currentBestAskUp = Math.min(...msg.asks.map((a) => parseFloat(a.price)));
      }
      if (msg.asset_id === tokenIdDown && msg.asks && msg.asks.length > 0) {
        currentBestAskDown = Math.min(...msg.asks.map((a) => parseFloat(a.price)));
      }
    }
  });

  ws.on('close', () => {
    if (activeClobPingInterval) {
      clearInterval(activeClobPingInterval);
      activeClobPingInterval = null;
    }
  });

  ws.on('error', (err) => console.error(`[CLOB] Errore: ${err.message}`));

  activeClobWs = ws;
}

// ========== Logica del segnale ==========
async function evaluateSignal() {
  if (signalFiredThisWindow) return; // un solo segnale per finestra
  if (chainlinkPrice === null || priceToBeat === null) return;
  if (currentBestAskUp === null && currentBestAskDown === null) return;
  if (currentWindowStart === null) return;

  // Coerenza finestra<->mercato: i best ask appartengono a currentMarketSlug,
  // che codifica il suo window-start. Se il rollover RTDS è avanti rispetto al
  // mercato caricato dal loop (token/ask ancora della finestra precedente), non
  // valutare: eviteremmo di usare ask di una finestra diversa da currentWindowStart.
  const slugWindowStart = currentMarketSlug ? parseInt(currentMarketSlug.split('-').pop(), 10) : null;
  if (slugWindowStart !== currentWindowStart) return;

  // GATE TEMPORALE: solo negli ultimi ENTRY_WINDOW_LAST_SECONDS della finestra.
  // L'inizio finestra è rumore (nei dati: ingresso mediano 12s, winrate 51%).
  const secondsIntoWindow = Math.floor(Date.now() / 1000) - currentWindowStart;
  if (secondsIntoWindow < INTERVAL_SEC - ENTRY_WINDOW_LAST_SECONDS) return; // troppo presto
  if (secondsIntoWindow >= INTERVAL_SEC) return; // finestra già chiusa

  // SOGLIA DI MOVIMENTO REALE: sotto MIN_MOVE_FOR_DIRECTION_PCT non c'è una
  // direzione affidabile da seguire (ed evita la riga "0.0" irreale della tabella).
  const rawDiff = chainlinkPrice - priceToBeat;
  const bodyPct = Math.abs(rawDiff / priceToBeat) * 100;
  if (bodyPct < MIN_MOVE_FOR_DIRECTION_PCT) return; // movimento non significativo

  const direction = rawDiff > 0 ? 'UP' : 'DOWN';

  const prob = estimateProb(bodyPct);
  const ask = direction === 'UP' ? currentBestAskUp : currentBestAskDown;
  if (ask === null || ask === undefined) return;

  // FILTRO ASK: niente acquisti a payoff troppo basso (ask alto = poco da vincere,
  // e nei dati reali la fascia ask alta perdeva).
  if (ask > MAX_ASK_PRICE) return;

  const ev = computeEV(prob, ask);

  if (ev > EV_MIN_THRESHOLD) {
    signalFiredThisWindow = true;
    console.log(
      `\n[SEGNALE] ${direction} | t=${secondsIntoWindow}s | body_pct=${bodyPct.toFixed(4)}% | ` +
      `prob_stimata=${prob.toFixed(3)} | ask=${ask.toFixed(3)} | EV=${ev.toFixed(4)} | slug=${currentMarketSlug}`
    );
    // secondsIntoWindow loggato per poter RICALIBRARE la PROB_TABLE sui dati
    // puliti (ingressi a fine finestra) raccolti d'ora in poi.
    logEvent({ type: 'signal', direction, bodyPct, prob, ask, ev, secondsIntoWindow, priceToBeat, slug: currentMarketSlug });

    await executeTrade(direction, ask, ev, prob);
  }
}

async function executeTrade(direction, ask, ev, prob) {
  if (checkKillSwitch()) {
    logEvent({ type: 'blocked_kill_switch', direction, slug: currentMarketSlug });
    return;
  }
  if (!checkRateLimit()) {
    logEvent({ type: 'blocked_rate_limit', direction, slug: currentMarketSlug });
    return;
  }
  if (!checkDailyLoss()) {
    logEvent({ type: 'blocked_daily_loss', direction, slug: currentMarketSlug });
    return;
  }

  const tokenId = direction === 'UP' ? currentTokenIdUp : currentTokenIdDown;
  const sizeUsdc = MAX_TRADE_SIZE_USDC;

  if (!IS_LIVE) {
    console.log(`[DRY-RUN] Comprerei $${sizeUsdc} di token ${direction} a ~${ask.toFixed(3)} (EV stimato ${ev.toFixed(4)}). Nessun ordine reale piazzato.`);
    logEvent({ type: 'dry_run_trade', direction, tokenId, sizeUsdc, ask, ev, prob, slug: currentMarketSlug });
    // Registriamo il segnale per la reconciliation: quando la finestra chiude,
    // controlliamo l'esito VERO e calcoliamo il P&L reale, non solo quello teorico.
    pendingReconciliation.push({
      slug: currentMarketSlug,
      direction,
      sizeUsdc,
      ask,
      evEstimated: ev,
      prob,
      signaledAtMs: Date.now(),
      live: false,
    });
    return;
  }

  try {
    console.log(`[LIVE] Piazzo ordine FOK: BUY $${sizeUsdc} token ${direction} (${tokenId.slice(0, 16)}...) a ask ${ask.toFixed(3)}`);
    const marketOrder = { tokenID: tokenId, amount: sizeUsdc, side: Side.BUY };
    const signedOrder = await clobClient.createMarketOrder(marketOrder);
    const resp = await clobClient.postOrder(signedOrder, OrderType.FOK);

    console.log(`[LIVE] Risposta ordine:`, resp);
    logEvent({ type: 'live_trade_submitted', direction, tokenId, sizeUsdc, ask, ev, prob, slug: currentMarketSlug, response: resp });

    tradesThisHour.push(Date.now());
    // Il P&L reale si conosce solo alla risoluzione del mercato (fino a 5 min dopo).
    // Registriamo qui per la reconciliation, che aggiornerà realizedPnL
    // con l'esito VERO non appena la finestra si chiude (vedi reconcileClosedWindows).
    pendingReconciliation.push({
      slug: currentMarketSlug,
      direction,
      sizeUsdc,
      ask,
      evEstimated: ev,
      prob,
      signaledAtMs: Date.now(),
      live: true,
    });
  } catch (err) {
    console.error(`[LIVE] ERRORE nel piazzare l'ordine: ${err.message}`);
    logEvent({ type: 'live_trade_failed', direction, tokenId, sizeUsdc, error: err.message, slug: currentMarketSlug });
  }
}

// ========== Reconciliation: controlla l'esito reale dei segnali passati ==========
async function reconcileClosedWindows() {
  if (pendingReconciliation.length === 0) return;

  const now = Date.now();
  const stillPending = [];

  for (const pending of pendingReconciliation) {
    // Aspettiamo almeno 6 minuti dal segnale per essere sicuri che la finestra
    // (5 min) sia chiusa e Gamma abbia aggiornato l'esito.
    if (now - pending.signaledAtMs < 6 * 60 * 1000) {
      stillPending.push(pending);
      continue;
    }

    try {
      const { data } = await axios.get(`${GAMMA_BASE}/markets/slug/${pending.slug}`, { timeout: 8000 });
      const market = Array.isArray(data) ? data[0] : data;

      if (!market || !market.closed) {
        // Non ancora risolto su Gamma, riprova al prossimo giro (fino a un timeout ragionevole)
        if (now - pending.signaledAtMs < 20 * 60 * 1000) {
          stillPending.push(pending);
        } else {
          console.warn(`[RECONCILE] ${pending.slug}: timeout, mercato non risolto dopo 20 min. Scarto senza contare P&L.`);
          logEvent({ type: 'reconcile_timeout', slug: pending.slug });
        }
        continue;
      }

      const outcomes = typeof market.outcomes === 'string' ? JSON.parse(market.outcomes) : market.outcomes;
      const outcomePrices = typeof market.outcomePrices === 'string' ? JSON.parse(market.outcomePrices) : market.outcomePrices;
      const upIdx = outcomes ? outcomes.indexOf('Up') : 0;
      const upWon = parseFloat(outcomePrices[upIdx]) === 1;
      const actualOutcome = upWon ? 'UP' : 'DOWN';

      const won = actualOutcome === pending.direction;
      // P&L reale: se vince, incasso (1 - ask) per ogni dollaro scommesso; se perde, perdo l'intero ask.
      const realPnl = won ? (1 - pending.ask) * pending.sizeUsdc : -pending.ask * pending.sizeUsdc;

      realizedPnL += realPnl;
      reconciledCount++;

      console.log(
        `[RECONCILE] ${pending.slug}: previsto ${pending.direction}, esito reale ${actualOutcome} -> ` +
        `${won ? 'VINTO' : 'PERSO'} | P&L reale: $${realPnl.toFixed(3)} | P&L cumulato: $${realizedPnL.toFixed(3)}`
      );
      logEvent({
        type: 'reconciled',
        slug: pending.slug,
        predictedDirection: pending.direction,
        actualOutcome,
        won,
        realPnl,
        evEstimatedAtSignal: pending.evEstimated,
        probEstimatedAtSignal: pending.prob,
        live: pending.live,
        cumulativeRealizedPnL: realizedPnL,
      });
    } catch (err) {
      console.warn(`[RECONCILE] Errore controllando ${pending.slug}: ${err.message}, riprovo al prossimo giro.`);
      stillPending.push(pending);
    }
  }

  pendingReconciliation = stillPending;
}

// ========== Refresh periodico del mercato attivo ==========
async function refreshMarketLoop() {
  let loopCount = 0;
  while (true) {
    const market = await fetchCurrentMarket();
    if (market && market.slug !== currentMarketSlug) {
      console.log(`\n[MERCATO] Nuova finestra attiva: ${market.slug}`);

      if (activeClobWs && activeClobWs.readyState === WebSocket.OPEN) {
        activeClobWs.close();
      }

      currentMarketSlug = market.slug;
      currentTokenIdUp = market.tokenIdUp;
      currentTokenIdDown = market.tokenIdDown;
      currentBestAskUp = null;
      currentBestAskDown = null;
      signalFiredThisWindow = false;
      // NB: priceToBeat NON si imposta qui. Lo cattura il rollover guidato da RTDS
      // (connectRTDS) al primo tick dopo l'apertura, così è il prezzo all'apertura
      // reale della finestra e non il valore campionato in ritardo da questo loop.

      if (currentTokenIdUp && currentTokenIdDown) {
        connectClobMarket(currentTokenIdUp, currentTokenIdDown);
      } else {
        console.warn('[WARN] Mercato trovato ma token IDs incompleti, salto questa finestra.');
      }
    }

    await reconcileClosedWindows();

    loopCount++;
    if (loopCount % 8 === 0 && reconciledCount > 0) {
      console.log(`\n[RIEPILOGO] ${reconciledCount} segnali riconciliati | P&L realizzato cumulato: $${realizedPnL.toFixed(3)} | ${pendingReconciliation.length} in attesa\n`);
    }

    await new Promise((r) => setTimeout(r, 15000));
  }
}

// ========== Main ==========
async function main() {
  console.log('=== Bot momentum BTC up/down — Polymarket ===');
  console.log(`Modalità: ${IS_LIVE ? 'LIVE (ordini reali!)' : 'DRY-RUN (nessun ordine reale)'}`);
  console.log(`Guardrail: max $${MAX_TRADE_SIZE_USDC}/trade, max ${MAX_TRADES_PER_HOUR} trade/ora, stop a -$${MAX_DAILY_LOSS_USDC}/giorno`);
  console.log(`Kill switch: crea il file '${KILL_SWITCH_FILE}' per fermare nuovi ordini in qualsiasi momento.\n`);

  if (IS_LIVE) {
    console.log('[ATTENZIONE] Modalità LIVE attiva. Gli ordini saranno reali e useranno fondi veri.');
    await initClobClient();
  }

  connectRTDS();
  await refreshMarketLoop();
}

process.on('SIGINT', () => {
  console.log('\nInterrotto dall\'utente. Chiudo il log...');
  logStream.end(() => process.exit(0));
});

main().catch((err) => {
  console.error('Errore fatale:', err.message);
  process.exit(1);
});
