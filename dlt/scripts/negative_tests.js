// E4: negative tests of the slice market (results appended to experiments/dlt/e4-negative.jsonl)
//  1 wrong amount -> contract revert "amount";  2 empty wallet -> rejected by the network (insufficient funds)
//  3 farm gateway asks the drone slice with no paid session -> CHF 403;  4 closed/used credit does not open a session
const { ethers } = require("ethers");
const http2 = require("http2");
const fs = require("fs"), os = require("os"), path = require("path");
const dir = path.join(os.homedir(), ".config/smartfarm");
const cfg = JSON.parse(fs.readFileSync(path.join(dir, "dlt-config.json")));
const w = JSON.parse(fs.readFileSync(path.join(dir, "dlt-wallets.json")));
const ids = JSON.parse(fs.readFileSync(path.join(dir, "ue-identities.json")));
const supi = (r) => (ids[r].supi.startsWith("imsi-") ? ids[r].supi : `imsi-${ids[r].supi}`);
const out = path.join(__dirname, "../../experiments/dlt/e4-negative.jsonl");
const rec = (o) => { const r = { at: new Date().toISOString(), ...o }; console.log(JSON.stringify(r)); fs.appendFileSync(out, JSON.stringify(r) + "\n"); };
const ABI = ["function purchase(uint8,bytes32,bool) payable returns (uint256)", "function quote(uint8,bool) view returns (uint256)"];
function nchf(body) {
  return new Promise((res) => {
    const c = http2.connect("http://127.0.0.30:7777");
    const s = c.request({ ":method": "POST", ":path": "/nchf-convergedcharging/v3/chargingdata", "content-type": "application/json" });
    let st = 0, d = ""; s.on("response", (h) => (st = h[":status"])); s.on("data", (x) => (d += x));
    s.on("end", () => { c.close(); res({ status: st, body: d }); }); s.end(JSON.stringify(body));
  });
}
const req = (who, dnn, sd) => ({ subscriberIdentifier: who, chargingId: 999, nfConsumerIdentification: { nodeFunctionality: "SMF" },
  pDUSessionChargingInformation: { pduSessionInformation: { pduSessionID: 9, dnnId: dnn, networkSlicingInfo: { sNSSAI: { sst: 1, sd } } } } });
(async () => {
  const p = new ethers.JsonRpcProvider(cfg.rpc);
  const sub = ethers.keccak256(ethers.toUtf8Bytes(supi("oneplus_8t")));
  const cli = new ethers.Contract(cfg.contract, ABI, new ethers.Wallet(w.cli.privateKey, p));
  const price = await cli.quote(3, false);
  try { await cli.purchase.staticCall(3, sub, false, { value: price - 1n }); rec({ test: "wrong_amount", result: "ACCEPTED (unexpected)" }); }
  catch (e) { rec({ test: "wrong_amount", sentWei: (price - 1n).toString(), priceWei: price.toString(), result: "rejected", reason: e.reason || e.shortMessage }); }
  const empty = ethers.Wallet.createRandom().connect(p);
  try { await cli.connect(empty).purchase(3, sub, false, { value: price }); rec({ test: "empty_wallet", result: "ACCEPTED (unexpected)" }); }
  catch (e) { rec({ test: "empty_wallet", wallet: empty.address, result: "rejected", reason: (e.shortMessage || e.message).slice(0, 120) }); }
  const farm = await nchf(req(supi("un3gpp_ue"), "internet", "000003"));
  rec({ test: "farm_without_paid_session", slice: "000003", http: farm.status, body: JSON.parse(farm.body || "{}").multipleUnitInformation });
  const phone = await nchf(req(supi("oneplus_8t"), "drone", "000003"));
  rec({ test: "phone_drone_without_unused_purchase", http: phone.status, body: JSON.parse(phone.body || "{}").multipleUnitInformation });
})().catch((e) => { console.error(e); process.exit(1); });
