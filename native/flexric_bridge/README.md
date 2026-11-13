# Native FlexRIC KPM bridge

This xApp is the read-only boundary between the pinned FlexRIC SDK and the
Smart Farming telemetry pipeline. It subscribes to KPM v2.03 report styles
advertised by the OAI E2 node and emits one versioned JSON object per line. It
does not construct or send E2 Control messages.

## Data path and backpressure

The FlexRIC callback copies bounded value objects into an in-memory queue and
returns. It never performs JSON encoding, socket/file I/O, allocation, sleeps,
or lock waits. Producer admission uses a non-blocking try gate: contention
between callback producers or a full 64-record queue drops the candidate
record instead of delaying FlexRIC. The single consumer does not take that
gate, so writer dequeue activity cannot cause a callback drop. The writer
thread performs JSON encoding and output I/O outside the callback.

The stderr health line reports:

- `received`: envelopes offered by the indication adapter, including
  explicit malformed-indication drop events;
- `enqueued`: envelopes accepted by the bounded queue;
- `dropped`: full, busy, or malformed adapter events;
- `unknown`: records whose names are outside the seven-name allowlist;
- `encoded`, `encode_errors`, and `output_errors`: writer-side results.

Every attempted envelope consumes a sequence number before queue admission.
Consequently, a downstream sequence gap is evidence of local telemetry loss
and can be reconciled with `dropped`. A new process creates a fresh UUID
`connection_epoch`.

## Measurements

The bridge requests only desired measurements that the E2 node advertises:

| KPM name | Unit emitted |
| --- | --- |
| `DRB.PdcpSduVolumeDL` | `Mbit` |
| `DRB.RlcSduDelayDl` | `us` |
| `DRB.UEThpDl` | `kbit/s` |
| `RRU.PrbTotDl` | `percent` |
| `CARR.PDSCHMCSDist` | `PDSCH_RBs` |
| `L1M.SS-RSRP` | `dBm` |
| `MR.NRScSSSINR` | `dB` |

KPM `noValue` is emitted as `kind="no_value", value=null`; it is never
converted to zero. The standard incomplete-record flag and distribution,
slice, QoS, PLMN, SSB, and MIMO labels used by OAI are copied when present.
Cell-level Format 1 records have `ue_id=null`; UE-level Format 3 records carry
a stable typed UE identifier.

The Style 4 subscription uses the project SST `1` without an SD filter. This
matches all three project slices (`000001`, `000002`, and `000003`) and
avoids re-subscribing when the active service changes. Slice/run attribution is
completed by the collector against the active orchestration state.

## Reproducible build

The SDK path and build path must refer to the verified FlexRIC pin:

```bash
cmake -S native/flexric_bridge -B .build/flexric-bridge -DBUILD_TESTING=ON -DBUILD_FLEXRIC_BRIDGE=ON -DFLEXRIC_SOURCE_DIR=/home/inatel-crr/5gc-un3gpp/.worktrees/openairinterface5g-e2-f8f7695/openair2/E2AP/flexric -DFLEXRIC_BUILD_DIR=/home/inatel-crr/5gc-un3gpp/smart-farming-oran/.build/flexric-e2ap-v2-kpm-v2.03
cmake --build .build/flexric-bridge --parallel
ctest --test-dir .build/flexric-bridge --output-on-failure
```

The bridge links the upstream static xApp SDK because this pinned revision's
shared SDK omits required ASN.1 link dependencies. The linked upstream
`conf_file.c.o` also marks the resulting stack executable; this inherited
warning remains an explicit live-deployment risk and is not hidden.

## Runtime output

With no extra environment variable, JSONL is written to the original stdout.
FlexRIC's stdout is redirected to stderr so it cannot corrupt the stream. To
send JSONL to a collector's Unix stream socket instead:

```bash
SMARTFARM_KPM_SOCKET=/run/smartfarm/kpm.sock .build/flexric-bridge/smartfarm_kpm_bridge -c deployment/flexric/flexric.conf -p .build/flexric-e2ap-v2-kpm-v2.03/stage-root/usr/local/lib/flexric
```

The socket server must already exist. The current architecture intentionally
selects the first E2 node advertising KPM and reports an error if none exists;
when multiple KPM nodes are present it warns and keeps the first so callbacks
cannot be attributed to the wrong node. The lab design has one OAI gNB.
Before failing, it waits up to 600 seconds by default. Set
`SMARTFARM_E2_DISCOVERY_TIMEOUT_S` to an integer from 1 through 86400 when a
longer coordinated radio startup window is required.

Building this target does not start the near-RT RIC, xApp, gNB, or any UE.
