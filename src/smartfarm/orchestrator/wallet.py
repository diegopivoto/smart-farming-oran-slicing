"""Wallet gateway of the DLT slice market for the Smart Farm app (branch dlt-slice-market).

The phone reaches only the operator network (10.45.0.1), so the orchestrator API gives the app:

    GET  /api/v1/wallet/quote?service=drone   contract, chain, slice id, price (read from the contract) and the
                                              subscriber hash (keccak256 of the phone SUPI) to put in the purchase
    POST /api/v1/wallet/rpc                   JSON-RPC relay to the Ethereum node (allow-listed methods only); the app
                                              signs its transactions locally, the key never leaves the phone
    GET  /api/v1/wallet/status[?address=0x]   CHF view: prices, chain head, purchases (with confirmations), charging
                                              sessions; filtered by buyer when address is given

Charging itself is enforced in the core: the SMF asks the CHF (Nchf_ConvergedCharging) for a quota before it
establishes the service PDU session, and the CHF grants it only for a confirmed SlicePurchased event.
"""

import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from smartfarm.contracts.service import ServiceId

CONFIG = Path.home() / ".config/smartfarm"
CHF_STATUS = "http://127.0.0.1:8096/"
SLICE_ID = {ServiceId.WEATHER: 1, ServiceId.CROP: 2, ServiceId.DRONE: 3}
RPC_METHODS = {"eth_chainId", "net_version", "eth_blockNumber", "eth_getBalance", "eth_getTransactionCount",
               "eth_gasPrice", "eth_maxPriorityFeePerGas", "eth_feeHistory", "eth_estimateGas", "eth_call",
               "eth_sendRawTransaction", "eth_getTransactionReceipt", "eth_getTransactionByHash", "eth_getLogs",
               "eth_getBlockByNumber"}
DLT_DIR = Path(__file__).resolve().parents[3] / "dlt"


def _post(url: str, payload: Any, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {"content-type": "application/json", "user-agent": "smartfarm-wallet-gateway/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed operator URLs
        return json.loads(resp.read())


class WalletGateway:
    def __init__(self, config_dir: Path = CONFIG) -> None:
        self.dlt = json.loads((config_dir / "dlt-config.json").read_text())
        ids = json.loads((config_dir / "ue-identities.json").read_text())
        supi = ids["oneplus_8t"]["supi"]
        self.phone_supi = supi if supi.startswith("imsi-") else f"imsi-{supi}"
        self.subscriber = self._keccak(self.phone_supi)
        self.quote_selector = self._keccak("quote(uint8,bool)")[:10]

    def _keccak(self, text: str) -> str:
        """keccak256 of a UTF-8 string with the ethers.js of the dlt/ project (no Python Ethereum dependency)."""

        out = subprocess.run(["node", "-e", "const {ethers}=require('ethers');"
                              "process.stdout.write(ethers.keccak256(ethers.toUtf8Bytes(process.argv[1])))", text],
                             cwd=DLT_DIR, capture_output=True, text=True, check=True, timeout=20)
        return out.stdout.strip()

    def quote(self, service: str, adaptive: bool = False) -> tuple[int, dict[str, Any]]:
        try:
            sid = ServiceId(service)
        except ValueError:
            return 400, {"error": "service must be weather, crop or drone"}
        slice_id = SLICE_ID[sid]
        data = self.quote_selector + f"{slice_id:064x}" + f"{int(adaptive):064x}"
        res = _post(self.dlt["rpc"], {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                      "params": [{"to": self.dlt["contract"], "data": data}, "latest"]})
        return 200, {"service": sid.value, "slice_id": slice_id, "adaptive": adaptive,
                     "amount_wei": str(int(res["result"], 16)), "subscriber": self.subscriber,
                     "contract": self.dlt["contract"], "chain_id": self.dlt["chainId"],
                     "operator": self.dlt["operator"], "explorer": self.dlt.get("explorer"),
                     "confirmations": self.dlt.get("confirmations", 2), "session_seconds": 600}

    def rpc(self, body: bytes) -> tuple[int, Any]:
        try:
            req = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"error": "body must be JSON-RPC"}
        calls = req if isinstance(req, list) else [req]
        if not calls or any(not isinstance(c, dict) or c.get("method") not in RPC_METHODS for c in calls):
            return 403, {"error": "JSON-RPC method not allowed"}
        return 200, _post(self.dlt["rpc"], req, timeout=20.0)

    def status(self, address: str | None = None) -> tuple[int, dict[str, Any]]:
        with urllib.request.urlopen(CHF_STATUS, timeout=5.0) as resp:  # noqa: S310 - local CHF
            view = json.loads(resp.read())
        head = view.get("chain", {}).get("head", 0)
        for p in view.get("purchases", []):
            p["confirmations"] = max(0, head - p["block"] + 1)
        if address:
            view["purchases"] = [p for p in view["purchases"] if p["buyer"].lower() == address.lower()]
            view["sessions"] = [s for s in view.get("sessions", []) if s.get("supi") == self.phone_supi]
        for s in view.get("sessions", []):
            s.pop("supi", None)  # identifiers stay in the core
        view["subscriber"] = self.subscriber
        return 200, view
