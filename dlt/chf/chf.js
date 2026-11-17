#!/usr/bin/env node
/*
 * Smart Farming CHF (Charging Function) with a DLT account balance ledger.
 *
 * 3GPP NF: registers in the Open5GS NRF (Nnrf_NFManagement, nfType CHF, heartbeat, deregistration) and serves
 * Nchf_ConvergedCharging v3 (TS 32.291: chargingdata create / update / release) over HTTP/2 cleartext (h2c).
 * Rating and account balance (RF/ABMF of the CCS) live on Ethereum: the SliceMarket contract prices each slice and
 * moves the payment from the UE wallet to the operator wallet. A quota is granted only for a confirmed, unused
 * SlicePurchased event of the same subscriber and slice; the farm gateway (UN3GPP UE) is granted only while a paid
 * session of the same service is active. Charging data records (CDRs) are appended as JSONL.
 */
const http2 = require("http2");
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { ethers } = require("ethers");

const CFG_DIR = path.join(os.homedir(), ".config/smartfarm");
const DLT = JSON.parse(fs.readFileSync(process.env.CHF_DLT_CONFIG || path.join(CFG_DIR, "dlt-config.json")));
const IDS = JSON.parse(fs.readFileSync(path.join(CFG_DIR, "ue-identities.json")));
const NRF = process.env.CHF_NRF || "http://127.0.0.10:7777";
const HOST = process.env.CHF_ADDR || "127.0.0.30";
const PORT = Number(process.env.CHF_PORT || 7777);
const STATUS_PORT = Number(process.env.CHF_STATUS_PORT || 8096);
const CONFIRMATIONS = Number(process.env.CHF_CONFIRMATIONS || DLT.confirmations || 2);
const VALIDITY_S = Number(process.env.CHF_PURCHASE_VALIDITY_S || 1800);
const OUT = process.env.CHF_OUT || path.join(__dirname, "../../experiments/chf");
const STATE = path.join(OUT, `chf-state-${DLT.contract.slice(2, 10).toLowerCase()}.json`);
const NF_ID = process.env.CHF_NF_ID || crypto.randomUUID();
const RATING = { weather: 1, crop: 2, drone: 3 };              // DNN -> slice id / rating group
// farm gateway: slice -> service; SD 000001 is also its default slice (weather shares it), so only crop and drone gate
const SD_SERVICE = { "000002": "crop", "000003": "drone" };
const SNSSAI = [{ sst: 1, sd: "000001" }, { sst: 1, sd: "000002" }, { sst: 1, sd: "000003" }];
const FARM = IDS.un3gpp_ue.supi.startsWith("imsi-") ? IDS.un3gpp_ue.supi : `imsi-${IDS.un3gpp_ue.supi}`;

fs.mkdirSync(OUT, { recursive: true });
const ABI = ["event SlicePurchased(uint256 indexed purchaseId, address indexed buyer, bytes32 indexed subscriber, uint8 sliceId, bool adaptive, uint256 amount, uint32 sessionSeconds)",
  "function quote(uint8,bool) view returns (uint256)",
  "event Charged(uint256 indexed purchaseId, uint8 minute, uint256 amount, uint256 remaining)",
  "event Closed(uint256 indexed purchaseId, uint256 used, uint256 refunded)",
  "function charge(uint256)", "function close(uint256)"];
const provider = new ethers.JsonRpcProvider(DLT.rpc, DLT.chainId, { staticNetwork: true });
const market = new ethers.Contract(DLT.contract, ABI, provider);
// metered charging (SliceMarketV2): the CHF signs charge/close with its own agent key
const METERED = DLT.version === 2;
const agentKey = METERED ? JSON.parse(fs.readFileSync(path.join(CFG_DIR, "dlt-wallets.json"))).chf.privateKey : null;
const agentMarket = METERED ? market.connect(new ethers.NonceManager(new ethers.Wallet(agentKey, provider))) : null;
let txQueue = Promise.resolve();
const meters = new Map();                                      // purchaseId -> { timer, minutes }
function agentTx(kind, id) {
  txQueue = txQueue.then(async () => {
    const t0 = Date.now();
    const tx = await agentMarket[kind](id);
    log(`[DLT] ${kind} purchase ${id} sent ${tx.hash}`);
    const rc = await tx.wait(1);
    log(`[DLT] ${kind} purchase ${id} mined block ${rc.blockNumber} in ${((Date.now() - t0) / 1000).toFixed(1)} s`);
    cdr({ event: kind, purchase: id, tx: tx.hash, block: rc.blockNumber });
  }).catch((e) => log(`[DLT] ${kind} purchase ${id} failed: ${e.shortMessage || e.message}`));
  return txQueue;
}
const CLOSE_GRACE_MS = 90000;   // a dropped radio link may re-establish the paid session within this time
function startMeter(p) {
  if (!METERED || (state.closed || {})[p.id]) return;
  let m = meters.get(p.id);
  if (m && m.closeTimer) { clearTimeout(m.closeTimer); m.closeTimer = null; log(`[CHF] purchase ${p.id} resumed`); }
  if (m && m.timer) return;
  if (!m) { m = { minutes: 0, timer: null, closeTimer: null }; meters.set(p.id, m); }
  const tick = () => {
    if (m.minutes >= 10) { clearInterval(m.timer); return; }
    m.minutes += 1; agentTx("charge", p.id);
  };
  if (m.minutes === 0) tick();
  m.timer = setInterval(tick, 60000);
}
function stopMeter(id) {
  const m = meters.get(id);
  if (!m) return;
  clearInterval(m.timer); m.timer = null;
  m.closeTimer = setTimeout(() => {
    meters.delete(id);
    state.closed = state.closed || {}; state.closed[id] = true; save();
    agentTx("close", id);
  }, CLOSE_GRACE_MS);
  log(`[CHF] purchase ${id} paused after ${m.minutes} min; closes in ${CLOSE_GRACE_MS / 1000} s unless re-established`);
}
const state = fs.existsSync(STATE) ? JSON.parse(fs.readFileSync(STATE)) : { fromBlock: DLT.deployBlock, purchases: {}, consumed: {} };
const sessions = new Map();                                    // ChargingDataRef -> open charging session
let head = 0;

const log = (...a) => console.log(new Date().toISOString(), ...a);
const save = () => fs.writeFileSync(STATE, JSON.stringify(state, null, 1));
const cdr = (rec) => fs.appendFileSync(path.join(OUT, "cdrs.jsonl"), JSON.stringify({ at: new Date().toISOString(), ...rec }) + "\n");
const subscriberHash = (supi) => ethers.keccak256(ethers.toUtf8Bytes(supi));

/* ---------------- DLT watcher: SlicePurchased events ---------------- */
async function poll() {
  try {
    head = await provider.getBlockNumber();
    if (head >= state.fromBlock) {
      const to = Math.min(head, state.fromBlock + 2000);
      for (const ev of await market.queryFilter(market.filters.SlicePurchased(), state.fromBlock, to)) {
        const id = ev.args.purchaseId.toString();
        if (!state.purchases[id]) {
          const block = await ev.getBlock();
          state.purchases[id] = { id, tx: ev.transactionHash, block: ev.blockNumber, time: block.timestamp,
            buyer: ev.args.buyer, subscriber: ev.args.subscriber, sliceId: Number(ev.args.sliceId),
            adaptive: ev.args.adaptive, amountWei: ev.args.amount.toString(), sessionSeconds: Number(ev.args.sessionSeconds) };
          log(`[DLT] purchase ${id} slice ${ev.args.sliceId} ${ethers.formatEther(ev.args.amount)} ETH tx ${ev.transactionHash}`);
        }
      }
      if (METERED) {
        state.charges = state.charges || {};
        for (const ev of await market.queryFilter(market.filters.Charged(), state.fromBlock, to)) {
          const id = ev.args.purchaseId.toString();
          (state.charges[id] = state.charges[id] || []).some((c) => c.tx === ev.transactionHash) ||
            state.charges[id].push({ minute: Number(ev.args.minute), amountWei: ev.args.amount.toString(),
              remainingWei: ev.args.remaining.toString(), tx: ev.transactionHash, block: ev.blockNumber });
        }
        for (const ev of await market.queryFilter(market.filters.Closed(), state.fromBlock, to)) {
          const id = ev.args.purchaseId.toString();
          if (state.purchases[id]) state.purchases[id].closedOnChain = { usedWei: ev.args.used.toString(),
            refundedWei: ev.args.refunded.toString(), tx: ev.transactionHash, block: ev.blockNumber };
        }
      }
      state.fromBlock = to + 1;
      save();
    }
  } catch (e) { log("[DLT] poll error", e.shortMessage || e.message); }
  setTimeout(poll, 3000);
}

function findPurchase(supi, sliceId) {
  const sub = subscriberHash(supi), now = Date.now() / 1000;
  const open = [...sessions.values()].some((s) => s.kind === "paid" && s.supi === supi && s.purchase.sliceId === sliceId);
  return Object.values(state.purchases).find((p) => p.subscriber === sub && p.sliceId === sliceId
    && head - p.block + 1 >= CONFIRMATIONS && now - p.time <= VALIDITY_S
    // unused, or re-establishment of the same paid session within its paid time
    && !(state.closed || {})[p.id]
    && (!state.consumed[p.id] || (!open && Date.now() - state.consumed[p.id].at < p.sessionSeconds * 1000)));
}

/* ---------------- Nchf_ConvergedCharging ---------------- */
function grant(ref, rg, seconds, extra) {
  return { invocationTimeStamp: new Date().toISOString(), invocationSequenceNumber: 0,
    multipleUnitInformation: [{ resultCode: "SUCCESS", ratingGroup: rg, grantedUnit: { time: seconds }, validityTime: seconds }], ...extra };
}
function deny(rg, cause) {
  return { invocationTimeStamp: new Date().toISOString(), invocationSequenceNumber: 0,
    invocationResult: { error: { status: 403, cause }, failureHandling: "TERMINATE" },
    multipleUnitInformation: [{ resultCode: cause, ratingGroup: rg }] };
}

function create(req) {
  const supi = req.subscriberIdentifier;
  const info = req.pDUSessionChargingInformation?.pduSessionInformation || {};
  // the phone selects the service by DNN; the farm gateway (UN3GPP UE) keeps DNN "internet" and changes the slice
  const sd = (info.networkSlicingInfo?.sNSSAI?.sd || "").toLowerCase();
  const dnn = supi === FARM ? (SD_SERVICE[sd] || info.dnnId) : info.dnnId, rg = RATING[dnn] || 0;
  const ref = crypto.randomUUID();
  const base = { ref, supi, dnn, snssai: info.networkSlicingInfo?.sNSSAI, psi: info.pduSessionID, chargingId: req.chargingId, openedAt: Date.now() };
  if (supi === FARM && !SD_SERVICE[sd]) {                     // farm gateway on its default slice
    sessions.set(ref, { ...base, kind: "zero-rated" });
    cdr({ event: "open", kind: "zero-rated", ...base });
    return [201, grant(ref, 0, 86400), ref];
  }
  if (!rg) {                                                   // default DNNs (phone, internet): not charged here
    sessions.set(ref, { ...base, kind: "zero-rated" });
    cdr({ event: "open", kind: "zero-rated", ...base });
    return [201, grant(ref, 0, 86400), ref];
  }
  if (supi === FARM) {                                         // farm gateway follows a paid session of the service
    const paid = [...sessions.values()].find((s) => s.kind === "paid" && s.dnn === dnn);
    if (!paid) { cdr({ event: "deny", reason: "no paid session for this service", ...base }); return [403, deny(rg, "END_USER_SERVICE_DENIED")]; }
    sessions.set(ref, { ...base, kind: "farm", paidRef: paid.ref });
    cdr({ event: "open", kind: "farm", paidRef: paid.ref, purchase: paid.purchase.id, ...base });
    return [201, grant(ref, rg, paid.purchase.sessionSeconds), ref];
  }
  const p = findPurchase(supi, rg);
  if (!p) { cdr({ event: "deny", reason: "no confirmed unused purchase", headBlock: head, ...base }); return [403, deny(rg, "QUOTA_LIMIT_REACHED")]; }
  if (!state.consumed[p.id]) { state.consumed[p.id] = { ref, at: Date.now() }; save(); }
  sessions.set(ref, { ...base, kind: "paid", purchase: p });
  startMeter(p);
  cdr({ event: "open", kind: "paid", purchase: p, confirmations: head - p.block + 1, ...base });
  log(`[CHF] grant ${supi.slice(0, 9)}… ${dnn} purchase ${p.id} tx ${p.tx}`);
  return [201, grant(ref, rg, p.sessionSeconds), ref];
}

function release(ref) {
  const s = sessions.get(ref);
  if (!s) return 404;
  sessions.delete(ref);
  if (s.kind === "paid") stopMeter(s.purchase.id);
  cdr({ event: "close", kind: s.kind, ref, supi: s.supi, dnn: s.dnn, usedSeconds: Math.round((Date.now() - s.openedAt) / 1000),
    purchase: s.purchase?.id ?? (s.paidRef ? sessions.get(s.paidRef)?.purchase?.id : undefined) });
  return 204;
}

const server = http2.createServer();
server.on("stream", (stream, hdr) => {
  const p = hdr[":path"], method = hdr[":method"];
  let body = "";
  stream.on("data", (c) => (body += c));
  stream.on("end", () => {
    const send = (status, obj, extra = {}) => {
      stream.respond({ ":status": status, ...(obj ? { "content-type": "application/json" } : {}), ...extra });
      stream.end(obj ? JSON.stringify(obj) : undefined);
    };
    try {
      const m = p.match(/^\/nchf-convergedcharging\/v3\/chargingdata(?:\/([^/]+)\/(update|release))?$/);
      if (!m || method !== "POST") return send(404, { title: "Not Found", status: 404 });
      const req = body ? JSON.parse(body) : {};
      if (!m[1]) {
        const [status, res, ref] = create(req);
        log(`[Nchf] create ${req.subscriberIdentifier} ${req.pDUSessionChargingInformation?.pduSessionInformation?.dnnId} -> ${status}`);
        return send(status, res, ref ? { location: `http://${HOST}:${PORT}/nchf-convergedcharging/v3/chargingdata/${ref}` } : {});
      }
      if (m[2] === "update") {
        const s = sessions.get(m[1]);
        return s ? send(200, grant(m[1], RATING[s.dnn] || 0, 60)) : send(404, { title: "Not Found", status: 404 });
      }
      const st = release(m[1]);
      log(`[Nchf] release ${m[1]} -> ${st}`);
      return send(st, null);
    } catch (e) { send(400, { title: "Bad Request", status: 400, detail: e.message }); }
  });
});

/* ---------------- NRF registration ---------------- */
const nrf = http2.connect(NRF);
nrf.on("error", (e) => log("[NRF] connection error", e.message));
function nrfRequest(method, p, body, ctype = "application/json") {
  return new Promise((resolve, reject) => {
    const s = nrf.request({ ":method": method, ":path": p, ...(body ? { "content-type": ctype } : {}) });
    let data = "", status = 0;
    s.on("response", (h) => (status = h[":status"]));
    s.on("data", (c) => (data += c));
    s.on("end", () => resolve({ status, data }));
    s.on("error", reject);
    s.end(body ? JSON.stringify(body) : undefined);
  });
}
const profile = { nfInstanceId: NF_ID, nfType: "CHF", nfStatus: "REGISTERED", heartBeatTimer: 10, ipv4Addresses: [HOST],
  plmnList: [{ mcc: "001", mnc: "01" }], sNssais: SNSSAI,
  nfServices: [{ serviceInstanceId: "nchf-convergedcharging", serviceName: "nchf-convergedcharging",
    versions: [{ apiVersionInUri: "v3", apiFullVersion: "3.1.0" }], scheme: "http", nfServiceStatus: "REGISTERED",
    ipEndPoints: [{ ipv4Address: HOST, transport: "TCP", port: PORT }], allowedNfTypes: ["SMF"] }] };
let hb = 10;
async function register() {
  const r = await nrfRequest("PUT", `/nnrf-nfm/v1/nf-instances/${NF_ID}`, profile);
  try { hb = JSON.parse(r.data).heartBeatTimer || hb; } catch {}
  log(`[NRF] NFRegister CHF ${NF_ID}: HTTP ${r.status} (heartbeat ${hb} s)`);
  if (r.status !== 200 && r.status !== 201) throw new Error(`NFRegister failed: ${r.status} ${r.data}`);
}
async function heartbeat() {
  try {
    const r = await nrfRequest("PATCH", `/nnrf-nfm/v1/nf-instances/${NF_ID}`, [{ op: "replace", path: "/nfStatus", value: "REGISTERED" }], "application/json-patch+json");
    if (r.status === 404) await register();
  } catch (e) { log("[NRF] heartbeat error", e.message); }
  setTimeout(heartbeat, hb * 1000);
}
async function shutdown() {
  try { const r = await nrfRequest("DELETE", `/nnrf-nfm/v1/nf-instances/${NF_ID}`); log(`[NRF] NFDeregister: HTTP ${r.status}`); } catch {}
  process.exit(0);
}
process.on("SIGINT", shutdown); process.on("SIGTERM", shutdown);

/* ---------------- status (operator dashboard, app wallet relay) ---------------- */
http.createServer(async (req, res) => {
  const q = await Promise.all([1, 2, 3].map((i) => market.quote(i, false))).catch(() => []);
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify({ nfInstanceId: NF_ID, chain: { rpc: DLT.rpc, chainId: DLT.chainId, contract: DLT.contract,
    explorer: DLT.explorer, head, confirmations: CONFIRMATIONS }, operator: DLT.operator,
    prices: Object.fromEntries(Object.entries(RATING).map(([k, i]) => [k, q[i - 1] ? ethers.formatEther(q[i - 1]) : null])),
    sessions: [...sessions.values()], metered: METERED, purchases: Object.values(state.purchases).map((p) => ({ ...p, consumed: !!state.consumed[p.id],
      charges: (state.charges || {})[p.id] || [], metering: meters.has(p.id) })) }, null, 1));
}).listen(STATUS_PORT, "127.0.0.1");

server.listen(PORT, HOST, async () => {
  log(`[CHF] Nchf_ConvergedCharging on http://${HOST}:${PORT} (h2c); contract ${DLT.contract} on chain ${DLT.chainId}`);
  await register(); setTimeout(heartbeat, hb * 1000); poll();
});
