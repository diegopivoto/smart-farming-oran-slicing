# PC3 (UE UN3GPP): instalação e execução da parte Smart Farming

## Resumo: o que se faz, quando e onde

Todos os comandos deste guia rodam **no PC3**.

| Quando | O quê | Comando no PC3 |
|---|---|---|
| **Uma vez**, ao preparar o PC3 | clonar o repositório, instalar, chave, mídia | passos 2 a 5 (`install_pc3.sh`) |
| **De novo só quando o código mudar** | atualizar o clone e reinstalar | passo 7 |
| **Toda vez que o laboratório sobe** | rádio da UE, N3IWUE e o *node agent* | passo 6.1 |
| Para **experimentos** (calibração, sessões do DDQN) | nada além do 6.1: o PC1 comanda tudo pelo node agent | — |
| Para a **demonstração com o app** | trocar para o modo app; no fim, voltar | `sf_app_up.sh` / `sf_app_down.sh` (passo 6.2) |

O **node agent** é o serviço do PC3 que obedece ao PC1: troca o slice da UE UN3GPP e liga ou desliga o gerador de
tráfego. Ele trabalha em um de dois modos:

- **modo medição**: o gerador é o `sf_traffic.py`, o tráfego sintético com números de sequência e carimbos de tempo
  usado em todos os experimentos do artigo (o celular recebe com o receptor do Termux);
- **modo app**: o gerador é o `sf_content.py`, com conteúdo real (vídeo do drone, imagens da lavoura, telemetria do
  clima) no mesmo perfil de tráfego, e o painel `http://127.0.0.1:8090/` (o celular recebe com o app).

O modo medição é o padrão; `sf_app_up.sh` troca para o modo app e `sf_app_down.sh` volta.

Cada script, quem roda e quando:

| Script | Máquina | Quem roda | Quando |
|---|---|---|---|
| `deployment/pc3/install_pc3.sh` | PC3 | você | uma vez após o clone; de novo só após atualizar o código |
| `scripts/startmodem.sh ue 1 680142857 560142857` (repo `gr-inatelxg`) | PC3 | você | toda partida (rádio da UE) |
| `run.sh` (repo `n3iwue`) | PC3 | você, num tmux | toda partida, depois do rádio |
| comando do node agent (passo 6.1; não é um `.sh`) | PC3 | você | toda partida, depois do N3IWUE |
| `~/src/smartfarm-content/sf_app_up.sh` | PC3 | você | só para a demo com o app |
| `~/src/smartfarm-content/sf_app_down.sh` | PC3 | você | no fim da demo |
| `~/src/smartfarm-content/run_node.sh` | PC3 | **ninguém à mão**: o `sf_app_up.sh` o chama | — |
| `deployment/content/deploy_pc3.sh` | PC1 | você, opcional | só para copiar a mídia do PC1 para o PC3 (passo 5) |

## O que é instalado

Este guia instala, a partir de um clone deste repositório, tudo o que o Smart Farming roda na UE UN3GPP (PC3):

| Componente | Instalado em | Para que serve |
|---|---|---|
| Node agent (`smartfarm_node_agent.py`) | `/opt/smartfarm/` | Recebe do PC1, pela rede 5G (dentro da VRF do N3IWUE), os pedidos de troca de slice e de início e fim de tráfego, assinados por HMAC |
| Cliente de slice (`n3iwue_slice_client.py`) | `/opt/smartfarm/` | Pede ao N3IWUE a troca de slice |
| Gerador de medição (`sf_traffic.py`) | `/opt/smartfarm/` | Tráfego das calibrações e das sessões de RL (UDP/TCP com sequência, SHA-256 e carimbo de tempo) |
| Gerador de conteúdo e painel (`sf_content.py`, `sfcontent/`) | `~/src/smartfarm-content/` | Conteúdo real do app (vídeo do drone, imagens multiespectrais, telemetria do clima) no mesmo perfil de tráfego, e painel `http://127.0.0.1:8090/` para mudar os estados de cada serviço |
| Scripts do modo app (`sf_app_up.sh`, `sf_app_down.sh`, `run_node.sh`) | `~/src/smartfarm-content/` | Sobem e descem o modo app com um comando |
| Mídia da demo (`media/`, ~244 MB) | `~/src/smartfarm-content/media/` | Quadros do vídeo do drone e imagens da lavoura; fica fora do git e é gerada ou copiada (passo 5) |

O código continua num lugar só, este repositório: o PC3 clona o projeto inteiro e o script de instalação copia apenas a
parte da UE. O protocolo entre o PC3 e o PC1 (orquestrador, coletor, avaliação do SLA e app) é o mesmo código, por isso
os dois lados devem estar sempre no mesmo commit da branch `oran-tvws-5g`.

## 1. Pré-requisitos

- Ubuntu com Python 3, `git`, `tmux`, `rsync`, `python3-numpy` e `python3-pil`; Firefox para o painel.
  ```bash
  sudo apt install git tmux rsync python3-numpy python3-pil
  ```
- O rádio UN3GPP da UE (`gr-inatelxg` + `inatel5g_range_mac`, `sudo ./scripts/startmodem.sh ue 1 680142857 560142857`) e o
  N3IWUE (`n3iwue`, branch `oran-tvws-5g`) instalados. Eles só precisam estar no ar para **rodar** (passo 6), não para
  instalar.

## 2. Clonar o repositório

Com acesso ao GitHub (o repositório é privado: use a conta diegopivoto e um token como senha):

```bash
mkdir -p ~/src && cd ~/src
git clone -b oran-tvws-5g https://github.com/diegopivoto/smart-farming-oran.git
```

No laboratório, o PC3 não tem acesso ao GitHub: o único caminho de rede dele é o próprio enlace UN3GPP, pelo PC2. O
clone chega por um bundle gerado no PC1 (~330 MB; faça fora de qualquer medição):

```bash
# PC1
cd ~/5gc-un3gpp/smart-farming-oran
git bundle create /tmp/sfo.bundle oran-tvws-5g
scp /tmp/sfo.bundle smartfarm-pc3:/tmp/

# PC3
cd ~/src && git clone -b oran-tvws-5g /tmp/sfo.bundle smart-farming-oran
cd smart-farming-oran && git remote set-url origin https://github.com/diegopivoto/smart-farming-oran.git
rm /tmp/sfo.bundle
```

## 3. Instalar

```bash
cd ~/src/smart-farming-oran
deployment/pc3/install_pc3.sh
```

O script pede a senha do sudo, confere os pré-requisitos, instala o node agent e o gerador de medição em
`/opt/smartfarm/` e o gerador de conteúdo em `~/src/smartfarm-content/`, e verifica a mídia e a chave. Ele só copia
arquivos: não mexe no rádio, no IKE, no N3IWUE nem em um nó de conteúdo em execução. Opções:

| Opção | Efeito |
|---|---|
| `--media DIR` | copia mídia já gerada de `DIR` (passo 5) |
| `--render` | gera a mídia nesta máquina (passo 5) |
| `--start-agent` | (re)inicia o node agent em modo medição no tmux `sf-agent` |
| `DEST=/outra/pasta` | instala o gerador de conteúdo em outra pasta (os scripts do PC1 esperam `~/src/smartfarm-content`) |

## 4. Chave do node agent

O PC1 assina cada pedido ao node agent com uma chave compartilhada. Ela deve ser **igual** nos dois lados, com permissão
600: PC1 `~/.config/smartfarm/node-agent.key` e PC3 `/etc/smartfarm/node-agent.key`. Ela não fica no git.

```bash
# PC1: enviar a chave existente ao PC3
scp ~/.config/smartfarm/node-agent.key smartfarm-pc3:/tmp/node-agent.key

# PC3
sudo install -d -m 755 /etc/smartfarm
sudo install -m 600 /tmp/node-agent.key /etc/smartfarm/node-agent.key && rm /tmp/node-agent.key
```

Numa instalação nova, sem chave em nenhum dos lados, gere uma (`openssl rand -hex 32 > node-agent.key`) e instale-a nos
dois caminhos acima.

## 5. Mídia da demo (só para o modo app)

O modo medição não precisa dela. Para o app, escolha uma das três formas:

1. **Copiar de quem já tem** (recomendado no laboratório). A partir do PC1, que tem a mídia em
   `deployment/content/media/`:
   ```bash
   # PC1 (fora de medições; BWLIMIT=4000 limita a 4 MB/s)
   cd ~/5gc-un3gpp/smart-farming-oran && deployment/content/deploy_pc3.sh
   ```
   ou, com a mídia já numa pasta do PC3: `deployment/pc3/install_pc3.sh --media /caminho/da/media`.
2. **Baixar da release** `oran-tvws-5g-20261105` do GitHub o arquivo `smart-farming-oran-content-media-20261105.tar`
   e extrair:
   ```bash
   tar -xf smart-farming-oran-content-media-20261105.tar -C ~/src/smartfarm-content/
   ```
3. **Gerar no PC3**: `deployment/pc3/install_pc3.sh --render`. São numpy e Pillow com prioridade mínima (`nice 19`), mas
   o PC3 tem poucos núcleos livres e o PHY é de tempo real: faça com o rádio **desligado**. O resultado é o mesmo das
   outras formas (sementes fixas).

## 6. Rodar

### 6.1 Toda vez que o laboratório sobe

Ordem geral do laboratório: núcleo e O-RAN no PC1 (`sf_oran_up.sh`), BS e N3IWF no PC2, depois no PC3:

```bash
# 1) rádio da UE (fora do tmux) e 2) N3IWUE, nos seus repositórios
cd ~/src/gr-inatelxg && sudo ./scripts/startmodem.sh ue 1 680142857 560142857
sudo tmux new-session -d -s n3iwue -c ~/src/Diego/n3iwue ./run.sh   # cria a VRF vrf-n3iwue com o IP 10.45.x.x
```

3) o node agent, em modo medição (se ainda não estiver no tmux `sf-agent`):

```bash
sudo tmux new-session -d -s sf-agent 'python3 /opt/smartfarm/smartfarm_node_agent.py --key /etc/smartfarm/node-agent.key'
sudo tmux attach -t sf-agent        # para ver; Ctrl+B e depois D para sair sem parar
```

(o mesmo que `deployment/pc3/install_pc3.sh --start-agent`). Para os experimentos, o PC3 fica só assim: o PC1 troca o
slice e dispara o tráfego pelo node agent.

### 6.2 Demonstração com o app

Para o modo app, um comando no PC3, que precisa do N3IWUE no ar, da mídia e do orquestrador da demo
rodando no PC1 (`sf_oran_up.sh`):

```bash
~/src/smartfarm-content/sf_app_up.sh                     # clima fixo às 13:30; --weather-hour 9 muda a hora
```

Ele troca o node agent para o gerador de conteúdo (tmux `sf-agent`), sobe o nó de conteúdo dentro da VRF (tmux
`sf-content`) e abre o painel `http://127.0.0.1:8090/` no Firefox, onde se mudam os estados de cada serviço (por
exemplo, seca, chuva, praga) e se vê a irrigação. Depois é só abrir o app no celular e tocar num serviço.

Para voltar ao modo medição:

```bash
~/src/smartfarm-content/sf_app_down.sh
```

## 7. Atualizar

Quando o código mudar, atualize o clone e rode a instalação de novo (nunca durante uma medição):

```bash
cd ~/src/smart-farming-oran
git pull                                             # com GitHub; no laboratório, um bundle do PC1:
# PC1: git bundle create /tmp/sfo.bundle <commit-que-o-PC3-tem>..oran-tvws-5g && scp /tmp/sfo.bundle smartfarm-pc3:/tmp/
# PC3: git fetch /tmp/sfo.bundle oran-tvws-5g:refs/remotes/origin/oran-tvws-5g && git merge --ff-only origin/oran-tvws-5g
deployment/pc3/install_pc3.sh --start-agent          # --start-agent só se o node agent deve recarregar o código
```

## 8. Conferir

| O que | Comando | Esperado |
|---|---|---|
| Node agent | `sudo tmux capture-pane -p -t sf-agent \| tail -3` | escutando na VRF; pedidos recusados por assinatura ou idade aparecem aqui (chave ou relógio) |
| Arquivos instalados | `ls /opt/smartfarm ~/src/smartfarm-content` | os componentes da tabela do início |
| Gerador de conteúdo | fim da saída do `install_pc3.sh` | `content generator OK: drone loop ... frames, crop object ... B` |
| Painel (modo app) | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/` | `200` |
