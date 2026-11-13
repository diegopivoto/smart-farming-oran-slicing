# Android (OnePlus 8T): app Smart Farm + sonda de capability

Dois módulos Gradle no mesmo projeto:

| Módulo | Pasta | O que é | APK em `dist/` |
|---|---|---|---|
| `:app` | `app/` | **App Smart Farm** (Kotlin + Jetpack Compose, Material 3): drone, imagens do campo e estação meteorológica, cada um na sua PDU session/slice, com painel de SLA ao vivo e relatório por segundo ao coletor | `sf-farm-app-<versão>.apk` (otimizado, instalar este) e `sf-farm-app-<versão>-debug.apk` |
| `:probe` | `probe/` | Sonda de capability da fase 1 (Java + Views), que validou o `requestNetwork` por serviço | `sf-slice-probe-<versão>.apk` |

Contrato de conteúdo com o gerador do PC3: [`docs/design/smartfarm-app-protocol.md`](../docs/design/smartfarm-app-protocol.md).
Mecanismo de slice por serviço: [`docs/runbooks/android-capability-probe.md`](../docs/runbooks/android-capability-probe.md).
Uso do app no lab: [`docs/runbooks/android-farm-app.md`](../docs/runbooks/android-farm-app.md). Design aprovado:
[`docs/design/app-mockup/`](../docs/design/app-mockup/); renders das telas do app: [`docs/design/app-screens/`](../docs/design/app-screens/).

## Build (PC1, sem sudo)

Toolchain em `~/opt`: JDK 17 (`~/opt/jdk-17.0.21+9`), Android SDK (`~/opt/android-sdk`: cmdline-tools, `platforms;android-34`,
`build-tools;34.0.0`, `platform-tools`). Gradle 8.7 vem do wrapper; AGP 8.5.2; Kotlin 2.0.21; Compose BOM 2024.10.01.

```bash
android/build.sh          # testes JVM + APKs do app (release otimizado e debug) + sonda, copiados para android/dist/
android/build.sh app      # só o app
android/build.sh probe    # só a sonda
```

Os APKs saem assinados com a chave de debug (`~/.android/debug.keystore`; reinstalar por cima exige a mesma chave). O
`sf-farm-app-<versão>.apk` é o build `release` (R8, não depurável, mesma chave de debug): o Compose e o vídeo a 30 fps
rodam bem mais rápido do que no build depurável.

## Arquitetura do app (`app/src/main/java/br/inatel/smartfarm/farm/`)

- `protocol/`: Kotlin puro, testado na JVM. `Wire` (cabeçalhos `SFT1`/`SFO1`, também aceita a grafia `SFTR`/`SFOB` do
  primeiro rascunho do contrato; `TIMEQ`/`TIMER`), `UdpStats` (porte de `sf_traffic.py`, mesmos campos e arredondamentos),
  `ClockSync`, `WeatherTelemetry`, `SfimStreamParser` (parte o container SFIM em streaming enquanto o SHA-256 é calculado),
  `FrameAssembler` (MJPEG: só mostra quadro completo; fillers contam nas métricas), `SlaMonitor` (SLA com as
  definições de `traffic_run.py evaluate`, janela deslizante; metas da resposta do orquestrador, que copia o
  `SERVICE_CATALOG`, ou a cópia embutida em `SlaCatalog` sem ele), `Orchestration` (cliente do orquestrador, política de
  repetição do POST e tolerância de 30 s dos polls).
- `core/`: `SliceSession` (`requestNetwork` com as capabilities por serviço), receptores ligados à `Network` do serviço
  (`UdpReceiver`, `CropReceiver`, `JpegDecoder` fora da thread de UI, com reuso de bitmap e queda automática para meia
  resolução se a decodificação 1080p passar de ~28 ms), `Reporter` (POST ao coletor pela sessão padrão), `ControlClient`
  (TCP 47010), `FarmController` (dono da sessão; registros por segundo idênticos ao `sf_traffic.py receive`) e
  `KeepAliveService` (serviço em primeiro plano + wake lock parcial enquanto uma tela de serviço está aberta).
- `ui/`: tema fixo (paleta do mockup, claro e escuro), fonte Inter embutida (SIL OFL), ilustrações vetoriais desenhadas
  em Canvas, telas Home, Conectando, Weather, Crop, Drone, Settings e o painel de SLA. Todo rótulo é de uma linha com
  redução automática de fonte (`AutoSizeText`) em vez de quebrar.

Cartão "O-RAN control" (desde 1.3.0): no fim de cada tela de serviço, enquanto a sessão do orquestrador está ativa, o
app mostra o que o O-RAN faz no slice, lido da resposta `GET /api/v1/session/<id>` (campo opcional `oran`, atualizado
a cada poll: 1 s conectando, 10 s em streaming): o modo de controle em vigor (desde 1.3.1: "DDQN agent" com o passo e
o veredito de SLA do último passo, ou "RAN defaults", com o motivo quando o DDQN foi pedido e não pôde entrar), uplink
UN3GPP (MCS/RBs da ação E2 ou "BS link adaptation"), quota de PRB do downlink OAI, o veredito E2 lido de volta
(CONFIRMED/CONSISTENT/RELEASED, APPLIED, REJECTED_*, SENT) com a idade e, ao tocar, os read-backs e as decisões
recentes (com o DDQN: a ação e o veredito de cada passo). Sem o campo (orquestrador antigo, `--no-oran-view`, journal
do xApp ilegível) o cartão some e nada mais muda; um `oran` malformado vira `null` (`Orchestration.parseOran` nunca
lança). O app só exibe: quem decide é o orquestrador.

Settings → "O-RAN radio control" (desde 1.3.1): **RAN defaults** (padrão) ou **DDQN agent**, salvo como as outras
opções e enviado como `oran_mode` no pedido do serviço; vale para a próxima sessão. Com o DDQN, deixe "Report metrics
to the lab collector" ligado: o agente julga o SLA de cada passo pelos registros por segundo do app.

Registros enviados ao coletor (`http://10.45.0.1:8088/report`, desligável em Settings): `receiver_window` (weather e
drone, por segundo) e `receiver_object` (crop, por objeto) com os mesmos campos do `sf_traffic.py`, mais `receiver:
"sf-farm-app"` e, no drone, `video_frames_*`; `app_session` (pedido, concessão em s, IP do celular na sessão do
serviço, primeiro dado, liberação, comandos de controle) com um `run_id` próprio do app.

## Testes sem celular

```bash
cd android && ./gradlew :app:testDebugUnitTest      # (o build.sh já roda)
```

- Parsers e SLA (`ProtocolTest.kt`): cabeçalhos, JSON do weather com padding, SFIM em pedaços aleatórios, quadros MJPEG
  fora de ordem/perdidos/duplicados/fillers, janelas do SLA.
- Paridade com o Python (`PythonParityTest.kt`): o mesmo fluxo sintético (perdas, reordenação, segundos ociosos, troca de
  run) passa pelo `UdpStats` do app e pelo do `deployment/traffic/sf_traffic.py`; os registros têm de ser iguais.
- Ponta a ponta no desktop (`FakeGeneratorIT.kt`): `tools/fake_generator.py` (escrito só a partir do contrato; só envia
  para 127.0.0.1, portas 57000+) manda weather/crop/drone, o teste recebe com as classes do app e confere telemetria,
  PDR, AoI, relógio, comando de irrigação, quadros JPEG íntegros, descarte por SHA-256 e partes do SFIM.
  O mesmo teste aceita outro gerador em `SF_GENERATOR`, por exemplo o gerador do lab em modo local:

  ```bash
  S=/tmp/sfgen; mkdir -p $S
  SF_GENERATOR="python3 -u $PWD/../deployment/content/sf_content.py node --target 127.0.0.1 --port-offset 10000 --bind 127.0.0.1 --report= --ui-port 18090 --node-socket $S/node.sock --state-file $S/state.json --autostart weather,crop,drone" \
  SF_GENERATOR_SECONDS=18 ./gradlew :app:testDebugUnitTest --tests '*FakeGeneratorIT*' --rerun-tasks
  ```

- Cliente do orquestrador (`OrchestrationTest.kt`): parse da visão, metas de SLA, tolerância dos polls, cartão O-RAN
  (presente, ausente, indisponível, malformado) e ponta a ponta contra `demo_orchestrator.py --dry-run`, que também
  simula decisões O-RAN.
- Renders das telas a 1080×2400 / 420 dpi (Robolectric), para conferir quebra e corte de texto:
  `SF_SCREENS=1 ./gradlew :app:testDebugUnitTest --tests '*ScreenshotRender*'` (tema escuro: `SF_SCREENS_NIGHT=1`),
  PNGs em `app/build/screens/`.

## Servir o APK e as sondas (PC1)

```bash
python3 -u android/tools/apk_server.py     # http://10.45.0.1:8089/ (só responde a 10.45.0.0/16)
```

Processo separado do coletor (`:8088`), que não precisa ser reiniciado. Rotas: `/` (página de download),
`/apk/<arquivo>.apk` e `/probe?proto=udp|tcp&port=47001..47003&count=N` (envia N pacotes `SFCP` de volta ao endereço que
fez o pedido, ou seja, ao IP do celular na rede do serviço).
