const WebSocket = require('ws');
const { HttpsProxyAgent } = require('https-proxy-agent');

// 1. CONFIGURA I DATI DEL TUO PROXY AZIENDALE
// Se utente o password contengono caratteri speciali (es. @, #, $), convertili in URL-encoded (es. @ diventa %40)
const proxyUser = 'paiella';
const proxyPassword = 'Samarcanda2028%23';
const proxyHost = '10.109.11.13';
const proxyPort = '8080'; // Sostituisci con la porta corretta (es. 8080, 3128)

const proxyUrl = `http://${proxyUser}:${proxyPassword}@${proxyHost}:${proxyPort}`;

console.log('Inizializzazione dell\'agente proxy...');
const agent = new HttpsProxyAgent(proxyUrl);

// 2. OPZIONI DI CONNESSIONE
const options = {
    agent: agent,
    // Rimuovi il commento dalla riga sotto SOLO se ricevi un errore di tipo "UNABLE_TO_VERIFY_LEAF_SIGNATURE"
    // rejectUnauthorized: false 
};

// 3. APERTURA DEL WEBSOCKET VERSO POLYMARKET
const ws = new WebSocket('wss://ws-live-data.polymarket.com', options);

// Gestione dell'evento di avvenuta connessione
ws.on('open', () => {
    console.log(' Connesso con successo al WebSocket di Polymarket via Proxy!');
    
    // Esempio: Invia un messaggio di Ping per mantenere attiva la connessione se richiesto dal server
    // ws.send(JSON.stringify({ type: 'ping' }));
});

// Ascolto dei dati in tempo reale inviati da Polymarket
ws.on('message', (data) => {
    console.log('Dati ricevuti:', data.toString());
});

// Gestione degli errori di rete o autenticazione
ws.on('error', (error) => {
    console.error('❌ Errore durante la connessione WebSocket:');
    console.error(error.message);
    
    if (error.message.includes('407')) {
        console.error('👉 Suggerimento: Errore 407 significa che le credenziali del proxy sono errate o non accettate.');
    } else if (error.message.includes('certificate')) {
        console.error('👉 Suggerimento: Il proxy blocca il certificato SSL. Attiva "rejectUnauthorized: false" nelle opzioni.');
    }
});

// Gestione della chiusura del canale
ws.on('close', (code, reason) => {
    console.log(` Connessione chiusa. Codice: ${code}, Motivo: ${reason}`);
});
