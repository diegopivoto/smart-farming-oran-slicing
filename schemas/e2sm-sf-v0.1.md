# Perfil experimental E2SM-SF/0.1 (Smart Farming)

- Identificador: `urn:inatel:smartfarm:e2sm-sf:0.1`, revisão 1.
- **Não** é O-RAN E2SM-RC nem um service model de slicing padronizado. Não há
  OID registrado. É um perfil de pesquisa transportado por um service model
  customizado já existente no FlexRIC.
- Transporte: service model customizado `SLICE` do FlexRIC (RAN Function ID
  145, OID `0.0.0.0.0.0.0.0.1.145.0`, codificação PLAIN), em E2AP v2, sem
  alterar o codec do FlexRIC fixado (`d7a71285`). Por isso os plugins do RIC
  instalados no gate KPM permanecem byte a byte iguais.

## Por que transportar em `label`

O SLICE SM já serializa, para cada fatia DL, um campo `label` de texto livre.
O perfil usa esse campo para levar pedidos e readback. A alternativa prevista
no plano original — estender o ASN.1/codec do SLICE SM — exigiria reconstruir
e reinstalar os plugins `lib*_sm.so` usados pelo RIC e pelo agente,
invalidando a instalação certificada no gate FlexRIC/OAI. A semântica é a
mesma; muda apenas a codificação.

## Gramática

Texto ASCII imprimível, sem espaços, até 511 bytes, pares `chave=valor`
separados por `;`. O primeiro par é sempre `sf=0.1`; o segundo é o seletor
(`op=` em pedidos, `kind=` em readback). Chaves desconhecidas, duplicadas ou
ausentes invalidam a mensagem inteira.

| Campo | Formato |
|---|---|
| `action_id`, `target_action_id` | decimal, uint64, ≥ 1 |
| `run_id` | 32 dígitos hex (UUID sem hífens) |
| `sst` | decimal 0–255 |
| `sd` | exatamente 6 dígitos hex; `ffffff` = sem SD |
| `rnti` | decimal 0–65535; `0` = todas as UEs do slice |
| `prb` | decimal, somente 24, 52, 78 ou 106 |
| `ttl_ms` | decimal 1–30000 |
| `issued_at_ns`, `expires_at_ns`, `at_ns` | decimal, CLOCK_REALTIME em ns |
| `token` | exatamente 16 dígitos hex |

### Pedidos (xApp → gNB, SLICE `ADD`, uma fatia DL por pedido)

```
sf=0.1;op=prb_quota;action_id=…;run_id=…;sst=…;sd=…;rnti=…;prb=…;ttl_ms=…;issued_at_ns=…
sf=0.1;op=rollback;action_id=…;run_id=…;target_action_id=…;token=…;ttl_ms=…;issued_at_ns=…
```

### Readback (gNB → xApp, indication SLICE)

Assinado a 10 ms (mínimo aceito pelo SLICE SM do FlexRIC); a gNB só emite a
5 Hz ou imediatamente após um outcome novo.

```
sf=0.1;kind=counters;lookups=…;unmapped=…;expired=…;fallback=…;capped=…;bypass_retx=…;version=…
sf=0.1;kind=rule;sst=…;sd=…;rnti=…;prb=…;action_id=…;expires_at_ns=…;version=…
sf=0.1;kind=outcome;action_id=…;status=…;dup=0|1;sst=…;sd=…;rnti=…;requested=…;applied=…;previous=…;token=…;expires_at_ns=…;version=…;at_ns=…
sf=0.1;kind=ue;rnti=…;sst=…;sd=…;prb=…;action_id=…
```

## Semântica na gNB

- A cota limita apenas **novas transmissões** DL dos candidatos cujo primeiro
  DRB pertence à S-NSSAI alvo (ou ao RNTI alvo). Retransmissões HARQ mantêm o
  tamanho original (`bypass_retx`). Carrier/BWP permanecem em 106 PRBs.
- Sem regra, ou com regra expirada, vale 106 (sem limite). A expiração por TTL
  não depende de nova mensagem E2: o RL precisa renovar a cota (lease) a cada
  ≤ 30 s.
- Regra por RNTI tem precedência sobre regra do slice. Até 4 regras vivas.
- `action_id` repetido com o mesmo conteúdo devolve o outcome original
  (`dup=1`) sem reaplicar. Com conteúdo diferente, devolve `CONFLICT` sem
  alterar nada.
- Pedidos com `issued_at_ns` mais de 1 s no futuro, ou mais antigos que o TTL,
  são `REJECTED_STALE`.
- O rollback exige o `token` devolvido no outcome original. Ele só desfaz se a
  regra vigente ainda for daquela ação (senão `ROLLBACK_SUPERSEDED`) e restaura
  a regra anterior ou remove a regra.
- Status: `APPLIED`, `ROLLED_BACK`, `REJECTED_RANGE`, `REJECTED_TTL`,
  `REJECTED_STALE`, `REJECTED_TARGET`, `REJECTED_RUN`, `CONFLICT`,
  `TABLE_FULL`, `ROLLBACK_UNKNOWN`, `ROLLBACK_TOKEN`, `ROLLBACK_SUPERSEDED`,
  `REJECTED_MALFORMED`.

## Confirmação de efeito

O ACK de E2 Control do FlexRIC só indica entrega: a API do xApp devolve apenas
um booleano. Uma ação só é considerada aplicada quando o readback contém o
outcome com o mesmo `action_id` e `at_ns` posterior ao envio
(`smartfarm.e2.control_client.E2ControlClient`).

## Limitações conhecidas

- Quando a gNB pula uma indication (fora do período de 200 ms), o agente SLICE
  do FlexRIC fixado já alocou o cabeçalho da indication e não o libera
  (~4 bytes por ciclo pulado, ~11 MB/h). A correção é reordenar
  `on_indication_slice_sm_ag()` no plugin `libslice_sm.so`, o que exige
  reconstruir os plugins do RIC numa janela de manutenção.
- `nr_dlbench` não linka com `E2_AGENT=ON` neste commit da OAI
  (`e2ap_store_setup_*` referenciados pela F1AP). Por isso os testes do
  scheduler rodam numa build sem E2, e os do codec/adapter na build E2.
